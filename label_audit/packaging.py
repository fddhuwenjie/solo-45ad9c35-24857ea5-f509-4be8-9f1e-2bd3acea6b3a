"""包装执行与卷标结算：开工绑定、幂等用标事件、双等式结算与盘点更正。

包装线领出卷标后，本模块把每一枚卷标的去向落成可审计记录：

- 开工绑定：包装运行绑定生产批次、领用记录、包装线、计划产量与每件用标数，
  并登记清场发现；旧卷标未隔离、领用横跨不同标签修订、卷标已冻结或适用
  产品不符时拒绝开工（all-or-nothing，门禁不过不登记任何记录）；
- 用标事件：合格品贴用 / 过程损耗 / 留样 / 退回隔离均以幂等事件追加，
  保留操作者与时刻；事件只增不改，同键重放复用原结果、同键冲突拒绝；
- 结算：同时满足「领用量 = 贴用 + 损耗 + 留样 + 退回隔离」与
  「贴用量 = 合格品数 × 每件用标数」才落结算记录；差异按数量来源逐项
  返回（领用来自哪些领用记录、各类别来自多少条用标事件与调整事件）；
  退回隔离数量留在领用方账上，不补回印刷批次可领用余量；
- 盘点更正：结算后记录不可覆盖，更正以 adjustment 调整事件（有符号增量，
  必须写明理由）追加，重新结算生成新的结算记录——结算记录同样只增不改；
- 波及清单：标签撤回 / 规格更正时，按生产批次列出未结算现场余量、
  已包装数量与待隔离批次，供处置评估。
"""
from __future__ import annotations

from .db import PACKAGING_CATEGORIES, new_id

# 开工门禁失败代码
GATE_CODES = (
    "old_rolls_not_isolated",    # 清场发现旧卷标但未隔离
    "issuance_unknown",          # 领用记录不存在
    "issuance_batch_mismatch",   # 领用记录属于其他生产批次
    "issuance_already_bound",    # 领用记录已绑定其他包装运行
    "mixed_label_revisions",     # 领用横跨不同标签修订
    "print_batch_frozen",        # 卷标已冻结
    "label_revision_blocked",    # 标签修订已撤回或带 stale 标记
    "product_not_applicable",    # 印刷批次适用产品不含待包装批次产品
)

# 调整门禁失败代码
ADJUSTMENT_CODES = (
    "negative_balance",          # 调整后类别合计为负
    "negative_good_units",       # 调整后合格品数为负
)


# ---------------------------------------------------------------------- 开工门禁

def evaluate_run_start(store, batch: dict, issuance_ids: list[str],
                       clearance_findings: list[dict]) -> dict:
    """开工门禁：清场、领用一致性与卷标状态逐项核对。

    返回 {"issuances": [...], "failures": [...]}；failures 非空时调用方应
    整体拒绝，不得登记任何运行记录。同一印刷批次/标签修订的同类失败只记一次。
    """
    failures: list[dict] = []

    # 清场发现：旧卷标未隔离不得开工（已隔离的发现照实登记，不阻断）
    for f in clearance_findings:
        found = f.get("old_rolls_found", 0)
        if found > 0 and not f.get("isolated", False):
            failures.append({
                "code": "old_rolls_not_isolated",
                "finding": f.get("finding"),
                "old_rolls_found": found,
                "message": f"清场发现旧卷标 {found} 卷未隔离，线边混入风险未消除，"
                           f"不得开工"})

    issuances: list[dict] = []
    for iss_id in sorted(set(issuance_ids)):
        iss = store.get_issuance(iss_id)
        if iss is None:
            failures.append({"code": "issuance_unknown", "issuance_id": iss_id,
                             "message": f"领用记录 {iss_id} 不存在"})
            continue
        issuances.append(iss)
        if iss["production_batch_id"] != batch["batch_id"]:
            failures.append({
                "code": "issuance_batch_mismatch", "issuance_id": iss_id,
                "expected": batch["batch_id"],
                "actual": iss["production_batch_id"],
                "message": f"领用记录 {iss_id} 属于生产批次 "
                           f"{iss['production_batch_id']}，不得绑定到 "
                           f"{batch['batch_id']} 的包装运行"})
        bound = store.issuance_bound_run(iss_id)
        if bound is not None:
            failures.append({
                "code": "issuance_already_bound", "issuance_id": iss_id,
                "run_id": bound,
                "message": f"领用记录 {iss_id} 已绑定包装运行 {bound}，"
                           f"不得重复绑定（避免重复计量）"})

    # 领用必须来自同一标签修订：不同修订的卷标同线混用是贴错批的典型来源
    revisions = sorted({(i["label_id"], i["label_revision"]) for i in issuances})
    if len(revisions) > 1:
        failures.append({
            "code": "mixed_label_revisions",
            "revisions": [{"label_id": lid, "revision": rev} for lid, rev in revisions],
            "message": "领用记录横跨不同标签修订，同线混用不得开工"})

    seen_print_batches, seen_labels = set(), set()
    for iss in issuances:
        if iss["print_batch_id"] not in seen_print_batches:
            seen_print_batches.add(iss["print_batch_id"])
            pb = store.get_print_batch(iss["print_batch_id"])
            if pb is not None:
                if pb["status"] == "frozen":
                    failures.append({
                        "code": "print_batch_frozen",
                        "print_batch_id": pb["print_batch_id"],
                        "frozen_reason": pb["frozen_reason"],
                        "message": f"印刷批次 {pb['print_batch_id']} 已冻结"
                                   f"（{pb['frozen_reason'] or '未注明原因'}），"
                                   f"线边余卷不得上线"})
                if batch["product_id"] not in pb["applicable_product_ids"]:
                    failures.append({
                        "code": "product_not_applicable",
                        "print_batch_id": pb["print_batch_id"],
                        "expected": pb["applicable_product_ids"],
                        "actual": batch["product_id"],
                        "message": f"印刷批次 {pb['print_batch_id']} 适用产品 "
                                   f"{pb['applicable_product_ids']} 不含待包装批次"
                                   f"产品 {batch['product_id']}"})
        if iss["label_id"] not in seen_labels:
            seen_labels.add(iss["label_id"])
            label = store.get_label(iss["label_id"])
            if label is not None and (label["status"] != "approved" or label["stale"]):
                failures.append({
                    "code": "label_revision_blocked",
                    "label_id": iss["label_id"],
                    "label_status": label["status"], "stale": label["stale"],
                    "message": f"标签修订 {iss['label_id']} 状态 {label['status']}"
                               f"{'（带 stale 标记）' if label['stale'] else ''}，"
                               f"其卷标不得上线"})
    return {"issuances": issuances, "failures": failures}


def start_run(store, run_id: str, batch: dict, line_id: str,
              planned_quantity: int, labels_per_unit: int,
              issuance_ids: list[str], operator: str,
              clearance_findings: list[dict]) -> dict:
    """开工登记（串行事务）：门禁不过不登记任何记录。"""
    with store.transaction():
        if store.get_packaging_run(run_id) is not None:
            return {"outcome": "conflict", "run_id": run_id}
        result = evaluate_run_start(store, batch, issuance_ids, clearance_findings)
        if result["failures"]:
            return {"outcome": "failed", "failures": result["failures"]}
        run = store.create_packaging_run(
            run_id, batch["batch_id"], line_id, planned_quantity, labels_per_unit,
            result["issuances"], operator, clearance_findings)
        return {"outcome": "created", "run": run}


# ---------------------------------------------------------------------- 数量合计

def run_totals(store, run_id: str) -> dict:
    """运行的用标合计：用标事件与盘点调整分列，类别合计 = 用标 + 调整。"""
    usage = {c: 0 for c in PACKAGING_CATEGORIES}
    adjustments = {c: 0 for c in PACKAGING_CATEGORIES}
    good_units_usage = 0
    good_units_adjustment = 0
    usage_events = 0
    adjustment_events = 0
    for e in store.events_for_run(run_id):
        if e["kind"] == "adjustment":
            adjustments[e["category"]] += e["quantity"]
            if e["category"] == "applied":
                good_units_adjustment += e["good_units"] or 0
            adjustment_events += 1
        else:
            usage[e["kind"]] += e["quantity"]
            if e["kind"] == "applied":
                good_units_usage += e["good_units"] or 0
            usage_events += 1
    return {
        "usage": usage,
        "adjustments": adjustments,
        "totals": {c: usage[c] + adjustments[c] for c in PACKAGING_CATEGORIES},
        "good_units_usage": good_units_usage,
        "good_units_adjustment": good_units_adjustment,
        "good_units": good_units_usage + good_units_adjustment,
        "usage_events": usage_events,
        "adjustment_events": adjustment_events,
    }


def _category_source(category: str, t: dict) -> dict:
    return {"total": t["totals"][category],
            "usage_quantity": t["usage"][category],
            "adjustment_quantity": t["adjustments"][category]}


def reconciliation(store, run: dict) -> dict:
    """实时对账：两条平衡等式 + 各数量的来源拆分（结算与差异返回共用）。"""
    t = run_totals(store, run["run_id"])
    tot = t["totals"]
    issued = sum(i["quantity"] for i in run["issuances"])
    accounted = tot["applied"] + tot["wasted"] + tot["sampled"] + tot["returned"]
    expected_applied = t["good_units"] * run["labels_per_unit"]
    issuance_balance = {
        "equation": "领用量 = 贴用量 + 损耗量 + 留样量 + 退回隔离量",
        "issued_quantity": issued,
        "accounted_quantity": accounted,
        "difference": issued - accounted,
        "balanced": issued == accounted,
    }
    application_balance = {
        "equation": "贴用量 = 合格品数 × 每件用标数",
        "applied_quantity": tot["applied"],
        "good_units": t["good_units"],
        "labels_per_unit": run["labels_per_unit"],
        "expected_applied": expected_applied,
        "difference": tot["applied"] - expected_applied,
        "balanced": tot["applied"] == expected_applied,
    }
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "balanced": issuance_balance["balanced"] and application_balance["balanced"],
        "balances": {"issuance_balance": issuance_balance,
                     "application_balance": application_balance},
        "sources": {
            "issued": {
                "total": issued,
                "issuances": [{"issuance_id": i["issuance_id"],
                               "print_batch_id": i["print_batch_id"],
                               "label_id": i["label_id"],
                               "label_revision": i["label_revision"],
                               "quantity": i["quantity"]}
                              for i in run["issuances"]]},
            "applied": _category_source("applied", t),
            "wasted": _category_source("wasted", t),
            "sampled": _category_source("sampled", t),
            "returned": _category_source("returned", t),
            "good_units": {"total": t["good_units"],
                           "usage_units": t["good_units_usage"],
                           "adjustment_units": t["good_units_adjustment"]},
        },
        "event_counts": {"usage_events": t["usage_events"],
                         "adjustment_events": t["adjustment_events"]},
        "on_line_remaining": issued - accounted,
        "planned": {"planned_quantity": run["planned_quantity"],
                    "expected_label_quantity": run["expected_label_quantity"],
                    "good_units": t["good_units"],
                    "unit_variance": t["good_units"] - run["planned_quantity"]},
    }


def run_view(store, run_id: str) -> dict:
    """运行完整视图：绑定、清场发现、用标/调整事件、结算记录与实时对账。"""
    run = store.get_packaging_run(run_id)
    return {**run,
            "events": store.events_for_run(run_id),
            "settlements": store.settlements_for_run(run_id),
            "reconciliation": reconciliation(store, run)}


# ---------------------------------------------------------------------- 用标事件

def _event_content(run_id: str, kind: str, category: str, quantity: int,
                   good_units: int | None, operator: str) -> dict:
    return {"run_id": run_id, "kind": kind, "category": category,
            "quantity": quantity, "good_units": good_units, "operator": operator}


def _stored_content(event: dict) -> dict:
    return _event_content(event["run_id"], event["kind"], event["category"],
                          event["quantity"], event["good_units"], event["operator"])


def record_event(store, run: dict, idempotency_key: str, kind: str,
                 quantity: int, good_units: int | None, operator: str,
                 occurred_at: str | None, reason: str | None) -> dict:
    """记录用标事件（幂等）：同键同内容重放复用原事件，同键内容冲突拒绝。"""
    content = _event_content(run["run_id"], kind, kind, quantity, good_units,
                             operator)
    with store.transaction():
        prior = store.packaging_event_by_key(idempotency_key)
        if prior is not None:
            if _stored_content(prior) != content:
                return {"outcome": "conflict", "prior": prior}
            return {"outcome": "reused", "event": prior}
        event = store.create_packaging_event(
            new_id("pev"), run["run_id"], idempotency_key, kind, kind,
            quantity, good_units, operator, occurred_at, reason)
        return {"outcome": "created", "event": event}


def record_adjustment(store, run: dict, idempotency_key: str, category: str,
                      delta: int, good_units_delta: int, reason: str,
                      operator: str, occurred_at: str | None) -> dict:
    """盘点更正调整事件（幂等）：有符号增量追加，不覆盖既有记录。

    调整不得使任一类别合计或合格品数为负；结算后重新结算即按新合计判定。
    """
    content = _event_content(run["run_id"], "adjustment", category, delta,
                             good_units_delta if category == "applied" else None,
                             operator)
    with store.transaction():
        prior = store.packaging_event_by_key(idempotency_key)
        if prior is not None:
            if _stored_content(prior) != content:
                return {"outcome": "conflict", "prior": prior}
            return {"outcome": "reused", "event": prior}
        t = run_totals(store, run["run_id"])
        failures = []
        if t["totals"][category] + delta < 0:
            failures.append({
                "code": "negative_balance", "category": category,
                "current": t["totals"][category], "delta": delta,
                "message": f"调整后 {category} 合计为 "
                           f"{t['totals'][category] + delta}，不得为负"})
        if category == "applied" and t["good_units"] + good_units_delta < 0:
            failures.append({
                "code": "negative_good_units",
                "current": t["good_units"], "delta": good_units_delta,
                "message": f"调整后合格品数为 {t['good_units'] + good_units_delta}，"
                           f"不得为负"})
        if failures:
            return {"outcome": "failed", "failures": failures}
        event = store.create_packaging_event(
            new_id("pev"), run["run_id"], idempotency_key, "adjustment", category,
            delta, good_units_delta if category == "applied" else None,
            operator, occurred_at, reason)
        return {"outcome": "created", "event": event}


# ---------------------------------------------------------------------- 结算

def settle(store, run_id: str, settled_by: str) -> dict:
    """结算：两条平衡等式同时满足才落结算记录（append-only）并置 settled。

    不平衡时不写任何记录，返回带数量来源的对账差异；盘点更正（调整事件）
    后重新调用即按最新合计重新判定，历史结算记录保持不可覆盖。
    """
    with store.transaction():
        run = store.get_packaging_run(run_id)
        rec = reconciliation(store, run)
        if not rec["balanced"]:
            return {"outcome": "discrepancy", "reconciliation": rec}
        settlement = store.add_settlement(run_id, "balanced", rec, settled_by)
        if run["status"] != "settled":
            store.set_packaging_run_status(run_id, "settled")
        return {"outcome": "settled", "settlement": settlement,
                "reconciliation": rec}


# ---------------------------------------------------------------------- 撤回/更正波及清单

def disposition_for_label(store, label_id: str) -> dict:
    """标签撤回 / 规格更正时的包装执行波及清单。

    按生产批次列出：未结算现场余量（领出未上线 + 未结算运行的线边余量）、
    已包装数量（贴用量与合格品数）与待隔离批次。已平衡结算的运行现场余量
    为零，不计入未结算现场余量。
    """
    per_batch: dict[str, dict] = {}
    bound_runs: dict[str, str] = {}  # run_id -> production_batch_id
    for pb in store.print_batches_for_label(label_id):
        for iss in store.issuances_for_print_batch(pb["print_batch_id"]):
            bid = iss["production_batch_id"]
            entry = per_batch.setdefault(bid, {
                "production_batch_id": bid,
                "product_id": (store.get_batch(bid) or {}).get("product_id"),
                "issued_quantity": 0,
                "unbound_quantity": 0,
                "runs": []})
            entry["issued_quantity"] += iss["quantity"]
            run_id = store.issuance_bound_run(iss["issuance_id"])
            if run_id is None:
                entry["unbound_quantity"] += iss["quantity"]
            else:
                bound_runs.setdefault(run_id, bid)

    for run_id, bid in sorted(bound_runs.items()):
        run = store.get_packaging_run(run_id)
        t = run_totals(store, run_id)
        issued = sum(i["quantity"] for i in run["issuances"])
        accounted = sum(t["totals"][c] for c in PACKAGING_CATEGORIES)
        per_batch[bid]["runs"].append({
            "run_id": run_id,
            "status": run["status"],
            "issued_quantity": issued,
            "applied_quantity": t["totals"]["applied"],
            "good_units": t["good_units"],
            "wasted_quantity": t["totals"]["wasted"],
            "sampled_quantity": t["totals"]["sampled"],
            "returned_quantity": t["totals"]["returned"],
            "on_line_remaining": issued - accounted,
        })

    batches = []
    pending_isolation = []
    for bid in sorted(per_batch):
        entry = per_batch[bid]
        unsettled_on_line = entry["unbound_quantity"] + sum(
            r["on_line_remaining"] for r in entry["runs"] if r["status"] != "settled")
        packed_quantity = sum(r["applied_quantity"] for r in entry["runs"])
        packed_units = sum(r["good_units"] for r in entry["runs"])
        pending = unsettled_on_line > 0 or packed_quantity > 0
        batches.append({
            **entry,
            "unsettled_on_line_quantity": unsettled_on_line,
            "packed_quantity": packed_quantity,
            "packed_units": packed_units,
            "pending_isolation": pending,
        })
        if pending:
            pending_isolation.append(bid)
    return {"label_id": label_id,
            "batches": batches,
            "pending_isolation_batches": pending_isolation}
