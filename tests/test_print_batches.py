"""印刷标签批次领用放行的端到端测试：

- 入库登记：只允许关联 approved 修订，文案规范化摘要、适用产品校验；
- 放行门禁：approved / 非 stale / 产品匹配 / 失效 / 余量，
  待包装批次当前分析对照批准快照与印刷摘要，差异返回 409 + 差异路径；
- 幂等：重复请求复用原结果，内容冲突拒绝；已领用数量不可倒扣；
- 影响传播：撤回 / 规格变化 / 阳性拭子冻结剩余印刷批次并列出处置生产批次；
- 余量处置：报废或隔离须写理由；核对包与事件日志保留版本/数量/冻结原因。
"""
import pytest
from fastapi.testclient import TestClient

from label_audit.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


# -------------------------------------------------------------- 夹具

def setup_flour(client, *, wheat="present") -> None:
    client.post("/ingredients", json={"id": "FLOUR", "name": "小麦粉"})
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": wheat}]})


def allocate_flour_lot(client, batch, *, lot="LOT-F1", ingredient="FLOUR",
                       spec="v1", qty=100):
    """登记到货批号（放行，已存在则复用）并投料到指定批次，锁定规格与配方一致。"""
    if client.get(f"/lots/{lot}").status_code == 404:
        client.post("/lots", json={
            "lot_id": lot, "ingredient_id": ingredient,
            "supplier_lot_no": f"SUP-{lot}", "spec_version": spec,
            "quantity_received": 10000, "status": "released"})
    res = client.post(f"/batches/{batch}/allocations", json={
        "idempotency_key": f"alloc-{batch}", "items": [{"lot_id": lot, "quantity": qty}]})
    assert res.status_code == 201, res.text


def setup_world(client, *, swab=0.5, started="2026-03-01") -> str:
    """小麦粉(wheat) + L1 产线（前序 B1 含花生）+ B2 清洁/拭子齐全路径关闭。

    返回已批准（无花生、free_from peanut）的标签修订 ID。
    """
    setup_flour(client)
    client.post("/lines", json={"id": "L1", "name": "1号线",
                                "allergens_handled": ["peanut"]})
    client.post("/products", json={"id": "P", "name": "曲奇"})
    client.post("/products/P/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    client.post("/products/P/lines/L1")
    client.post("/batches", json={
        "batch_id": "B1", "product_id": "P", "line_id": "L1", "sequence": 1,
        "allergens": ["peanut"], "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/batches", json={
        "batch_id": "B2", "product_id": "P", "line_id": "L1", "sequence": 2,
        "started_at": started, "allergens": [],
        "equipment_segments": [{"segment_id": "MIX"}]})
    # 投料谱系：B2 的用料记录（锁定规格 v1，推导结果与旧回退一致）
    allocate_flour_lot(client, "B2")
    client.post("/cleaning-programs", json={
        "program_id": "CP", "version": "v1", "line_id": "L1",
        "allergens": ["peanut"], "required_points": ["p1"],
        "valid_from": "2026-01-01", "limit_ppm": 2.0})
    client.post("/cleaning-records", json={
        "record_id": "R1", "line_id": "L1", "batch_id": "B2", "segment_id": "MIX",
        "program_id": "CP", "program_version": "v1", "cleaned_at": "2026-03-01"})
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": swab})
    copy = {"declared_allergens": ["Wheat", "WHEAT"], "may_contain": [],
            "free_from_claims": ["peanut"], "ingredients_text": "  小麦粉 "}
    label = client.post("/labels",
                        json={"product_id": "P", "batch_id": "B2", "copy": copy}).json()
    assert label["open_blockers"] == []
    client.post(f"/labels/{label['id']}/submit")
    res = client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 200, res.text
    return label["id"]


def register_print_batch(client, label_id, pb_id="PB1", **over) -> dict:
    body = {"print_batch_id": pb_id, "label_id": label_id, "quantity_received": 1000,
            "received_at": "2026-03-02", "expires_at": "2026-12-31"}
    body.update(over)
    res = client.post("/print-batches", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def issue(client, pb_id, key, *, batch="B2", qty=200):
    return client.post(f"/print-batches/{pb_id}/issue", json={
        "production_batch_id": batch, "quantity": qty, "idempotency_key": key})


# -------------------------------------------------------------- 入库登记

def test_register_print_batch_normalizes_summary_from_approved_copy(client):
    lid = setup_world(client)
    pb = register_print_batch(client, lid)
    # 规范化：大小写归一、去重排序、配料表压缩空白
    assert pb["copy_summary"] == {
        "declared_allergens": ["wheat"], "may_contain": [],
        "free_from_claims": ["peanut"], "ingredients_text": "小麦粉"}
    assert pb["status"] == "available"
    assert pb["applicable_product_ids"] == ["P"]
    assert pb["remaining_quantity"] == 1000


def test_register_requires_approved_label_and_applicable_products(client):
    setup_world(client)
    # 未批准的草稿标签不能送印
    draft = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {"declared_allergens": ["wheat"]}}).json()
    r = client.post("/print-batches", json={
        "print_batch_id": "PBX", "label_id": draft["id"], "quantity_received": 10})
    assert r.status_code == 409
    # 标签不存在
    r = client.post("/print-batches", json={
        "print_batch_id": "PBX", "label_id": "nope", "quantity_received": 10})
    assert r.status_code == 404
    # 适用产品包含不存在的产品
    lid = client.get("/products/P/impact").json()["labels"][0]["label_id"]
    r = client.post("/print-batches", json={
        "print_batch_id": "PBX", "label_id": lid, "quantity_received": 10,
        "applicable_product_ids": ["P", "GHOST"]})
    assert r.status_code == 404
    # 适用产品不含标签所属产品
    client.post("/products", json={"id": "P2", "name": "其他"})
    r = client.post("/print-batches", json={
        "print_batch_id": "PBX", "label_id": lid, "quantity_received": 10,
        "applicable_product_ids": ["P2"]})
    assert r.status_code == 422
    # 重复批号
    register_print_batch(client, lid)
    r = client.post("/print-batches", json={
        "print_batch_id": "PB1", "label_id": lid, "quantity_received": 1})
    assert r.status_code == 409
    r = client.post("/print-batches", json={
        "print_batch_id": "PB2", "label_id": lid, "quantity_received": 10,
        "received_at": "2026-09-01", "expires_at": "2026-01-01"})
    assert r.status_code == 422


def test_register_rejects_summary_not_matching_approved_copy(client):
    lid = setup_world(client)
    # 混入旧版文案：多了 soy、少了无花生宣称 -> 422 且给出差异
    r = client.post("/print-batches", json={
        "print_batch_id": "PBOLD", "label_id": lid, "quantity_received": 500,
        "copy": {"declared_allergens": ["wheat", "soy"], "may_contain": [],
                 "free_from_claims": []}})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["error"]
    assert body["diff"]["declared_allergens"]["removed"] == ["soy"]
    assert body["diff"]["free_from_claims"]["added"] == ["peanut"]
    # 与批准文案一致的自报摘要允许入库
    r = client.post("/print-batches", json={
        "print_batch_id": "PBOK", "label_id": lid, "quantity_received": 500,
        "copy": {"declared_allergens": ["wheat"], "may_contain": [],
                 "free_from_claims": ["peanut"], "ingredients_text": "小麦粉"}})
    assert r.status_code == 201


# -------------------------------------------------------------- 放行门禁

def test_issue_happy_path_records_analysis_version_and_quantity(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    res = issue(client, "PB1", "key-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reused"] is False
    assert body["issuance"]["analysis_version"].startswith("ana-")
    assert body["issuance"]["label_revision"] == 1
    assert body["print_batch"]["issued_quantity"] == 200
    assert body["print_batch"]["remaining_quantity"] == 800
    # 事件日志记录放行采用的修订与分析版本
    ev = next(e for e in client.get("/events").json()
              if e["kind"] == "print_batch_issued")
    assert ev["payload"]["label_revision"] == 1
    assert ev["payload"]["analysis_version"] == body["issuance"]["analysis_version"]


def test_issue_blocks_non_approved_or_stale_label(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    # 供应商更换：小麦粉新规格多出 milk；登记新版本即沿依赖图传播
    r = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [
            {"allergen": "wheat", "status": "present"},
            {"allergen": "milk", "status": "present"}]})
    assert "P" in r.json()["impacted_products"]
    r = issue(client, "PB1", "key-x")
    assert r.status_code == 409
    paths = [d["path"] for d in r.json()["detail"]["differences"]]
    assert "label.stale" in paths
    # 标签撤回后再领用 -> label.status 差异
    client.post(f"/labels/{lid}/withdraw", json={"reason": "换版"})
    r = issue(client, "PB1", "key-y")
    assert r.status_code == 409
    assert r.json()["detail"]["differences"][0]["path"] == "label.status"


def test_issue_blocks_product_mismatch_and_unknown_batch(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    # 另一个产品的批次（独立产线 L2，无花生前序，避免共线差异干扰产品匹配核对）
    client.post("/lines", json={"id": "L2", "name": "2号线",
                                "allergens_handled": []})
    client.post("/products", json={"id": "P2", "name": "饼干"})
    client.post("/products/P2/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    client.post("/batches", json={
        "batch_id": "BX", "product_id": "P2", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    allocate_flour_lot(client, "BX")
    r = issue(client, "PB1", "key-x", batch="BX")
    assert r.status_code == 409
    assert r.json()["detail"]["differences"][0]["path"] == "product_match"
    # 多产品适用清单允许放行
    register_print_batch(client, lid, pb_id="PB2",
                         applicable_product_ids=["P", "P2"])
    r = issue(client, "PB2", "key-x", batch="BX")
    assert r.status_code == 200, r.text
    # 不存在的批次
    assert issue(client, "PB1", "key-y", batch="GHOST").status_code == 404


def test_issue_blocks_expired_lot_and_over_quantity(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    register_print_batch(client, lid, pb_id="PBF",
                         received_at="2025-06-01", expires_at="2026-01-31")
    r = issue(client, "PBF", "key-e")
    assert r.status_code == 409
    assert r.json()["detail"]["differences"][0]["path"] == "print_batch.expires_at"
    # 余量不足
    r = issue(client, "PB1", "key-big", qty=5000)
    assert r.status_code == 409
    assert r.json()["detail"]["differences"][0]["path"] == "remaining_quantity"
    # 无开工时刻的批次按当前日期核对失效
    client.post("/batches", json={
        "batch_id": "BN", "product_id": "P", "line_id": "L1", "sequence": 3,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    assert issue(client, "PBF", "key-e2", batch="BN").status_code == 409


def test_issue_409_differences_when_current_analysis_drifts(client):
    """新待包装批次清洁缺失 -> 当前分析多出 peanut 交叉接触，与批准快照不符。"""
    lid = setup_world(client)
    register_print_batch(client, lid)
    client.post("/batches", json={
        "batch_id": "B3", "product_id": "P", "line_id": "L1", "sequence": 3,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    allocate_flour_lot(client, "B3")
    r = issue(client, "PB1", "key-drift", batch="B3")
    assert r.status_code == 409
    paths = [d["path"] for d in r.json()["detail"]["differences"]]
    assert "derived.may_contain.extra[peanut]" in paths
    # 放行失败不写领用记录、不扣数量
    pb = client.get("/print-batches/PB1").json()
    assert pb["issued_quantity"] == 0 and pb["remaining_quantity"] == 1000


def test_print_summary_checked_against_approved_snapshot(client):
    """入库后标签被撤回并以新文案重新批准：旧印刷批次即便标签状态恢复可查，

    通过标签撤回/重批场景验证印刷摘要比对（差异走 print_summary 路径）。"""
    lid = setup_world(client)
    pb = register_print_batch(client, lid)
    # 撤回旧修订（冻结 PB1），用新文案（多声明 milk）批准 rev2
    client.post(f"/labels/{lid}/withdraw", json={"reason": "换版"})
    rev2 = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2",
        "copy": {"declared_allergens": ["wheat", "milk"],
                 "free_from_claims": ["peanut"]}}).json()
    assert rev2["revision"] == 2
    # rev2 因 milk 推导不出会有 warning，不阻止批准
    client.post(f"/labels/{rev2['id']}/submit")
    client.post(f"/labels/{rev2['id']}/approve", json={"approved_by": "qa"})
    # 直接核对摘要差异：旧批次摘要（wheat）对照新批准快照（wheat+milk）
    from label_audit import printing
    store = client.app.state.store
    fresh = store.get_print_batch("PB1")
    label2 = store.get_label(rev2["id"])
    batch = store.get_batch("B2")
    result = printing.evaluate_release(store, fresh, batch, label2)
    paths = {d["path"] for d in result["differences"]}
    assert "print_summary.declared_allergens.missing[milk]" in paths
    assert pb["print_batch_id"] == "PB1"


# -------------------------------------------------------------- 幂等与不可倒扣

def test_idempotent_reissue_reuses_original_result(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    first = issue(client, "PB1", "idem-1")
    assert first.status_code == 200
    second = issue(client, "PB1", "idem-1")
    assert second.status_code == 200
    a, b = first.json(), second.json()
    assert b["reused"] is True and a["reused"] is False
    assert b["issuance"]["issuance_id"] == a["issuance"]["issuance_id"]
    # 复用不重复扣减数量
    assert client.get("/print-batches/PB1").json()["issued_quantity"] == 200
    # 同键不同内容 -> 409（已领用数量不可倒扣）
    r = issue(client, "PB1", "idem-1", qty=100)
    assert r.status_code == 409
    assert "不可倒扣" in r.json()["detail"]["error"]
    # 同键用于另一印刷批次 -> 409
    register_print_batch(client, lid, pb_id="PB2")
    r = issue(client, "PB2", "idem-1")
    assert r.status_code == 409


def test_issued_quantity_can_only_increase_and_auto_close(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    assert issue(client, "PB1", "a", qty=600).status_code == 200
    assert issue(client, "PB1", "b", qty=400).status_code == 200
    pb = client.get("/print-batches/PB1").json()
    assert pb["remaining_quantity"] == 0 and pb["status"] == "closed"
    # 已结案批次不可再领用
    assert issue(client, "PB1", "c", qty=1).status_code == 409
    # 不存在冲正/倒扣入口：处置也只接受 frozen 批次
    r = client.post("/print-batches/PB1/dispose", json={
        "action": "scrap", "quantity": 1, "reason": "x"})
    assert r.status_code == 409


# -------------------------------------------------------------- 影响传播与冻结

def test_withdraw_freezes_remaining_and_lists_disposition_batches(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=300)
    r = client.post(f"/labels/{lid}/withdraw", json={"reason": "标签换版"})
    freeze = r.json()["print_freeze"]
    assert [f["print_batch_id"] for f in freeze["frozen_print_batches"]] == ["PB1"]
    assert freeze["frozen_print_batches"][0]["remaining_quantity"] == 700
    # 已领用它的生产批次进入处置清单
    assert freeze["disposition_batches"][0]["production_batch_id"] == "B2"
    assert freeze["disposition_batches"][0]["issued_quantity"] == 300
    pb = client.get("/print-batches/PB1").json()
    assert pb["status"] == "frozen"
    assert pb["frozen_reason"] == "标签撤回：标签换版"
    # 冻结后拒绝领用
    assert issue(client, "PB1", "k2").status_code == 409


def test_spec_change_freezes_print_batches_via_impact(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=100)
    # 供应商更换：小麦粉新规格多出 milk；登记新版本即沿依赖图传播
    r = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [
            {"allergen": "wheat", "status": "present"},
            {"allergen": "milk", "status": "present"}]})
    marked = [a for a in r.json()["actions"] if a["action"] == "marked_stale"]
    freeze = marked[0]["print_freeze"]
    assert [f["print_batch_id"] for f in freeze["frozen_print_batches"]] == ["PB1"]
    assert "新规格版本" in freeze["reason"]
    disp = freeze["disposition_batches"][0]
    assert disp["production_batch_id"] == "B2"
    assert disp["issued_print_batches"][0]["analysis_version"].startswith("ana-")


def test_positive_swab_backfill_freezes_print_batches(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=100)
    r = client.put("/swabs/S1/result", json={"value_ppm": 12.0})
    assert r.status_code == 200 and r.json()["exceeded_limit"] is True
    marked = [a for a in r.json()["actions"] if a["action"] == "marked_stale"]
    freeze = marked[0]["print_freeze"]
    assert [f["print_batch_id"] for f in freeze["frozen_print_batches"]] == ["PB1"]
    assert "阳性" in freeze["reason"]
    assert freeze["disposition_batches"][0]["production_batch_id"] == "B2"
    pb = client.get("/print-batches/PB1").json()
    assert pb["status"] == "frozen" and pb["remaining_quantity"] == 900
    # 阳性结果同时也是放行门禁差异（硬证据 blocker），冻结前若再来领用必被拦
    assert issue(client, "PB1", "k2").status_code == 409


def test_negative_backfill_does_not_freeze_print_batches(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1")
    r = client.put("/swabs/S1/result", json={"value_ppm": 0.2})
    assert r.json()["exceeded_limit"] is False
    assert all("print_freeze" not in a for a in r.json()["actions"])
    assert client.get("/print-batches/PB1").json()["status"] == "available"


# -------------------------------------------------------------- 余量处置

def test_frozen_remaining_can_be_scrapped_or_quarantined_with_reason(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=300)
    client.post(f"/labels/{lid}/withdraw", json={"reason": "换版"})
    # 非 frozen 状态不可处置（先验 available 的分支在自动结案用例中覆盖）
    # 隔离一部分
    r = client.post("/print-batches/PB1/dispose", json={
        "action": "quarantine", "quantity": 200,
        "reason": "等待供应商偏差调查结论"})
    assert r.status_code == 200
    assert r.json()["remaining_quantity"] == 500 and r.json()["status"] == "frozen"
    # 缺理由 -> 422；超量 -> 409；非法动作 -> 422
    assert client.post("/print-batches/PB1/dispose",
                       json={"action": "scrap", "quantity": 1, "reason": ""}).status_code == 422
    assert client.post("/print-batches/PB1/dispose",
                       json={"action": "scrap", "quantity": 999, "reason": "x"}).status_code == 409
    assert client.post("/print-batches/PB1/dispose",
                       json={"action": "burn", "quantity": 1, "reason": "x"}).status_code == 422
    # 报废剩余 -> 自动结案
    r = client.post("/print-batches/PB1/dispose", json={
        "action": "scrap", "quantity": 500, "reason": "调查确认旧文案失效，报废"})
    assert r.json()["status"] == "closed" and r.json()["remaining_quantity"] == 0
    assert [(d["action"], d["quantity"]) for d in r.json()["dispositions"]] == \
        [("quarantine", 200), ("scrap", 500)]


# -------------------------------------------------------------- 核对包与事件

def test_check_package_retains_print_control(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=300)
    client.post(f"/labels/{lid}/withdraw", json={"reason": "换版"})
    client.post("/print-batches/PB1/dispose", json={
        "action": "scrap", "quantity": 700, "reason": "旧版报废"})
    pkg = client.get(f"/labels/{lid}/check-package").json()
    pc = pkg["print_control"]
    assert pc["total_received"] == 1000
    assert pc["total_issued"] == 300 and pc["total_disposed"] == 700
    p = pc["print_batches"][0]
    # 放行采用的标签修订、分析版本、数量变化与冻结原因都保留
    assert p["frozen_reason"] == "标签撤回：换版"
    assert p["issuances"][0]["label_revision"] == 1
    assert p["issuances"][0]["analysis_version"].startswith("ana-")
    assert p["issuances"][0]["production_batch_id"] == "B2"
    assert p["dispositions"][0]["reason"] == "旧版报废"


def test_event_log_records_print_lifecycle(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1")
    client.post(f"/labels/{lid}/withdraw", json={"reason": "r"})
    client.post("/print-batches/PB1/dispose",
                json={"action": "scrap", "quantity": 800, "reason": "x"})
    kinds = [e["kind"] for e in client.get("/events").json()]
    for k in ("print_batch_registered", "print_batch_issued",
              "print_batch_frozen", "print_batch_disposed"):
        assert k in kinds


# -------------------------------------------------------------- 回归：跨产品按批次所属产品推导
def test_cross_product_issue_derives_from_batch_product_recipe(client):
    """跨产品领用必须按待包装批次所属产品的当前配方推导，而非标签所属产品。

    P 批准快照：required={wheat}；P2 的配方需要 milk（无 milk 供应商声明 ->
    视为资料缺口，同时快照 milk 缺失）。即使卷标适用清单包含 P2，放行也必须
    409 并给出差异路径，且不写领用记录、不扣减库存。
    """
    lid = setup_world(client)
    # P2：含乳制品（声明 present）的配方，独立产线无共线噪声
    client.post("/ingredients", json={"id": "MILK", "name": "全脂乳粉"})
    client.post("/ingredients/MILK/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "milk", "status": "present"}]})
    client.post("/lines", json={"id": "L2", "name": "2号线",
                                "allergens_handled": []})
    client.post("/products", json={"id": "P2", "name": "奶味饼干"})
    client.post("/products/P2/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "MILK", "version": "v1", "percentage": 100}]})
    client.post("/batches", json={
        "batch_id": "BM", "product_id": "P2", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    allocate_flour_lot(client, "BM", lot="LOT-M1", ingredient="MILK")
    register_print_batch(client, lid, pb_id="PBX",
                         applicable_product_ids=["P", "P2"])
    r = issue(client, "PBX", "k-cross", batch="BM")
    assert r.status_code == 409
    paths = {d["path"] for d in r.json()["detail"]["differences"]}
    # P2 当前分析多出 milk 应声明项；批准快照（P/wheat）没有
    assert "derived.required.extra[milk]" in paths
    # 放行失败不扣减库存、不留领用流水
    pb = client.get("/print-batches/PBX").json()
    assert pb["issued_quantity"] == 0 and pb["remaining_quantity"] == 1000
    assert pb["issuances"] == []
    # 同一卷标贴回配方兼容的 P/B2 仍可正常放行（门禁不是按产品名一刀切）
    ok = issue(client, "PBX", "k-ok", batch="B2")
    assert ok.status_code == 200, ok.text
    assert ok.json()["issuance"]["quantity"] == 200


def test_cross_product_issue_matching_derivation_succeeds(client):
    """跨产品但配方推导一致时可放行；分析版本应带待包装批次所属产品。"""
    lid = setup_world(client)
    client.post("/lines", json={"id": "L2", "name": "2号线",
                                "allergens_handled": []})
    client.post("/products", json={"id": "P3", "name": "原味饼干"})
    client.post("/products/P3/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    client.post("/batches", json={
        "batch_id": "BW", "product_id": "P3", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    allocate_flour_lot(client, "BW")
    register_print_batch(client, lid, pb_id="PBW",
                         applicable_product_ids=["P", "P3"])
    r = issue(client, "PBW", "k-w", batch="BW")
    assert r.status_code == 200, r.text
    # 分析版本对 P/B2 与 P3/BW 必须不同（产品维度进入哈希）
    r2 = issue(client, "PBW", "k-p", batch="B2")
    assert r2.status_code == 200, r2.text
    assert r.json()["issuance"]["analysis_version"] != \
        r2.json()["issuance"]["analysis_version"]


# -------------------------------------------------------------- 回归：stale 不可被分析/审查单清除
def test_stale_approved_revision_survives_reanalysis_review_sheet_and_registration(client):
    """规格变化把已批准修订标 stale 后：重新分析、生成审查单、再登记印刷批次

    都不得清除 stale；该修订下的任何领用始终 409。
    """
    lid = setup_world(client)
    register_print_batch(client, lid)
    issue(client, "PB1", "k1", qty=100)
    # 规格变化：小麦粉新规格多出 milk -> rev1 标 stale 并派生新修订
    r = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [
            {"allergen": "wheat", "status": "present"},
            {"allergen": "milk", "status": "present"}]})
    assert any(a["action"] == "marked_stale" for a in r.json()["actions"])

    def assert_stale():
        view = client.get(f"/labels/{lid}").json()
        assert view["status"] == "approved" and view["stale"] is True

    assert_stale()
    # 重新分析（显式端点）不得清除已批准修订的 stale
    assert client.post(f"/labels/{lid}/reanalyze").status_code == 200
    assert_stale()
    # 分析查询（GET /analysis 内部会重跑分析）同样不得清除
    assert client.get(f"/labels/{lid}/analysis").status_code == 200
    assert_stale()
    # 生成审查单也会刷新分析——stale 仍保留，且审查单明确标注“资料已过期”
    sheet = client.get(f"/labels/{lid}/review-sheet")
    assert sheet.status_code == 200 and "资料已过期" in sheet.text
    assert_stale()
    # stale 后新登记的印刷批次同样不能领用（批准修订仍为 stale）
    register_print_batch(client, lid, pb_id="PB2")
    r = issue(client, "PB2", "k2")
    assert r.status_code == 409
    assert [d["path"] for d in r.json()["detail"]["differences"]] == ["label.stale"]
    # 已登记的旧批次也仍被冻结（规格变化时自动冻结），领用同样拒绝
    r = issue(client, "PB1", "k3")
    assert r.status_code == 409
    # 库存维持冻结时的值，无新增扣减
    pb = client.get("/print-batches/PB1").json()
    assert pb["issued_quantity"] == 100 and pb["remaining_quantity"] == 900
    # 核对包仍记录 stale 状态
    pkg = client.get(f"/labels/{lid}/check-package").json()
    assert pkg["label"]["stale"] is True
