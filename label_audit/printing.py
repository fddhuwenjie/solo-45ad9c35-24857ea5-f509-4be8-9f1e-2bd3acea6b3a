"""印刷标签批次领用放行：文案规范化摘要、放行逐项对照与影响冻结。

放行是“批准快照”与“待包装批次当前分析”之间的最后一道核对：仅核对产品名
不足以防止旧版卷标混入，因此领用端点逐项比对

  0. 待包装批次**所属产品**（跨产品领用时不是标签修订所属产品）的当前配方/
     规格推导出的声明；
  1. 印刷摘要（入库时由批准文案规范化）与批准快照文案；
  2. 待包装批次当前推导出的应声明/交叉接触项与批准快照；
  3. 当前分析重新计算后新增的开放 blocker（阳性拭子等硬证据失败）；
  4. 印刷批次适用产品是否包含待包装批次所属产品。

任一不符返回差异路径（差异路径形如 ``derived.required.missing[soy]``），
不写领用记录。标签撤回、规格变化、拭子阳性等影响传播时，由
:func:`freeze_for_label` 冻结该修订下仍有余量的印刷批次，并汇总已领用
批次进入处置。
"""
from __future__ import annotations

import hashlib
import json

from .db import new_id
from .engine import BLOCKER, compare_with_copy, derive_declarations, expand_recipe, norm
from .packaging import disposition_for_label
from .trace import trace_findings

# 重新分析时可能出现、但声明集合差异已经表达过的发现项；放行门禁不重复计列
_COPY_FINDING_KINDS = {
    "missing_declaration", "missing_cross_contact",
    "unnecessary_declaration", "claim_contradiction",
}


def canonical_copy_summary(copy: dict) -> dict:
    """把批准文案规范化为印刷摘要：过敏原统一规范化后排序去重，配料表压缩空白。

    现场只核对产品名会让失效文案贴到新批次；摘要把“印在卷标上的声明”固化
    为可逐项对照的结构。
    """
    def items(key: str) -> list[str]:
        return sorted({norm(x) for x in (copy or {}).get(key, [])})

    return {
        "declared_allergens": items("declared_allergens"),
        "may_contain": items("may_contain"),
        "free_from_claims": items("free_from_claims"),
        "ingredients_text": " ".join(str((copy or {}).get("ingredients_text") or "").split()),
    }


def current_basis(store, label: dict, production_batch: dict) -> dict:
    """对待包装批次当前资料重新推导（不改动标签的落库分析结果）。

    跨产品领用时必须按**待包装批次所属产品**的当前配方/规格展开，而不是标签
    修订所属产品——同一卷标适用多个产品时，各产品的应声明项可能不同。
    """
    product_id = production_batch["product_id"]
    exp = expand_recipe(store, product_id, batch_id=production_batch["batch_id"])
    derived = derive_declarations(store, product_id, exp,
                                  batch_id=production_batch["batch_id"])
    findings = list(exp.findings) + compare_with_copy(label["copy"], derived)
    findings += trace_findings(store, production_batch["batch_id"], label["copy"])
    return {"derived": derived, "findings": findings,
            "graph": [n.as_dict() for n in exp.nodes]}


def analysis_version(basis: dict, label: dict, production_batch: dict) -> str:
    """放行采用的分析版本：当前推导结构（含证据路径）的稳定哈希。"""
    payload = {
        "product_id": production_batch["product_id"],
        "batch_id": production_batch["batch_id"],
        "derived": basis["derived"],
        "copy_revision": label["revision"],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "ana-" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _summary_diff(printed: dict, approved: dict) -> list[dict]:
    """印刷摘要逐项对照批准快照文案；任一列表项不一致都计差异。"""
    diffs: list[dict] = []
    for key, label_name in (("declared_allergens", "已声明过敏原"),
                           ("may_contain", "交叉接触提示"),
                           ("free_from_claims", "“无…”宣称")):
        p = set(printed.get(key, []))
        a = set(approved.get(key, []))
        for a_name in sorted(p - a):
            diffs.append({
                "path": f"print_summary.{key}.extra[{a_name}]",
                "expected": sorted(a), "actual": sorted(p),
                "message": f"印刷卷标的{label_name}多出 “{a_name}”，不属于批准文案"})
        for a_name in sorted(a - p):
            diffs.append({
                "path": f"print_summary.{key}.missing[{a_name}]",
                "expected": sorted(a), "actual": sorted(p),
                "message": f"印刷卷标的{label_name}缺少 “{a_name}”"})
    pt = printed.get("ingredients_text") or ""
    at = approved.get("ingredients_text") or ""
    if pt != at:
        diffs.append({
            "path": "print_summary.ingredients_text",
            "expected": at, "actual": pt,
            "message": "印刷卷标的配料表文本与批准文案不一致"})
    return diffs


def _derived_diff(basis: dict, approved_derived: dict) -> list[dict]:
    """待包装批次当前分析出的声明逐项对照批准快照。"""
    cur, appr = basis["derived"], approved_derived or {}
    diffs: list[dict] = []
    for bucket, label_name in (("required", "应声明过敏原"),
                               ("may_contain", "交叉接触提示")):
        c = set((cur or {}).get(bucket, {}))
        a = set((appr or {}).get(bucket, {}))
        for allergen in sorted(c - a):
            diffs.append({
                "path": f"derived.{bucket}.extra[{allergen}]",
                "message": f"待包装批次当前分析新增{label_name} “{allergen}”，"
                           f"批准快照未包含",
                "expected": sorted(a), "actual": sorted(c)})
        for allergen in sorted(a - c):
            diffs.append({
                "path": f"derived.{bucket}.missing[{allergen}]",
                "message": f"批准快照包含{label_name} “{allergen}”，"
                           f"待包装批次当前分析已不包含",
                "expected": sorted(a), "actual": sorted(c)})
    return diffs


def _fresh_blocker_diffs(basis: dict, approved_snapshot: dict) -> list[dict]:
    """当前分析相对批准时刻新增的开放 blocker（阳性拭子、新资料缺口等）。

    批准时被审核人覆盖（overridden）的指纹放行时仍视为可接受；文案声明
    集合类发现项已由 :func:`_derived_diff` 表达，不重复计列。
    """
    overridden = {
        f.get("fingerprint")
        for f in (approved_snapshot.get("findings") or [])
        if f.get("status") == "overridden" and f.get("fingerprint")
    }
    diffs: list[dict] = []
    for f in basis["findings"]:
        if f.severity != BLOCKER or f.kind in _COPY_FINDING_KINDS:
            continue
        if f.fingerprint in overridden:
            continue
        diffs.append({
            "path": f"analysis.open_blockers[{f.kind}:{f.subject}]".rstrip(":"),
            "message": f.message,
            "kind": f.kind, "detail": f.detail,
            "fingerprint": f.fingerprint,
        })
    return diffs


def evaluate_release(store, print_batch: dict, production_batch: dict,
                     label: dict) -> dict:
    """执行放行门禁。返回 {"ok": bool, "differences": [...], "basis": {...},
    "analysis_version": ...}；ok 为 False 时差异路径逐项给出。"""
    approvals = store.approvals_for_label(label["id"])
    snapshot = approvals[-1]["snapshot"] if approvals else {}
    basis = current_basis(store, label, production_batch)
    diffs: list[dict] = []
    diffs += _summary_diff(print_batch["copy_summary"],
                           canonical_copy_summary(snapshot.get("copy") or {}))
    diffs += _derived_diff(basis, snapshot.get("derived") or {})
    diffs += _fresh_blocker_diffs(basis, snapshot)
    return {"ok": not diffs, "differences": diffs, "basis": basis,
            "analysis_version": analysis_version(
                basis, label, production_batch),
            "approved_snapshot": snapshot}


def issue(store, print_batch: dict, production_batch: dict, quantity: int,
          idempotency_key: str) -> dict:
    """门禁通过后写入领用记录（幂等键唯一）；已领用数量只增不减。"""
    label = store.get_label(print_batch["label_id"])
    result = evaluate_release(store, print_batch, production_batch, label)
    issuance = store.create_issuance(
        new_id("iss"), print_batch["print_batch_id"],
        production_batch["batch_id"], quantity, idempotency_key,
        label["id"], label["revision"], result["analysis_version"],
        {"derived": result["basis"]["derived"]})
    return {"issuance": issuance, "reused": False,
            "analysis_version": result["analysis_version"]}


def freeze_for_label(store, label_id: str, reason: str) -> dict:
    """影响传播：冻结标签修订下仍有余量的印刷批次，汇总已领用它们的生产批次。

    已全部领用（余量为 0）的批次无需冻结，但其领用记录仍出现在处置清单里——
    已经贴到产品上的旧文案必须进入处置评估。包装执行波及一并列出：未结算
    现场余量（领出未上线 + 未结算运行的线边余量）、已包装数量与待隔离批次。
    """
    frozen = store.freeze_available_print_batches_for_label(label_id, reason)
    affected_batches: dict[str, dict] = {}
    for pb in store.print_batches_for_label(label_id):
        for iss in store.issuances_for_print_batch(pb["print_batch_id"]):
            bid = iss["production_batch_id"]
            entry = affected_batches.setdefault(bid, {
                "production_batch_id": bid,
                "product_id": (store.get_batch(bid) or {}).get("product_id"),
                "issued_print_batches": [],
                "issued_quantity": 0})
            entry["issued_print_batches"].append({
                "print_batch_id": pb["print_batch_id"],
                "quantity": iss["quantity"],
                "issuance_id": iss["issuance_id"],
                "analysis_version": iss["analysis_version"]})
            entry["issued_quantity"] += iss["quantity"]
    packaging_disposition = disposition_for_label(store, label_id)
    packaging_by_batch = {b["production_batch_id"]: b
                          for b in packaging_disposition["batches"]}
    for bid, entry in affected_batches.items():
        entry["packaging"] = packaging_by_batch.get(bid)
    return {
        "label_id": label_id,
        "reason": reason,
        "frozen_print_batches": [
            {"print_batch_id": pb["print_batch_id"],
             "remaining_quantity": pb["remaining_quantity"],
             "issued_quantity": pb["issued_quantity"],
             "frozen_reason": pb["frozen_reason"]}
            for pb in frozen],
        "disposition_batches": sorted(affected_batches.values(),
                                      key=lambda b: b["production_batch_id"]),
        "pending_isolation_batches":
            packaging_disposition["pending_isolation_batches"],
    }
