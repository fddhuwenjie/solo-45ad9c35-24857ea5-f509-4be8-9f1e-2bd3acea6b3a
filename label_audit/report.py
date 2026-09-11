"""报告：JSON 核对包与可打印审查单（HTML）。"""
from __future__ import annotations

import html

from .db import utcnow
from .engine import analyze_label, expand_recipe


def build_check_package(store, label_id: str) -> dict:
    """汇总一个标签修订的全部审核资料，供存档或对接下游系统。"""
    label = store.get_label(label_id)
    if label is None:
        raise KeyError(f"label {label_id} not found")
    product = store.get_product(label["product_id"])
    recipe = store.current_recipe(label["product_id"])
    exp = expand_recipe(store, label["product_id"])
    findings = store.findings_for_label(label_id)
    approvals = store.approvals_for_label(label_id)

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
    if recipe:
        evidence_index["recipes"].append({
            "ref": f"recipe:{recipe['product_id']}:{recipe['version']}",
            "product_id": recipe["product_id"], "version": recipe["version"],
            "items": recipe["items"],
        })

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

    required_rows = [
        (a, ev) for a, evs in derived["required"].items() for ev in evs
    ]
    may_rows = [
        (a, ev) for a, evs in derived["may_contain"].items() for ev in evs
    ]

    def finding_cells(f):
        ov = f.get("override")
        ov_text = ""
        if ov:
            ov_text = f"{ov['reviewer']}：{ov['reason']}（证据：{', '.join(ov['evidence_refs'])}）"
        return (f["kind"], f["severity"], f["status"], f["message"], ov_text)

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

<h2>四、发现项（含覆盖记录）</h2>
<table><tr><th>类型</th><th>级别</th><th>状态</th><th>说明</th><th>覆盖理由与证据</th></tr>
{''.join('<tr class="' + _esc(f['severity']) + '">' + ''.join(f'<td>{_esc(c)}</td>' for c in finding_cells(f)) + '</tr>' for f in findings) or "<tr><td colspan='5' class='empty'>（无）</td></tr>"}
</table>

<h2>五、批准记录</h2>
<table><tr><th>#</th><th>批准人</th><th>批准时间</th></tr>
{_rows(approvals, [lambda a: a['id'], lambda a: a['approved_by'], lambda a: a['approved_at']])}
</table>

<h2>六、签字</h2>
<table class="sign"><tr><th>环节</th><th>签字</th><th>日期</th><th>备注</th></tr>
{''.join(f"<tr><td>{s}</td><td></td><td></td><td></td></tr>" for s in ('草拟', '复核', '批准', '撤回'))}
</table>
</body></html>"""
    return sheet
