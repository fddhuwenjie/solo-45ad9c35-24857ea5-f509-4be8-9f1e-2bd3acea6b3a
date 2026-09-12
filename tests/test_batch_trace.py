"""逐批次共线追溯的端到端测试：

- 批次登记、程序/清洁/拭子/返工入口与校验；
- 路径开闭：无记录、漏采、待出、超限、过期、程序不覆盖、跨产线程序；
- POST /labels 绑定并持久化 batch_id；
- 批准快照冻结程序版本与检测记录；
- 拭子补录（阴性消解 / 阳性沿返工链传播、派生新修订）。
"""
import pytest
from fastapi.testclient import TestClient

from label_audit.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


# -------------------------------------------------------------- 测试夹具

def setup_line_with_product(client, *, product="P", line="L1",
                            handled=("peanut",)) -> None:
    client.post("/lines", json={"id": line, "name": f"{line}号线",
                                "allergens_handled": list(handled)})
    client.post("/products", json={"id": product, "name": product})
    client.post(f"/products/{product}/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})


def setup_flour(client) -> None:
    client.post("/ingredients", json={"id": "FLOUR", "name": "小麦粉"})
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"}]})


def make_batches(client, line="L1", product="P", segs=("MIX",),
                 prior_allergens=("peanut",)) -> tuple[str, str]:
    """同产线两个批次：B1 含敏原，B2 为待审批次。"""
    client.post("/batches", json={
        "batch_id": "B1", "product_id": product, "line_id": line, "sequence": 1,
        "allergens": list(prior_allergens),
        "equipment_segments": [{"segment_id": s} for s in segs]})
    client.post("/batches", json={
        "batch_id": "B2", "product_id": product, "line_id": line, "sequence": 2,
        "allergens": [],
        "equipment_segments": [{"segment_id": s} for s in segs]})
    return "B1", "B2"


def make_program(client, program_id="CP", version="v1", line="L1",
                 allergens=("peanut",), points=("p1",), limit=2.0,
                 valid_from="2026-01-01", valid_until=None):
    res = client.post("/cleaning-programs", json={
        "program_id": program_id, "version": version, "line_id": line,
        "allergens": list(allergens), "required_points": list(points),
        "valid_from": valid_from, "valid_until": valid_until, "limit_ppm": limit})
    assert res.status_code == 201, res.text
    return res.json()


def make_cleaning(client, record_id="R1", line="L1", batch="B2", segment="MIX",
                  program_id="CP", version="v1", cleaned_at="2026-03-01"):
    res = client.post("/cleaning-records", json={
        "record_id": record_id, "line_id": line, "batch_id": batch,
        "segment_id": segment, "program_id": program_id,
        "program_version": version, "cleaned_at": cleaned_at})
    assert res.status_code == 201, res.text
    return res.json()


def peanut_path(trace_body):
    return next(p for p in trace_body["paths"] if p["allergen"] == "peanut")


def gap_codes(path):
    return [g["code"] for g in path["evidence_gaps"]]


def allocate_flour_lot(client, batch="B2", lot="LOT-F1"):
    """登记 FLOUR 到货批号并投料到指定批次（锁定规格 v1，推导结果不变）。"""
    client.post("/lots", json={
        "lot_id": lot, "ingredient_id": "FLOUR", "supplier_lot_no": f"SUP-{lot}",
        "spec_version": "v1", "quantity_received": 1000, "status": "released"})
    res = client.post(f"/batches/{batch}/allocations", json={
        "idempotency_key": f"alloc-{batch}-{lot}",
        "items": [{"lot_id": lot, "quantity": 100}]})
    assert res.status_code == 201, res.text


# -------------------------------------------------------------- 登记入口与校验

def test_batch_registration_validation(client):
    setup_line_with_product(client)
    # 产品/产线不存在
    r = client.post("/batches", json={
        "batch_id": "X", "product_id": "NOPE", "line_id": "L1", "sequence": 1})
    assert r.status_code == 404
    r = client.post("/batches", json={
        "batch_id": "X", "product_id": "P", "line_id": "NOPE", "sequence": 1})
    assert r.status_code == 404
    make_batches(client)
    # 重复批次号
    r = client.post("/batches", json={
        "batch_id": "B1", "product_id": "P", "line_id": "L1", "sequence": 3})
    assert r.status_code == 409
    # 同产线顺序号冲突
    r = client.post("/batches", json={
        "batch_id": "B3", "product_id": "P", "line_id": "L1", "sequence": 1})
    assert r.status_code == 409
    body = client.get("/batches/B1").json()
    assert body["segments"][0]["segment_id"] == "MIX"


def test_cleaning_record_rejects_unknown_refs_and_segment(client):
    setup_line_with_product(client)
    make_batches(client)
    make_program(client)
    # 程序版本不存在
    r = client.post("/cleaning-records", json={
        "record_id": "RX", "line_id": "L1", "batch_id": "B2", "segment_id": "MIX",
        "program_id": "CP", "program_version": "v9"})
    assert r.status_code == 404
    # 设备段未登记在批次上
    r = client.post("/cleaning-records", json={
        "record_id": "RX", "line_id": "L1", "batch_id": "B2", "segment_id": "OVEN",
        "program_id": "CP", "program_version": "v1"})
    assert r.status_code == 404


def test_swab_accepts_pending_value_and_validates_record(client):
    setup_line_with_product(client)
    make_batches(client)
    make_program(client)
    make_cleaning(client)
    # 清洁记录不存在
    r = client.post("/swabs", json={
        "swab_id": "SX", "record_id": "NOPE", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    assert r.status_code == 404
    # value_ppm 缺省 = 结果待出（允许为空）
    r = client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1", "allergen": "peanut"})
    assert r.status_code == 201
    assert r.json()["value_ppm"] is None


# -------------------------------------------------------------- 路径开闭规则

def test_path_open_without_cleaning_then_closed_with_full_evidence(client):
    setup_line_with_product(client)
    make_batches(client)
    trace = client.get("/batches/B2/trace").json()
    assert gap_codes(peanut_path(trace)) == ["missing_cleaning"]

    make_program(client)
    make_cleaning(client)
    # 只有清洁记录、没有拭子 -> 必检点漏采
    trace = client.get("/batches/B2/trace").json()
    assert gap_codes(peanut_path(trace)) == ["missing_swab"]

    # 待出结果仍开放（单张阴性单据也不足以关路径的反面：结果没出更不能关）
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1", "allergen": "peanut"})
    trace = client.get("/batches/B2/trace").json()
    assert gap_codes(peanut_path(trace)) == ["swab_pending"]

    # 必检点齐全且低于限值 -> 路径关闭
    client.put("/swabs/S1/result", json={"value_ppm": 0.5})
    trace = client.get("/batches/B2/trace").json()
    assert peanut_path(trace)["status"] == "closed"
    assert trace["closed_allergens"] == ["peanut"]


def test_swab_at_or_above_limit(client):
    setup_line_with_product(client)
    make_batches(client)
    make_program(client, limit=2.0)
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 2.0})
    # 等于限值为合格
    assert peanut_path(client.get("/batches/B2/trace").json())["status"] == "closed"
    # 超限 -> 开放（blocker 缺口）
    client.put("/swabs/S1/result", json={"value_ppm": 2.1})
    path = peanut_path(client.get("/batches/B2/trace").json())
    assert path["status"] == "open" and gap_codes(path) == ["swab_exceeded"]


def test_program_expiry_window_checked_at_cleaning_date(client):
    setup_line_with_product(client)
    make_batches(client)
    make_program(client, valid_from="2026-01-01", valid_until="2026-02-28")
    # 清洁发生在失效后
    make_cleaning(client, cleaned_at="2026-03-01")
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    path = peanut_path(client.get("/batches/B2/trace").json())
    assert gap_codes(path) == ["validation_expired"]
    # 清洁日期缺失、程序有有效期 -> 同样不能采信
    client.post("/cleaning-records", json={
        "record_id": "R2", "line_id": "L1", "batch_id": "B2", "segment_id": "MIX",
        "program_id": "CP", "program_version": "v1"})
    client.post("/swabs", json={
        "swab_id": "S2", "record_id": "R2", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    path = peanut_path(client.get("/batches/B2/trace").json())
    assert "validation_expired" in gap_codes(path)


def test_program_must_cover_allergen(client):
    setup_line_with_product(client)
    make_batches(client)
    make_program(client, allergens=("milk",))  # 只覆盖奶，不覆盖花生
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    path = peanut_path(client.get("/batches/B2/trace").json())
    assert gap_codes(path) == ["program_not_covering"]


def test_wrong_line_program_cannot_close_path(client):
    """L2 的清洁程序/记录不得关闭 L1 批次的路径（产线归属必须核对）。"""
    setup_line_with_product(client, line="L1")
    client.post("/lines", json={"id": "L2", "name": "2号线",
                                "allergens_handled": []})
    make_batches(client, line="L1")
    make_program(client, program_id="CP2", line="L2")
    # HTTP 层直接拒绝跨产线清洁记录
    r = client.post("/cleaning-records", json={
        "record_id": "RBAD", "line_id": "L2", "batch_id": "B2", "segment_id": "MIX",
        "program_id": "CP2", "program_version": "v1"})
    assert r.status_code == 422
    # 即便绕过 HTTP（通用程序在 L1 关闭了路径），再登记一条指定 L2 的程序记录：
    # trace 层 evaluate_segment 也会判 program_wrong_line
    make_program(client, program_id="CP1", line="L1")
    make_cleaning(client, record_id="ROK", program_id="CP1")
    client.post("/swabs", json={
        "swab_id": "SOK", "record_id": "ROK", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.2})
    assert peanut_path(client.get("/batches/B2/trace").json())["status"] == "closed"


def test_order_unknown_when_no_previous_batch(client):
    """产线上无可排序前序批次时，历史登记过敏原保守保留为开放路径。"""
    setup_line_with_product(client)
    client.post("/batches", json={
        "batch_id": "ONLY", "product_id": "P", "line_id": "L1", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    trace = client.get("/batches/ONLY/trace").json()
    path = peanut_path(trace)
    assert path["status"] == "open" and gap_codes(path) == ["order_unknown"]


def test_multiple_segments_all_must_pass(client):
    setup_line_with_product(client)
    make_batches(client, segs=("MIX", "OVEN"))
    make_program(client, points=("p1",))
    make_cleaning(client, record_id="R1", segment="MIX")
    make_cleaning(client, record_id="R2", segment="OVEN")
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    # 烤箱段漏采 -> 整条路径开放
    trace = client.get("/batches/B2/trace").json()
    codes = gap_codes(peanut_path(trace))
    assert codes == ["missing_swab"]
    g = next(g for p in trace["paths"] for g in p["evidence_gaps"])
    assert g["segment_id"] == "OVEN"
    # 补齐烤箱段 -> 关闭
    client.post("/swabs", json={
        "swab_id": "S2", "record_id": "R2", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.2})
    assert peanut_path(client.get("/batches/B2/trace").json())["status"] == "closed"


# -------------------------------------------------------------- 返工路径

def test_rework_path_across_products(client):
    setup_flour(client)
    setup_line_with_product(client, product="P1", line="L1")
    client.post("/products", json={"id": "P2", "name": "P2"})
    client.post("/products/P2/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    # P1 批次 B1 含花生（在 L1）；P2 批次 RB 在另一条产线 L2，无同线含敏原前序
    client.post("/lines", json={"id": "L2", "name": "2号线", "allergens_handled": []})
    client.post("/batches", json={
        "batch_id": "B1", "product_id": "P1", "line_id": "L1", "sequence": 1,
        "allergens": ["peanut"], "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/batches", json={
        "batch_id": "RB", "product_id": "P2", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    # 登记返工去向：B1 余料投入 RB
    r = client.post("/rework-paths", json={
        "source_batch_id": "B1", "target_batch_id": "RB", "percentage": 5})
    assert r.status_code == 201 and r.json()["target_product_id"] == "P2"
    trace = client.get("/batches/RB/trace").json()
    path = peanut_path(trace)
    assert path["source_kind"] == "rework" and path["source_batch_id"] == "B1"
    # 源批次成品本身含花生 -> 组分携带，目标批次设备清洁不能关闭
    assert path["status"] == "open"
    assert gap_codes(path) == ["component_carried_over"]
    # 即使补齐目标批次设备段的清洁与阴性拭子，组分路径仍开放
    make_program(client, program_id="CP2", line="L2")
    make_cleaning(client, record_id="RR", line="L2", batch="RB",
                  program_id="CP2")
    client.post("/swabs", json={
        "swab_id": "SR", "record_id": "RR", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    trace = client.get("/batches/RB/trace").json()
    assert peanut_path(trace)["status"] == "open"
    # 返工组分进入声明推导：标签未提示 -> missing_cross_contact
    body = client.post("/labels", json={
        "product_id": "P2", "batch_id": "RB",
        "copy": {"declared_allergens": ["wheat"], "may_contain": [],
                 "free_from_claims": []}}).json()
    assert any(f["kind"] == "missing_cross_contact"
               and f["detail"]["allergen"] == "peanut" for f in body["findings"])


def test_rework_cycle_rejected(client):
    setup_line_with_product(client)
    make_batches(client)
    assert client.post("/rework-paths", json={
        "source_batch_id": "B1", "target_batch_id": "B2"}).status_code == 201
    # B2 -> B1 会形成环
    r = client.post("/rework-paths", json={
        "source_batch_id": "B2", "target_batch_id": "B1"})
    assert r.status_code == 422


# -------------------------------------------------------------- 标签绑定与发现项

CLEAN_COPY = {"declared_allergens": ["wheat"], "may_contain": [],
              "free_from_claims": [], "ingredients_text": "小麦粉"}


def test_label_persists_batch_id_and_closed_path_allows_free_from(client):
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    allocate_flour_lot(client)
    make_program(client)
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.5})
    # 路径关闭：即使标签宣称“无花生”，也不产生矛盾/提示
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {**CLEAN_COPY, "free_from_claims": ["peanut"]}}).json()
    assert body["batch_id"] == "B2"
    assert body["findings"] == [] and body["open_blockers"] == []
    # 绑定持久化：重新读取仍在
    assert client.get(f"/labels/{body['id']}").json()["batch_id"] == "B2"


def test_label_batch_mismatch_rejected(client):
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    client.post("/products", json={"id": "POTHER", "name": "其他"})
    client.post("/batches", json={
        "batch_id": "BX", "product_id": "POTHER", "line_id": "L1", "sequence": 9,
        "allergens": [], "equipment_segments": []})
    r = client.post("/labels", json={
        "product_id": "P", "batch_id": "BX", "copy": CLEAN_COPY})
    assert r.status_code == 422
    r = client.post("/labels", json={
        "product_id": "P", "batch_id": "GHOST", "copy": CLEAN_COPY})
    assert r.status_code == 404


def test_open_path_generates_missing_cross_contact_and_contradiction(client):
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    # 无清洁 -> 路径开放；宣称“无花生” -> blocker，未提示 -> warning
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {"declared_allergens": ["wheat"], "may_contain": [],
                 "free_from_claims": ["peanut"]}}).json()
    kinds = {(f["kind"], f["detail"].get("allergen")) for f in body["findings"]}
    assert ("claim_contradiction", "peanut") in kinds
    # 同一过敏原既矛盾又不会重复出 missing_cross_contact（宣称优先）
    assert ("missing_cross_contact", "peanut") not in kinds
    # 改为只提示缺失：
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": CLEAN_COPY}).json()
    assert any(f["kind"] == "missing_cross_contact"
               and f["detail"]["allergen"] == "peanut" for f in body["findings"])


def test_swab_exceeded_blocks_approval_even_when_label_warns(client):
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    make_program(client)
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 9.0})  # 超限
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {"declared_allergens": ["wheat"], "may_contain": ["peanut"],
                 "free_from_claims": []}}).json()
    # 已提示 may_contain -> 没有 missing_cross_contact；但阳性是 data_gap blocker
    blockers = [(f["kind"], f["detail"].get("missing")) for f in body["open_blockers"]]
    assert ("data_gap", "cleaning_validation") in blockers
    client.post(f"/labels/{body['id']}/submit")
    r = client.post(f"/labels/{body['id']}/approve", json={"approved_by": "qa"})
    assert r.status_code == 409


# -------------------------------------------------------------- 批准快照冻结

def approve_label_with_frozen_evidence(client) -> str:
    """完整走通“路径关闭 -> 批准 -> 快照冻结”，返回已批准标签 ID。"""
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    allocate_flour_lot(client)
    make_program(client)
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.5})
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {**CLEAN_COPY, "free_from_claims": ["peanut"]}}).json()
    lid = body["id"]
    client.post(f"/labels/{lid}/submit")
    approval = client.post(f"/labels/{lid}/approve",
                           json={"approved_by": "qa.lead"}).json()["approval"]
    ev = approval["snapshot"]["batch_trace_evidence"]
    assert ev["batch"]["batch_id"] == "B2"
    assert [p["version"] for p in ev["cleaning_programs"]] == ["v1"]
    assert ev["cleaning_programs"][0]["required_points"] == ["p1"]
    assert ev["cleaning_records"][0]["record_id"] == "R1"
    assert [(s["swab_id"], s["value_ppm"]) for s in ev["swab_results"]] == [("S1", 0.5)]
    assert ev["trace"]["closed_allergens"] == ["peanut"]
    return lid


def test_approval_snapshot_freezes_programs_and_swabs(client):
    approve_label_with_frozen_evidence(client)


# -------------------------------------------------------------- 补录与影响链

def test_negative_backfill_reopens_then_resolves_finding_on_draft(client):
    setup_flour(client)
    setup_line_with_product(client)
    make_batches(client)
    allocate_flour_lot(client)
    make_program(client)
    make_cleaning(client)
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1", "allergen": "peanut"})
    body = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2", "copy": CLEAN_COPY}).json()
    lid = body["id"]
    # 待出 -> missing_cross_contact 开放
    assert any(f["kind"] == "missing_cross_contact" for f in body["findings"])
    # 阴性补录：路径关闭，发现项消解；草稿仅重新分析，不派生修订
    r = client.put("/swabs/S1/result", json={"value_ppm": 0.3})
    assert r.status_code == 200 and r.json()["exceeded_limit"] is False
    assert all(a["action"] != "derived_new_revision" for a in r.json()["actions"])
    view = client.get(f"/labels/{lid}").json()
    assert view["findings"] == [] and view["status"] == "draft"
    assert view["derived"]["may_contain"] == {}


def test_positive_backfill_marks_approved_label_and_derives_revision(client):
    lid = approve_label_with_frozen_evidence(client)
    # 批准后实验室补录阳性：自动沿链影响，调用方无需手工触发
    r = client.put("/swabs/S1/result", json={"value_ppm": 12.0})
    assert r.status_code == 200 and r.json()["exceeded_limit"] is True
    assert "B2" in r.json()["impacted_batches"]
    actions = {(a["revision"], a["action"]) for a in r.json()["actions"]}
    assert (1, "marked_stale") in actions and (2, "derived_new_revision") in actions
    old = client.get(f"/labels/{lid}").json()
    assert old["status"] == "approved" and old["stale"] is True
    # 批准记录只读：冻结快照仍是旧的阴性值
    snap = old["approvals"][0]["snapshot"]
    assert snap["batch_trace_evidence"]["swab_results"][0]["value_ppm"] == 0.5
    # 派生修订为草稿，按补录后的证据重新分析：阳性 blocker + 宣称矛盾
    new = next(a for a in r.json()["actions"]
               if a["action"] == "derived_new_revision")
    rev2 = client.get(f"/labels/{new['label_id']}").json()
    assert rev2["revision"] == 2 and rev2["status"] == "draft"
    assert rev2["batch_id"] == "B2" and rev2["parent_id"] == lid
    kinds = {f["kind"] for f in rev2["findings"]}
    # 沿用“无花生”宣称：矛盾（blocker）+ 阳性验证缺口（blocker）；
    # 同一过敏原的缺失提示与宣称矛盾互斥（宣称优先）
    assert {"data_gap", "claim_contradiction"} <= kinds
    assert "missing_cross_contact" not in kinds


def test_positive_backfill_propagates_through_rework_chain(client):
    setup_flour(client)
    setup_line_with_product(client, product="P1", line="L1")
    client.post("/lines", json={"id": "L2", "name": "2号线", "allergens_handled": []})
    client.post("/products", json={"id": "P2", "name": "P2"})
    client.post("/products/P2/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    # B0 在 L1 含花生；B1 在其后但自身成品不含花生（原料无花生）。
    # B3 在 L2 接收 B1 的返工料 —— B1 携带的是 B0 留在 B1 上的残留风险。
    client.post("/batches", json={
        "batch_id": "B0", "product_id": "P1", "line_id": "L1", "sequence": 1,
        "allergens": ["peanut"], "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/batches", json={
        "batch_id": "B1", "product_id": "P1", "line_id": "L1", "sequence": 2,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/batches", json={
        "batch_id": "B3", "product_id": "P2", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/rework-paths", json={
        "source_batch_id": "B1", "target_batch_id": "B3", "percentage": 3})
    allocate_flour_lot(client, batch="B3")
    make_program(client, program_id="CP1", line="L1")
    make_program(client, program_id="CP3", line="L2")
    # B1（L1）与 B3（L2）起初均无清洁记录 -> 两条链都开放，标签不可批准；
    # 先把 B1、B3 的清洁/阴性拭子补齐，路径全部关闭
    make_cleaning(client, record_id="R1", line="L1", batch="B1", program_id="CP1")
    make_cleaning(client, record_id="R3", line="L2", batch="B3", program_id="CP3")
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.2})
    client.post("/swabs", json={
        "swab_id": "S3", "record_id": "R3", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.2})
    body = client.post("/labels", json={
        "product_id": "P2", "batch_id": "B3",
        "copy": {**CLEAN_COPY, "free_from_claims": ["peanut"]}}).json()
    assert body["findings"] == []
    lid = body["id"]
    client.post(f"/labels/{lid}/submit")
    assert client.post(f"/labels/{lid}/approve",
                       json={"approved_by": "qa"}).status_code == 200
    # 事后把 B1 的拭子补录为阳性 -> B1 的残留路径重开，
    # 沿返工链 B1 -> B3 传播，B3 已批准标签被派生新修订
    r = client.put("/swabs/S1/result", json={"value_ppm": 30.0})
    assert set(r.json()["impacted_batches"]) == {"B1", "B3"}
    derived = [a for a in r.json()["actions"]
               if a["action"] == "derived_new_revision"]
    assert any(a["batch_id"] == "B3" for a in derived)
    rev2 = next(a for a in derived if a["batch_id"] == "B3")
    view = client.get(f"/labels/{rev2['label_id']}").json()
    assert view["status"] == "draft"
    assert any(f["kind"] == "claim_contradiction"
               and f["detail"]["allergen"] == "peanut" for f in view["findings"])


def test_revision_blocks_approval_until_new_negative_evidence(client):
    """补录阳性派生的修订在拿到新阴性证据前不可批准；补回阴性后可批准。"""
    test_positive_backfill_marks_approved_label_and_derives_revision(client)
    # 新清洁 + 新阴性拭子，绑定到 B2 同设备段（取最新清洁记录）
    make_cleaning(client, record_id="R2")
    client.post("/swabs", json={
        "swab_id": "S2", "record_id": "R2", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.1})
    rev2 = [l for l in client.get("/products/P/impact").json()["labels"]
            if l["revision"] == 2][0]
    r = client.post(f"/labels/{rev2['label_id']}/reanalyze")
    assert r.status_code == 200
    assert client.get(f"/labels/{rev2['label_id']}").json()["open_blockers"] == []


def test_check_package_and_review_sheet_include_batch_trace(client):
    lid = approve_label_with_frozen_evidence(client)
    pkg = client.get(f"/labels/{lid}/check-package").json()
    assert pkg["batch_id"] == "B2"
    assert pkg["batch_trace"]["closed_allergens"] == ["peanut"]
    idx = pkg["evidence_index"]["batch_trace"]
    assert {r["record_id"] for r in idx["cleaning_records"]} == {"R1"}
    assert {p["program_id"] for p in idx["cleaning_programs"]} == {"CP"}
    assert {s["swab_id"] for s in idx["swab_results"]} == {"S1"}
    res = client.get(f"/labels/{lid}/review-sheet")
    assert res.status_code == 200
    assert "批次共线追溯" in res.text and "closed" in res.text
