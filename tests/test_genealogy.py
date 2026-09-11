"""投料谱系的端到端测试：

- 到货批号：供应商批号/规格/收货量/有效期/质检状态登记与校验；
- 投料分配门禁：未放行、已过期、余量不足、规格不符合配方均拒绝且不扣量；
- 幂等：同键重放复用原结果不重复扣量，同键内容冲突 409；
- 锁定规格展开：来源图按开工锁定的投料规格；用料缺项记证据空白，
  不悄悄采用最新规格；
- 供应商更正：沿扣料关系定位成品/标签/已发放卷标，列明涉事用量、
  声明差异与处置边界，其他成品保持原状态；
- 撤销：开工前撤销记反向流水恢复余量；批次查询/核对包/事件日志还原。
"""
import pytest
from fastapi.testclient import TestClient

from label_audit.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


# -------------------------------------------------------------- 夹具

def setup_world(client) -> None:
    """FLOUR v1（wheat/milk absent）+ SUGAR v1（soy）+ 产品 P（配方锁定 v1）+ 批次 B2。"""
    client.post("/ingredients", json={"id": "FLOUR", "name": "小麦粉"})
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "milk", "status": "absent"}]})
    client.post("/ingredients", json={"id": "SUGAR", "name": "白砂糖"})
    client.post("/ingredients/SUGAR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "soy", "status": "present"}]})
    client.post("/lines", json={"id": "L1", "name": "1号线", "allergens_handled": []})
    client.post("/products", json={"id": "P", "name": "曲奇"})
    client.post("/products/P/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 60},
                  {"ingredient_ref": "SUGAR", "version": "v1", "percentage": 40}]})
    client.post("/products/P/lines/L1")
    client.post("/batches", json={
        "batch_id": "B2", "product_id": "P", "line_id": "L1", "sequence": 1,
        "started_at": "2026-03-01", "allergens": [],
        "equipment_segments": [{"segment_id": "MIX"}]})


def make_lot(client, lot_id, ingredient="FLOUR", spec="v1", qty=100,
             supplier_no=None, expires="2026-12-31", release=True, **over):
    body = {"lot_id": lot_id, "ingredient_id": ingredient,
            "supplier_lot_no": supplier_no or f"SUP-{lot_id}",
            "spec_version": spec, "quantity_received": qty,
            "received_at": "2026-02-01", "expires_at": expires}
    body.update(over)
    res = client.post("/lots", json=body)
    assert res.status_code == 201, res.text
    if release:
        res = client.put(f"/lots/{lot_id}/status", json={"status": "released"})
        assert res.status_code == 200, res.text
    return client.get(f"/lots/{lot_id}").json()


def allocate(client, batch, key, items):
    return client.post(f"/batches/{batch}/allocations",
                       json={"idempotency_key": key, "items": items})


def allocate_ok(client, batch, key, items):
    res = allocate(client, batch, key, items)
    assert res.status_code == 201, res.text
    return res.json()


def make_label(client, copy=None, batch="B2"):
    copy = copy or {"declared_allergens": ["wheat", "soy"], "may_contain": [],
                    "free_from_claims": [], "ingredients_text": "小麦粉、白砂糖"}
    res = client.post("/labels", json={"product_id": "P", "batch_id": batch,
                                       "copy": copy})
    assert res.status_code == 201, res.text
    return res.json()


# -------------------------------------------------------------- 到货批号登记

def test_lot_registration_and_status_flow(client):
    setup_world(client)
    lot = make_lot(client, "LOT-F1", release=False)
    # 默认待检；余量等于收货量
    assert lot["status"] == "pending"
    assert lot["quantity_received"] == 100
    assert lot["allocated_quantity"] == 0 and lot["remaining_quantity"] == 100
    assert lot["spec_version"] == "v1" and lot["supplier_lot_no"] == "SUP-LOT-F1"
    # 待检 -> 放行 -> 隔离 -> 再放行
    for status in ("released", "quarantined", "released"):
        r = client.put("/lots/LOT-F1/status",
                       json={"status": status, "reason": f"qc->{status}"})
        assert r.status_code == 200 and r.json()["status"] == status
    # 事件日志还原状态变更
    changes = [e for e in client.get("/events").json()
               if e["kind"] == "lot_status_changed"]
    assert [c["payload"]["new_status"] for c in changes] == \
        ["released", "quarantined", "released"]
    assert changes[0]["payload"]["reason"] == "qc->released"


def test_lot_registration_validation(client):
    setup_world(client)
    # 原料不存在 / 规格版本不存在
    assert client.post("/lots", json={
        "lot_id": "X1", "ingredient_id": "GHOST", "supplier_lot_no": "S1",
        "spec_version": "v1", "quantity_received": 10}).status_code == 404
    assert client.post("/lots", json={
        "lot_id": "X1", "ingredient_id": "FLOUR", "supplier_lot_no": "S1",
        "spec_version": "v9", "quantity_received": 10}).status_code == 404
    # 收货量必须为正、状态枚举合法
    assert client.post("/lots", json={
        "lot_id": "X1", "ingredient_id": "FLOUR", "supplier_lot_no": "S1",
        "spec_version": "v1", "quantity_received": 0}).status_code == 422
    assert client.post("/lots", json={
        "lot_id": "X1", "ingredient_id": "FLOUR", "supplier_lot_no": "S1",
        "spec_version": "v1", "quantity_received": 1,
        "status": "shipped"}).status_code == 422
    # 有效期早于收货时刻
    assert client.post("/lots", json={
        "lot_id": "X1", "ingredient_id": "FLOUR", "supplier_lot_no": "S1",
        "spec_version": "v1", "quantity_received": 1,
        "received_at": "2026-05-01", "expires_at": "2026-01-01"}).status_code == 422
    make_lot(client, "LOT-F1", release=False)
    # 批号重复 / 同一原料供应商批号重复
    assert client.post("/lots", json={
        "lot_id": "LOT-F1", "ingredient_id": "FLOUR", "supplier_lot_no": "OTHER",
        "spec_version": "v1", "quantity_received": 1}).status_code == 409
    assert client.post("/lots", json={
        "lot_id": "LOT-F2", "ingredient_id": "FLOUR", "supplier_lot_no": "SUP-LOT-F1",
        "spec_version": "v1", "quantity_received": 1}).status_code == 409
    # 同一供应商批号可属于不同原料（不同原料各自唯一）
    assert client.post("/lots", json={
        "lot_id": "LOT-S1", "ingredient_id": "SUGAR", "supplier_lot_no": "SUP-LOT-F1",
        "spec_version": "v1", "quantity_received": 1}).status_code == 201
    assert client.get("/lots/NOPE").status_code == 404
    assert client.put("/lots/NOPE/status",
                      json={"status": "released"}).status_code == 404


# -------------------------------------------------------------- 分配门禁

def test_allocation_gate_rejects_unreleased_expired_insufficient_and_mismatch(client):
    setup_world(client)
    make_lot(client, "LOT-PEND", release=False)                       # 待检
    make_lot(client, "LOT-Q", release=False)
    client.put("/lots/LOT-Q/status", json={"status": "quarantined"})  # 隔离
    make_lot(client, "LOT-EXP", expires="2026-01-31",
             received_at="2025-06-01")  # 已过期（开工 2026-03-01）
    make_lot(client, "LOT-OK", qty=30)
    # 规格 v2 批号（配方锁定 v1）-> 规格不符
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"}]})
    make_lot(client, "LOT-V2", spec="v2")
    # 不在配方中的原料
    client.post("/ingredients", json={"id": "MILK", "name": "乳粉"})
    client.post("/ingredients/MILK/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "milk", "status": "present"}]})
    make_lot(client, "LOT-M", ingredient="MILK")

    def codes(res):
        assert res.status_code == 409, res.text
        return {f["code"] for f in res.json()["detail"]["failures"]}

    assert codes(allocate(client, "B2", "g1", [{"lot_id": "LOT-PEND", "quantity": 1}])) == \
        {"not_released"}
    assert codes(allocate(client, "B2", "g2", [{"lot_id": "LOT-Q", "quantity": 1}])) == \
        {"not_released"}
    assert codes(allocate(client, "B2", "g3", [{"lot_id": "LOT-EXP", "quantity": 1}])) == \
        {"expired"}
    r = allocate(client, "B2", "g4", [{"lot_id": "LOT-OK", "quantity": 31}])
    assert codes(r) == {"insufficient_quantity"}
    assert r.json()["detail"]["failures"][0]["remaining_quantity"] == 30
    assert codes(allocate(client, "B2", "g5", [{"lot_id": "LOT-V2", "quantity": 1}])) == \
        {"spec_mismatch"}
    assert codes(allocate(client, "B2", "g6", [{"lot_id": "LOT-M", "quantity": 1}])) == \
        {"ingredient_not_in_recipe"}
    assert codes(allocate(client, "B2", "g7", [{"lot_id": "GHOST", "quantity": 1}])) == \
        {"lot_unknown"}
    # 一次请求多个批号：任一不符整体拒绝，合规批号也不扣量
    r = allocate(client, "B2", "g8", [{"lot_id": "LOT-OK", "quantity": 10},
                                      {"lot_id": "LOT-PEND", "quantity": 1}])
    assert codes(r) == {"not_released"}
    assert client.get("/lots/LOT-OK").json()["remaining_quantity"] == 30
    # 同请求内相同批号用量合并核对余量
    assert codes(allocate(client, "B2", "g9", [{"lot_id": "LOT-OK", "quantity": 20},
                                               {"lot_id": "LOT-OK", "quantity": 20}])) == \
        {"insufficient_quantity"}
    # 批次不存在
    assert allocate(client, "NOPE", "g10",
                    [{"lot_id": "LOT-OK", "quantity": 1}]).status_code == 404


def test_allocation_success_deducts_and_idempotent_replay(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    make_lot(client, "LOT-S1", ingredient="SUGAR", qty=50)
    # 一次请求分配多个批号
    res = allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 40},
                                           {"lot_id": "LOT-S1", "quantity": 20}])
    assert res["reused"] is False
    assert {a["lot_id"] for a in res["allocations"]} == {"LOT-F1", "LOT-S1"}
    assert all(a["spec_version"] == "v1" and a["status"] == "active"
               for a in res["allocations"])
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 60
    assert client.get("/lots/LOT-S1").json()["remaining_quantity"] == 30
    # 同键同内容重放：复用原结果，不重复扣量
    replay = allocate(client, "B2", "k1", [{"lot_id": "LOT-S1", "quantity": 20},
                                           {"lot_id": "LOT-F1", "quantity": 40}])
    assert replay.status_code == 200
    body = replay.json()
    assert body["reused"] is True
    assert [a["allocation_id"] for a in body["allocations"]] == \
        [a["allocation_id"] for a in res["allocations"]]
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 60
    # 同键内容冲突：409，已扣量不可倒扣
    conflict = allocate(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 41}])
    assert conflict.status_code == 409
    assert "不可倒扣" in conflict.json()["detail"]["error"]
    # 不同键继续扣量
    allocate_ok(client, "B2", "k2", [{"lot_id": "LOT-F1", "quantity": 15}])
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 45
    # 批次查询还原扣量与流水
    view = client.get("/batches/B2").json()
    assert len(view["allocations"]) == 3
    assert [(e["kind"], e["lot_id"], e["quantity"]) for e in view["lot_ledger"]] == [
        ("allocate", "LOT-F1", 40), ("allocate", "LOT-S1", 20),
        ("allocate", "LOT-F1", 15)]
    # 批号视图同样带分配与流水
    lot = client.get("/lots/LOT-F1").json()
    assert lot["allocated_quantity"] == 55
    assert len(lot["allocations"]) == 2 and len(lot["ledger"]) == 2


# -------------------------------------------------------------- 锁定规格展开

def test_source_graph_expands_from_locked_allocation_spec(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    make_lot(client, "LOT-S1", ingredient="SUGAR", qty=50)
    allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 40},
                                     {"lot_id": "LOT-S1", "quantity": 20}])
    # 供应商更正：FLOUR v2 新增 milk（此时无标签，影响传播为空动作）
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "milk", "status": "present"}]})
    # 绑定 B2 的来源图仍按开工锁定的 v1 展开：有 wheat、无 milk
    graph = client.get("/products/P/source-graph", params={"batch_id": "B2"}).json()
    assert set(graph["derived"]["required"]) == {"wheat", "soy"}
    flour = next(n for n in graph["graph"] if n["ingredient_id"] == "FLOUR")
    assert flour["version"] == "v1" and "LOT-F1" in flour["note"]
    ev = graph["derived"]["required"]["wheat"][0]
    assert ev["lot_ids"] == ["LOT-F1"]
    # 无投料记录的批次（原料已启用批号管理）：不得回退配方锁定/当前/最新
    # 规格，逐项记 material_allocation 证据空白，推导为空
    client.post("/batches", json={
        "batch_id": "B3", "product_id": "P", "line_id": "L1", "sequence": 2,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    graph3 = client.get("/products/P/source-graph", params={"batch_id": "B3"}).json()
    assert graph3["derived"]["required"] == {}
    gaps3 = [f for f in graph3["findings"]
             if f["detail"].get("missing") == "material_allocation"]
    assert {f["detail"]["ingredient_id"] for f in gaps3} == {"FLOUR", "SUGAR"}
    # 配方未锁定版本同样不得回退最新规格（v2 的 milk 不出现）
    client.post("/products", json={"id": "P3", "name": "威化"})
    client.post("/products/P3/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "percentage": 100}]})
    client.post("/batches", json={
        "batch_id": "B5", "product_id": "P3", "line_id": "L1", "sequence": 5,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    graph5 = client.get("/products/P3/source-graph", params={"batch_id": "B5"}).json()
    assert "milk" not in graph5["derived"]["required"]
    assert [f for f in graph5["findings"]
            if f["detail"].get("missing") == "material_allocation"]


def test_missing_usage_record_flags_evidence_gap(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    # 只投 FLOUR，SUGAR 用料缺项
    allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 40}])
    label = make_label(client)
    gaps = [f for f in label["open_blockers"]
            if f["kind"] == "data_gap"
            and f["detail"].get("missing") == "material_allocation"]
    assert [g["detail"]["ingredient_id"] for g in gaps] == ["SUGAR"]
    # 缺项原料不得悄悄采用最新规格：soy 不出现在推导中
    assert "soy" not in label["derived"]["required"]
    # 证据空白阻止批准
    client.post(f"/labels/{label['id']}/submit")
    assert client.post(f"/labels/{label['id']}/approve",
                       json={"approved_by": "qa"}).status_code == 409
    # 补登 SUGAR 投料后缺口消解，推导纳入 soy，可批准
    make_lot(client, "LOT-S1", ingredient="SUGAR", qty=50)
    allocate_ok(client, "B2", "k2", [{"lot_id": "LOT-S1", "quantity": 20}])
    res = client.post(f"/labels/{label['id']}/reanalyze").json()
    assert "soy" in res["derived"]["required"]
    assert client.post(f"/labels/{label['id']}/approve",
                       json={"approved_by": "qa"}).status_code == 200


# -------------------------------------------------------------- 供应商更正波及链

def test_supplier_correction_follows_consumption_chain(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    make_lot(client, "LOT-S1", ingredient="SUGAR", qty=50)
    allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 50},
                                     {"lot_id": "LOT-S1", "quantity": 20}])
    # 另一产品 P2 引用同一原料但批次无投料记录
    client.post("/lines", json={"id": "L2", "name": "2号线", "allergens_handled": []})
    client.post("/products", json={"id": "P2", "name": "饼干"})
    client.post("/products/P2/recipes", json={
        "version": "v1",
        "items": [{"ingredient_ref": "FLOUR", "version": "v1", "percentage": 100}]})
    client.post("/batches", json={
        "batch_id": "BX", "product_id": "P2", "line_id": "L2", "sequence": 1,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    # P 标签批准并发放卷标到 B2；P2 批次无投料记录 -> 证据空白阻止批准
    label = make_label(client)
    client.post(f"/labels/{label['id']}/submit")
    assert client.post(f"/labels/{label['id']}/approve",
                       json={"approved_by": "qa"}).status_code == 200
    client.post("/print-batches", json={
        "print_batch_id": "PB1", "label_id": label["id"], "quantity_received": 1000})
    r = client.post("/print-batches/PB1/issue", json={
        "production_batch_id": "B2", "quantity": 100, "idempotency_key": "iss-1"})
    assert r.status_code == 200, r.text
    label2 = client.post("/labels", json={
        "product_id": "P2", "batch_id": "BX",
        "copy": {"declared_allergens": ["wheat"]}}).json()
    gaps2 = [f for f in label2["open_blockers"]
             if f["detail"].get("missing") == "material_allocation"]
    assert [f["detail"]["ingredient_id"] for f in gaps2] == ["FLOUR"]
    client.post(f"/labels/{label2['id']}/submit")
    assert client.post(f"/labels/{label2['id']}/approve",
                       json={"approved_by": "qa"}).status_code == 409

    # 供应商更正：FLOUR 实际含奶（v1 声明 milk absent -> v2 声明 milk present）
    res = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "milk", "status": "present"}]})
    assert res.status_code == 201
    body = res.json()
    assert body["impacted_products"] == ["P"]
    corr = body["correction"]
    # 涉事批号与用量
    assert [l["lot_id"] for l in corr["affected_lots"]] == ["LOT-F1"]
    lot = corr["affected_lots"][0]
    assert lot["allocated_quantity"] == 50
    assert lot["consumed_by"] == [
        {"batch_id": "B2", "allocation_id": lot["consumed_by"][0]["allocation_id"],
         "quantity": 50}]
    # 声明差异
    assert corr["declaration_changes"] == [
        {"spec_version": "v1", "allergen": "milk",
         "old_status": "absent", "new_status": "present"}]
    assert corr["affected_batches"] == ["B2"]
    assert corr["affected_products"] == ["P"]
    assert corr["corrected_versions"] == ["v1"]
    # 批次无投料记录的产品不得判为未受影响：列入 consumption_unknown 证据空白
    assert corr["unaffected_products"] == []
    assert corr["consumption_unknown"] == [{
        "product_id": "P2",
        "unrecorded_batches": ["BX"],
        "evidence_gap": "material_allocation",
        "detail": "批次无该原料的有效投料记录，无法证明未消耗被更正规格，"
                  "不得判为未受影响"}]
    # 但未证明受影响的产品不强行传播：P2 标签保持原状态（复核中、未 stale）
    view2 = client.get(f"/labels/{label2['id']}").json()
    assert view2["status"] == "in_review" and view2["stale"] is False
    # 处置边界：P 标签 stale + 派生新修订；已发放卷标冻结并列出处置批次
    boundary = corr["disposition_boundary"]
    assert {l["action"] for l in boundary["labels"]} == \
        {"marked_stale", "derived_new_revision"}
    assert [p["print_batch_id"] for p in boundary["frozen_print_batches"]] == ["PB1"]
    assert boundary["issued_to_batches"][0]["production_batch_id"] == "B2"
    assert boundary["issued_to_batches"][0]["issued_quantity"] == 100
    view = client.get(f"/labels/{label['id']}").json()
    assert view["stale"] is True
    assert client.get("/print-batches/PB1").json()["status"] == "frozen"
    # 审计轨迹还原更正与波及链
    events = [e for e in client.get("/events").json()
              if e["kind"] == "supplier_correction"]
    assert len(events) == 1 and events[0]["payload"]["affected_batches"] == ["B2"]
    # 审查单还原锁定规格、扣量流水与更正声明差异
    sheet = client.get(f"/labels/{label['id']}/review-sheet").text
    assert "投料谱系" in sheet and "LOT-F1" in sheet
    assert "供应商更正波及" in sheet and "milk: absent→present（v1）" in sheet
    # 核对包还原扣量、锁定规格与波及链
    pkg = client.get(f"/labels/{label['id']}/check-package").json()
    gen = pkg["material_genealogy"]
    assert {a["lot_id"] for a in gen["allocations"]} == {"LOT-F1", "LOT-S1"}
    assert gen["spec_corrections"][0]["declaration_changes"][0]["allergen"] == "milk"
    # 批准快照冻结当时的投料批号/规格，不受更正改写
    snap = view["approvals"][0]["snapshot"]
    assert [(a["lot_id"], a["spec_version"], a["quantity"]) for a
            in snap["material_allocations"]] == [("LOT-F1", "v1", 50),
                                                 ("LOT-S1", "v1", 20)]


def test_correction_without_lots_falls_back_to_dependency_impact(client):
    """未启用批号管理时保持旧的依赖图影响传播。"""
    setup_world(client)
    label = make_label(client)
    client.post(f"/labels/{label['id']}/submit")
    client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa"})
    res = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "milk", "status": "present"}]})
    body = res.json()
    assert body["correction"] is None
    assert body["impacted_products"] == ["P"]
    assert client.get(f"/labels/{label['id']}").json()["stale"] is True


# -------------------------------------------------------------- 撤销分配

def test_reverse_allocation_writes_reverse_ledger_and_restores_quantity(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    # 未开工批次（无 started_at）允许撤销
    client.post("/batches", json={
        "batch_id": "B9", "product_id": "P", "line_id": "L1", "sequence": 9,
        "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
    res = allocate_ok(client, "B9", "r1", [{"lot_id": "LOT-F1", "quantity": 40}])
    alloc_id = res["allocations"][0]["allocation_id"]
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 60
    r = client.post(f"/allocations/{alloc_id}/reverse",
                    json={"reason": "计划变更，改投其他批号"})
    assert r.status_code == 200, r.text
    assert r.json()["allocation"]["status"] == "reversed"
    assert r.json()["reversal"]["kind"] == "reverse"
    assert r.json()["reversal"]["quantity"] == 40
    # 余量恢复；批次查询还原正向扣量与反向流水
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 100
    view = client.get("/batches/B9").json()
    assert [(e["kind"], e["quantity"]) for e in view["lot_ledger"]] == \
        [("allocate", 40), ("reverse", 40)]
    assert view["allocations"][0]["status"] == "reversed"
    # 全部撤销后批次回到“无有效投料记录”：原料已启用批号管理，来源图
    # 不得回退现行规格，逐项记 material_allocation 证据空白
    graph = client.get("/products/P/source-graph", params={"batch_id": "B9"}).json()
    gaps = [f for f in graph["findings"]
            if f["detail"].get("missing") == "material_allocation"]
    assert {f["detail"]["ingredient_id"] for f in gaps} == {"FLOUR", "SUGAR"}
    assert graph["derived"]["required"] == {}
    # 重复撤销 409；不存在 404；缺理由 422
    assert client.post(f"/allocations/{alloc_id}/reverse",
                       json={"reason": "again"}).status_code == 409
    assert client.post("/allocations/NOPE/reverse",
                       json={"reason": "x"}).status_code == 404
    assert client.post(f"/allocations/{alloc_id}/reverse",
                       json={"reason": ""}).status_code == 422
    # 事件日志记录反向流水
    events = [e for e in client.get("/events").json()
              if e["kind"] == "allocation_reversed"]
    assert events[0]["payload"]["quantity"] == 40


def test_reverse_rejected_after_batch_started(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    # B2 开工时刻 2026-03-01（过去）-> 不可撤销
    res = allocate_ok(client, "B2", "r1", [{"lot_id": "LOT-F1", "quantity": 10}])
    alloc_id = res["allocations"][0]["allocation_id"]
    r = client.post(f"/allocations/{alloc_id}/reverse", json={"reason": "晚于开工"})
    assert r.status_code == 409
    assert "已开工" in r.json()["detail"]["error"]
    assert client.get("/lots/LOT-F1").json()["remaining_quantity"] == 90


# -------------------------------------------------------------- 原子事务与并发

def test_concurrent_same_key_allocations_deduct_once(client):
    """并发同键请求：只扣量一次、只留一组分配与流水。"""
    setup_world(client)
    make_lot(client, "LOT-F1")
    import concurrent.futures

    def post(_):
        return client.post("/batches/B2/allocations", json={
            "idempotency_key": "race", "items": [{"lot_id": "LOT-F1", "quantity": 10}]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(post, range(8)))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1            # 恰好一个请求真正扣量
    assert statuses.count(200) == 7            # 其余复用原结果
    created = next(r for r in results if r.status_code == 201).json()
    for r in results:
        assert [a["allocation_id"] for a in r.json()["allocations"]] == \
            [a["allocation_id"] for a in created["allocations"]]
    lot = client.get("/lots/LOT-F1").json()
    assert lot["allocated_quantity"] == 10 and lot["remaining_quantity"] == 90
    view = client.get("/batches/B2").json()
    assert len(view["allocations"]) == 1
    assert [e["kind"] for e in view["lot_ledger"]] == ["allocate"]
    # 事件日志同样只有一组扣量记录
    assert len([e for e in client.get("/events").json()
                if e["kind"] == "lots_allocated"]) == 1


def test_conflict_and_gate_failure_leave_no_side_effects(client):
    """冲突/门禁失败路径不残留副作用：无分配、无流水、余量不变。"""
    setup_world(client)
    make_lot(client, "LOT-F1")
    allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 40}])
    before = client.get("/lots/LOT-F1").json()
    # 同键冲突
    assert allocate(client, "B2", "k1",
                    [{"lot_id": "LOT-F1", "quantity": 41}]).status_code == 409
    # 门禁失败（超量）
    assert allocate(client, "B2", "k2",
                    [{"lot_id": "LOT-F1", "quantity": 999}]).status_code == 409
    after = client.get("/lots/LOT-F1").json()
    assert (after["allocated_quantity"], after["remaining_quantity"]) == \
        (before["allocated_quantity"], before["remaining_quantity"])
    view = client.get("/batches/B2").json()
    assert len(view["allocations"]) == 1
    assert [e["kind"] for e in view["lot_ledger"]] == ["allocate"]


# -------------------------------------------------------------- 更正范围隔离（v1/v2 -> v3）

def test_correction_scope_isolated_to_corrected_version(client):
    """v1、v2 批号同时在库，v3 更正 v2：只波及 v2 批号的扣料关系。"""
    client.post("/ingredients", json={"id": "FLOUR", "name": "小麦粉"})
    for ver, decls in (("v1", [{"allergen": "wheat", "status": "present"}]),
                       ("v2", [{"allergen": "wheat", "status": "present"},
                               {"allergen": "sesame", "status": "may_contain"}])):
        client.post("/ingredients/FLOUR/versions", json={
            "version": ver, "supplier_declarations": decls})
    client.post("/lines", json={"id": "L1", "name": "1号线", "allergens_handled": []})
    # 两个产品配方分别锁定 v1 / v2，各自批次消耗对应批号
    for pid, ver, lot, batch, seq in (("P1", "v1", "LOT-V1", "B1", 1),
                                      ("P2", "v2", "LOT-V2", "B2", 2)):
        client.post("/products", json={"id": pid, "name": pid})
        client.post(f"/products/{pid}/recipes", json={
            "version": "r1",
            "items": [{"ingredient_ref": "FLOUR", "version": ver, "percentage": 100}]})
        client.post("/batches", json={
            "batch_id": batch, "product_id": pid, "line_id": "L1", "sequence": seq,
            "allergens": [], "equipment_segments": [{"segment_id": "MIX"}]})
        make_lot(client, lot, spec=ver)
        allocate_ok(client, batch, f"a-{batch}", [{"lot_id": lot, "quantity": 30}])
    # 两个产品的标签（声明 wheat + sesame 提示）均批准
    labels = {}
    for pid, batch in (("P1", "B1"), ("P2", "B2")):
        label = client.post("/labels", json={
            "product_id": pid, "batch_id": batch,
            "copy": {"declared_allergens": ["wheat"], "may_contain": ["sesame"]}}).json()
        client.post(f"/labels/{label['id']}/submit")
        assert client.post(f"/labels/{label['id']}/approve",
                           json={"approved_by": "qa"}).status_code == 200
        labels[pid] = label["id"]
    # v3 更正 v2（sesame 实为 present）：只波及 LOT-V2 -> B2 -> P2
    res = client.post("/ingredients/FLOUR/versions", json={
        "version": "v3", "corrects_version": "v2",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "sesame", "status": "present"}]})
    assert res.status_code == 201, res.text
    corr = res.json()["correction"]
    assert corr["corrected_versions"] == ["v2"]
    # v1 批号不得纳入 affected_lots
    assert [l["lot_id"] for l in corr["affected_lots"]] == ["LOT-V2"]
    assert corr["affected_lots"][0]["allocated_quantity"] == 30
    assert corr["affected_batches"] == ["B2"]
    assert corr["affected_products"] == ["P2"]
    assert corr["declaration_changes"] == [
        {"spec_version": "v2", "allergen": "sesame",
         "old_status": "may_contain", "new_status": "present"}]
    # P1 的投料记录证明其消耗的是 v1（非被更正规格）-> 未受影响且保持原状态
    assert corr["unaffected_products"] == ["P1"]
    assert corr["consumption_unknown"] == []
    assert client.get(f"/labels/{labels['P1']}").json()["stale"] is False
    assert client.get(f"/labels/{labels['P2']}").json()["stale"] is True
    # 缺省 corrects_version 时取登记前最新版本（v3）：v1/v2 批号均不波及
    res = client.post("/ingredients/FLOUR/versions", json={
        "version": "v4",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"},
                                  {"allergen": "sesame", "status": "present"}]})
    corr = res.json()["correction"]
    assert corr["corrected_versions"] == ["v3"]
    assert corr["affected_lots"] == [] and corr["affected_products"] == []
    assert sorted(corr["unaffected_products"]) == ["P1", "P2"]
    # corrects_version 校验：不存在 / 与新版本相同 -> 422
    assert client.post("/ingredients/FLOUR/versions", json={
        "version": "v5", "corrects_version": "v9",
        "supplier_declarations": []}).status_code == 422
    assert client.post("/ingredients/FLOUR/versions", json={
        "version": "v5", "corrects_version": "v5",
        "supplier_declarations": []}).status_code == 422


# -------------------------------------------------------------- 审查材料还原

def test_check_package_and_review_sheet_restore_genealogy(client):
    setup_world(client)
    make_lot(client, "LOT-F1")
    make_lot(client, "LOT-S1", ingredient="SUGAR", qty=50)
    allocate_ok(client, "B2", "k1", [{"lot_id": "LOT-F1", "quantity": 40},
                                     {"lot_id": "LOT-S1", "quantity": 20}])
    label = make_label(client)
    client.post(f"/labels/{label['id']}/submit")
    client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa"})
    pkg = client.get(f"/labels/{label['id']}/check-package").json()
    gen = pkg["material_genealogy"]
    assert gen["batch_id"] == "B2"
    assert {(a["lot_id"], a["quantity"]) for a in gen["allocations"]} == \
        {("LOT-F1", 40), ("LOT-S1", 20)}
    assert {l["lot_id"] for l in gen["lots"]} == {"LOT-F1", "LOT-S1"}
    assert [e["kind"] for e in gen["ledger"]] == ["allocate", "allocate"]
    assert gen["spec_corrections"] == []
    # 审查单含投料谱系小节
    sheet = client.get(f"/labels/{label['id']}/review-sheet").text
    assert "投料谱系" in sheet and "LOT-F1" in sheet and "SUP-LOT-F1" in sheet
    # 事件日志覆盖到货/状态/扣量
    kinds = [e["kind"] for e in client.get("/events").json()]
    for k in ("lot_registered", "lot_status_changed", "lots_allocated"):
        assert k in kinds
