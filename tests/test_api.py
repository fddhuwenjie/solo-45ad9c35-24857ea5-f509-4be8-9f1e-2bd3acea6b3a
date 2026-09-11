"""端到端测试：规则引擎、生命周期、覆盖、影响传播、比较与报告。"""
import pytest
from fastapi.testclient import TestClient

from label_audit.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


def seed_base(client: TestClient) -> str:
    """基础场景：复合原料（巧克力豆 -> 大豆磷脂）+ 共线产线。返回产品 ID。"""
    client.post("/ingredients", json={
        "id": "LEC", "name": "大豆磷脂", "aliases": ["卵磷脂"], "is_compound": False})
    client.post("/ingredients/LEC/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "soy", "status": "present",
                                   "source": "SUP-DECL-001"}]})
    client.post("/ingredients", json={
        "id": "FLOUR", "name": "小麦粉", "aliases": [], "is_compound": False})
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"}]})
    client.post("/ingredients", json={
        "id": "SUGAR", "name": "白砂糖", "aliases": [], "is_compound": False})
    client.post("/ingredients/SUGAR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "peanut", "status": "absent"}]})
    client.post("/ingredients", json={
        "id": "CHOC", "name": "巧克力豆", "aliases": [], "is_compound": True})
    client.post("/ingredients/CHOC/versions", json={
        "version": "v2",
        "sub_components": [{"ingredient_ref": "卵磷脂", "percentage": 2},
                           {"ingredient_ref": "SUGAR", "version": "v1", "percentage": 45}],
        "supplier_declarations": [{"allergen": "milk", "status": "may_contain",
                                   "source": "SUP-DECL-002"}]})
    client.post("/products", json={"id": "COOKIE", "name": "巧克力曲奇"})
    client.post("/products/COOKIE/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 55},
                  {"ingredient_ref": "CHOC", "version": "v2", "percentage": 40},
                  {"ingredient_ref": "SUGAR", "version": "v1", "percentage": 5}]})
    client.post("/lines", json={"id": "L1", "name": "1号产线",
                                "allergens_handled": ["milk", "peanut"]})
    client.post("/products/COOKIE/lines/L1")
    # 批次：含奶花生的 B01 在前，曲奇 B02 在后；无清洁记录 -> 两条路径均开放，
    # 与旧静态推导（共线处理奶/花生）的保守结论一致
    client.post("/batches", json={
        "batch_id": "B01", "product_id": "COOKIE", "line_id": "L1", "sequence": 1,
        "allergens": ["milk", "peanut"],
        "equipment_segments": [{"segment_id": "MIX", "name": "搅拌段"}]})
    client.post("/batches", json={
        "batch_id": "B02", "product_id": "COOKIE", "line_id": "L1", "sequence": 2,
        "allergens": [],
        "equipment_segments": [{"segment_id": "MIX", "name": "搅拌段"}]})
    return "COOKIE"


def make_label(client: TestClient, product_id: str, copy: dict) -> dict:
    res = client.post("/labels", json={"product_id": product_id, "copy": copy})
    assert res.status_code == 201, res.text
    return res.json()


GOOD_COPY = {
    "declared_allergens": ["wheat", "soy"],
    "may_contain": ["milk", "peanut"],
    "free_from_claims": [],
    "ingredients_text": "小麦粉、巧克力豆、白砂糖",
}
BAD_COPY = {
    "declared_allergens": ["wheat"],
    "may_contain": [],
    "free_from_claims": ["milk"],
    "ingredients_text": "小麦粉、巧克力豆、白砂糖",
}


def approve_ok(client: TestClient, label_id: str) -> dict:
    assert client.post(f"/labels/{label_id}/submit").status_code == 200
    res = client.post(f"/labels/{label_id}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- 来源图与推导

def test_source_graph_expands_compound_via_alias(client):
    seed_base(client)
    res = client.get("/products/COOKIE/source-graph")
    assert res.status_code == 200
    data = res.json()
    paths = {n["path"] for n in data["graph"]}
    assert any("CHOC@v2 > LEC@v1" in p for p in paths), paths
    lec_node = next(n for n in data["graph"] if n["ingredient_id"] == "LEC")
    assert "别名" in lec_node["note"]
    assert set(data["derived"]["required"]) == {"wheat", "soy"}
    assert set(data["derived"]["may_contain"]) == {"milk", "peanut"}
    # 证据路径可追溯到具体规格版本
    soy_ev = data["derived"]["required"]["soy"][0]
    assert soy_ev["source"] == "spec:LEC:v1"
    assert soy_ev["declaration_ref"] == "SUP-DECL-001"


def test_missing_declaration_and_claim_contradiction_block_approval(client):
    seed_base(client)
    label = make_label(client, "COOKIE", BAD_COPY)
    kinds = {(f["kind"], f["detail"].get("allergen")) for f in label["findings"]}
    assert ("missing_declaration", "soy") in kinds
    assert ("claim_contradiction", "milk") in kinds
    assert ("missing_cross_contact", "peanut") in kinds
    assert client.post(f"/labels/{label['id']}/submit").status_code == 200
    res = client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 409
    blockers = res.json()["detail"]["open_blockers"]
    assert {b["kind"] for b in blockers} >= {"missing_declaration", "claim_contradiction"}


def test_clean_label_approves_and_snapshot_persisted(client):
    seed_base(client)
    label = make_label(client, "COOKIE", GOOD_COPY)
    assert label["open_blockers"] == []
    out = approve_ok(client, label["id"])
    snap = out["approval"]["snapshot"]
    assert snap["copy"]["declared_allergens"] == ["wheat", "soy"]
    assert set(snap["derived"]["required"]) == {"wheat", "soy"}
    assert client.post(f"/labels/{label['id']}/withdraw",
                       json={"reason": "换版"}).status_code == 200
    assert client.get(f"/labels/{label['id']}").json()["status"] == "withdrawn"


def test_lifecycle_transitions_enforced(client):
    seed_base(client)
    label = make_label(client, "COOKIE", GOOD_COPY)
    assert client.post(f"/labels/{label['id']}/approve",
                       json={"approved_by": "x"}).status_code == 409  # 未提交复核
    assert client.post(f"/labels/{label['id']}/withdraw",
                       json={"reason": "r"}).status_code == 409  # 未批准
    approve_ok(client, label["id"])
    assert client.put(f"/labels/{label['id']}/copy",
                      json={"copy": GOOD_COPY}).status_code == 409  # 已批准不可改文案


# ---------------------------------------------------------------- 覆盖

def test_override_requires_reason_and_resolvable_evidence(client):
    seed_base(client)
    label = make_label(client, "COOKIE", BAD_COPY)
    finding = next(f for f in label["findings"] if f["kind"] == "claim_contradiction")
    # 空证据列表 -> 422（Pydantic）
    res = client.post(f"/labels/{label['id']}/findings/{finding['id']}/override",
                      json={"reviewer": "qa", "reason": "x", "evidence_refs": []})
    assert res.status_code == 422
    # 无法解析的证据 -> 422
    res = client.post(f"/labels/{label['id']}/findings/{finding['id']}/override",
                      json={"reviewer": "qa", "reason": "x",
                            "evidence_refs": ["spec:NOPE:v9"]})
    assert res.status_code == 422
    # 缺理由 -> 422
    res = client.post(f"/labels/{label['id']}/findings/{finding['id']}/override",
                      json={"reviewer": "qa", "reason": "",
                            "evidence_refs": ["line:L1"]})
    assert res.status_code == 422


def test_override_all_blockers_allows_approval(client):
    seed_base(client)
    label = make_label(client, "COOKIE", BAD_COPY)
    client.post(f"/labels/{label['id']}/submit")
    evidence = {"claim_contradiction": ["line:L1", "spec:CHOC:v2"],
                "missing_declaration": ["declaration:LEC:v1:soy"]}
    for f in client.get(f"/labels/{label['id']}").json()["findings"]:
        if f["severity"] == "blocker" and f["status"] == "open":
            res = client.post(
                f"/labels/{label['id']}/findings/{f['id']}/override",
                json={"reviewer": "qa.lead", "reason": "供应商确认风险可控，留档",
                      "evidence_refs": evidence[f["kind"]]})
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "overridden"
            assert res.json()["override"]["evidence_refs"]
    res = client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 200, res.text
    # 覆盖记录在重新分析后仍然保留（按指纹匹配）
    kept = client.get(f"/labels/{label['id']}").json()["findings"]
    assert all(f["status"] == "overridden" for f in kept if f["severity"] == "blocker")


# ---------------------------------------------------------------- 问题检测

def test_circular_reference_detected(client):
    client.post("/ingredients", json={"id": "A", "name": "甲", "is_compound": True})
    client.post("/ingredients/A/versions", json={
        "version": "v1", "sub_components": [{"ingredient_ref": "B"}],
        "supplier_declarations": [{"allergen": "soy", "status": "absent"}]})
    client.post("/ingredients", json={"id": "B", "name": "乙", "is_compound": True})
    client.post("/ingredients/B/versions", json={
        "version": "v1", "sub_components": [{"ingredient_ref": "A"}],
        "supplier_declarations": [{"allergen": "soy", "status": "absent"}]})
    client.post("/products", json={"id": "P", "name": "丙"})
    client.post("/products/P/recipes", json={
        "version": "v1", "items": [{"ingredient_ref": "A", "percentage": 100}]})
    label = make_label(client, "P", {})
    circ = [f for f in label["findings"] if f["kind"] == "circular_reference"]
    assert circ and circ[0]["detail"]["cycle"] == ["A", "B", "A"]


def test_unexpandable_compound_detected(client):
    client.post("/ingredients", json={"id": "MIX", "name": "预拌粉", "is_compound": True})
    client.post("/ingredients/MIX/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"}]})
    client.post("/products", json={"id": "P2", "name": "面包"})
    client.post("/products/P2/recipes", json={
        "version": "v1", "items": [{"ingredient_ref": "MIX", "percentage": 100}]})
    label = make_label(client, "P2", {"declared_allergens": ["wheat"]})
    kinds = {f["kind"] for f in label["findings"]}
    assert "unexpandable_compound" in kinds


def test_data_gaps_detected(client):
    seed_base(client)
    client.post("/products", json={"id": "P3", "name": "测试品"})
    client.post("/products/P3/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v9", "percentage": 50},
                  {"ingredient_ref": "神秘粉", "percentage": 50}]})
    label = make_label(client, "P3", {})
    gaps = [f for f in label["findings"] if f["kind"] == "data_gap"]
    missing = {f["detail"]["missing"] for f in gaps}
    assert "spec_version" in missing   # FLOUR@v9 不存在
    assert "ingredient" in missing     # “神秘粉”无法解析


def test_missing_supplier_declaration_is_gap(client):
    client.post("/ingredients", json={"id": "OIL", "name": "植物油"})
    client.post("/ingredients/OIL/versions", json={"version": "v1"})  # 无声明
    client.post("/products", json={"id": "P4", "name": "饼干"})
    client.post("/products/P4/recipes", json={
        "version": "v1", "items": [{"ingredient_ref": "OIL", "percentage": 100}]})
    label = make_label(client, "P4", {})
    gaps = [f for f in label["findings"] if f["kind"] == "data_gap"]
    assert any(f["detail"]["missing"] == "supplier_declaration" for f in gaps)


def test_alias_mixing_detected(client):
    client.post("/ingredients", json={"id": "G1", "name": "明胶", "aliases": ["胶"]})
    client.post("/ingredients/G1/versions", json={
        "version": "v1", "supplier_declarations": [{"allergen": "soy", "status": "absent"}]})
    client.post("/ingredients", json={"id": "G2", "name": "果胶", "aliases": ["胶"]})
    client.post("/ingredients/G2/versions", json={
        "version": "v1", "supplier_declarations": [{"allergen": "soy", "status": "absent"}]})
    client.post("/products", json={"id": "P5", "name": "软糖"})
    # 用共享别名引用 -> 歧义（blocker）
    client.post("/products/P5/recipes", json={
        "version": "v1", "items": [{"ingredient_ref": "胶", "percentage": 100}]})
    label = make_label(client, "P5", {})
    amb = [f for f in label["findings"] if f["kind"] == "alias_conflict"]
    assert any(f["severity"] == "blocker" for f in amb)
    # 用 ID 同时引用两者 -> 同图别名混用（warning）
    client.post("/products/P5/recipes", json={
        "version": "v2", "items": [{"ingredient_ref": "G1", "percentage": 50},
                                   {"ingredient_ref": "G2", "percentage": 50}]})
    client.post(f"/labels/{label['id']}/reanalyze")
    mixing = [f for f in client.get(f"/labels/{label['id']}").json()["findings"]
              if f["kind"] == "alias_conflict" and f["severity"] == "warning"]
    assert mixing and mixing[0]["detail"]["ingredient_ids"] == ["G1", "G2"]


# ---------------------------------------------------------------- 影响传播与比较

def test_spec_update_marks_impacted_and_derives_revision(client):
    seed_base(client)
    # 第二个产品共用大豆磷脂，验证波及范围
    client.post("/products", json={"id": "CANDY", "name": "软糖"})
    client.post("/products/CANDY/recipes", json={
        "version": "v1", "items": [{"ingredient_ref": "LEC", "percentage": 3},
                                   {"ingredient_ref": "SUGAR", "version": "v1",
                                    "percentage": 97}]})
    label = make_label(client, "COOKIE", GOOD_COPY)
    approve_ok(client, label["id"])
    approvals_before = client.get(f"/labels/{label['id']}").json()["approvals"]
    assert len(approvals_before) == 1

    # 供应商更换：大豆磷脂新规格声明 soy=absent
    res = client.post("/ingredients/LEC/versions", json={
        "version": "v2",
        "supplier_declarations": [{"allergen": "soy", "status": "absent",
                                   "source": "SUP-DECL-101"}]})
    assert res.status_code == 201
    body = res.json()
    assert set(body["impacted_products"]) == {"COOKIE", "CANDY"}
    actions = {(a["product_id"], a["action"]) for a in body["actions"]}
    assert ("COOKIE", "derived_new_revision") in actions

    old = client.get(f"/labels/{label['id']}").json()
    assert old["status"] == "approved" and old["stale"] is True
    # 批准记录保持只读：仍是原来那一条，快照未变
    assert client.get(f"/labels/{label['id']}").json()["approvals"] == approvals_before
    # 派生的新修订为草稿，且按新资料重新推导（soy 不再是应声明项）
    new_label = next(a for a in body["actions"]
                     if a["action"] == "derived_new_revision")
    rev2 = client.get(f"/labels/{new_label['label_id']}").json()
    assert rev2["revision"] == 2 and rev2["status"] == "draft"
    assert set(rev2["derived"]["required"]) == {"wheat"}

    # 比较两版：soy 被移除，波及到共用 LEC 的 CANDY
    cmp_res = client.get("/products/COOKIE/labels/compare",
                         params={"from_revision": 1, "to_revision": 2})
    assert cmp_res.status_code == 200
    diff = cmp_res.json()
    assert diff["required"] == {"added": [], "removed": ["soy"]}
    assert diff["changed_ingredients"] == ["LEC"]
    assert any(p["product_id"] == "CANDY" for p in diff["impact_scope"])


def test_recipe_update_marks_label_stale(client):
    seed_base(client)
    label = make_label(client, "COOKIE", GOOD_COPY)
    approve_ok(client, label["id"])
    res = client.post("/products/COOKIE/recipes", json={
        "version": "v2",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    assert res.status_code == 201
    actions = {(a["product_id"], a["action"]) for a in res.json()["actions"]}
    assert ("COOKIE", "derived_new_revision") in actions
    impact = client.get("/products/COOKIE/impact").json()
    assert any(l["stale"] for l in impact["labels"])
    assert set(impact["referenced_ingredients"]) == {"FLOUR"}


# ---------------------------------------------------------------- 发现项生命周期

def test_reappearing_finding_reopens_and_blocks_approval(client):
    """open -> resolved -> 再次出现：发现项必须重新打开，批准被拦截。"""
    seed_base(client)
    missing_wheat = {
        "declared_allergens": ["soy"],
        "may_contain": ["milk", "peanut"],
        "free_from_claims": [],
    }
    full = {
        "declared_allergens": ["wheat", "soy"],
        "may_contain": ["milk", "peanut"],
        "free_from_claims": [],
    }
    # 1. 初次检出 missing_declaration(wheat)，状态 open
    label = make_label(client, "COOKIE", missing_wheat)
    lid = label["id"]
    hits = [f for f in label["findings"] if f["kind"] == "missing_declaration"]
    assert len(hits) == 1 and hits[0]["status"] == "open"
    assert hits[0]["detail"]["allergen"] == "wheat"
    fp = hits[0]["fingerprint"]

    # 2. 补入 wheat -> 同一指纹转为 resolved，open_blockers 清空
    client.put(f"/labels/{lid}/copy", json={"copy": full})
    view = client.get(f"/labels/{lid}").json()
    assert view["open_blockers"] == []
    assert all(f["fingerprint"] != fp for f in view["findings"])  # resolved 不再列出

    # 3. 再次删除 wheat -> 同一指纹必须恢复为 open
    client.put(f"/labels/{lid}/copy", json={"copy": missing_wheat})
    view = client.get(f"/labels/{lid}").json()
    reopened = [f for f in view["findings"] if f["fingerprint"] == fp]
    assert len(reopened) == 1 and reopened[0]["status"] == "open"
    assert {b["kind"] for b in view["open_blockers"]} == {"missing_declaration"}

    # 4. 提交复核后批准必须被拒：不写批准记录、不进入 approved
    assert client.post(f"/labels/{lid}/submit").status_code == 200
    res = client.post(f"/labels/{lid}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 409
    assert any(b["kind"] == "missing_declaration"
               for b in res.json()["detail"]["open_blockers"])
    view = client.get(f"/labels/{lid}").json()
    assert view["status"] == "in_review"
    assert view["approvals"] == []


# ---------------------------------------------------------------- 报告与样例

def test_check_package_and_review_sheet(client):
    seed_base(client)
    label = make_label(client, "COOKIE", BAD_COPY)
    pkg = client.get(f"/labels/{label['id']}/check-package").json()
    assert pkg["package_type"] == "label_audit_check_package"
    assert pkg["source_graph"] and pkg["findings"]
    assert {s["ref"] for s in pkg["evidence_index"]["specs"]} >= {
        "spec:LEC:v1", "spec:CHOC:v2"}
    assert pkg["evidence_index"]["lines"][0]["ref"] == "line:L1"

    res = client.get(f"/labels/{label['id']}/review-sheet")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    assert "食品标签审查单" in res.text and "claim_contradiction" in res.text


def test_sample_endpoint_roundtrip(client):
    sample = client.get("/samples/compound-coline").json()
    assert sample["steps"] and sample["expected_findings"]
    label_id = None
    for step in sample["steps"]:
        path = step["path"].replace("{label_id}", label_id or "")
        res = client.request(step["method"], path, json=step.get("body"))
        assert res.status_code == step["expected_status"], (step["comment"], res.text)
        if step["path"] == "/labels":
            label_id = res.json()["id"]
    # 重放完毕，样例预期的四类发现项全部出现
    kinds = {f["kind"] for f in client.get(f"/labels/{label_id}").json()["findings"]}
    assert kinds >= {e["kind"] for e in sample["expected_findings"]}
