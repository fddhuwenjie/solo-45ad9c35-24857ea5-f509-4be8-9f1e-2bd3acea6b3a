"""投料谱系：到货批号、投料分配门禁、锁定规格展开与供应商更正波及链。

同名原料的到货批号可分别采用不同规格版本；只记配方版本无法在供应商更正
声明后圈出真正消耗过该批原料的成品。本模块把“哪一批原料投进了哪个生产
批次”落成可审计的扣量关系：

- 到货批号：供应商批号、对应规格版本、收货量、有效期与质检状态
  （pending 待检 / released 放行 / quarantined 隔离）；
- 投料分配：为生产批次分配一个或多个批号及用量，门禁拒绝未放行、已过期、
  余量不足、规格不符合配方（配方项锁定版本不一致或原料不在配方中）的分配；
  幂等键唯一，同键重放复用原结果、不重复扣量；
- 锁定规格：批次一旦存在有效投料记录，来源图按开工时锁定的投料规格展开；
  配方项缺用料记录时记 data_gap(material_allocation) 证据空白，
  不回退最新规格；批次无投料记录时保持旧的现行规格展开（兼容未启用
  批号管理的资料）；
- 供应商更正：新规格版本登记时，若该原料已启用批号管理，则沿扣料关系
  定位消耗过旧规格批号的批次/标签/已发放卷标，列明涉事用量、声明差异与
  处置边界；未消耗该批号的其他成品保持原状态。未启用批号管理时回退旧的
  依赖图影响传播；
- 撤销：开工前可撤销分配并记一笔 reverse 反向流水恢复余量；每次扣量与
  撤销都写入 lot_ledger，批次查询与核对包据此还原。
"""
from __future__ import annotations

from . import engine
from .db import new_id, utcnow
from .engine import norm, resolve_ref

# 分配门禁失败代码
GATE_CODES = (
    "lot_unknown",              # 批号不存在
    "not_released",             # 未放行（待检/隔离）
    "expired",                  # 已过期（按批次开工时刻，缺省按当前日期）
    "insufficient_quantity",    # 余量不足
    "ingredient_not_in_recipe", # 原料不在批次产品配方中
    "spec_mismatch",            # 批号规格与配方锁定版本不一致
    "recipe_unknown",           # 批次产品缺配方，无法核对规格符合性
)


def locked_specs_for_batch(store, batch_id: str) -> dict | None:
    """批次开工时锁定的投料规格：ingredient_id -> [{lot_id, spec_version, quantity}]。

    批次存在任一有效（未撤销）投料记录时返回映射——未覆盖的配方项由展开层
    记证据空白；无任何有效投料记录时返回 None，来源图回退现行规格展开。
    """
    allocations = store.allocations_for_batch(batch_id, active_only=True)
    if not allocations:
        return None
    locked: dict[str, list] = {}
    for a in allocations:
        locked.setdefault(a["ingredient_id"], []).append({
            "lot_id": a["lot_id"],
            "spec_version": a["spec_version"],
            "quantity": a["quantity"],
        })
    return locked


# ---------------------------------------------------------------------- 分配门禁

def _recipe_version_map(store, product_id: str) -> dict | None:
    """产品当前配方的 原料ID -> 锁定规格版本（None 表示未锁定）。无配方返回 None。"""
    recipe = store.current_recipe(product_id)
    if recipe is None:
        return None
    versions: dict[str, str | None] = {}
    for item in recipe["items"]:
        ing, err, _ = resolve_ref(store, item["ingredient_ref"])
        if ing is not None:
            versions[ing["id"]] = item.get("version")
    return versions


def evaluate_allocation(store, batch: dict, items: list[dict]) -> dict:
    """投料分配门禁：逐项核对批号状态、有效期、余量与配方规格符合性。

    返回 {"items": [{lot, quantity}], "failures": [...]}；failures 非空时
    调用方应整体拒绝（all-or-nothing），不得部分扣量。同一请求内相同批号的
    用量合并后再核对余量。
    """
    failures: list[dict] = []
    quantities: dict[str, float] = {}
    for it in items:
        quantities[it["lot_id"]] = quantities.get(it["lot_id"], 0.0) + it["quantity"]

    recipe_versions = _recipe_version_map(store, batch["product_id"])
    # 以批次开工时刻核对有效期；开工时刻缺失时按当前日期
    reference_date = (batch.get("started_at") or utcnow())[:10]

    lots: dict[str, dict] = {}
    for lot_id, qty in sorted(quantities.items()):
        lot = store.get_lot(lot_id)
        if lot is None:
            failures.append({"lot_id": lot_id, "code": "lot_unknown",
                             "message": f"原料批号 {lot_id} 不存在"})
            continue
        lots[lot_id] = lot
        if lot["status"] != "released":
            failures.append({
                "lot_id": lot_id, "code": "not_released", "status": lot["status"],
                "message": f"批号 {lot_id} 质检状态为 {lot['status']}，未放行不得投料"})
        if lot["expires_at"] and reference_date > lot["expires_at"][:10]:
            failures.append({
                "lot_id": lot_id, "code": "expired",
                "expires_at": lot["expires_at"], "reference_date": reference_date,
                "batch_started_at": batch.get("started_at"),
                "message": f"批号 {lot_id} 有效期至 {lot['expires_at'][:10]}，已过期"})
        if qty > lot["remaining_quantity"]:
            failures.append({
                "lot_id": lot_id, "code": "insufficient_quantity",
                "requested": qty, "remaining_quantity": lot["remaining_quantity"],
                "message": f"批号 {lot_id} 余量 {lot['remaining_quantity']} 不足，"
                           f"本次申请 {qty}"})
        if recipe_versions is None:
            failures.append({
                "lot_id": lot_id, "code": "recipe_unknown",
                "product_id": batch["product_id"],
                "message": f"产品 {batch['product_id']} 缺少配方，无法核对规格符合性"})
        else:
            pinned = recipe_versions.get(lot["ingredient_id"], ...)
            if pinned is ...:
                failures.append({
                    "lot_id": lot_id, "code": "ingredient_not_in_recipe",
                    "ingredient_id": lot["ingredient_id"],
                    "product_id": batch["product_id"],
                    "message": f"原料 {lot['ingredient_id']} 不在产品 "
                               f"{batch['product_id']} 的当前配方中"})
            elif pinned and pinned != lot["spec_version"]:
                failures.append({
                    "lot_id": lot_id, "code": "spec_mismatch",
                    "ingredient_id": lot["ingredient_id"],
                    "expected": pinned, "actual": lot["spec_version"],
                    "message": f"批号 {lot_id} 规格 {lot['spec_version']} 与配方锁定"
                               f"版本 {pinned} 不一致"})
    ok_items = [{"lot": lots[lot_id], "quantity": quantities[lot_id]}
                for lot_id in sorted(lots)]
    return {"items": ok_items, "failures": failures}


def apply_allocation(store, batch: dict, idempotency_key: str, payload: dict,
                     items: list[dict]) -> dict:
    """门禁通过后写入分配与扣量流水（每个批号一条 allocate 流水）。"""
    request_id = new_id("areq")
    allocations = []
    for it in items:
        lot = it["lot"]
        alloc = store.create_allocation(
            new_id("alo"), request_id, batch["batch_id"], lot["lot_id"],
            lot["ingredient_id"], lot["spec_version"], it["quantity"])
        store.add_ledger_entry(lot["lot_id"], batch["batch_id"],
                               alloc["allocation_id"], "allocate", it["quantity"])
        allocations.append(alloc)
    store.create_allocation_request(
        request_id, idempotency_key, batch["batch_id"], payload,
        [a["allocation_id"] for a in allocations])
    store.log_event("lots_allocated", {
        "request_id": request_id, "batch_id": batch["batch_id"],
        "idempotency_key": idempotency_key,
        "items": [{"allocation_id": a["allocation_id"], "lot_id": a["lot_id"],
                   "ingredient_id": a["ingredient_id"],
                   "spec_version": a["spec_version"], "quantity": a["quantity"]}
                  for a in allocations]})
    return {"request_id": request_id, "allocations": allocations}


def reverse_allocation(store, allocation: dict, reason: str) -> dict:
    """撤销分配：标记 reversed 并记一笔 reverse 反向流水恢复批号余量。"""
    updated = store.set_allocation_reversed(allocation["allocation_id"])
    entry = store.add_ledger_entry(
        allocation["lot_id"], allocation["batch_id"], allocation["allocation_id"],
        "reverse", allocation["quantity"], reason)
    store.log_event("allocation_reversed", {
        "allocation_id": allocation["allocation_id"], "lot_id": allocation["lot_id"],
        "batch_id": allocation["batch_id"], "quantity": allocation["quantity"],
        "reason": reason, "ledger_id": entry["id"]})
    return {"allocation": updated, "reversal": entry}


# ---------------------------------------------------------------------- 供应商更正波及链

def _declaration_diff(old_decls: list[dict], new_decls: list[dict]) -> list[dict]:
    """两版供应商声明的逐项差异（过敏原 -> 旧状态/新状态；None 表示未声明）。"""
    old_map = {norm(d["allergen"]): d["status"] for d in old_decls}
    new_map = {norm(d["allergen"]): d["status"] for d in new_decls}
    changes = []
    for allergen in sorted(set(old_map) | set(new_map)):
        old, new = old_map.get(allergen), new_map.get(allergen)
        if old != new:
            changes.append({"allergen": allergen, "old_status": old, "new_status": new})
    return changes


def correction_impact(store, ingredient_id: str, new_version: str) -> dict:
    """新规格版本登记后的影响传播。

    原料已启用批号管理（存在到货批号）时，沿扣料关系定位消耗过旧规格批号
    的批次：受影响标签按批次影响链处理（草稿/复核中重新分析，已批准标
    stale 并派生新修订），该修订下仍有余量的印刷批次冻结、已发放到包装
    现场的卷标列入处置边界；未消耗涉事批号的其他成品保持原状态。
    原料尚无到货批号时回退旧的依赖图影响传播。
    """
    lots = store.lots_for_ingredient(ingredient_id)
    reason = f"供应商更正 {ingredient_id} 过敏原声明（新规格 {ingredient_id}@{new_version}）"
    if not lots:
        impacted = engine.impacted_products(store, [ingredient_id])
        actions = engine.apply_impact(store, impacted,
                                      reason=f"新规格版本 {ingredient_id}@{new_version}")
        return {"impacted_products": sorted(impacted), "actions": actions,
                "correction": None}

    new_ver = store.get_version(ingredient_id, new_version)
    new_decls = new_ver["supplier_declarations"] if new_ver else []
    affected_lots, affected_batches = [], set()
    for lot in lots:
        if lot["spec_version"] == new_version:
            continue  # 已是新规格的批号不属于被更正对象
        allocations = store.allocations_for_lot(lot["lot_id"], active_only=True)
        if not allocations:
            continue  # 未实际消耗的批号不构成波及
        affected_lots.append({"lot": lot, "allocations": allocations})
        affected_batches.update(a["batch_id"] for a in allocations)

    actions = engine.apply_batch_impact(store, sorted(affected_batches),
                                        positive=True, reason=reason)
    affected_products = sorted({
        (store.get_batch(b) or {}).get("product_id") for b in affected_batches} - {None})
    referencing = engine.impacted_products(store, [ingredient_id])

    declaration_changes = []
    seen_versions = set()
    for entry in affected_lots:
        old_version = entry["lot"]["spec_version"]
        if old_version in seen_versions:
            continue
        seen_versions.add(old_version)
        old_ver = store.get_version(ingredient_id, old_version)
        for change in _declaration_diff(
                old_ver["supplier_declarations"] if old_ver else [], new_decls):
            declaration_changes.append({"spec_version": old_version, **change})

    frozen_print_batches, issued_to = [], {}
    for action in actions:
        freeze = action.get("print_freeze")
        if not freeze:
            continue
        frozen_print_batches.extend(freeze["frozen_print_batches"])
        for d in freeze["disposition_batches"]:
            issued_to.setdefault(d["production_batch_id"], d)

    correction = {
        "ingredient_id": ingredient_id,
        "new_version": new_version,
        "reason": reason,
        "declaration_changes": declaration_changes,
        "affected_lots": [
            {"lot_id": e["lot"]["lot_id"],
             "supplier_lot_no": e["lot"]["supplier_lot_no"],
             "spec_version": e["lot"]["spec_version"],
             "allocated_quantity": sum(a["quantity"] for a in e["allocations"]),
             "consumed_by": [{"batch_id": a["batch_id"],
                              "allocation_id": a["allocation_id"],
                              "quantity": a["quantity"]}
                             for a in e["allocations"]]}
            for e in affected_lots],
        "affected_batches": sorted(affected_batches),
        "affected_products": affected_products,
        "unaffected_products": sorted(referencing - set(affected_products)),
        "disposition_boundary": {
            "labels": [{"label_id": a["label_id"], "revision": a["revision"],
                        "action": a["action"]} for a in actions],
            "frozen_print_batches": frozen_print_batches,
            "issued_to_batches": sorted(issued_to.values(),
                                        key=lambda d: d["production_batch_id"]),
        },
    }
    store.log_event("supplier_correction", correction)
    return {"impacted_products": affected_products, "actions": actions,
            "correction": correction}


# ---------------------------------------------------------------------- 证据还原

def batch_material_evidence(store, batch_id: str) -> dict:
    """批次的投料谱系证据：分配（含已撤销）、涉及批号与扣量流水。"""
    allocations = store.allocations_for_batch(batch_id)
    lots = {}
    for a in allocations:
        lot = store.get_lot(a["lot_id"])
        if lot:
            lots[lot["lot_id"]] = lot
    return {
        "batch_id": batch_id,
        "allocations": allocations,
        "lots": [lots[k] for k in sorted(lots)],
        "ledger": store.ledger_for_batch(batch_id),
    }


def corrections_for_batch(store, batch_id: str) -> list[dict]:
    """波及该批次的供应商更正事件（沿扣料关系定位到的规格更正链）。"""
    out = []
    for e in store.events():
        if e["kind"] != "supplier_correction":
            continue
        payload = e["payload"]
        if batch_id in (payload.get("affected_batches") or []):
            out.append({"ts": e["ts"], **payload})
    return out
