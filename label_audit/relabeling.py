"""成品换标处置：处置单、审核重算、处置事件与结案核平。

批准文案在产品装箱后才失效（标签撤回 / 供应商规格更正 / 阳性拭子传播），
现场只能靠手工表追踪哪些托盘拦下、哪些外箱换标。本模块把补救过程落成
可审计、可核平的记录：

- 处置单：引用撤回或规格更正产生的**受影响批次**（必须能在影响传播事件
  中找到来源，凭手工输入不得开工），逐项登记外箱/托盘标识与件数，创建即
  锁定该批次原包装运行（锁只增不删，处置期间与结案后原包装账都不可改），
  并指定候选的新标签修订与新卷标印刷批次；
- 审核：重新计算该批次的过敏原声明，逐项比对新标签批准快照与印刷文案；
  未隔离、标识被其他处置占用、声明不符或卷标余量不足均不得开工。审核
  通过即预留新卷标（从印刷批次可领用余量中冻结给本处置单）；
- 处置事件：拆标 / 重贴 / 报废 / 抽检失败 / 放行以幂等事件追加（带
  idempotency_key，同键同内容重放复用、同键冲突拒绝），事件只增不改；
- 结案：同时核平
  「隔离件数 = 换标合格 + 报废 + 仍隔离」与新卷标消耗
  （预留 = 重贴消耗 + 结案退回），结案快照 append-only、记录只读；
- 在办失效：规则或标签再变动（撤回 / 更正 / 阳性传播 / 新候选修订失效）
  时，在办处置单失效并列出待复核标识，未消耗预留退回可领用余量；复核
  重审按新一轮（epoch）事件核平，历史事件全部保留。
"""
from __future__ import annotations

from .db import RELABEL_EVENT_KINDS, RELABEL_STATUSES, new_id, utcnow
from .engine import BLOCKER
from .printing import (
    canonical_copy_summary,
    current_basis,
    analysis_version as compute_analysis_version,
)

# 创建门禁失败代码
CREATE_CODES = (
    "batch_unknown",              # 生产批次不存在
    "batch_not_affected",         # 批次不在撤回/更正/阳性传播的受影响清单内
    "new_label_unknown",          # 候选新标签修订不存在
    "new_label_not_approved",     # 候选新标签修订未批准 / 已撤回 / 带 stale
    "new_label_product_mismatch", # 新标签产品与受影响批次产品不一致
    "print_batch_unknown",        # 新卷标印刷批次不存在
    "print_batch_mismatch",       # 印刷批次不属于候选新标签修订
    "duplicate_identifier",       # 同一处置单内标识重复
    "identifier_occupied",        # 标识已被其他在办处置单占用
    "identifier_unknown",         # 处置事件引用的标识不属于本处置单
)

# 审核门禁失败代码
REVIEW_CODES = (
    "not_isolated",               # 标识未隔离不得开工
    "declaration_mismatch",       # 批次当前过敏原声明与新标签批准快照不符
    "printed_copy_mismatch",      # 新卷标印刷文案与批准快照不符
    "open_blocker",               # 批次当前分析存在新增开放 blocker
    "label_not_approved",         # 候选新标签修订不再可用（撤回/stale）
    "print_batch_unavailable",    # 新卷标印刷批次不可用（冻结/结案/失效）
    "product_not_applicable",     # 印刷批次适用产品不含受影响批次产品
    "insufficient_label_quantity",# 新卷标余量不足
)

# 处置事件门禁失败代码
EVENT_CODES = (
    "not_approved",               # 处置单未通过审核 / 已失效 / 已结案
    "stale_epoch",                # 事件属于失效的旧审核轮次，待复核重审
    "removed_limit_exceeded",     # 累计拆标超过隔离件数
    "not_removed",                # 重贴/报废前未先拆标
    "relabel_limit_exceeded",     # 重贴超过已拆标待处置件数
    "scrap_limit_exceeded",       # 报废超过已拆标待处置件数
    "release_limit_exceeded",     # 放行超过换标合格且未放行件数
    "insufficient_label_quantity",# 重贴时预留/实际新卷标余量不足
    "print_batch_unavailable",    # 重贴时新卷标印刷批次已不可用
)

# 结案门禁失败代码
CLOSE_CODES = (
    "not_approved",               # 未在办（未审核/已失效/已结案）
    "unreleased_units",           # 仍有换标合格件未放行
    "imbalanced",                 # 隔离件数/新卷标核平等式不成立
)


# ---------------------------------------------------------------------- 受影响来源

def affected_source(store, batch_id: str) -> dict | None:
    """在影响传播事件中定位批次的受影响来源。

    处置单只能引用撤回 / 规格更正 / 阳性拭子传播实际圈出的受影响批次，
    凭手工输入的批次不得开工。撤回优先（审批动作最明确），其次阳性拭子
    补录，再次供应商更正。
    """
    withdraw = None
    positive = None
    for e in store.events():
        kind, payload = e["kind"], e["payload"]
        if kind == "label_withdrawn":
            freeze = payload.get("print_freeze") or {}
            hit_batches = set(freeze.get("pending_isolation_batches") or [])
            hit_batches |= {
                b["production_batch_id"]
                for b in freeze.get("disposition_batches", [])}
            if batch_id in hit_batches:
                withdraw = {"event_id": e["id"], "ts": e["ts"],
                            "kind": "withdrawal",
                            "label_id": payload.get("label_id"),
                            "reason": payload.get("reason")}
        elif kind == "batch_impact_applied" and payload.get("positive"):
            actions = payload.get("actions") or []
            if any(a.get("batch_id") == batch_id
                   and a.get("action") in ("marked_stale", "reanalyzed")
                   for a in actions):
                positive = {"event_id": e["id"], "ts": e["ts"],
                            "kind": "positive_swab", "reason": payload.get("reason")}
        elif kind == "supplier_correction":
            if batch_id in (payload.get("affected_batches") or []):
                positive = positive or {"event_id": e["id"], "ts": e["ts"],
                                        "kind": "spec_correction",
                                        "ingredient_id": payload.get("ingredient_id"),
                                        "new_version": payload.get("new_version"),
                                        "reason": payload.get("reason")}
    return withdraw or positive


def batch_packaging_run_ids(store, batch_id: str) -> list[str]:
    """受影响批次的原包装运行（创建处置单时全部锁定）。"""
    return [r["run_id"] for r in store.runs_for_batch(batch_id)]


# ---------------------------------------------------------------------- 创建门禁

def evaluate_create(store, batch: dict, items: list[dict], new_label_id: str,
                    new_print_batch_id: str,
                    exclude_disposition_id: str | None = None) -> dict:
    """创建处置单门禁：候选修订/印刷批次可用、标识未被其他在办处置占用。

    受影响来源与逐项隔离在审核时复核（创建允许草拟不完整清单），但同单内
    重复标识与其他在办处置的占用在创建时即拒绝。改指定时传入本处置单 ID
    排除自身（invalidated 状态仍算占用，避免同单标识把自己挡住）。
    """
    failures: list[dict] = []
    label = store.get_label(new_label_id)
    if label is None:
        failures.append({"code": "new_label_unknown", "label_id": new_label_id,
                         "message": f"候选新标签修订 {new_label_id} 不存在"})
    else:
        if label["status"] != "approved" or label["stale"]:
            failures.append({
                "code": "new_label_not_approved", "label_id": new_label_id,
                "label_status": label["status"], "stale": label["stale"],
                "message": f"候选新标签修订 {new_label_id} 状态 {label['status']}"
                           f"{'（带 stale 标记）' if label['stale'] else ''}，"
                           f"不得作为换标候选"})
        if label["product_id"] != batch["product_id"]:
            failures.append({
                "code": "new_label_product_mismatch", "label_id": new_label_id,
                "label_product_id": label["product_id"],
                "batch_product_id": batch["product_id"],
                "message": f"新标签修订属于产品 {label['product_id']}，"
                           f"受影响批次属于 {batch['product_id']}"})
    pb = store.get_print_batch(new_print_batch_id)
    if pb is None:
        failures.append({"code": "print_batch_unknown",
                         "print_batch_id": new_print_batch_id,
                         "message": f"新卷标印刷批次 {new_print_batch_id} 不存在"})
    else:
        if label is not None and pb["label_id"] != new_label_id:
            failures.append({
                "code": "print_batch_mismatch",
                "print_batch_id": new_print_batch_id,
                "expected_label_id": new_label_id,
                "actual_label_id": pb["label_id"],
                "message": f"印刷批次 {new_print_batch_id} 属于标签 "
                           f"{pb['label_id']}，不是候选修订 {new_label_id}"})

    seen: set[str] = set()
    for it in items:
        ident = it["identifier"]
        if ident in seen:
            failures.append({
                "code": "duplicate_identifier", "identifier": ident,
                "message": f"标识 {ident} 在同一处置单内重复登记"})
            continue
        seen.add(ident)
        owner = store.active_owner_of_identifier(ident)
        if owner is not None \
                and owner["disposition_id"] != exclude_disposition_id:
            failures.append({
                "code": "identifier_occupied", "identifier": ident,
                "owner_disposition_id": owner["disposition_id"],
                "owner_status": owner["status"],
                "message": f"标识 {ident} 已被在办处置单 "
                           f"{owner['disposition_id']}（{owner['status']}）占用"})
    return {"failures": failures}


def create_disposition(store, disposition_id: str, batch: dict, source: dict,
                       new_label_id: str, new_print_batch_id: str,
                       labels_per_unit: int, items: list[dict],
                       created_by: str) -> dict:
    """登记处置单（串行事务）：门禁不过不登记任何记录。"""
    with store.transaction():
        if store.get_relabel_disposition(disposition_id) is not None:
            return {"outcome": "conflict", "disposition_id": disposition_id}
        result = evaluate_create(store, batch, items, new_label_id,
                                 new_print_batch_id)
        if result["failures"]:
            return {"outcome": "failed", "failures": result["failures"]}
        enriched = [{**it, "item_id": new_id("itm")} for it in items]
        run_ids = batch_packaging_run_ids(store, batch["batch_id"])
        disposition = store.create_relabel_disposition(
            disposition_id, batch["batch_id"], source["kind"],
            source.get("event_id") and f"events:{source['event_id']}",
            source.get("reason"), new_label_id, new_print_batch_id,
            labels_per_unit, enriched, run_ids, created_by)
        return {"outcome": "created", "disposition": disposition,
                "locked_run_ids": run_ids, "source": source}


# ---------------------------------------------------------------------- 数量合计与状态

def _prior_terminal_units(store, disposition: dict) -> int:
    """此前各轮已落不可逆终态（报废 + 已放行）的件数合计。

    重审将开启 current_epoch+1 轮；属于该新轮的事件不计入历史。
    重审预留新卷标时这些件不进入新一轮处置池；跨轮继承在 item_state 中
    逐项计算，本函数只给审核预留量提供总量。
    """
    new_epoch = disposition["current_epoch"] + 1
    total = 0
    for e in store.relabel_events(disposition["disposition_id"]):
        if e["epoch"] >= new_epoch:
            continue
        if e["kind"] in ("scrapped", "released"):
            total += e["quantity"]
    return total


def _epoch_item_aggregates(store, disposition: dict, item: dict) -> dict:
    """按 epoch 汇总某标识的处置事件（含实物件数与实际用标量）。"""
    by_epoch: dict[int, dict] = {}
    for e in store.relabel_events(disposition["disposition_id"]):
        if e["item_id"] != item["item_id"]:
            continue
        agg = by_epoch.setdefault(e["epoch"], {
            "removed": 0, "relabelled": 0, "scrapped": 0,
            "inspection_failed": 0, "released": 0, "labels_consumed": 0})
        agg[e["kind"]] = agg.get(e["kind"], 0) + e["quantity"]
        agg["labels_consumed"] += e["labels_used"]
    return by_epoch


def _epoch_good(agg: dict) -> int:
    """某轮的当前合格件 = 重贴件 - 该轮抽检失败件（失败回待处置池返工）。"""
    return max(0, agg.get("relabelled", 0) - agg.get("inspection_failed", 0))


def item_state(store, disposition: dict, item: dict) -> dict:
    """跨审核轮次推导标识处置状态（含此前 epoch 的不可逆实物结果继承）。

    数量维度：removed 已拆标 / relabelled 换标合格 / scrapped 报废 /
    failed 抽检失败（回待处置返工）/ released 已放行。

    报废与放行是**不可逆实物结果**，跨轮累计继承：已报废件不会恢复为仍隔离，
    也不能再次拆标或报废；上一轮已放行件同样不进入新一轮处置池。上一轮拆后
    尚未落终态（未重贴/未报废/抽检失败待返工）的件回到新一轮仍隔离，可再次
    拆标（新轮已无旧标实物账）与重贴/报废。
    结案恒等式：units = 累计换标合格 + 累计报废 + 仍隔离。
    """
    cur_epoch = disposition["current_epoch"]
    by_epoch = _epoch_item_aggregates(store, disposition, item)

    # 此前各轮的**不可逆实物结果**：报废与已放行。上轮换标合格但未放行的件
    # 在处置失效后其新标不再被承认，回到新一轮仍隔离处置池（可再次拆标重贴），
    # 不属于终态、不阻断新轮处置。
    prior_scrapped = prior_released = prior_removed = 0
    for ep, agg in sorted(by_epoch.items()):
        if ep >= cur_epoch:
            continue
        prior_scrapped += agg.get("scrapped", 0)
        prior_released += agg.get("released", 0)
        prior_removed += agg.get("removed", 0)
    cur = by_epoch.get(cur_epoch, {
        "removed": 0, "relabelled": 0, "scrapped": 0,
        "inspection_failed": 0, "released": 0, "labels_consumed": 0})

    # 本轮开工时可处置实物池：未报废、未放行的全部件（含上轮换标后未放行、
    # 因失效回流的件）。已报废件既不会恢复为仍隔离，也不能再次拆标/报废。
    available_base = item["units"] - prior_scrapped - prior_released
    open_units = available_base - cur.get("removed", 0)
    cur_good = _epoch_good(cur)
    # 待处置 = 本轮已拆 - 本轮当前合格 - 本轮报废
    pending = cur.get("removed", 0) - cur_good - cur.get("scrapped", 0)
    # 跨轮终态/合格累计
    scrapped_total = prior_scrapped + cur.get("scrapped", 0)
    cur_released = cur.get("released", 0)
    released_total = prior_released + cur_released
    # 当前持有效合格标结果：历史已放行（终态）+ 本轮合格件
    good_current = prior_released + cur_good
    still_quarantined = item["units"] - scrapped_total - good_current
    # 待放行只认本轮合格件：历史未放行件已回流，须重新拆标重贴后才能放行
    unreleased = cur_good - cur_released

    if open_units == 0 and pending == 0 and unreleased == 0 \
            and still_quarantined >= 0:
        if good_current > 0:
            status = "released"
        elif scrapped_total > 0:
            status = "scrapped"
        else:
            status = "closed"
    elif released_total > 0 and (unreleased > 0 or still_quarantined > 0):
        status = "partially_released"
    elif cur_good > 0:
        status = "relabelled"
    elif cur.get("removed", 0) > 0:
        status = "removed"
    else:
        status = "quarantined"
    return {
        "item_id": item["item_id"],
        "identifier": item["identifier"],
        "identifier_kind": item["identifier_kind"],
        "units": item["units"],
        "isolated": item["isolated"],
        "status": status,
        "removed_units": cur.get("removed", 0),
        "relabelled_units": cur.get("relabelled", 0),
        "inspection_failed_units": cur.get("inspection_failed", 0),
        "good_relabelled_units": cur_good,
        "scrapped_units": cur.get("scrapped", 0),
        "released_units": cur_released,
        # 跨轮继承的不可逆实物结果
        "prior_scrapped_units": prior_scrapped,
        "prior_released_units": prior_released,
        "prior_removed_units": prior_removed,
        "total_scrapped_units": scrapped_total,
        "total_released_units": released_total,
        "total_good_relabelled_units": good_current,
        "available_base_units": available_base,
        "unremoved_units": max(0, open_units),
        "pending_units": max(0, pending),
        "still_quarantined_units": still_quarantined,
        "unreleased_units": unreleased,
        "labels_consumed": cur.get("labels_consumed", 0),
    }


def reconciliation(store, disposition: dict) -> dict:
    """实时核平：件数等式 + 新卷标预留/消耗等式。

    件数等式跨审核轮次累计（报废/合格为不可逆实物结果）；卷标等式按当前
    epoch 的预留账独立核平（上一轮预留已在失效/结案时退回）。
    """
    states = [item_state(store, disposition, it)
              for it in store.relabel_items(disposition["disposition_id"])]
    isolated_units = sum(s["units"] for s in states)
    # 当前轮的重贴尝试件数（含抽检失败返工）；累计合格含历史轮不可逆结果
    relabelled = sum(s["relabelled_units"] for s in states)
    good_relabelled = sum(s["total_good_relabelled_units"] for s in states)
    scrapped = sum(s["total_scrapped_units"] for s in states)
    released = sum(s["total_released_units"] for s in states)
    failed = sum(s["inspection_failed_units"] for s in states)
    still_quarantined = sum(s["still_quarantined_units"] for s in states)
    removed = sum(s["removed_units"] for s in states)
    labels_per_unit = disposition["labels_per_unit"]
    labels_consumed = sum(s["labels_consumed"] for s in states)

    epoch = disposition["current_epoch"]
    reserved = consumed = returned_epoch = 0
    for entry in store.relabel_label_ledger(disposition["disposition_id"]):
        if entry["epoch"] != epoch:
            continue
        if entry["kind"] == "reserved":
            reserved += entry["quantity"]
        elif entry["kind"] == "consumed":
            consumed += entry["quantity"]
        elif entry["kind"] == "returned":
            returned_epoch += entry["quantity"]
    outstanding_reserved = reserved - consumed - returned_epoch
    # 每次重贴（含抽检失败后返工重贴）都实际消耗新卷标
    expected_consumed = relabelled * labels_per_unit
    unit_balance = {
        "equation": "隔离件数 = 换标合格 + 报废 + 仍隔离",
        "isolated_units": isolated_units,
        "relabelled_units": good_relabelled,
        "relabel_attempts_units": relabelled,
        "inspection_failed_units": failed,
        "scrapped_units": scrapped,
        "still_quarantined_units": still_quarantined,
        "difference": isolated_units - good_relabelled - scrapped
        - still_quarantined,
        "balanced":
            isolated_units == good_relabelled + scrapped + still_quarantined,
    }
    label_balance = {
        "equation": "预留卷标 = 重贴消耗 + 结案退回",
        "reserved_quantity": reserved,
        "labels_consumed": labels_consumed,
        "consumed_ledger_quantity": consumed,
        "expected_consumed_quantity": expected_consumed,
        "returned_quantity": returned_epoch,
        "outstanding_quantity": outstanding_reserved,
        "consumption_matches_relabel":
            labels_consumed == consumed == expected_consumed,
        "balanced": False,  # 退回在结案时发生，实时对账仅在预留全部消耗时平衡
    }
    label_balance["balanced"] = (
        label_balance["consumption_matches_relabel"]
        and reserved == consumed + returned_epoch)
    return {
        "disposition_id": disposition["disposition_id"],
        "epoch": epoch,
        "status": disposition["status"],
        "balanced": unit_balance["balanced"] and label_balance["balanced"],
        "unit_balance": unit_balance,
        "label_balance": label_balance,
        "totals": {
            "removed_units": removed,
            "released_units": released,
            "inspection_failed_units": failed,
            "unreleased_units": good_relabelled - released,
            "labels_per_unit": labels_per_unit,
        },
        "items": states,
    }


def disposition_view(store, disposition_id: str) -> dict:
    """处置单完整视图：标识、事件、各轮审核、结案与实时核平。"""
    d = store.get_relabel_disposition(disposition_id)
    return {
        **d,
        "items": store.relabel_items(disposition_id),
        "events": store.relabel_events(disposition_id),
        "reviews": store.relabel_reviews(disposition_id),
        "closures": store.relabel_closures(disposition_id),
        "run_locks": store.run_locks_for_disposition(disposition_id),
        "label_ledger": store.relabel_label_ledger(disposition_id),
        "reconciliation": reconciliation(store, d),
    }


# ---------------------------------------------------------------------- 审核门禁

def _print_batch_gate(store, pb: dict, batch: dict, reference_date: str) -> list[dict]:
    failures: list[dict] = []
    if pb["status"] != "available":
        failures.append({
            "code": "print_batch_unavailable",
            "print_batch_id": pb["print_batch_id"], "status": pb["status"],
            "frozen_reason": pb.get("frozen_reason"),
            "message": f"新卷标印刷批次 {pb['print_batch_id']} 状态 {pb['status']}，"
                       f"不得用于换标"})
    if pb["expires_at"] and reference_date > pb["expires_at"][:10]:
        failures.append({
            "code": "print_batch_unavailable",
            "print_batch_id": pb["print_batch_id"],
            "expires_at": pb["expires_at"], "reference_date": reference_date,
            "message": f"新卷标印刷批次已于 {pb['expires_at'][:10]} 失效"})
    if batch["product_id"] not in pb["applicable_product_ids"]:
        failures.append({
            "code": "product_not_applicable",
            "print_batch_id": pb["print_batch_id"],
            "expected": pb["applicable_product_ids"],
            "actual": batch["product_id"],
            "message": f"印刷批次适用产品 {pb['applicable_product_ids']} 不含"
                       f"受影响批次产品 {batch['product_id']}"})
    return failures


def review_evaluation(store, disposition: dict) -> dict:
    """审核门禁：重算批次过敏原声明并逐项比对批准快照/印刷文案。

    返回 {"ok", "failures", "item_checks", "basis", "approved_snapshot",
    "print_summary", "analysis_version", "required_labels"}。
    未隔离、标识被占用、声明不符、存在新增开放 blocker、卷标冻结/失效或
    余量不足都计为失败；failures 非空不得开工。
    """
    batch = store.get_batch(disposition["affected_batch_id"])
    label = store.get_label(disposition["new_label_id"])
    pb = store.get_print_batch(disposition["new_print_batch_id"])
    items = store.relabel_items(disposition["disposition_id"])
    failures: list[dict] = []
    item_checks: list[dict] = []

    if label is None or label["status"] != "approved" or label["stale"]:
        failures.append({
            "code": "label_not_approved",
            "label_id": disposition["new_label_id"],
            "label_status": None if label is None else label["status"],
            "stale": None if label is None else label["stale"],
            "message": "候选新标签修订未批准、已撤回或带 stale 标记，不得开工"})
    approvals = store.approvals_for_label(disposition["new_label_id"])
    snapshot = approvals[-1]["snapshot"] if approvals else {}
    approved_copy = snapshot.get("copy") or (label or {}).get("copy") or {}
    approved_summary = canonical_copy_summary(approved_copy)
    print_summary = pb["copy_summary"] if pb else {}

    basis = current_basis(store, label or {"id": disposition["new_label_id"],
                                           "copy": approved_copy,
                                           "revision": 0}, batch)
    ana_ver = compute_analysis_version(
        basis, label or {"id": disposition["new_label_id"], "revision": 0},
        batch)

    # 逐项隔离与占用核对（未隔离不得开工）
    for it in items:
        check = {"item_id": it["item_id"], "identifier": it["identifier"],
                 "identifier_kind": it["identifier_kind"], "units": it["units"],
                 "isolated": it["isolated"], "ok": True, "differences": []}
        if not it["isolated"]:
            check["ok"] = False
            check["differences"].append({
                "path": "item.isolated", "expected": True, "actual": False,
                "message": "标识未隔离，不得开工换标"})
            failures.append({
                "code": "not_isolated", "item_id": it["item_id"],
                "identifier": it["identifier"],
                "message": f"标识 {it['identifier']} 未隔离，不得开工"})
        owner = store.active_owner_of_identifier(it["identifier"])
        if owner is not None and owner["disposition_id"] != disposition["disposition_id"]:
            check["ok"] = False
            check["differences"].append({
                "path": "item.occupier", "expected": disposition["disposition_id"],
                "actual": owner["disposition_id"],
                "message": "标识被其他在办处置单占用"})
            failures.append({
                "code": "identifier_occupied", "item_id": it["item_id"],
                "identifier": it["identifier"],
                "owner_disposition_id": owner["disposition_id"],
                "message": f"标识 {it['identifier']} 被处置单 "
                           f"{owner['disposition_id']} 占用"})
        item_checks.append(check)

    # 重算该批次过敏原声明，逐项比对新标签批准快照与印刷文案
    derived_diffs = _derived_diffs(basis, snapshot.get("derived") or {})
    for d in derived_diffs:
        failures.append({"code": "declaration_mismatch", **d})
    summary_diffs = _summary_diffs(print_summary, approved_summary)
    for d in summary_diffs:
        failures.append({"code": "printed_copy_mismatch", **d})
    overridden = {
        f.get("fingerprint") for f in (snapshot.get("findings") or [])
        if f.get("status") == "overridden" and f.get("fingerprint")}
    copy_kinds = {"missing_declaration", "missing_cross_contact",
                  "unnecessary_declaration", "claim_contradiction"}
    for f in basis["findings"]:
        if f.severity != BLOCKER or f.kind in copy_kinds:
            continue
        if f.fingerprint in overridden:
            continue
        failures.append({
            "code": "open_blocker", "kind": f.kind,
            "fingerprint": f.fingerprint, "message": f.message,
            "detail": f.detail})

    # 预留量只覆盖本轮仍可处置实物池：扣除此前各轮已报废/已放行的不可逆
    # 终态件（它们既不恢复为仍隔离，也不再需要新卷标）
    terminal_units = _prior_terminal_units(store, disposition)
    processable = sum(i["units"] for i in items if i["isolated"]) \
        - terminal_units
    required_labels = processable * disposition["labels_per_unit"]
    if pb is not None:
        reference_date = utcnow()[:10]
        for f in _print_batch_gate(store, pb, batch, reference_date):
            failures.append(f)
        if pb["status"] == "available" and required_labels > pb["remaining_quantity"]:
            failures.append({
                "code": "insufficient_label_quantity",
                "print_batch_id": pb["print_batch_id"],
                "required_quantity": required_labels,
                "remaining_quantity": pb["remaining_quantity"],
                "message": f"新卷标余量 {pb['remaining_quantity']} 不足，"
                           f"审核通过需预留 {required_labels}"})
    else:
        failures.append({"code": "print_batch_unavailable",
                         "print_batch_id": disposition["new_print_batch_id"],
                         "message": "新卷标印刷批次不存在"})
    return {
        "ok": not failures,
        "failures": failures,
        "item_checks": item_checks,
        "basis": basis,
        "approved_snapshot": snapshot,
        "approved_copy": approved_copy,
        "print_summary": print_summary,
        "analysis_version": ana_ver,
        "required_labels": required_labels,
    }


def _derived_diffs(basis: dict, approved_derived: dict) -> list[dict]:
    """受影响批次当前重算声明逐项对照新标签批准快照。"""
    cur, appr = basis["derived"], approved_derived or {}
    diffs: list[dict] = []
    for bucket, label_name in (("required", "应声明过敏原"),
                               ("may_contain", "交叉接触提示")):
        c = set((cur or {}).get(bucket, {}))
        a = set((appr or {}).get(bucket, {}))
        for allergen in sorted(c - a):
            diffs.append({
                "path": f"derived.{bucket}.extra[{allergen}]",
                "message": f"受影响批次当前重算新增{label_name} “{allergen}”，"
                           f"新标签批准快照未包含",
                "expected": sorted(a), "actual": sorted(c)})
        for allergen in sorted(a - c):
            diffs.append({
                "path": f"derived.{bucket}.missing[{allergen}]",
                "message": f"新标签批准快照包含{label_name} “{allergen}”，"
                           f"受影响批次当前重算不包含",
                "expected": sorted(a), "actual": sorted(c)})
    return diffs


def _summary_diffs(printed: dict, approved: dict) -> list[dict]:
    """新卷标印刷摘要逐项对照新标签批准快照文案。"""
    diffs: list[dict] = []
    for key, label_name in (("declared_allergens", "已声明过敏原"),
                           ("may_contain", "交叉接触提示"),
                           ("free_from_claims", "“无…”宣称")):
        p = set((printed or {}).get(key, []))
        a = set((approved or {}).get(key, []))
        for name in sorted(p - a):
            diffs.append({
                "path": f"print_summary.{key}.extra[{name}]",
                "message": f"新卷标的{label_name}多出 “{name}”，不属于批准文案",
                "expected": sorted(a), "actual": sorted(p)})
        for name in sorted(a - p):
            diffs.append({
                "path": f"print_summary.{key}.missing[{name}]",
                "message": f"新卷标的{label_name}缺少 “{name}”",
                "expected": sorted(a), "actual": sorted(p)})
    if (printed or {}).get("ingredients_text", "") \
            != (approved or {}).get("ingredients_text", ""):
        diffs.append({
            "path": "print_summary.ingredients_text",
            "message": "新卷标的配料表文本与批准文案不一致",
            "expected": (approved or {}).get("ingredients_text", ""),
            "actual": (printed or {}).get("ingredients_text", "")})
    return diffs


def submit_review(store, disposition_id: str, reviewer: str) -> dict:
    """审核（串行事务）：门禁不过不落任何记录；通过则抬升 epoch 并预留新卷标。

    草拟与失效待复核的处置单都可送审；每次通过开启新一轮（epoch+1），
    重审预留只按当前仍可处置实物件数计算（扣除此前各轮已报废/已放行的
    不可逆终态件；旧轮预留已在失效时退回）。
    """
    with store.transaction():
        d = store.get_relabel_disposition(disposition_id)
        if d is None:
            return {"outcome": "not_found"}
        if d["status"] not in ("draft", "invalidated"):
            return {"outcome": "wrong_status", "status": d["status"]}
        result = review_evaluation(store, d)
        if result["failures"]:
            return {"outcome": "failed", "failures": result["failures"],
                    "item_checks": result["item_checks"],
                    "analysis_version": result["analysis_version"]}
        epoch = d["current_epoch"] + 1
        pb = store.get_print_batch(d["new_print_batch_id"])
        ledger = [{"print_batch_id": pb["print_batch_id"], "kind": "reserved",
                   "quantity": result["required_labels"],
                   "reason": f"处置单 {disposition_id} 第 {epoch} 轮审核通过预留"}]
        review = {
            "new_label_id": d["new_label_id"],
            "new_print_batch_id": d["new_print_batch_id"],
            "analysis_version": result["analysis_version"],
            "approved_copy": result["approved_copy"],
            "print_summary": result["print_summary"],
            "basis_derived": result["basis"]["derived"],
            "item_checks": result["item_checks"],
        }
        store.approve_relabel_review(disposition_id, reviewer, epoch, review,
                                     ledger)
        return {"outcome": "approved",
                "analysis_version": result["analysis_version"],
                "epoch": epoch,
                "reserved_quantity": result["required_labels"],
                "item_checks": result["item_checks"]}


def amend_candidate(store, disposition_id: str, new_label_id: str,
                    new_print_batch_id: str) -> dict:
    """失效/草拟处置单改指定候选新标签修订与印刷批次（改后须重新审核）。"""
    with store.transaction():
        d = store.get_relabel_disposition(disposition_id)
        if d is None:
            return {"outcome": "not_found"}
        if d["status"] not in ("draft", "invalidated"):
            return {"outcome": "wrong_status", "status": d["status"]}
        batch = store.get_batch(d["affected_batch_id"])
        result = evaluate_create(
            store, batch, store.relabel_items(disposition_id),
            new_label_id, new_print_batch_id,
            exclude_disposition_id=disposition_id)
        if result["failures"]:
            return {"outcome": "failed", "failures": result["failures"]}
        store.set_relabel_candidate(disposition_id, new_label_id,
                                    new_print_batch_id)
        return {"outcome": "amended"}


# ---------------------------------------------------------------------- 处置事件

def _event_content(disposition_id: str, item_id: str, kind: str, quantity: int,
                   labels_used: int, epoch: int, operator: str) -> dict:
    return {"disposition_id": disposition_id, "item_id": item_id, "kind": kind,
            "quantity": quantity, "labels_used": labels_used, "epoch": epoch,
            "operator": operator}


def record_event(store, disposition_id: str, item_id: str,
                 idempotency_key: str, kind: str, quantity: int,
                 operator: str, occurred_at: str | None,
                 reason: str | None) -> dict:
    """追加处置事件（幂等）：同键同内容重放复用原事件，同键内容冲突拒绝。

    仅当前审核轮次（approved）接受事件；处置单失效后旧轮事件账冻结待复核，
    重审通过后按新 epoch 重新登记。
    """
    with store.transaction():
        prior = store.relabel_event_by_key(idempotency_key)
        d = store.get_relabel_disposition(disposition_id)
        if prior is not None:
            content = _event_content(
                disposition_id, item_id, kind, quantity,
                prior["labels_used"], prior["epoch"], operator)
            if _event_content(prior["disposition_id"], prior["item_id"],
                              prior["kind"], prior["quantity"],
                              prior["labels_used"], prior["epoch"],
                              prior["operator"]) != content:
                return {"outcome": "conflict", "prior": prior}
            return {"outcome": "reused", "event": prior}
        if d is None:
            return {"outcome": "not_found"}
        if d["status"] != "approved":
            return {"outcome": "failed",
                    "failures": [{"code": "not_approved", "status": d["status"],
                                  "message": f"处置单状态 {d['status']}，"
                                             f"仅审核通过后可登记处置事件"}]}
        item = store.relabel_item(item_id)
        if item is None or item["disposition_id"] != disposition_id:
            return {"outcome": "failed",
                    "failures": [{"code": "identifier_unknown", "item_id": item_id,
                                  "message": "标识项不属于本处置单"}]}
        epoch = d["current_epoch"]
        states = {s["item_id"]: s for s in
                  reconciliation(store, d)["items"]}
        st = states[item_id]
        failures = _event_gate(kind, quantity, st)
        labels_used = 0
        if kind == "relabelled" and not failures:
            labels_used = quantity * d["labels_per_unit"]
            failures = _label_consumption_gate(store, d, labels_used)
        if failures:
            return {"outcome": "failed", "failures": failures}
        event = store.create_relabel_event(
            new_id("rev"), disposition_id, item_id, idempotency_key, kind,
            quantity, labels_used, epoch, operator, occurred_at, reason)
        return {"outcome": "created", "event": event}


def _event_gate(kind: str, quantity: int, st: dict) -> list[dict]:
    """单事件门禁：保证任一中间账都不超过隔离件数，且先拆标再处置。"""
    failures: list[dict] = []

    def over(code: str, field: str, limit: int, message: str):
        if quantity > limit:
            failures.append({"code": code, "item_id": st["item_id"],
                             "identifier": st["identifier"], "field": field,
                             "requested": quantity, "limit": limit,
                             "message": message})

    if kind == "removed":
        over("removed_limit_exceeded", "removed", st["unremoved_units"],
             f"拆标 {quantity} 件超过未拆标 {st['unremoved_units']} 件")
    elif kind in ("relabelled", "scrapped"):
        code = "relabel_limit_exceeded" if kind == "relabelled" \
            else "scrap_limit_exceeded"
        if st["removed_units"] == 0:
            failures.append({"code": "not_removed", "item_id": st["item_id"],
                             "identifier": st["identifier"],
                             "message": "须先拆标后才能重贴/报废"})
        # 待处置 = 已拆 - 已合格 - 已报废（抽检失败件仍在其中等待返工）
        over(code, kind, st["pending_units"],
             f"{kind} {quantity} 件超过待处置 {st['pending_units']} 件")
    elif kind == "inspection_failed":
        # 抽检失败的是当前换标合格且未放行件；失败件冲减合格、回到待处置
        # 池，后续须重新重贴或报废（不能放行失败件）
        over("relabel_limit_exceeded", "inspection_failed",
             st["unreleased_units"],
             f"抽检失败 {quantity} 件超过换标合格未放行 "
             f"{st['unreleased_units']} 件")
    elif kind == "released":
        over("release_limit_exceeded", "released", st["unreleased_units"],
             f"放行 {quantity} 件超过换标合格未放行 {st['unreleased_units']} 件")
    return failures


def _label_consumption_gate(store, d: dict, labels_used: int) -> list[dict]:
    """重贴消耗新卷标：不得超过该轮预留，且印刷批次仍可领用。"""
    pb = store.get_print_batch(d["new_print_batch_id"])
    if pb is None or pb["status"] != "available":
        return [{"code": "print_batch_unavailable",
                 "print_batch_id": d["new_print_batch_id"],
                 "status": None if pb is None else pb["status"],
                 "message": "重贴时新卷标印刷批次已不可用，处置单须复核"}]
    rec = reconciliation(store, d)
    outstanding = rec["label_balance"]["outstanding_quantity"]
    if labels_used > outstanding:
        return [{"code": "insufficient_label_quantity",
                 "print_batch_id": pb["print_batch_id"],
                 "requested": labels_used,
                 "reserved_outstanding_quantity": outstanding,
                 "message": f"重贴需 {labels_used} 枚新卷标，超过该轮预留"
                            f"未消耗 {outstanding} 枚"}]
    return []


# ---------------------------------------------------------------------- 结案

def close_disposition(store, disposition_id: str, closed_by: str) -> dict:
    """结案核平（串行事务）：件数等式与新卷标等式同时成立才落结案记录。

    - 隔离件数 = 换标合格 + 报废 + 仍隔离（仍隔离含未拆标与抽检失败件）；
    - 预留卷标 = 重贴消耗 + 结案退回（未消耗预留退回印刷批次可领用余量）；
    - 换标合格件必须全部放行。
    结案快照 append-only，处置单置 closed 后只读。
    """
    with store.transaction():
        d = store.get_relabel_disposition(disposition_id)
        if d is None:
            return {"outcome": "not_found"}
        if d["status"] != "approved":
            return {"outcome": "failed",
                    "failures": [{"code": "not_approved", "status": d["status"],
                                  "message": f"处置单状态 {d['status']}，"
                                             f"仅审核通过的在办单可结案"}]}
        rec = reconciliation(store, d)
        failures: list[dict] = []
        if rec["totals"]["unreleased_units"] > 0:
            failures.append({
                "code": "unreleased_units",
                "unreleased_units": rec["totals"]["unreleased_units"],
                "message": "仍有换标合格件未放行，不得结案"})
        outstanding = rec["label_balance"]["outstanding_quantity"]
        if not rec["unit_balance"]["balanced"]:
            failures.append({"code": "imbalanced", "balance": "unit",
                             "difference": rec["unit_balance"]["difference"],
                             "message": "件数核平等式不成立"})
        if not rec["label_balance"]["consumption_matches_relabel"]:
            failures.append({"code": "imbalanced", "balance": "label_consumption",
                             "message": "新卷标消耗与换标合格件数不一致"})
        if failures:
            return {"outcome": "failed", "failures": failures,
                    "reconciliation": rec}
        # 未消耗预留退回新卷标印刷批次（returned 账恢复可领用余量）
        if outstanding > 0:
            store._exec(
                "INSERT INTO relabel_label_ledger (disposition_id, print_batch_id,"
                " kind, quantity, epoch, reason, created_at)"
                " VALUES (?,?,'returned',?,?,?,?)",
                (disposition_id, d["new_print_batch_id"], outstanding,
                 d["current_epoch"], f"处置单 {disposition_id} 结案退回未消耗预留",
                 utcnow()))
            store.log_event("relabel_labels_returned", {
                "disposition_id": disposition_id,
                "print_batch_id": d["new_print_batch_id"],
                "quantity": outstanding, "epoch": d["current_epoch"]})
        closure = store.close_relabel_disposition(
            disposition_id, d["current_epoch"],
            {"reconciliation": reconciliation(
                store, store.get_relabel_disposition(disposition_id))},
            closed_by)
        return {"outcome": "closed", "closure": closure,
                "reconciliation": store.get_relabel_closure(
                    closure["closure_id"])["snapshot"]["reconciliation"]}


# ---------------------------------------------------------------------- 在办失效传播

def invalidate_for_batch(store, batch_ids, reason: str) -> list[dict]:
    """受影响批次资料再变动（规格更正/阳性拭子）：在办处置单失效待复核。"""
    invalidated = []
    for bid in sorted(batch_ids):
        for d in store.relabel_dispositions_for_batch(bid):
            if d["status"] in ("approved", "draft"):
                invalidated.append(_invalidate_one(store, d, reason))
    return [x for x in invalidated if x]


def invalidate_for_label(store, label_id: str, reason: str) -> list[dict]:
    """候选新标签修订撤回/再变动：引用它的在办处置单失效。"""
    invalidated = []
    for d in store.relabel_dispositions_for_new_label(label_id):
        if d["status"] in ("approved", "draft"):
            invalidated.append(_invalidate_one(store, d, reason))
    return [x for x in invalidated if x]


def invalidate_for_print_batch(store, print_batch_id: str, reason: str) -> list[dict]:
    """候选新卷标印刷批次被冻结/失效：引用它的在办处置单失效。"""
    invalidated = []
    for d in store.relabel_dispositions_for_print_batch(print_batch_id):
        if d["status"] in ("approved", "draft"):
            invalidated.append(_invalidate_one(store, d, reason))
    return [x for x in invalidated if x]


def _invalidate_one(store, d: dict, reason: str) -> dict:
    """退回该轮未消耗预留并置 invalidated；事件与审核快照全部保留。"""
    epoch = d["current_epoch"]
    reserved = consumed = returned = 0
    if d["status"] == "approved":
        for entry in store.relabel_label_ledger(d["disposition_id"]):
            if entry["epoch"] != epoch:
                continue
            if entry["kind"] == "reserved":
                reserved += entry["quantity"]
            elif entry["kind"] == "consumed":
                consumed += entry["quantity"]
            elif entry["kind"] == "returned":
                returned += entry["quantity"]
    outstanding = max(0, reserved - consumed - returned)
    store.invalidate_relabel_disposition(
        d["disposition_id"], reason, d["new_print_batch_id"], epoch, outstanding)
    rec = reconciliation(store, store.get_relabel_disposition(d["disposition_id"]))
    return {
        "disposition_id": d["disposition_id"],
        "affected_batch_id": d["affected_batch_id"],
        "epoch": epoch,
        "returned_reserved_quantity": outstanding,
        "pending_review_items": [
            {"item_id": s["item_id"], "identifier": s["identifier"],
             "identifier_kind": s["identifier_kind"], "units": s["units"],
             "status": s["status"],
             "relabelled_units": s["relabelled_units"],
             "scrapped_units": s["scrapped_units"],
             "removed_units": s["removed_units"]}
            for s in rec["items"]],
    }


# ---------------------------------------------------------------------- 审计导出

def revision_chain(store, label_id: str) -> list[dict]:
    """标签修订链：沿 parent_id 从新到旧串起撤回/更正派生关系。"""
    chain = []
    seen = set()
    cur = store.get_label(label_id)
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        approvals = store.approvals_for_label(cur["id"])
        chain.append({
            "label_id": cur["id"], "revision": cur["revision"],
            "product_id": cur["product_id"], "status": cur["status"],
            "stale": cur["stale"], "parent_id": cur["parent_id"],
            "created_at": cur["created_at"],
            "approved_at": approvals[-1]["approved_at"] if approvals else None,
            "approved_by": approvals[-1]["approved_by"] if approvals else None,
        })
        cur = store.get_label(cur["parent_id"]) if cur["parent_id"] else None
    return chain


def original_packaging(store, disposition: dict) -> dict:
    """原包装段：受影响批次的包装运行、用标账与被锁状态。"""
    bid = disposition["affected_batch_id"]
    runs = []
    for run in store.runs_for_batch(bid):
        from .packaging import reconciliation as pack_rec

        lock = store.run_relabel_lock(run["run_id"])
        runs.append({
            "run_id": run["run_id"], "line_id": run["line_id"],
            "status": run["status"],
            "planned_quantity": run["planned_quantity"],
            "labels_per_unit": run["labels_per_unit"],
            "issuances": run["issuances"],
            "clearance_findings": run["clearance_findings"],
            "reconciliation": pack_rec(store, run),
            "locked_by_disposition": lock["disposition_id"] if lock else None,
        })
    return {
        "batch_id": bid,
        "product_id": (store.get_batch(bid) or {}).get("product_id"),
        "source_kind": disposition["source_kind"],
        "source_ref": disposition["source_ref"],
        "source_reason": disposition["source_reason"],
        "runs": runs,
    }


def build_audit_export(store, disposition_id: str) -> dict:
    """审计导出：串起原包装、换标去向与修订链（含各轮审核与结案快照）。"""
    d = store.get_relabel_disposition(disposition_id)
    if d is None:
        raise KeyError(f"relabel disposition {disposition_id} not found")
    new_label = store.get_label(d["new_label_id"])
    new_pb = store.get_print_batch(d["new_print_batch_id"])
    reviews = store.relabel_reviews(disposition_id)
    closures = store.relabel_closures(disposition_id)
    events = store.relabel_events(disposition_id)
    # 旧修订：从受影响批次的包装运行领用记录还原实际贴用的标签修订
    old_label_ids = []
    for run in store.runs_for_batch(d["affected_batch_id"]):
        for iss in run["issuances"]:
            if iss["label_id"] not in old_label_ids:
                old_label_ids.append(iss["label_id"])
    reviews_enriched = []
    for rv in reviews:
        lbl = store.get_label(rv["new_label_id"]) if rv.get("new_label_id") else None
        pbv = store.get_print_batch(rv["new_print_batch_id"]) \
            if rv.get("new_print_batch_id") else None
        reviews_enriched.append({
            **rv,
            # 直接还原该轮候选标签修订（无需再查处置单当前指向）
            "candidate_label": (
                {"label_id": lbl["id"], "revision": lbl["revision"],
                 "product_id": lbl["product_id"], "status": lbl["status"],
                 "stale": lbl["stale"], "copy": lbl["copy"]}
                if lbl else None),
            "candidate_print_batch": (
                {"print_batch_id": pbv["print_batch_id"],
                 "label_id": pbv["label_id"],
                 "copy_summary": pbv["copy_summary"],
                 "status": pbv["status"]}
                if pbv else None),
        })
    ledger = store.relabel_label_ledger(disposition_id)
    # label_ledger 保留全部明细（跨 PB2/PB3、跨轮），并给出按印刷批次+轮次
    # 的分轮汇总，便于直接核对每轮预留/消耗/退回
    ledger_by_epoch: dict[tuple, dict] = {}
    for e in ledger:
        key = (e["print_batch_id"], e["epoch"])
        grp = ledger_by_epoch.setdefault(key, {
            "print_batch_id": e["print_batch_id"], "epoch": e["epoch"],
            "reserved": 0, "consumed": 0, "returned": 0})
        grp[e["kind"]] = grp.get(e["kind"], 0) + e["quantity"]
    return {
        "export_type": "relabel_disposition_audit",
        "generated_at": utcnow(),
        "disposition": d,
        "original_packaging": original_packaging(store, d),
        "relabel_outcome": {
            **disposition_view(store, disposition_id),
        },
        "new_label": (
            {"label_id": new_label["id"], "revision": new_label["revision"],
             "status": new_label["status"], "stale": new_label["stale"],
             "copy": new_label["copy"]} if new_label else None),
        "new_print_batch": (
            {"print_batch_id": new_pb["print_batch_id"],
             "status": new_pb["status"],
             "copy_summary": new_pb["copy_summary"],
             "quantity_received": new_pb["quantity_received"],
             "reserved_quantity": new_pb["reserved_quantity"],
             "reserved_consumed_quantity": new_pb["reserved_consumed_quantity"],
             "reserved_returned_quantity": new_pb["reserved_returned_quantity"],
             "remaining_quantity": new_pb["remaining_quantity"],
             "frozen_reason": new_pb["frozen_reason"]} if new_pb else None),
        "revision_chain": {
            "new_label_chain": revision_chain(store, d["new_label_id"]),
            "candidate_chains_by_epoch": [
                {"epoch": rv["epoch"],
                 "new_label_id": rv.get("new_label_id"),
                 "chain": revision_chain(store, rv["new_label_id"])
                 if rv.get("new_label_id") else []}
                for rv in reviews],
            "old_label_chains": [revision_chain(store, lid)
                                 for lid in old_label_ids],
        },
        "reviews": reviews_enriched,
        "closures": closures,
        "events": events,
        "run_locks": store.run_locks_for_disposition(disposition_id),
        "label_ledger": ledger,
        "label_ledger_by_epoch": sorted(
            ledger_by_epoch.values(),
            key=lambda g: (g["epoch"], g["print_batch_id"])),
    }


def label_disposition_evidence(store, label_id: str) -> dict:
    """标签核对包用：汇总与该修订相关的全部换标处置单。

    - as_new_label：以该修订为候选新标签的处置（换标去向）；
    - as_old_label：受影响批次原包装实际贴用过该修订的处置（原包装来源）。
    """
    as_new = store.relabel_dispositions_for_new_label(label_id)
    as_new_ids = {d["disposition_id"] for d in as_new}
    as_old = []
    for pb in store.print_batches_for_label(label_id):
        for iss in store.issuances_for_print_batch(pb["print_batch_id"]):
            bid = iss["production_batch_id"]
            for d in store.relabel_dispositions_for_batch(bid):
                if d["disposition_id"] in as_new_ids:
                    continue
                if d["disposition_id"] in {x["disposition_id"] for x in as_old}:
                    continue
                as_old.append(d)

    def brief(d: dict) -> dict:
        return {
            "disposition_id": d["disposition_id"],
            "affected_batch_id": d["affected_batch_id"],
            "source_kind": d["source_kind"],
            "source_reason": d["source_reason"],
            "status": d["status"],
            "current_epoch": d["current_epoch"],
            "invalidated_reason": d["invalidated_reason"],
            "new_label_id": d["new_label_id"],
            "new_print_batch_id": d["new_print_batch_id"],
            "reconciliation": reconciliation(store, d),
        }

    return {
        "label_id": label_id,
        "as_new_label": [brief(d) for d in as_new],
        "as_old_label": [brief(d) for d in as_old],
    }


def dedup_invalidated(entries: list[dict]) -> list[dict]:
    """合并多个失效来源（标签 / 印刷批次 / 批次维度），按处置单去重。"""
    seen, out = set(), []
    for e in entries:
        if e["disposition_id"] in seen:
            continue
        seen.add(e["disposition_id"])
        out.append(e)
    return out
