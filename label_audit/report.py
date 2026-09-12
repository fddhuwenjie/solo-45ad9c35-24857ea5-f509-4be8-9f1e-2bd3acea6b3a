"""报告：JSON 核对包与可打印审查单（HTML）。"""
from __future__ import annotations

import html

from . import genealogy, packaging, trace
from .db import utcnow
from .engine import analyze_label, expand_recipe


def build_check_package(store, label_id: str) -> dict:
    """汇总一个标签修订的全部审核资料，供存档或对接下游系统。"""
    label = store.get_label(label_id)
    if label is None:
        raise KeyError(f"label {label_id} not found")
    product = store.get_product(label["product_id"])
    recipe = store.current_recipe(label["product_id"])
    batch_id = store.get_label_batch(label_id)
    exp = expand_recipe(store, label["product_id"], batch_id=batch_id)
    findings = store.findings_for_label(label_id)
    approvals = store.approvals_for_label(label_id)
    batch_evidence = trace.batch_trace_evidence(store, batch_id) if batch_id else None
    material_genealogy = None
    if batch_id:
        material_genealogy = {
            **genealogy.batch_material_evidence(store, batch_id),
            "spec_corrections": genealogy.corrections_for_batch(store, batch_id),
        }

    spec_refs = sorted({e["source"] for b in (label["derived"] or {}).values()
                        for evs in b.values() for e in evs
                        if (e.get("source") or "").startswith("spec:")})
    evidence_index = {"specs": [], "lines": [], "recipes": []}
    for ref in spec_refs:
        _, iid, ver = ref.split(":", 2)
        v = store.get_version(iid, ver)
        if v:
            evidence_index["specs"].append({
                "ref": ref, "ingredient_id": iid, "version": ver,
                "supplier_declarations": v["supplier_declarations"],
            })
    for line in store.lines_for_product(label["product_id"]):
        evidence_index["lines"].append({
            "ref": f"line:{line['id']}", "line_id": line["id"],
            "name": line["name"], "allergens_handled": line["allergens_handled"],
        })
    if batch_evidence:
        evidence_index["batch_trace"] = {
            "batch_id": batch_id,
            "segments": batch_evidence["batch"]["segments"],
            "cleaning_records": [
                {"ref": f"cleaning:{r['record_id']}", **r}
                for r in batch_evidence["cleaning_records"]],
            "cleaning_programs": [
                {"ref": f"program:{p['program_id']}@{p['version']}", **p}
                for p in batch_evidence["cleaning_programs"]],
            "swab_results": [
                {"ref": f"swab:{s['swab_id']}", **s} for s in batch_evidence["swab_results"]],
        }
    if recipe:
        evidence_index["recipes"].append({
            "ref": f"recipe:{recipe['product_id']}:{recipe['version']}",
            "product_id": recipe["product_id"], "version": recipe["version"],
            "items": recipe["items"],
        })

    # 印刷标签批次控制：放行采用的标签修订、分析版本、数量变化与冻结原因
    print_batches = []
    for pb in store.print_batches_for_label(label_id):
        print_batches.append({
            "print_batch_id": pb["print_batch_id"],
            "status": pb["status"],
            "copy_summary": pb["copy_summary"],
            "quantity_received": pb["quantity_received"],
            "issued_quantity": pb["issued_quantity"],
            "disposed_quantity": pb["disposed_quantity"],
            "remaining_quantity": pb["remaining_quantity"],
            "received_at": pb["received_at"],
            "expires_at": pb["expires_at"],
            "applicable_product_ids": pb["applicable_product_ids"],
            "frozen_reason": pb["frozen_reason"],
            "frozen_at": pb["frozen_at"],
            "issuances": [
                {"issuance_id": i["issuance_id"],
                 "production_batch_id": i["production_batch_id"],
                 "quantity": i["quantity"],
                 "label_revision": i["label_revision"],
                 "analysis_version": i["analysis_version"],
                 "analysis_snapshot": i["analysis_snapshot"],
                 "issued_at": i["created_at"]}
                for i in store.issuances_for_print_batch(pb["print_batch_id"])],
            "dispositions": store.dispositions_for_print_batch(pb["print_batch_id"]),
        })

    # 包装执行与卷标结算：串起清场发现、用标/调整事件与结算记录
    packaging_runs = []
    seen_runs = set()
    for pb in store.print_batches_for_label(label_id):
        for iss in store.issuances_for_print_batch(pb["print_batch_id"]):
            run_id = store.issuance_bound_run(iss["issuance_id"])
            if run_id and run_id not in seen_runs:
                seen_runs.add(run_id)
                packaging_runs.append(packaging.run_view(store, run_id))

    return {
        "package_type": "label_audit_check_package",
        "generated_at": utcnow(),
        "label": {k: label[k] for k in ("id", "product_id", "revision", "status", "stale", "copy")},
        "product": product,
        "recipe": recipe,
        "source_graph": [n.as_dict() for n in exp.nodes],
        "derived": label["derived"],
        "findings": findings,
        "approvals": approvals,
        "batch_id": batch_id,
        "batch_trace": batch_evidence["trace"] if batch_evidence else None,
        "material_genealogy": material_genealogy,
        "print_control": {
            "label_id": label_id,
            "print_batches": print_batches,
            "total_received": sum(p["quantity_received"] for p in print_batches),
            "total_issued": sum(p["issued_quantity"] for p in print_batches),
            "total_disposed": sum(p["disposed_quantity"] for p in print_batches),
            "frozen": [p["print_batch_id"] for p in print_batches
                       if p["status"] == "frozen"],
        },
        "packaging_execution": {
            "label_id": label_id,
            "runs": packaging_runs,
            "disposition": packaging.disposition_for_label(store, label_id),
        },
        "evidence_index": evidence_index,
    }


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _rows(items, cols) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(c(item))}</td>" for c in cols) + "</tr>"
        for item in items
    )
    return body or f"<tr><td colspan='{len(cols)}' class='empty'>（无）</td></tr>"


def build_review_sheet(store, label_id: str) -> str:
    """生成可打印 HTML 审查单：推导结论、来源路径、发现项、覆盖记录与签字栏。"""
    if store.get_label(label_id) is None:
        raise KeyError(f"label {label_id} not found")
    analysis = analyze_label(store, label_id)  # 打印前刷新，保证与最新资料一致
    label = store.get_label(label_id)
    product = store.get_product(label["product_id"])
    copy = label["copy"]
    derived = analysis["derived"]
    findings = store.findings_for_label(label_id)
    approvals = store.approvals_for_label(label_id)
    batch_id = analysis.get("batch_id")
    batch_trace = analysis.get("batch_trace")

    required_rows = [
        (a, ev) for a, evs in derived["required"].items() for ev in evs
    ]
    may_rows = [
        (a, ev) for a, evs in derived["may_contain"].items() for ev in evs
    ]

    def batch_section() -> str:
        if not batch_trace:
            return ("<h2>四、批次共线追溯</h2>"
                    "<table><tr><td class='empty'>未绑定生产批次（按产线历史登记"
                    "保守推导）</td></tr></table>")
        rows = ""
        for p in batch_trace["paths"]:
            gaps = "；".join(
                f"{g['code']}" + (f"（{g.get('segment_id') or g.get('point_id') or ''}）"
                                  if g.get("segment_id") or g.get("point_id") else "")
                for g in p["evidence_gaps"]) or "—"
            rows += (f"<tr><td>{_esc(p['allergen'])}</td>"
                     f"<td>{_esc(p['source_batch_id'] or '未知')}</td>"
                     f"<td>{_esc(p['source_kind'])}</td>"
                     f"<td><b>{_esc(p['status'])}</b></td>"
                     f"<td>{_esc(gaps)}</td>"
                     f"<td>{_esc(p['route'])}</td></tr>")
        return ("<h2>四、批次共线追溯（前序含敏原批次 + 返工路径）</h2>"
                f"<table><tr><th>过敏原</th><th>来源批次</th><th>来源类型</th>"
                f"<th>路径状态</th><th>证据缺口</th><th>追溯链</th></tr>{rows}</table>"
                f"<p>审核批次：{_esc(batch_id)}（产线 {_esc(batch_trace['line_id'])}，"
                f"顺序号 {batch_trace['sequence']}）；开放："
                f"{_esc(', '.join(batch_trace['open_allergens']) or '无')}；"
                f"已关闭：{_esc(', '.join(batch_trace['closed_allergens']) or '无')}</p>")

    def finding_cells(f):
        ov = f.get("override")
        ov_text = ""
        if ov:
            ov_text = f"{ov['reviewer']}：{ov['reason']}（证据：{', '.join(ov['evidence_refs'])}）"
        return (f["kind"], f["severity"], f["status"], f["message"], ov_text)

    def print_section() -> str:
        rows = store.print_batches_for_label(label_id)
        if not rows:
            return "<table><tr><td class='empty'>（无登记印刷批次）</td></tr></table>"
        body = ""
        for pb in rows:
            issuances = store.issuances_for_print_batch(pb["print_batch_id"])
            issued = ", ".join(
                f"{i['production_batch_id']}×{i['quantity']}"
                f"（{i['analysis_version']}）" for i in issuances) or "—"
            freeze = f"；冻结原因：{_esc(pb['frozen_reason'])}" if pb["frozen_reason"] else ""
            body += (
                f"<tr><td>{_esc(pb['print_batch_id'])}</td>"
                f"<td>{_esc(pb['status'])}{freeze}</td>"
                f"<td>{pb['quantity_received']}</td>"
                f"<td>{pb['issued_quantity']}</td>"
                f"<td>{pb['disposed_quantity']}</td>"
                f"<td>{pb['remaining_quantity']}</td>"
                f"<td>{_esc(', '.join(pb['applicable_product_ids']))}</td>"
                f"<td>{_esc(pb['expires_at'] or '—')}</td>"
                f"<td>{_esc(issued)}</td></tr>")
        return ("<table><tr><th>印刷批号</th><th>状态</th><th>入库</th>"
                "<th>已领用</th><th>已处置</th><th>剩余</th><th>适用产品</th>"
                "<th>失效时刻</th><th>领用生产批次（分析版本）</th></tr>"
                f"{body}</table>")

    def genealogy_section() -> str:
        if not batch_id:
            return "<table><tr><td class='empty'>未绑定生产批次</td></tr></table>"
        ev = genealogy.batch_material_evidence(store, batch_id)
        if not ev["allocations"]:
            return "<table><tr><td class='empty'>（无投料记录；来源图按现行规格展开）</td></tr></table>"
        lots = {l["lot_id"]: l for l in ev["lots"]}
        body = ""
        for a in ev["allocations"]:
            lot = lots.get(a["lot_id"], {})
            body += (
                f"<tr><td>{_esc(a['lot_id'])}</td>"
                f"<td>{_esc(lot.get('supplier_lot_no') or '—')}</td>"
                f"<td>{_esc(a['ingredient_id'])}</td>"
                f"<td>{_esc(a['spec_version'])}</td>"
                f"<td>{a['quantity']}</td>"
                f"<td>{_esc(a['status'])}</td></tr>")
        ledger = "".join(
            f"<tr><td>{_esc(e['created_at'])}</td><td>{_esc(e['kind'])}</td>"
            f"<td>{_esc(e['lot_id'])}</td><td>{e['quantity']}</td>"
            f"<td>{_esc(e['reason'] or '—')}</td></tr>"
            for e in ev["ledger"])
        corrections = genealogy.corrections_for_batch(store, batch_id)

        def _corr_text(c) -> str:
            parts = ["{}: {}→{}（{}）".format(ch["allergen"], ch["old_status"],
                                             ch["new_status"], ch["spec_version"])
                     for ch in c["declaration_changes"]]
            return "；".join(parts) or "—"

        corr = "".join(
            "<tr><td>{}</td><td>{}@{}</td><td>{}</td></tr>".format(
                _esc(c["ts"]), _esc(c["ingredient_id"]), _esc(c["new_version"]),
                _esc(_corr_text(c)))
            for c in corrections)
        return ("<table><tr><th>原料批号</th><th>供应商批号</th><th>原料</th>"
                "<th>锁定规格</th><th>投料用量</th><th>分配状态</th></tr>"
                f"{body}</table>"
                "<h3>扣量流水</h3>"
                "<table><tr><th>时间</th><th>类型</th><th>批号</th><th>数量</th>"
                f"<th>原因</th></tr>{ledger}</table>"
                + ("<h3>供应商更正波及</h3>"
                   "<table><tr><th>时间</th><th>更正规格</th><th>声明差异</th></tr>"
                   f"{corr}</table>" if corrections else ""))

    def packaging_section() -> str:
        runs = []
        seen = set()
        for pb in store.print_batches_for_label(label_id):
            for iss in store.issuances_for_print_batch(pb["print_batch_id"]):
                rid = store.issuance_bound_run(iss["issuance_id"])
                if rid and rid not in seen:
                    seen.add(rid)
                    runs.append(packaging.run_view(store, rid))
        if not runs:
            return "<table><tr><td class='empty'>（无包装运行记录）</td></tr></table>"
        out = ""
        kind_names = {"applied": "合格品贴用", "wasted": "过程损耗",
                      "sampled": "留样", "returned": "退回隔离",
                      "adjustment": "盘点调整"}
        for run in runs:
            rec = run["reconciliation"]
            clearance = "".join(
                f"<tr><td>{_esc(f['finding'])}</td><td>{f['old_rolls_found']}</td>"
                f"<td>{'已隔离' if f['isolated'] else '未隔离'}</td>"
                f"<td>{_esc(f['note'] or '—')}</td></tr>"
                for f in run["clearance_findings"])
            clearance = (f"<table><tr><th>清场发现</th><th>旧卷标数</th><th>处置</th>"
                         f"<th>备注</th></tr>{clearance}</table>" if clearance else "")
            events = "".join(
                f"<tr><td>{_esc(e['occurred_at'])}</td>"
                f"<td>{_esc(kind_names.get(e['kind'], e['kind']))}"
                f"{'（' + _esc(e['category']) + '）' if e['kind'] == 'adjustment' else ''}</td>"
                f"<td>{e['quantity']}</td><td>{_esc(e['good_units'])}</td>"
                f"<td>{_esc(e['operator'])}</td><td>{_esc(e['reason'] or '—')}</td></tr>"
                for e in run["events"])
            settlements = "".join(
                f"<tr><td>{_esc(s['settlement_id'])}</td><td>{_esc(s['result'])}</td>"
                f"<td>{_esc(s['settled_by'])}</td><td>{_esc(s['created_at'])}</td></tr>"
                for s in run["settlements"])
            ib = rec["balances"]["issuance_balance"]
            ab = rec["balances"]["application_balance"]
            out += (
                f"<h3>包装运行 {_esc(run['run_id'])}（包装线 {_esc(run['line_id'])}，"
                f"状态 {_esc(run['status'])}）</h3>"
                f"<p>计划产量 {run['planned_quantity']} 件 × 每件用标 "
                f"{run['labels_per_unit']} 枚；领用 {ib['issued_quantity']} 枚，"
                f"贴用 {ab['applied_quantity']} 枚（合格品 {ab['good_units']} 件），"
                f"损耗 {rec['sources']['wasted']['total']} 枚，"
                f"留样 {rec['sources']['sampled']['total']} 枚，"
                f"退回隔离 {rec['sources']['returned']['total']} 枚，"
                f"线边未记账余量 {rec['on_line_remaining']} 枚；"
                f"对账：{'平衡' if rec['balanced'] else '不平衡'}</p>"
                + clearance
                + (f"<table><tr><th>时刻</th><th>事件</th><th>数量</th>"
                   f"<th>合格品数</th><th>操作者</th><th>事由</th></tr>{events}</table>"
                   if events else "")
                + (f"<table><tr><th>结算号</th><th>结果</th><th>结算人</th>"
                   f"<th>结算时刻</th></tr>{settlements}</table>"
                   if settlements else ""))
        return out

    sheet = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>标签审查单 - {_esc(product['name'])} 第{label['revision']}版</title>
<style>
  body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 2em; color: #222; }}
  h1 {{ font-size: 1.4em; }} h2 {{ font-size: 1.1em; border-bottom: 1px solid #999; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.6em 0 1.2em; }}
  th, td {{ border: 1px solid #999; padding: 5px 8px; font-size: 0.92em; text-align: left; vertical-align: top; }}
  th {{ background: #eee; }}
  .blocker {{ color: #b00020; font-weight: bold; }}
  .warning {{ color: #8a6d00; }}
  .empty {{ color: #888; text-align: center; }}
  .meta td {{ border: none; padding: 2px 12px 2px 0; }}
  .sign td {{ height: 2.6em; }}
  @media print {{ body {{ margin: 0; }} h2 {{ page-break-after: avoid; }} table {{ page-break-inside: avoid; }} }}
</style></head><body>
<h1>食品标签审查单</h1>
<table class="meta"><tr>
  <td><b>产品</b>：{_esc(product['name'])}（{_esc(product['id'])}）</td>
  <td><b>标签修订</b>：第 {label['revision']} 版</td>
  <td><b>状态</b>：{_esc(label['status'])}{'（资料已过期）' if label['stale'] else ''}</td>
  <td><b>生成时间</b>：{_esc(utcnow())}</td>
</tr></table>

<h2>一、标签文案</h2>
<table>
<tr><th>已声明过敏原</th><td>{_esc(', '.join(copy.get('declared_allergens', [])))}</td></tr>
<tr><th>交叉接触提示</th><td>{_esc(', '.join(copy.get('may_contain', [])))}</td></tr>
<tr><th>“无…”宣称</th><td>{_esc(', '.join(copy.get('free_from_claims', [])))}</td></tr>
<tr><th>配料表</th><td>{_esc(copy.get('ingredients_text', ''))}</td></tr>
</table>

<h2>二、推导应声明项（含来源路径）</h2>
<table><tr><th>过敏原</th><th>状态</th><th>来源路径</th><th>证据</th></tr>
{_rows(required_rows, [lambda r: r[0], lambda r: r[1]['status'], lambda r: r[1]['path'], lambda r: r[1]['source']])}
</table>

<h2>三、交叉接触（共线）提示</h2>
<table><tr><th>过敏原</th><th>状态</th><th>来源路径</th><th>证据</th></tr>
{_rows(may_rows, [lambda r: r[0], lambda r: r[1]['status'], lambda r: r[1]['path'], lambda r: r[1]['source']])}
</table>

{batch_section()}

<h2>五、发现项（含覆盖记录）</h2>
<table><tr><th>类型</th><th>级别</th><th>状态</th><th>说明</th><th>覆盖理由与证据</th></tr>
{''.join('<tr class="' + _esc(f['severity']) + '">' + ''.join(f'<td>{_esc(c)}</td>' for c in finding_cells(f)) + '</tr>' for f in findings) or "<tr><td colspan='5' class='empty'>（无）</td></tr>"}
</table>

<h2>六、批准记录</h2>
<table><tr><th>#</th><th>批准人</th><th>批准时间</th></tr>
{_rows(approvals, [lambda a: a['id'], lambda a: a['approved_by'], lambda a: a['approved_at']])}
</table>

<h2>七、印刷标签批次领用放行</h2>
{print_section()}

<h2>八、投料谱系（锁定规格与扣量流水）</h2>
{genealogy_section()}

<h2>九、包装执行与卷标结算（清场 / 用标 / 调整 / 结算）</h2>
{packaging_section()}

<h2>十、签字</h2>
<table class="sign"><tr><th>环节</th><th>签字</th><th>日期</th><th>备注</th></tr>
{''.join(f"<tr><td>{s}</td><td></td><td></td><td></td></tr>" for s in ('草拟', '复核', '批准', '撤回'))}
</table>
</body></html>"""
    return sheet
