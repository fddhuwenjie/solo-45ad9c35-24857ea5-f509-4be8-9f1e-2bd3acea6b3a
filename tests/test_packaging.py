"""包装执行与卷标结算的端到端测试：

- 开工绑定：生产批次 / 领用记录 / 包装线 / 计划产量 / 每件用标数 + 清场发现；
  旧卷标未隔离、领用横跨不同标签修订、卷标已冻结、适用产品不符均阻止开工；
- 用标事件：合格品贴用 / 过程损耗 / 留样 / 退回隔离幂等追加，保留操作者与时刻；
  同键重放复用、同键冲突拒绝；applied 事件校验 贴用量 = 合格品数 × 每件用标数；
- 结算：领用量 = 贴用 + 损耗 + 留样 + 退回隔离 且 贴用量 = 合格品数 × 每件用标数
  同时满足才落记录；差异返回数量来源；退回量不补回印刷批次可领用余量；
- 盘点更正：结算后记录不可覆盖，调整事件追加并重新结算生成新记录；
- 撤回 / 规格更正：列出未结算现场余量、已包装数量与待隔离批次；
- 核对包与事件日志串起清场、用标与调整记录。
"""
import pytest
from fastapi.testclient import TestClient

from label_audit.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


# -------------------------------------------------------------- 夹具

def setup_world(client) -> str:
    """小麦粉(wheat) + L1 产线 + B2 清洁/拭子齐全，返回已批准标签修订 ID。"""
    client.post("/ingredients", json={"id": "FLOUR", "name": "小麦粉"})
    client.post("/ingredients/FLOUR/versions", json={
        "version": "v1",
        "supplier_declarations": [{"allergen": "wheat", "status": "present"}]})
    client.post("/lines", json={"id": "L1", "name": "1号线",
                                "allergens_handled": ["peanut"]})
    client.post("/lines", json={"id": "PK1", "name": "包装1线",
                                "allergens_handled": []})
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
        "started_at": "2026-03-01", "allergens": [],
        "equipment_segments": [{"segment_id": "MIX"}]})
    client.post("/lots", json={
        "lot_id": "LOT-F1", "ingredient_id": "FLOUR",
        "supplier_lot_no": "SUP-F1", "spec_version": "v1",
        "quantity_received": 10000, "status": "released"})
    client.post("/batches/B2/allocations", json={
        "idempotency_key": "alloc-B2", "items": [{"lot_id": "LOT-F1", "quantity": 100}]})
    client.post("/cleaning-programs", json={
        "program_id": "CP", "version": "v1", "line_id": "L1",
        "allergens": ["peanut"], "required_points": ["p1"],
        "valid_from": "2026-01-01", "limit_ppm": 2.0})
    client.post("/cleaning-records", json={
        "record_id": "R1", "line_id": "L1", "batch_id": "B2", "segment_id": "MIX",
        "program_id": "CP", "program_version": "v1", "cleaned_at": "2026-03-01"})
    client.post("/swabs", json={
        "swab_id": "S1", "record_id": "R1", "point_id": "p1",
        "allergen": "peanut", "value_ppm": 0.5})
    copy = {"declared_allergens": ["wheat"], "may_contain": [],
            "free_from_claims": ["peanut"], "ingredients_text": "小麦粉"}
    label = client.post("/labels",
                        json={"product_id": "P", "batch_id": "B2", "copy": copy}).json()
    client.post(f"/labels/{label['id']}/submit")
    res = client.post(f"/labels/{label['id']}/approve", json={"approved_by": "qa.lead"})
    assert res.status_code == 200, res.text
    return label["id"]


def register_print_batch(client, label_id, pb_id="PB1", **over) -> None:
    body = {"print_batch_id": pb_id, "label_id": label_id, "quantity_received": 1000,
            "received_at": "2026-03-02", "expires_at": "2026-12-31"}
    body.update(over)
    res = client.post("/print-batches", json=body)
    assert res.status_code == 201, res.text


def issue(client, pb_id, key, *, batch="B2", qty=200) -> str:
    res = client.post(f"/print-batches/{pb_id}/issue", json={
        "production_batch_id": batch, "quantity": qty, "idempotency_key": key})
    assert res.status_code == 200, res.text
    return res.json()["issuance"]["issuance_id"]


def start_run(client, iss_ids, *, run_id="RUN1", planned=80, per_unit=2,
              findings=None, batch="B2"):
    body = {"run_id": run_id, "production_batch_id": batch, "line_id": "PK1",
            "planned_quantity": planned, "labels_per_unit": per_unit,
            "issuance_ids": iss_ids, "operator": "op.zhang",
            "clearance_findings": findings or []}
    return client.post("/packaging-runs", json=body)


def started_run(client, *, qty=200, planned=80, per_unit=2, findings=None) -> str:
    """登记印刷批次、领用并开工，返回领用记录 ID。"""
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=qty)
    res = start_run(client, [iss], planned=planned, per_unit=per_unit,
                    findings=findings)
    assert res.status_code == 201, res.text
    return iss


def post_event(client, run_id, key, kind, quantity, *, good_units=None,
               operator="op.zhang", reason=None):
    body = {"kind": kind, "quantity": quantity, "operator": operator,
            "idempotency_key": key}
    if good_units is not None:
        body["good_units"] = good_units
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/packaging-runs/{run_id}/events", json=body)


def post_adjustment(client, run_id, key, category, delta, *, units_delta=0,
                    reason="盘点更正", operator="qa.li"):
    return client.post(f"/packaging-runs/{run_id}/adjustments", json={
        "category": category, "delta": delta, "good_units_delta": units_delta,
        "reason": reason, "operator": operator, "idempotency_key": key})


def balanced_events(client, run_id="RUN1"):
    """贴用 160（80 件 × 2）+ 损耗 20 + 留样 10 + 退回 10 = 领用 200。"""
    assert post_event(client, run_id, "ev-applied", "applied", 160,
                      good_units=80).status_code == 201
    assert post_event(client, run_id, "ev-wasted", "wasted", 20,
                      reason="开机废标").status_code == 201
    assert post_event(client, run_id, "ev-sampled", "sampled", 10).status_code == 201
    assert post_event(client, run_id, "ev-returned", "returned", 10,
                      reason="余卷退回隔离").status_code == 201


# -------------------------------------------------------------- 开工绑定与门禁

def test_start_run_binds_and_records_clearance(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    findings = [
        {"finding": "线边发现上一班旧卷标，已入隔离区", "old_rolls_found": 2,
         "isolated": True, "note": "QA-LOCK-1"},
        {"finding": "现场无异物、无混批", "old_rolls_found": 0, "isolated": True},
    ]
    res = start_run(client, [iss], findings=findings)
    assert res.status_code == 201, res.text
    run = res.json()
    assert run["status"] == "open"
    assert run["production_batch_id"] == "B2"
    assert run["line_id"] == "PK1"
    assert run["planned_quantity"] == 80
    assert run["labels_per_unit"] == 2
    assert run["expected_label_quantity"] == 160
    assert run["issuances"] == [{
        "issuance_id": iss, "print_batch_id": "PB1", "label_id": lid,
        "label_revision": 1, "quantity": 200}]
    assert [f["finding"] for f in run["clearance_findings"]] == [
        "线边发现上一班旧卷标，已入隔离区", "现场无异物、无混批"]
    assert run["clearance_findings"][0]["isolated"] is True
    # 重复 run_id 拒绝
    assert start_run(client, [iss], findings=[]).status_code == 409


def test_start_blocked_when_old_rolls_not_isolated(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1")
    res = start_run(client, [iss], findings=[
        {"finding": "线边混入上一班旧卷标", "old_rolls_found": 3,
         "isolated": False}])
    assert res.status_code == 409
    failures = res.json()["detail"]["failures"]
    assert [f["code"] for f in failures] == ["old_rolls_not_isolated"]
    assert failures[0]["old_rolls_found"] == 3
    # 门禁不过不登记任何运行记录
    assert client.get("/packaging-runs/RUN1").status_code == 404


def test_start_blocked_mixed_label_revisions(client):
    lid = setup_world(client)
    register_print_batch(client, lid, "PB1")
    iss1 = issue(client, "PB1", "iss-1", qty=100)
    # 同一产品的第二修订：不同标签修订的卷标不得同线混用
    copy = {"declared_allergens": ["wheat"], "may_contain": [],
            "free_from_claims": ["peanut"], "ingredients_text": "小麦粉"}
    label2 = client.post("/labels", json={
        "product_id": "P", "batch_id": "B2", "copy": copy}).json()
    assert label2["revision"] == 2
    client.post(f"/labels/{label2['id']}/submit")
    assert client.post(f"/labels/{label2['id']}/approve",
                       json={"approved_by": "qa.lead"}).status_code == 200
    register_print_batch(client, label2["id"], "PB2")
    iss2 = issue(client, "PB2", "iss-2", qty=100)
    res = start_run(client, [iss1, iss2])
    assert res.status_code == 409
    codes = [f["code"] for f in res.json()["detail"]["failures"]]
    assert "mixed_label_revisions" in codes
    revisions = [f for f in res.json()["detail"]["failures"]
                 if f["code"] == "mixed_label_revisions"][0]["revisions"]
    assert {(r["label_id"], r["revision"]) for r in revisions} == {
        (lid, 1), (label2["id"], 2)}


def test_start_blocked_when_print_batch_frozen(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1")
    # 撤回标签：印刷批次冻结，线边余卷不得上线
    client.post(f"/labels/{lid}/withdraw", json={"reason": "文案勘误"})
    res = start_run(client, [iss])
    assert res.status_code == 409
    codes = {f["code"] for f in res.json()["detail"]["failures"]}
    assert "print_batch_frozen" in codes
    assert "label_revision_blocked" in codes


def test_start_blocked_issuance_of_other_batch_and_product(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", batch="B2")
    # 另一产品的批次绑定 B2 的领用：领用归属与适用产品双双不符
    client.post("/products", json={"id": "P2", "name": "酥饼"})
    client.post("/batches", json={
        "batch_id": "B3", "product_id": "P2", "line_id": "L1", "sequence": 3,
        "allergens": []})
    res = start_run(client, [iss], batch="B3")
    assert res.status_code == 409
    codes = {f["code"] for f in res.json()["detail"]["failures"]}
    assert "issuance_batch_mismatch" in codes
    assert "product_not_applicable" in codes


def test_start_blocked_unknown_and_rebound_issuance(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1")
    res = start_run(client, ["iss-ghost"])
    assert res.status_code == 409
    assert [f["code"] for f in res.json()["detail"]["failures"]] == [
        "issuance_unknown"]
    assert start_run(client, [iss]).status_code == 201
    # 同一领用记录不得绑定第二个运行（避免重复计量）
    res = start_run(client, [iss], run_id="RUN2")
    assert res.status_code == 409
    failures = res.json()["detail"]["failures"]
    assert [f["code"] for f in failures] == ["issuance_already_bound"]
    assert failures[0]["run_id"] == "RUN1"


# -------------------------------------------------------------- 用标事件

def test_usage_events_idempotent_and_keep_operator(client):
    started_run(client)
    res = post_event(client, "RUN1", "ev-1", "applied", 40, good_units=20,
                     operator="op.zhang")
    assert res.status_code == 201
    event = res.json()["event"]
    assert event["operator"] == "op.zhang"
    assert event["occurred_at"]  # 保留时刻
    assert event["kind"] == "applied" and event["good_units"] == 20
    # 同键同内容重放：复用原事件，不重复记录
    again = post_event(client, "RUN1", "ev-1", "applied", 40, good_units=20,
                       operator="op.zhang")
    assert again.status_code == 200
    assert again.json()["reused"] is True
    assert again.json()["event"]["event_id"] == event["event_id"]
    assert len(client.get("/packaging-runs/RUN1").json()["events"]) == 1
    # 同键内容冲突：拒绝，不改写原事件
    conflict = post_event(client, "RUN1", "ev-1", "applied", 42, good_units=21,
                          operator="op.zhang")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["original"]["quantity"] == 40


def test_applied_event_validates_good_units(client):
    started_run(client)
    # 缺合格品数
    assert post_event(client, "RUN1", "ev-1", "applied", 40).status_code == 422
    # 贴用量 ≠ 合格品数 × 每件用标数
    res = post_event(client, "RUN1", "ev-2", "applied", 41, good_units=20)
    assert res.status_code == 422
    assert res.json()["detail"]["expected_quantity"] == 40
    # 非贴用事件不得携带合格品数
    assert post_event(client, "RUN1", "ev-3", "wasted", 5,
                      good_units=2).status_code == 422


def test_events_rejected_after_settlement(client):
    started_run(client)
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    res = post_event(client, "RUN1", "ev-late", "wasted", 1)
    assert res.status_code == 409
    assert "调整事件" in res.json()["detail"]["error"]


# -------------------------------------------------------------- 结算

def test_settle_balanced_records_immutable_snapshot(client):
    started_run(client)
    balanced_events(client)
    res = client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    assert res.status_code == 200, res.text
    settlement = res.json()["settlement"]
    assert settlement["result"] == "balanced"
    assert settlement["settled_by"] == "qa.li"
    rec = res.json()["reconciliation"]
    assert rec["balanced"] is True
    assert rec["balances"]["issuance_balance"] == {
        "equation": "领用量 = 贴用量 + 损耗量 + 留样量 + 退回隔离量",
        "issued_quantity": 200, "accounted_quantity": 200,
        "difference": 0, "balanced": True}
    assert rec["balances"]["application_balance"]["balanced"] is True
    assert res.json()["run"]["status"] == "settled"


def test_settle_discrepancy_returns_quantity_sources(client):
    started_run(client)
    # 只记录 190：领用 200 差 10；贴用 160 与 80 件 × 2 平衡
    post_event(client, "RUN1", "ev-applied", "applied", 160, good_units=80)
    post_event(client, "RUN1", "ev-wasted", "wasted", 20)
    post_event(client, "RUN1", "ev-sampled", "sampled", 10)
    res = client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    assert res.status_code == 409
    rec = res.json()["detail"]["reconciliation"]
    ib = rec["balances"]["issuance_balance"]
    assert ib["balanced"] is False
    assert ib["issued_quantity"] == 200
    assert ib["accounted_quantity"] == 190
    assert ib["difference"] == 10
    # 数量来源：领用来自哪条领用记录，各类别来自用标事件还是调整
    assert rec["sources"]["issued"]["issuances"][0]["print_batch_id"] == "PB1"
    assert rec["sources"]["issued"]["total"] == 200
    assert rec["sources"]["applied"] == {
        "total": 160, "usage_quantity": 160, "adjustment_quantity": 0}
    assert rec["sources"]["returned"] == {
        "total": 0, "usage_quantity": 0, "adjustment_quantity": 0}
    assert rec["on_line_remaining"] == 10
    # 不平衡不落结算记录
    assert client.get("/packaging-runs/RUN1").json()["settlements"] == []


def test_settle_discrepancy_on_application_equation(client):
    started_run(client)
    # 账面领用平衡，但贴用 160 ≠ 合格品 79 × 2（盘点更正合格品数后）
    balanced_events(client)
    assert post_adjustment(client, "RUN1", "adj-1", "applied", 0,
                           units_delta=-1,
                           reason="抽检剔除 1 件不合格").status_code == 201
    res = client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    assert res.status_code == 409
    ab = res.json()["detail"]["reconciliation"]["balances"]["application_balance"]
    assert ab["balanced"] is False
    assert ab["applied_quantity"] == 160
    assert ab["good_units"] == 79
    assert ab["expected_applied"] == 158
    assert ab["difference"] == 2


def test_returned_does_not_replenish_issuable_remaining(client):
    started_run(client)
    assert client.get("/print-batches/PB1").json()["remaining_quantity"] == 800
    balanced_events(client)  # 含退回隔离 10
    # 退回隔离留在领用方账上，不补回印刷批次可领用余量
    pb = client.get("/print-batches/PB1").json()
    assert pb["remaining_quantity"] == 800
    assert pb["issued_quantity"] == 200


# -------------------------------------------------------------- 盘点更正

def test_adjustment_after_settlement_and_resettle(client):
    started_run(client)
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    # 盘点发现 2 枚贴用实为损耗：追加调整事件（不覆盖既有记录）
    res = post_adjustment(client, "RUN1", "adj-1", "applied", -2,
                          units_delta=-1, reason="盘点：2 枚贴用实为开机废标")
    assert res.status_code == 201
    assert res.json()["event"]["kind"] == "adjustment"
    assert res.json()["event"]["reason"] == "盘点：2 枚贴用实为开机废标"
    assert post_adjustment(client, "RUN1", "adj-2", "wasted", 2,
                           reason="盘点：2 枚贴用实为开机废标").status_code == 201
    # 调整同键重放复用
    replay = post_adjustment(client, "RUN1", "adj-2", "wasted", 2,
                             reason="盘点：2 枚贴用实为开机废标")
    assert replay.status_code == 200 and replay.json()["reused"] is True
    # 重新结算：生成新记录，历史结算快照不可覆盖
    res = client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    assert res.status_code == 200, res.text
    run = res.json()["run"]
    assert len(run["settlements"]) == 2
    first, second = run["settlements"]
    assert first["settlement_id"] != second["settlement_id"]
    assert first["snapshot"]["balances"]["application_balance"]["applied_quantity"] == 160
    assert second["snapshot"]["balances"]["application_balance"]["applied_quantity"] == 158
    assert second["snapshot"]["balances"]["application_balance"]["good_units"] == 79
    assert second["snapshot"]["balances"]["issuance_balance"]["accounted_quantity"] == 200
    # 数量来源把调整量单列
    assert second["snapshot"]["sources"]["applied"] == {
        "total": 158, "usage_quantity": 160, "adjustment_quantity": -2}
    assert second["snapshot"]["sources"]["wasted"] == {
        "total": 22, "usage_quantity": 20, "adjustment_quantity": 2}


def test_adjustment_negative_balance_rejected(client):
    started_run(client)
    balanced_events(client)
    res = post_adjustment(client, "RUN1", "adj-neg", "wasted", -21,
                          reason="误录冲正")
    assert res.status_code == 409
    assert res.json()["detail"]["failures"][0]["code"] == "negative_balance"
    res = post_adjustment(client, "RUN1", "adj-neg2", "applied", 0,
                          units_delta=-81, reason="误录冲正")
    assert res.status_code == 409
    assert res.json()["detail"]["failures"][0]["code"] == "negative_good_units"
    # 调整内容必填理由与增量
    assert client.post("/packaging-runs/RUN1/adjustments", json={
        "category": "wasted", "delta": 0, "good_units_delta": 0,
        "reason": "无变化", "operator": "qa.li",
        "idempotency_key": "adj-zero"}).status_code == 422


def test_adjustment_allowed_on_open_run_for_miscount(client):
    started_run(client)
    post_event(client, "RUN1", "ev-w", "wasted", 30, reason="多记 10 枚")
    # 未结算也可用调整事件更正误录（事件只增不改）
    assert post_adjustment(client, "RUN1", "adj-1", "wasted", -10,
                           reason="误录冲正").status_code == 201
    rec = client.get("/packaging-runs/RUN1/reconciliation").json()
    assert rec["sources"]["wasted"] == {
        "total": 20, "usage_quantity": 30, "adjustment_quantity": -10}


def test_adjustment_unsettles_run_then_withdraw(client):
    """先平衡结算、再经调整变为不平衡、随后撤回：

    调整写入后按最新事件账重新判定——运行回退 open，波及清单按实时对账
    列出现场余量与待隔离批次；历史结算快照保持不可覆盖。
    """
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    assert start_run(client, [iss]).status_code == 201
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    assert client.get("/packaging-runs/RUN1").json()["status"] == "settled"
    # 盘点更正：退回隔离多记 2 枚 → 实时对账出现 2 枚线边余量
    res = post_adjustment(client, "RUN1", "adj-1", "returned", -2,
                          reason="盘点：退回多记 2 枚")
    assert res.status_code == 201
    assert res.json()["run_status"] == "open"  # 不再平衡，不得继续按 settled 处理
    run = client.get("/packaging-runs/RUN1").json()
    assert run["status"] == "open"
    assert run["reconciliation"]["balanced"] is False
    assert run["reconciliation"]["on_line_remaining"] == 2
    # 历史结算快照不可覆盖
    assert len(run["settlements"]) == 1
    snap = run["settlements"][0]["snapshot"]
    assert snap["balances"]["issuance_balance"]["accounted_quantity"] == 200
    assert snap["balances"]["issuance_balance"]["balanced"] is True
    # 重新判定写入事件日志
    reopened = [e for e in client.get("/events").json()
                if e["kind"] == "packaging_run_reopened"]
    assert len(reopened) == 1
    assert reopened[0]["payload"]["run_id"] == "RUN1"
    # 再次结算返回 discrepancy（差异 2 枚）
    res = client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    assert res.status_code == 409
    ib = res.json()["detail"]["reconciliation"]["balances"]["issuance_balance"]
    assert ib["balanced"] is False and ib["difference"] == 2
    # 撤回：波及清单按实时对账列出 2 枚现场余量与待隔离批次
    res = client.post(f"/labels/{lid}/withdraw", json={"reason": "文案勘误"})
    assert res.status_code == 200
    freeze = res.json()["print_freeze"]
    assert freeze["pending_isolation_batches"] == ["B2"]
    pack = freeze["disposition_batches"][0]["packaging"]
    assert pack["unsettled_on_line_quantity"] == 2
    assert pack["packed_quantity"] == 160
    assert pack["pending_isolation"] is True
    run_info = pack["runs"][0]
    assert run_info["status"] == "open"
    assert run_info["on_line_remaining"] == 2
    assert run_info["currently_balanced"] is False


def test_adjustment_unsettles_then_rebalance_and_resettle(client):
    """回退 open 后补记调整恢复平衡，可再次结算；两条结算记录均不可覆盖。"""
    started_run(client)
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    # 调整打破平衡 → open；再补一笔恢复平衡
    assert post_adjustment(client, "RUN1", "adj-1", "returned", -2,
                           reason="盘点：退回多记 2 枚").status_code == 201
    assert client.get("/packaging-runs/RUN1").json()["status"] == "open"
    res = post_adjustment(client, "RUN1", "adj-2", "wasted", 2,
                          reason="盘点：漏记损耗 2 枚")
    assert res.status_code == 201
    assert res.json()["run_status"] == "open"  # 重新判定只在结算时回升 settled
    assert client.get("/packaging-runs/RUN1").json()[
        "reconciliation"]["balanced"] is True
    # 再次结算成功：状态回升 settled，生成第二条结算记录
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    run = client.get("/packaging-runs/RUN1").json()
    assert run["status"] == "settled"
    assert len(run["settlements"]) == 2
    first, second = run["settlements"]
    assert first["settlement_id"] != second["settlement_id"]
    # 首条快照保持结算当时的数据（退回 10），第二条反映调整后（退回 8、损耗 22）
    assert first["snapshot"]["sources"]["returned"]["total"] == 10
    assert first["snapshot"]["sources"]["wasted"]["total"] == 20
    assert second["snapshot"]["sources"]["returned"] == {
        "total": 8, "usage_quantity": 10, "adjustment_quantity": -2}
    assert second["snapshot"]["sources"]["wasted"] == {
        "total": 22, "usage_quantity": 20, "adjustment_quantity": 2}


def test_settled_restored_only_by_resettle(client):
    """单笔调整打破任一等式即回退 open；只有重新结算成功才回升 settled。"""
    started_run(client)
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    # 贴用 -2（合格品 -1）：贴用等式仍平衡，但领用等式被打破（198 ≠ 200）
    res = post_adjustment(client, "RUN1", "adj-1", "applied", -2,
                          units_delta=-1, reason="盘点：2 枚贴用实为损耗")
    assert res.status_code == 201
    assert res.json()["run_status"] == "open"
    # 再补损耗 +2 恢复平衡：状态仍为 open，settled 只能由重新结算授予
    assert post_adjustment(client, "RUN1", "adj-2", "wasted", 2,
                           reason="盘点：2 枚贴用实为损耗").status_code == 201
    run = client.get("/packaging-runs/RUN1").json()
    assert run["status"] == "open"
    assert run["reconciliation"]["balanced"] is True
    # 回退只记一次日志（已 open 的运行不重复记）
    reopened = [e for e in client.get("/events").json()
                if e["kind"] == "packaging_run_reopened"]
    assert len(reopened) == 1
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    assert client.get("/packaging-runs/RUN1").json()["status"] == "settled"


# -------------------------------------------------------------- 撤回 / 更正波及清单

def test_withdraw_lists_packaging_disposition(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    assert start_run(client, [iss]).status_code == 201
    post_event(client, "RUN1", "ev-applied", "applied", 160, good_units=80)
    post_event(client, "RUN1", "ev-wasted", "wasted", 20)
    # 另领 50 枚未上线（领出未绑定运行，同样计入未结算现场余量）
    issue(client, "PB1", "iss-2", qty=50)
    res = client.post(f"/labels/{lid}/withdraw", json={"reason": "文案勘误"})
    assert res.status_code == 200
    freeze = res.json()["print_freeze"]
    assert freeze["pending_isolation_batches"] == ["B2"]
    disp = freeze["disposition_batches"][0]
    assert disp["production_batch_id"] == "B2"
    pack = disp["packaging"]
    # 未结算现场余量 = 未上线 50 + 运行线边未记账 20（200 - 160 - 20）
    assert pack["unsettled_on_line_quantity"] == 70
    assert pack["unbound_quantity"] == 50
    assert pack["packed_quantity"] == 160
    assert pack["packed_units"] == 80
    assert pack["pending_isolation"] is True
    run_info = pack["runs"][0]
    assert run_info["run_id"] == "RUN1"
    assert run_info["on_line_remaining"] == 20
    assert run_info["status"] == "open"


def test_settled_run_not_counted_as_unsettled_on_line(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    assert start_run(client, [iss]).status_code == 201
    balanced_events(client)
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    res = client.post(f"/labels/{lid}/withdraw", json={"reason": "文案勘误"})
    pack = res.json()["print_freeze"]["disposition_batches"][0]["packaging"]
    # 已平衡结算：现场余量清零；已包装数量仍进入处置评估
    assert pack["unsettled_on_line_quantity"] == 0
    assert pack["packed_quantity"] == 160
    assert pack["pending_isolation"] is True  # 已包装成品仍待隔离评估


def test_spec_correction_lists_packaging_disposition(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    assert start_run(client, [iss]).status_code == 201
    post_event(client, "RUN1", "ev-applied", "applied", 100, good_units=50)
    # 供应商更正：FLOUR v2 新增 milk（沿扣料关系波及 B2）
    res = client.post("/ingredients/FLOUR/versions", json={
        "version": "v2", "corrects_version": "v1",
        "supplier_declarations": [
            {"allergen": "wheat", "status": "present"},
            {"allergen": "milk", "status": "present"}]})
    assert res.status_code == 201
    marked = [a for a in res.json()["actions"] if a["action"] == "marked_stale"]
    freeze = marked[0]["print_freeze"]
    assert freeze["pending_isolation_batches"] == ["B2"]
    pack = freeze["disposition_batches"][0]["packaging"]
    assert pack["unsettled_on_line_quantity"] == 100  # 200 领用 - 100 贴用
    assert pack["packed_quantity"] == 100
    assert pack["packed_units"] == 50


# -------------------------------------------------------------- 核对包与事件日志

def test_check_package_links_clearance_usage_and_adjustments(client):
    lid = setup_world(client)
    register_print_batch(client, lid)
    iss = issue(client, "PB1", "iss-1", qty=200)
    findings = [{"finding": "线边旧卷标已隔离", "old_rolls_found": 1,
                 "isolated": True, "note": "QA-LOCK-1"}]
    assert start_run(client, [iss], findings=findings).status_code == 201
    balanced_events(client)
    assert post_adjustment(client, "RUN1", "adj-1", "wasted", 2,
                           reason="盘点补记损耗").status_code == 201
    # 调整后领用平衡被打破（202 ≠ 200），补一笔退回 2 再结算
    assert post_event(client, "RUN1", "ev-returned2", "returned", 0,
                      ).status_code == 422  # 数量必须为正
    assert post_adjustment(client, "RUN1", "adj-2", "returned", -2,
                           reason="盘点：退回数多记").status_code == 201
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    pkg = client.get(f"/labels/{lid}/check-package").json()
    execution = pkg["packaging_execution"]
    assert execution["label_id"] == lid
    run = execution["runs"][0]
    assert run["run_id"] == "RUN1"
    # 清场、用标与调整记录串进核对包
    assert run["clearance_findings"][0]["note"] == "QA-LOCK-1"
    kinds = [e["kind"] for e in run["events"]]
    assert kinds.count("adjustment") == 2
    assert {"applied", "wasted", "sampled", "returned"} <= set(kinds)
    assert all(e["operator"] for e in run["events"])
    assert len(run["settlements"]) == 1
    assert run["reconciliation"]["balanced"] is True
    # 撤回波及清单（当前无撤回，仍给出结构）
    assert execution["disposition"]["label_id"] == lid
    assert execution["disposition"]["batches"][0]["packed_quantity"] == 160


def test_event_log_records_packaging_trail(client):
    started_run(client, findings=[
        {"finding": "清场合格", "old_rolls_found": 0, "isolated": True}])
    balanced_events(client)
    assert post_adjustment(client, "RUN1", "adj-1", "returned", -2,
                           reason="盘点：退回多记 2 枚").status_code == 201
    assert post_adjustment(client, "RUN1", "adj-2", "wasted", 2,
                           reason="盘点：漏记损耗 2 枚").status_code == 201
    assert client.post("/packaging-runs/RUN1/settle",
                       json={"settled_by": "qa.li"}).status_code == 200
    kinds = [e["kind"] for e in client.get("/events").json()]
    assert "packaging_run_started" in kinds
    assert "packaging_event_recorded" in kinds
    assert "packaging_run_settled" in kinds
    started = [e for e in client.get("/events").json()
               if e["kind"] == "packaging_run_started"][0]
    assert started["payload"]["clearance_findings"][0]["finding"] == "清场合格"
    assert started["payload"]["expected_label_quantity"] == 160
    settled = [e for e in client.get("/events").json()
               if e["kind"] == "packaging_run_settled"][0]
    assert settled["payload"]["issued_quantity"] == 200
    assert settled["payload"]["good_units"] == 80


def test_batch_view_lists_packaging_runs(client):
    started_run(client)
    balanced_events(client)
    client.post("/packaging-runs/RUN1/settle", json={"settled_by": "qa.li"})
    batch = client.get("/batches/B2").json()
    assert batch["packaging_runs"] == [{
        "run_id": "RUN1", "line_id": "PK1", "status": "settled",
        "planned_quantity": 80, "labels_per_unit": 2, "issued_quantity": 200}]
