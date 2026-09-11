"""逐批次共线追溯：从待审批次向前追溯最近含敏原批次与返工路径。

一条（过敏原 -> 来源批次 -> 设备段/返工）路径只有在以下条件**全部**满足时才关闭：
  1. 批次生产顺序明确（同产线可定位上一含敏原批次）；
  2. 目标批次使用到的每个相关设备段都有清洁执行记录，且引用的程序版本存在；
  3. 程序在清洁时处于有效期内（valid_from/valid_until），且程序覆盖该过敏原；
  4. 程序声明的每个必检采样点都已采样（漏采即开放）；
  5. 全部拭子结果为阴性且未超过程序定量限值（阳性/超限即开放，且为 blocker 级证据缺口）。

任何一项不满足，路径保持 open 并附 evidence_gaps（缺口代码）：
  order_unknown        批次顺序不明（无序号/同产线无法排序）
  missing_cleaning     缺少清洁步骤（设备段无清洁记录）
  program_unknown      清洁记录引用的程序版本不存在
  program_not_covering 程序不覆盖该过敏原
  validation_expired   清洁时程序不在有效期
  missing_swab         必检采样点漏采
  swab_pending         已采样但定量结果未出
  swab_exceeded        拭子定量结果超限（blocker）
  program_wrong_line   程序/清洁记录属于其他产线，不能用于本产线路径
  component_carried_over 返工源批次成品本身含该过敏原，目标批次清洁不能去除组分
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import BLOCKER, Finding, norm

# 阻断批准的证据缺口：仅凭残缺记录不能放行
BLOCKER_GAPS = {"swab_exceeded"}


@dataclass
class TracePath:
    allergen: str
    source_batch_id: str | None        # 最近含敏原批次；None 表示仅来自返工或顺序不明
    source_kind: str                   # previous_batch（同产线前序）/ rework / unknown
    route: str                         # 人类可读路径
    status: str = "open"               # open / closed
    evidence_gaps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allergen": self.allergen,
            "source_batch_id": self.source_batch_id,
            "source_kind": self.source_kind,
            "route": self.route,
            "status": self.status,
            "evidence_gaps": self.evidence_gaps,
            "evidence": self.evidence,
        }


def _date(s: str | None) -> str | None:
    """取 ISO 字符串的日期部分用于有效期比较（ISO8601 字典序可直接比较）。"""
    if not s:
        return None
    return s[:10]


def evaluate_segment(store, target_batch: dict, segment_id: str,
                     allergen: str) -> tuple[list[dict], list[dict]]:
    """评估单条设备段上的清洁/验证证据。

    返回 (gaps, evidence)；gaps 为空即该设备段对该过敏原验证通过。
    """
    gaps: list[dict] = []
    evidence: list[dict] = []
    records = store.cleaning_records_for_batch(target_batch["batch_id"], segment_id)
    if not records:
        gaps.append({"code": "missing_cleaning", "segment_id": segment_id,
                     "detail": "该设备段在本批次投产前无清洁记录"})
        return gaps, evidence
    # 可能登记了多段清洁，取最近一条（同批次同设备段按插入顺序最后一条）
    rec = records[-1]
    if rec["line_id"] != target_batch["line_id"]:
        gaps.append({"code": "program_wrong_line", "segment_id": segment_id,
                     "record_id": rec["record_id"],
                     "record_line_id": rec["line_id"],
                     "batch_line_id": target_batch["line_id"],
                     "detail": f"清洁记录 {rec['record_id']} 属于产线 {rec['line_id']}，"
                               f"不能用于产线 {target_batch['line_id']} 的批次"})
        return gaps, evidence
    evidence.append({"type": "cleaning_record", "ref": f"cleaning:{rec['record_id']}",
                     "record_id": rec["record_id"], "segment_id": segment_id,
                     "program": f"{rec['program_id']}@{rec['program_version']}"})
    program = store.get_cleaning_program(rec["program_id"], rec["program_version"])
    if program is None:
        gaps.append({"code": "program_unknown", "segment_id": segment_id,
                     "program": f"{rec['program_id']}@{rec['program_version']}",
                     "detail": "清洁记录引用的程序版本不存在"})
        return gaps, evidence
    # 程序必须适用于本产线：line_id 为空表示全产线通用；指定产线的程序不能跨线使用
    if program["line_id"] is not None and program["line_id"] != target_batch["line_id"]:
        gaps.append({"code": "program_wrong_line", "segment_id": segment_id,
                     "program": f"{program['program_id']}@{program['version']}",
                     "program_line_id": program["line_id"],
                     "batch_line_id": target_batch["line_id"],
                     "detail": f"程序属于产线 {program['line_id']}，"
                               f"不能用于产线 {target_batch['line_id']} 的批次"})
        return gaps, evidence
    if norm(allergen) not in {norm(a) for a in program["allergens"]}:
        gaps.append({"code": "program_not_covering", "segment_id": segment_id,
                     "program": f"{program['program_id']}@{program['version']}",
                     "detail": f"程序不覆盖过敏原 {allergen}"})
        return gaps, evidence
    cleaned = _date(rec.get("cleaned_at")) or _date(target_batch.get("started_at"))
    vf, vu = _date(program.get("valid_from")), _date(program.get("valid_until"))
    if (vf and cleaned and cleaned < vf) or (vu and cleaned and cleaned > vu):
        gaps.append({"code": "validation_expired", "segment_id": segment_id,
                     "program": f"{program['program_id']}@{program['version']}",
                     "cleaned_at": cleaned, "valid_from": vf, "valid_until": vu,
                     "detail": "清洁时程序不在有效期内"})
        # 过期程序的拭子结果同样不能采信，直接判开放
        return gaps, evidence
    if (vf or vu) and not cleaned:
        gaps.append({"code": "validation_expired", "segment_id": segment_id,
                     "program": f"{program['program_id']}@{program['version']}",
                     "cleaned_at": None, "valid_from": vf, "valid_until": vu,
                     "detail": "清洁日期缺失，无法确认程序在有效期内"})
        return gaps, evidence
    swabs = store.swabs_for_record(rec["record_id"])
    # 同一采样点可能检测多种过敏原；关闭某过敏原路径时只采信对应过敏原的拭子
    by_point: dict[str, dict] = {}
    for s in swabs:
        if norm(s["allergen"]) == norm(allergen):
            by_point[s["point_id"]] = s
    for point in program["required_points"]:
        swab = by_point.get(point)
        if swab is None:
            gaps.append({"code": "missing_swab", "segment_id": segment_id,
                         "point_id": point,
                         "detail": f"必检采样点 {point} 漏采 {allergen} 拭子"})
            continue
        evidence.append({"type": "swab", "ref": f"swab:{swab['swab_id']}",
                         "swab_id": swab["swab_id"], "record_id": rec["record_id"],
                         "point_id": point, "allergen": norm(swab["allergen"]),
                         "value_ppm": swab["value_ppm"], "limit_ppm": program["limit_ppm"]})
        if swab["value_ppm"] is None:
            gaps.append({"code": "swab_pending", "segment_id": segment_id,
                         "point_id": point, "swab_id": swab["swab_id"],
                         "detail": "已采样但定量结果未出，阴性单据不足以单独关路径"})
        elif swab["value_ppm"] > program["limit_ppm"]:
            gaps.append({"code": "swab_exceeded", "segment_id": segment_id,
                         "point_id": point, "swab_id": swab["swab_id"],
                         "value_ppm": swab["value_ppm"], "limit_ppm": program["limit_ppm"],
                         "detail": f"拭子结果 {swab['value_ppm']} ppm 超过限值 "
                                   f"{program['limit_ppm']} ppm"})
    return gaps, evidence


def _build_path(store, target_batch: dict, allergen: str, source_batch_id: str | None,
                source_kind: str, route: str,
                order_gap: dict | None = None) -> TracePath:
    """逐设备段汇总证据，任一设备段开放则整条路径开放。"""
    path = TracePath(allergen=norm(allergen), source_batch_id=source_batch_id,
                     source_kind=source_kind, route=route)
    if order_gap:
        path.evidence_gaps.append(order_gap)
    else:
        for seg in target_batch["segments"]:
            gaps, ev = evaluate_segment(
                store, target_batch, seg["segment_id"], allergen)
            path.evidence_gaps.extend(gaps)
            path.evidence.extend(ev)
        if not target_batch["segments"]:
            # 未登记设备段：无法逐段验证，视为清洁步骤缺口
            path.evidence_gaps.append({"code": "missing_cleaning", "segment_id": None,
                                       "detail": "本批次未登记任何设备段，无法验证清洁"})
    path.status = "closed" if not path.evidence_gaps else "open"
    return path


def _line_paths(store, batch: dict) -> list[TracePath]:
    """批次自身在同产线上的前序残留路径（在本批次设备段上验证清洁）。"""
    paths: list[TracePath] = []
    previous = store.previous_batches(batch)
    if previous:
        found: dict[str, str] = {}
        for cand in previous:  # previous 已按序号倒序，setdefault 保留最近批次
            for a in cand["allergens"]:
                found.setdefault(norm(a), cand["batch_id"])
        for allergen, src in sorted(found.items()):
            paths.append(_build_path(
                store, batch, allergen, src, "previous_batch",
                f"批次 {batch['batch_id']} <产线 {batch['line_id']}> 前序批次 {src} 含 {allergen}"))
    else:
        # 无任何前序批次：无法确认顺序/历史，保守标记顺序缺口（不凭空引入过敏原）
        line = store.get_line(batch["line_id"])
        for a in (line["allergens_handled"] if line else []):
            paths.append(_build_path(
                store, batch, a, None, "unknown",
                f"批次 {batch['batch_id']} 在产线 {batch['line_id']} 上无可排序的前序批次记录",
                order_gap={"code": "order_unknown",
                           "detail": "批次生产顺序不明，无法定位最近含敏原批次；"
                                     "产线历史登记过敏原需保守保留"}))
    return paths


def _rework_paths(store, batch: dict, chain_seen: frozenset) -> list[TracePath]:
    """返工路径：

    a) 源批次成品含有的过敏原 -> 组分携带，目标批次设备清洁无法去除（恒开放）；
    b) 源批次自身未关闭的同线/返工残留路径 -> 随返工料传递（带缺口直接传递，
       不再用目标批次设备段重新验证，避免“目标清洁良好”掩盖上游污染）。
    """
    paths: list[TracePath] = []
    for rw in store.rework_into(batch["batch_id"]):
        src = store.get_batch(rw["source_batch_id"])
        if src is None or rw["source_batch_id"] in chain_seen:
            continue
        pct = f"（返工比例 {rw['percentage']}%）" if rw.get("percentage") is not None else ""
        component = {norm(a) for a in src["allergens"]}
        # a) 组分携带
        for a in sorted(component):
            paths.append(TracePath(
                allergen=a, source_batch_id=src["batch_id"], source_kind="rework",
                route=f"返工料自批次 {src['batch_id']}（产品 {src['product_id']}）"
                      f"投入 {batch['batch_id']}{pct}：源批次成品本身含 {a}，"
                      f"设备清洁不能去除已混入组分",
                status="open",
                evidence_gaps=[{"code": "component_carried_over",
                                "detail": "返工源批次成品含有该过敏原，目标批次设备清洁"
                                          "只处理残留、不能去除已混入的组分"}],
            ))
        # b) 源批次自身未关闭的残留路径（同线 + 源批次的返工链）随返工料传递
        upstream = _line_paths(store, src)
        upstream += _rework_paths(store, src, chain_seen | {src["batch_id"]})
        for sub in upstream:
            if sub.status == "closed":
                continue
            if sub.allergen in component:
                continue  # 组分路径已以 component_carried_over 表达
            if any(p.allergen == sub.allergen and p.source_batch_id == sub.source_batch_id
                   for p in paths):
                continue
            paths.append(TracePath(
                allergen=sub.allergen, source_batch_id=sub.source_batch_id,
                source_kind="rework",
                route=f"返工链 {sub.source_batch_id} -> {src['batch_id']} -> "
                      f"{batch['batch_id']}：{sub.allergen} 残留路径未关闭（{sub.route}）",
                status="open",
                evidence_gaps=list(sub.evidence_gaps),
                evidence=list(sub.evidence),
            ))
    return paths


def _collect_paths(store, batch_id: str) -> list[TracePath]:
    """收集待追溯路径：同产线前序残留 + 返工组分/返工链传递（可跨产品、可传递）。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        return []
    return _line_paths(store, batch) + _rework_paths(store, batch, frozenset({batch_id}))


def trace_batch(store, batch_id: str) -> dict:
    """追溯一个批次：返回每条过敏原路径的开放/关闭状态与证据链。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise KeyError(f"batch {batch_id} not found")
    raw = _collect_paths(store, batch_id)
    # 同过敏原合并：任一来源路径开放则开放（并集证据）
    merged: dict[str, TracePath] = {}
    for p in raw:
        if p.allergen not in merged:
            merged[p.allergen] = p
        else:
            m = merged[p.allergen]
            m.evidence_gaps.extend(p.evidence_gaps)
            m.evidence.extend(p.evidence)
            if p.status == "open":
                m.status = "open"
    paths = [merged[a] for a in sorted(merged)]
    return {
        "batch_id": batch_id,
        "product_id": batch["product_id"],
        "line_id": batch["line_id"],
        "sequence": batch["sequence"],
        "paths": [p.as_dict() for p in paths],
        "open_allergens": sorted(p.allergen for p in paths if p.status == "open"),
        "closed_allergens": sorted(p.allergen for p in paths if p.status == "closed"),
    }


def open_cross_contact(store, batch_id: str) -> dict:
    """供声明推导使用：开放路径过敏原 -> 证据（含缺口）。"""
    trace = trace_batch(store, batch_id)
    may: dict[str, list] = {}
    for p in trace["paths"]:
        if p["status"] != "open":
            continue
        may.setdefault(p["allergen"], []).append({
            "allergen": p["allergen"],
            "status": "cross_contact",
            "ingredient_id": None,
            "version": None,
            "path": p["route"],
            "source": f"batch:{batch_id}",
            "declaration_ref": None,
            "source_batch_id": p["source_batch_id"],
            "source_kind": p["source_kind"],
            "evidence_gaps": p["evidence_gaps"],
        })
    return may


def batch_trace_evidence(store, batch_id: str) -> dict:
    """收集批次追溯实际采用的清洁程序与检测记录，供批准快照冻结。

    冻结对象：批次登记的每个设备段上、批次投产前的清洁记录（最新一条）、
    其引用的程序版本（按 program_id@version 去重）及该记录下的全部拭子结果。
    批准后这些值随 approvals 快照永久保留，不受后续补录影响。
    """
    batch = store.get_batch(batch_id)
    if batch is None:
        raise KeyError(f"batch {batch_id} not found")
    records, programs, swabs = [], {}, []
    for seg in batch["segments"]:
        recs = store.cleaning_records_for_batch(batch_id, seg["segment_id"])
        if not recs:
            continue
        rec = recs[-1]
        records.append(rec)
        prog = store.get_cleaning_program(rec["program_id"], rec["program_version"])
        if prog is not None:
            programs[f"{prog['program_id']}@{prog['version']}"] = prog
        swabs.extend(store.swabs_for_record(rec["record_id"]))
    return {
        "batch": batch,
        "cleaning_records": records,
        "cleaning_programs": sorted(programs.values(), key=lambda p: (p["program_id"], p["version"])),
        "swab_results": swabs,
        "trace": trace_batch(store, batch_id),
    }


def trace_findings(store, batch_id: str, copy: dict) -> list[Finding]:
    """追溯专属发现项：

    拭子阳性/超限属于硬证据失败：即使标签已提示“可能含”，该批次也不应放行
    -> data_gap(blocker)。未提示/宣称矛盾由 compare_with_copy 依据开放路径
    （derived.may_contain）统一生成 missing_cross_contact / claim_contradiction，
    避免重复指纹。
    """
    trace = trace_batch(store, batch_id)
    findings: list[Finding] = []
    for p in trace["paths"]:
        if p["status"] != "open":
            continue
        blocker_gaps = [g for g in p["evidence_gaps"] if g["code"] in BLOCKER_GAPS]
        if blocker_gaps:
            findings.append(Finding(
                "data_gap", BLOCKER,
                f"批次 {batch_id} 过敏原 {p['allergen']} 的清洁验证存在阳性/超限结果，"
                f"交叉接触路径不可关闭",
                {"allergen": p["allergen"], "missing": "cleaning_validation",
                 "batch_id": batch_id, "route": p["route"],
                 "source_batch_id": p["source_batch_id"],
                 "evidence_gaps": p["evidence_gaps"], "evidence": p["evidence"]},
                subject=f"swab:{batch_id}:{p['allergen']}",
            ))
    return findings
