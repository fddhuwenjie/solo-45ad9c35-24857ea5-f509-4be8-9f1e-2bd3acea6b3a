"""规则引擎：来源图展开、应声明项推导、问题检测、影响标记与版本比较。

检测规则一览（severity: blocker 会阻止批准，warning 仅提示）：
  data_gap               资料缺口：未知原料引用 / 缺规格版本 / 缺供应商声明 / 状态未知   blocker
  alias_conflict         别名混用：引用命中多个原料(blocker)；同一名称/别名被多个原料使用(warning)
  circular_reference     循环引用：复合原料子成分沿路径回到祖先                            blocker
  unexpandable_compound  无法展开的复合原料：标记为复合但无子成分拆分                       blocker
  missing_declaration    应声明而未声明的过敏原                                            blocker
  missing_cross_contact  应提示而未提示的交叉接触风险                                       warning
  unnecessary_declaration 标签声明了推导不出的过敏原                                        warning
  claim_contradiction    “无某过敏原”宣称与原料资料或共线资料矛盾                            blocker
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

BLOCKER = "blocker"
WARNING = "warning"
MAX_DEPTH = 25


def norm(term) -> str:
    """名称/别名/过敏原的规范化：去空白、小写，用于跨资料比对。"""
    return " ".join(str(term).strip().lower().split())


def fmt_path(path: list[dict]) -> str:
    return " > ".join(f"{p['ingredient_id']}@{p.get('version') or '?'}" for p in path)


@dataclass
class Finding:
    kind: str
    severity: str
    message: str
    detail: dict
    subject: str = ""

    @property
    def fingerprint(self) -> str:
        """稳定指纹：同一标签重复分析时用于保留审核人的覆盖记录。"""
        return hashlib.sha1(f"{self.kind}|{self.subject}".encode("utf-8")).hexdigest()[:16]


@dataclass
class GraphNode:
    ingredient_id: str
    version: str | None
    name: str
    terms: set
    path: list[dict]
    percentage: float | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        out = {
            "ingredient_id": self.ingredient_id,
            "version": self.version,
            "name": self.name,
            "path": fmt_path(self.path),
            "percentage": self.percentage,
        }
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Expansion:
    nodes: list[GraphNode] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)  # allergen -> [evidence]
    referenced: set = field(default_factory=set)  # 展开中引用到的 ingredient_id

    def add_evidence(self, allergen: str, ev: dict) -> None:
        self.evidence.setdefault(norm(allergen), []).append(ev)


# ---------------------------------------------------------------------- 展开

def resolve_ref(store, ref: str):
    """把 ingredient_ref 解析为原料。返回 (ingredient|None, Finding|None, note|None)。"""
    ing = store.get_ingredient(ref)
    if ing:
        return ing, None, None
    matches = store.find_ingredients_by_term(ref)
    if len(matches) == 1:
        return matches[0], None, f"通过名称/别名 “{ref}” 解析为 {matches[0]['id']}"
    if len(matches) > 1:
        return None, Finding(
            "alias_conflict", BLOCKER,
            f"引用 “{ref}” 命中多个原料：{[m['id'] for m in matches]}，请改用原料 ID",
            {"ref": ref, "candidates": [m["id"] for m in matches]},
            subject=f"ref:{norm(ref)}",
        ), None
    return None, Finding(
        "data_gap", BLOCKER,
        f"未知原料引用 “{ref}”：既非原料 ID，也不匹配任何名称/别名",
        {"ref": ref, "missing": "ingredient"},
        subject=f"ref:{norm(ref)}",
    ), None


def expand_recipe(store, product_id: str) -> Expansion:
    """从产品当前配方出发，递归展开复合原料，构建来源图并收集证据与发现项。"""
    exp = Expansion()
    recipe = store.current_recipe(product_id)
    if recipe is None:
        exp.findings.append(Finding(
            "data_gap", BLOCKER, f"产品 {product_id} 缺少配方，无法展开来源图",
            {"product_id": product_id, "missing": "recipe"}, subject=f"recipe:{product_id}",
        ))
        return exp
    for item in recipe["items"]:
        _expand_ref(store, item["ingredient_ref"], item.get("version"), [], exp,
                    percentage=item.get("percentage"))
    _detect_alias_mixing(exp)
    return exp


def _expand_ref(store, ref, version, path, exp: Expansion, percentage=None) -> None:
    if len(path) >= MAX_DEPTH:
        exp.findings.append(Finding(
            "data_gap", BLOCKER, f"展开深度超过 {MAX_DEPTH} 层，疑似数据异常",
            {"path": fmt_path(path), "ref": ref}, subject=f"depth:{ref}",
        ))
        return
    ing, err, note = resolve_ref(store, ref)
    if err:
        err.detail.setdefault("path", fmt_path(path) or "(配方顶层)")
        exp.findings.append(err)
        return
    iid = ing["id"]
    if any(p["ingredient_id"] == iid for p in path):
        cycle = [p["ingredient_id"] for p in path] + [iid]
        exp.findings.append(Finding(
            "circular_reference", BLOCKER,
            f"循环引用：{' > '.join(cycle)}",
            {"cycle": cycle, "path": fmt_path(path)},
            subject="cycle:" + ">".join(sorted(set(cycle))),
        ))
        return
    ver = store.get_version(iid, version) if version else store.current_version(iid)
    node_path = path + [{
        "ingredient_id": iid,
        "version": ver["version"] if ver else version,
        "name": ing["name"],
    }]
    exp.referenced.add(iid)
    terms = {norm(ing["name"])} | {norm(a) for a in ing["aliases"]}
    exp.nodes.append(GraphNode(
        ingredient_id=iid,
        version=ver["version"] if ver else version,
        name=ing["name"], terms=terms, path=node_path,
        percentage=percentage, note=note,
    ))
    if ver is None:
        exp.findings.append(Finding(
            "data_gap", BLOCKER,
            f"原料 {iid} 缺少规格版本 {version or '(未指定，且无已登记版本)'}",
            {"ingredient_id": iid, "version": version, "missing": "spec_version",
             "path": fmt_path(node_path)},
            subject=f"spec:{iid}:{version or ''}",
        ))
        return
    decls = ver["supplier_declarations"]
    if not decls:
        exp.findings.append(Finding(
            "data_gap", BLOCKER,
            f"原料 {iid}@{ver['version']} 缺少供应商过敏原声明",
            {"ingredient_id": iid, "version": ver["version"],
             "missing": "supplier_declaration", "path": fmt_path(node_path)},
            subject=f"decl:{iid}:{ver['version']}",
        ))
    for d in decls:
        allergen = norm(d["allergen"])
        if d["status"] == "unknown":
            exp.findings.append(Finding(
                "data_gap", BLOCKER,
                f"原料 {iid}@{ver['version']} 的过敏原 {allergen} 状态未知",
                {"ingredient_id": iid, "version": ver["version"], "allergen": allergen,
                 "missing": "allergen_status", "path": fmt_path(node_path)},
                subject=f"decl:{iid}:{ver['version']}:{allergen}",
            ))
        elif d["status"] in ("present", "may_contain"):
            exp.add_evidence(allergen, {
                "allergen": allergen,
                "status": d["status"],
                "ingredient_id": iid,
                "version": ver["version"],
                "path": fmt_path(node_path),
                "source": f"spec:{iid}:{ver['version']}",
                "declaration_ref": d.get("source"),
            })
    if ing["is_compound"]:
        subs = ver["sub_components"]
        if not subs:
            exp.findings.append(Finding(
                "unexpandable_compound", BLOCKER,
                f"复合原料 {iid}@{ver['version']} 无子成分拆分，无法展开",
                {"ingredient_id": iid, "version": ver["version"], "path": fmt_path(node_path)},
                subject=f"compound:{iid}:{ver['version']}",
            ))
        for s in subs:
            _expand_ref(store, s["ingredient_ref"], s.get("version"), node_path, exp,
                        percentage=s.get("percentage"))


def _detect_alias_mixing(exp: Expansion) -> None:
    """同一产品来源图内，同一名称/别名被多个原料使用 -> 别名混用。"""
    term_to_ids: dict[str, set] = {}
    for node in exp.nodes:
        for term in node.terms:
            term_to_ids.setdefault(term, set()).add(node.ingredient_id)
    for term, ids in sorted(term_to_ids.items()):
        if len(ids) > 1:
            exp.findings.append(Finding(
                "alias_conflict", WARNING,
                f"名称/别名 “{term}” 同时被原料 {sorted(ids)} 使用，存在别名混用风险",
                {"term": term, "ingredient_ids": sorted(ids)},
                subject=f"term:{term}",
            ))


# ---------------------------------------------------------------------- 推导

def derive_declarations(store, product_id: str, exp: Expansion,
                        batch_id: str | None = None) -> dict:
    """由证据推导应声明项：present -> 必须声明；may_contain 与共线 -> 交叉接触提示。

    共线风险不再按产线 allergens_handled 静态推导：
    当产品有生产批次时，逐批次追溯（前序含敏原批次 + 返工路径，
    仅保留清洁/拭子证据无法关闭的开放路径）。
    未登记任何批次时回退到产线历史登记（保守兼容旧资料）。
    """
    required: dict[str, list] = {}
    may: dict[str, list] = {}
    for allergen, evs in exp.evidence.items():
        present = [e for e in evs if e["status"] == "present"]
        maybe = [e for e in evs if e["status"] == "may_contain"]
        if present:
            required[allergen] = present + maybe
        elif maybe:
            may[allergen] = maybe

    batch = store.get_batch(batch_id) if batch_id else store.latest_batch_for_product(product_id)
    if batch is not None:
        from .trace import open_cross_contact  # 延迟导入避免循环依赖

        for a, evs in open_cross_contact(store, batch["batch_id"]).items():
            if a in required:
                continue
            may.setdefault(a, []).extend(evs)
    else:
        for line in store.lines_for_product(product_id):
            for a in line["allergens_handled"]:
                a = norm(a)
                if a in required:
                    continue
                may.setdefault(a, []).append({
                    "allergen": a,
                    "status": "cross_contact",
                    "ingredient_id": None,
                    "version": None,
                    "path": f"产线 {line['name']}（共线，未登记批次，按历史登记保守推导）",
                    "source": f"line:{line['id']}",
                    "declaration_ref": None,
                })
    return {"required": required, "may_contain": may}


def _evidence_summary(evs: list[dict]) -> list[dict]:
    out = []
    for e in evs:
        item = {"path": e["path"], "status": e["status"], "source": e["source"],
                **({"declaration_ref": e["declaration_ref"]} if e.get("declaration_ref") else {})}
        if e.get("source_batch_id"):
            item["source_batch_id"] = e["source_batch_id"]
            item["source_kind"] = e.get("source_kind")
        if e.get("evidence_gaps"):
            item["evidence_gaps"] = e["evidence_gaps"]
        out.append(item)
    return out


def compare_with_copy(copy: dict, derived: dict) -> list[Finding]:
    """标签文案 vs 推导结果，产出声明类发现项。"""
    findings: list[Finding] = []
    declared = {norm(a) for a in copy.get("declared_allergens", [])}
    labeled_may = {norm(a) for a in copy.get("may_contain", [])}
    free = {norm(a) for a in copy.get("free_from_claims", [])}
    req = derived["required"]
    may = derived["may_contain"]

    for a in sorted(set(req) - declared):
        findings.append(Finding(
            "missing_declaration", BLOCKER,
            f"应声明过敏原 “{a}” 未在标签声明",
            {"allergen": a, "evidence": _evidence_summary(req[a])},
            subject=f"allergen:{a}",
        ))
    for a in sorted(set(may) - labeled_may - free):
        findings.append(Finding(
            "missing_cross_contact", WARNING,
            f"交叉接触风险 “{a}” 未在标签提示",
            {"allergen": a, "evidence": _evidence_summary(may[a])},
            subject=f"allergen:{a}",
        ))
    for a in sorted(declared - set(req)):
        findings.append(Finding(
            "unnecessary_declaration", WARNING,
            f"标签声明了 “{a}”，但现有资料推导不出该过敏原",
            {"allergen": a},
            subject=f"allergen:{a}",
        ))
    for a in sorted(free & set(req)):
        findings.append(Finding(
            "claim_contradiction", BLOCKER,
            f"“无{a}”宣称与资料矛盾：来源图中检出 “{a}”",
            {"allergen": a, "conflict": "present", "evidence": _evidence_summary(req[a])},
            subject=f"allergen:{a}",
        ))
    for a in sorted((free & set(may)) - set(req)):
        findings.append(Finding(
            "claim_contradiction", BLOCKER,
            f"“无{a}”宣称与资料矛盾：存在 “{a}” 交叉接触风险",
            {"allergen": a, "conflict": "cross_contact", "evidence": _evidence_summary(may[a])},
            subject=f"allergen:{a}",
        ))
    return findings


# ---------------------------------------------------------------------- 分析编排

def analyze_label(store, label_id: str, batch_id: str | None = None) -> dict:
    """对一个标签修订执行完整分析：展开 -> 批次追溯 -> 推导 -> 比对 -> 落库。

    batch_id 缺省时使用标签已绑定的批次，再缺省取产品最新批次。
    """
    from .trace import trace_batch, trace_findings

    label = store.get_label(label_id)
    if label is None:
        raise KeyError(f"label {label_id} not found")
    batch_id = batch_id or store.get_label_batch(label_id)
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch_for_product(label["product_id"])
    if batch is not None:
        batch_id = batch["batch_id"]
        store.set_label_batch(label_id, batch_id)
    else:
        batch_id = None
    exp = expand_recipe(store, label["product_id"])
    derived = derive_declarations(store, label["product_id"], exp, batch_id=batch_id)
    findings = exp.findings + compare_with_copy(label["copy"], derived)
    if batch_id:
        findings += trace_findings(store, batch_id, label["copy"])
    store.sync_findings(label_id, findings)
    store.set_label_derived(label_id, derived)
    store.set_stale(label_id, False)
    trace = trace_batch(store, batch_id) if batch_id else None
    return {
        "label_id": label_id,
        "batch_id": batch_id,
        "batch_trace": trace,
        "graph": [n.as_dict() for n in exp.nodes],
        "derived": derived,
        "findings": store.findings_for_label(label_id, include_resolved=False),
    }


def open_blockers(store, label_id: str) -> list[dict]:
    return [
        f for f in store.findings_for_label(label_id, include_resolved=False)
        if f["severity"] == BLOCKER and f["status"] == "open"
    ]


# ---------------------------------------------------------------------- 影响标记

def impacted_products(store, ingredient_ids) -> set[str]:
    """沿依赖图反查：哪些产品的来源图引用了这些原料。"""
    targets = set(ingredient_ids)
    hit = set()
    for p in store.all_products():
        exp = expand_recipe(store, p["id"])
        if exp.referenced & targets:
            hit.add(p["id"])
    return hit


def apply_impact(store, product_ids, reason: str) -> list[dict]:
    """标记受影响标签：草稿/复核中置 stale；已批准的派生新修订（批准记录保持只读）。

    已批准标签下仍有余量的印刷批次同步冻结，并汇总已领用它们的生产批次进入处置。
    """
    from .printing import freeze_for_label  # 延迟导入避免循环依赖

    actions = []
    for pid in sorted(product_ids):
        for label in store.labels_for_product(pid):
            if label["status"] in ("draft", "in_review"):
                store.set_stale(label["id"], True)
                actions.append({"label_id": label["id"], "product_id": pid,
                                "revision": label["revision"], "action": "marked_stale"})
            elif label["status"] == "approved":
                store.set_stale(label["id"], True)
                print_freeze = freeze_for_label(store, label["id"], reason=reason)
                new_label = store.create_label(pid, label["copy"], parent_id=label["id"])
                analyze_label(store, new_label["id"])
                actions.append({"label_id": label["id"], "product_id": pid,
                                "revision": label["revision"], "action": "marked_stale",
                                "print_freeze": print_freeze})
                actions.append({"label_id": new_label["id"], "product_id": pid,
                                "revision": new_label["revision"],
                                "action": "derived_new_revision"})
    store.log_event("impact_applied", {"reason": reason, "actions": actions})
    return actions


def impacted_batches_via_rework(store, batch_ids) -> set[str]:
    """返工影响链：从给定批次出发，沿返工去向（可跨产品、可传递）扩展。"""
    hit = set(batch_ids)
    frontier = list(batch_ids)
    while frontier:
        b = frontier.pop()
        for rw in store.rework_from(b):
            t = rw["target_batch_id"]
            if t not in hit and store.get_batch(t) is not None:
                hit.add(t)
                frontier.append(t)
    return hit


def apply_batch_impact(store, batch_ids, reason: str, positive: bool = True) -> list[dict]:
    """补录后的沿链影响：

    - 草稿/复核中标签：重新分析（同一绑定批次），开放路径立即重开/消解；
    - 已批准标签（仅阳性补录）：冻结的批准记录不动，标记 stale 并派生新修订
      （草稿、重新分析）；该修订下仍有余量的印刷批次同步冻结；阴性补录不影响
      已批准标签与印刷批次；
    - 已撤回标签：仅记录，不派生。
    """
    from .printing import freeze_for_label  # 延迟导入避免循环依赖

    actions = []
    for bid in sorted(batch_ids):
        for label_id in store.labels_for_batch(bid):
            label = store.get_label(label_id)
            if label is None:
                continue
            if label["status"] in ("draft", "in_review"):
                analyze_label(store, label_id, batch_id=bid)
                actions.append({"batch_id": bid, "label_id": label_id,
                                "product_id": label["product_id"],
                                "revision": label["revision"],
                                "action": "reanalyzed"})
            elif label["status"] == "approved" and positive:
                store.set_stale(label_id, True)
                print_freeze = freeze_for_label(store, label_id, reason=reason)
                new_label = store.create_label(label["product_id"], label["copy"],
                                               parent_id=label["id"])
                store.set_label_batch(new_label["id"], bid)
                analyze_label(store, new_label["id"], batch_id=bid)
                actions.append({"batch_id": bid, "label_id": label_id,
                                "product_id": label["product_id"],
                                "revision": label["revision"], "action": "marked_stale",
                                "print_freeze": print_freeze})
                actions.append({"batch_id": bid, "label_id": new_label["id"],
                                "product_id": label["product_id"],
                                "revision": new_label["revision"],
                                "action": "derived_new_revision"})
    store.log_event("batch_impact_applied", {"reason": reason, "positive": positive,
                                             "actions": actions})
    return actions


# ---------------------------------------------------------------------- 版本比较

def _collect_evidence_ids(derived: dict, allergens: set) -> tuple[set, set]:
    """从推导快照中收集与指定过敏原相关的原料 ID 与产线 ID。"""
    ings, lines = set(), set()
    for bucket in ("required", "may_contain"):
        for allergen, evs in (derived or {}).get(bucket, {}).items():
            if allergen not in allergens:
                continue
            for e in evs:
                if e.get("ingredient_id"):
                    ings.add(e["ingredient_id"])
                src = e.get("source") or ""
                if src.startswith("line:"):
                    lines.add(src.split(":", 1)[1])
    return ings, lines


def impact_scope(store, exclude_product_id: str, ingredient_ids) -> list[dict]:
    """波及范围：其他产品中来源图与给定原料集合相交者，附其标签状态。"""
    out = []
    targets = set(ingredient_ids)
    for p in store.all_products():
        if p["id"] == exclude_product_id:
            continue
        exp = expand_recipe(store, p["id"])
        shared = sorted(exp.referenced & targets)
        if shared:
            out.append({
                "product_id": p["id"],
                "product_name": p["name"],
                "shared_ingredients": shared,
                "labels": [
                    {"label_id": l["id"], "revision": l["revision"],
                     "status": l["status"], "stale": l["stale"]}
                    for l in store.labels_for_product(p["id"])
                ],
            })
    return out


def compare_revisions(store, product_id: str, from_revision: int, to_revision: int) -> dict:
    """比较同一产品两个标签修订的推导声明：增删、发现项变化与波及范围。"""
    a = store.get_label_by_revision(product_id, from_revision)
    b = store.get_label_by_revision(product_id, to_revision)
    if a is None or b is None:
        raise KeyError(f"revision {from_revision} 或 {to_revision} 不存在")
    da, db = a["derived"] or {}, b["derived"] or {}
    req_a, req_b = set(da.get("required", {})), set(db.get("required", {}))
    may_a, may_b = set(da.get("may_contain", {})), set(db.get("may_contain", {}))

    changed = (req_a ^ req_b) | (may_a ^ may_b)
    ings_a, lines_a = _collect_evidence_ids(da, changed)
    ings_b, lines_b = _collect_evidence_ids(db, changed)
    changed_ingredients = ings_a | ings_b

    fa = {f["fingerprint"]: f for f in store.findings_for_label(a["id"], include_resolved=False)}
    fb = {f["fingerprint"]: f for f in store.findings_for_label(b["id"], include_resolved=False)}

    return {
        "product_id": product_id,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "required": {"added": sorted(req_b - req_a), "removed": sorted(req_a - req_b)},
        "may_contain": {"added": sorted(may_b - may_a), "removed": sorted(may_a - may_b)},
        "copy_diff": _copy_diff(a["copy"], b["copy"]),
        "findings": {
            "added": sorted((fb[k]["kind"], fb[k]["message"]) for k in fb.keys() - fa.keys()),
            "resolved": sorted((fa[k]["kind"], fa[k]["message"]) for k in fa.keys() - fb.keys()),
        },
        "changed_ingredients": sorted(changed_ingredients),
        "changed_lines": sorted(lines_a | lines_b),
        "impact_scope": impact_scope(store, product_id, changed_ingredients),
    }


def _copy_diff(copy_a: dict, copy_b: dict) -> dict:
    out = {}
    for key in ("declared_allergens", "may_contain", "free_from_claims"):
        sa = {norm(x) for x in (copy_a or {}).get(key, [])}
        sb = {norm(x) for x in (copy_b or {}).get(key, [])}
        out[key] = {"added": sorted(sb - sa), "removed": sorted(sa - sb)}
    return out


# ---------------------------------------------------------------------- 证据引用

def validate_evidence_ref(store, ref: str) -> bool:
    """校验覆盖理由中引用的输入证据是否真实存在。"""
    parts = ref.split(":")
    if len(parts) == 3 and parts[0] == "spec":
        return store.get_version(parts[1], parts[2]) is not None
    if len(parts) == 4 and parts[0] == "declaration":
        ver = store.get_version(parts[1], parts[2])
        return ver is not None and any(
            norm(d["allergen"]) == norm(parts[3]) for d in ver["supplier_declarations"]
        )
    if len(parts) == 2 and parts[0] == "line":
        return store.get_line(parts[1]) is not None
    if len(parts) == 3 and parts[0] == "recipe":
        return store.get_recipe(parts[1], parts[2]) is not None
    if len(parts) == 2 and parts[0] == "batch":
        return store.get_batch(parts[1]) is not None
    if len(parts) == 2 and parts[0] == "cleaning":
        return store.get_cleaning_record(parts[1]) is not None
    if len(parts) == 2 and parts[0] == "swab":
        return store.get_swab(parts[1]) is not None
    if len(parts) >= 3 and parts[0] == "program":
        # program:{program_id}@{version}
        body = ref[len("program:"):]
        if "@" not in body:
            return False
        pid, ver = body.rsplit("@", 1)
        return store.get_cleaning_program(pid, ver) is not None
    return False
