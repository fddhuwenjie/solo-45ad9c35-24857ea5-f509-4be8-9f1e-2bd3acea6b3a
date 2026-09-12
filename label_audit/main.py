"""FastAPI 路由：资料登记、标签生命周期、分析、比较与报告。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from . import __version__, engine, genealogy, packaging, printing, report, trace
from .db import Store, utcnow
from .models import (
    AllocationCreate,
    AllocationReverse,
    ApproveRequest,
    BatchCreate,
    CleaningProgramCreate,
    CleaningRecordCreate,
    IngredientCreate,
    IngredientVersionCreate,
    LabelCopyUpdate,
    LabelCreate,
    LineCreate,
    LotStatusUpdate,
    MaterialLotCreate,
    OverrideRequest,
    PackagingAdjustmentCreate,
    PackagingEventCreate,
    PackagingRunCreate,
    PackagingSettle,
    PrintBatchCreate,
    PrintBatchDispose,
    PrintBatchIssue,
    ProductCreate,
    RecipeCreate,
    ReworkPathCreate,
    SwabBackfill,
    SwabResultCreate,
    WithdrawRequest,
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def get_store(request: Request) -> Store:
    return request.app.state.store


def _label_or_404(store: Store, label_id: str) -> dict:
    label = store.get_label(label_id)
    if label is None:
        raise HTTPException(404, f"标签 {label_id} 不存在")
    return label


def _label_view(store: Store, label: dict) -> dict:
    findings = store.findings_for_label(label["id"], include_resolved=False)
    batch_id = store.get_label_batch(label["id"])
    return {
        **label,
        "batch_id": batch_id,
        "findings": findings,
        "open_blockers": [f for f in findings
                          if f["severity"] == engine.BLOCKER and f["status"] == "open"],
        "approvals": store.approvals_for_label(label["id"]),
    }


def create_app(db_path: str = ":memory:") -> FastAPI:
    app = FastAPI(
        title="食品标签审核 API",
        version=__version__,
        description="配方追溯、过敏原声明推导与标签生命周期审核",
    )
    app.state.store = Store(db_path)

    # ------------------------------------------------------------- 资料登记
    @app.post("/ingredients", status_code=201, tags=["资料登记"])
    def create_ingredient(body: IngredientCreate, store: Store = Depends(get_store)):
        if store.get_ingredient(body.id):
            raise HTTPException(409, f"原料 {body.id} 已存在")
        return store.create_ingredient(body.id, body.name, body.aliases, body.is_compound)

    @app.get("/ingredients/{ingredient_id}", tags=["资料登记"])
    def get_ingredient(ingredient_id: str, store: Store = Depends(get_store)):
        ing = store.get_ingredient(ingredient_id)
        if ing is None:
            raise HTTPException(404, f"原料 {ingredient_id} 不存在")
        return {**ing, "versions": store.versions_of(ingredient_id)}

    @app.post("/ingredients/{ingredient_id}/versions", status_code=201, tags=["资料登记"])
    def add_ingredient_version(ingredient_id: str, body: IngredientVersionCreate,
                               store: Store = Depends(get_store)):
        """登记新规格版本（供应商更换/声明更正），随后沿依赖关系传播影响。

        原料已启用批号管理时，沿扣料关系定位消耗过旧规格批号的批次、标签与
        已发放卷标（未消耗涉事批号的成品保持原状态）；否则回退依赖图传播。
        """
        if store.get_ingredient(ingredient_id) is None:
            raise HTTPException(404, f"原料 {ingredient_id} 不存在")
        if store.get_version(ingredient_id, body.version):
            raise HTTPException(409, f"版本 {body.version} 已存在；规格版本不可变，请登记新版本号")
        if body.corrects_version is not None:
            if body.corrects_version == body.version:
                raise HTTPException(422, "被更正规格不能与新版本相同")
            if store.get_version(ingredient_id, body.corrects_version) is None:
                raise HTTPException(
                    422, f"被更正规格版本 {body.corrects_version} 不存在")
        ver = store.add_ingredient_version(
            ingredient_id, body.version,
            [s.model_dump() for s in body.sub_components],
            [d.model_dump() for d in body.supplier_declarations],
        )
        impact = genealogy.correction_impact(store, ingredient_id, body.version,
                                             corrects_version=body.corrects_version)
        return {"version": ver, **impact}

    @app.post("/products", status_code=201, tags=["资料登记"])
    def create_product(body: ProductCreate, store: Store = Depends(get_store)):
        if store.get_product(body.id):
            raise HTTPException(409, f"产品 {body.id} 已存在")
        return store.create_product(body.id, body.name)

    @app.post("/products/{product_id}/recipes", status_code=201, tags=["资料登记"])
    def add_recipe(product_id: str, body: RecipeCreate, store: Store = Depends(get_store)):
        """登记新配方版本，并沿依赖图标记受影响标签。"""
        if store.get_product(product_id) is None:
            raise HTTPException(404, f"产品 {product_id} 不存在")
        if store.get_recipe(product_id, body.version):
            raise HTTPException(409, f"配方版本 {body.version} 已存在")
        recipe = store.add_recipe(product_id, body.version, [i.model_dump() for i in body.items])
        actions = engine.apply_impact(store, [product_id], reason=f"新配方版本 {product_id}@{body.version}")
        return {"recipe": recipe, "actions": actions}

    @app.post("/lines", status_code=201, tags=["资料登记"])
    def create_line(body: LineCreate, store: Store = Depends(get_store)):
        if store.get_line(body.id):
            raise HTTPException(409, f"产线 {body.id} 已存在")
        return store.create_line(body.id, body.name, body.allergens_handled)

    @app.post("/products/{product_id}/lines/{line_id}", status_code=201, tags=["资料登记"])
    def assign_line(product_id: str, line_id: str, store: Store = Depends(get_store)):
        """登记产线共线情况；共线变化同样会使相关标签过期。"""
        if store.get_product(product_id) is None:
            raise HTTPException(404, f"产品 {product_id} 不存在")
        if store.get_line(line_id) is None:
            raise HTTPException(404, f"产线 {line_id} 不存在")
        store.assign_line(product_id, line_id)
        actions = engine.apply_impact(store, [product_id], reason=f"共线变更 {product_id} -> {line_id}")
        return {"product_id": product_id, "line_id": line_id, "actions": actions}

    # ------------------------------------------------------------- 批次与逐批追溯
    def _batch_or_404(batch_id: str, store: Store) -> dict:
        batch = store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        return batch

    @app.post("/batches", status_code=201, tags=["批次追溯"])
    def create_batch(body: BatchCreate, store: Store = Depends(get_store)):
        """登记生产批次（产线、生产顺序号、设备段、本批次过敏原）。"""
        if store.get_batch(body.batch_id):
            raise HTTPException(409, f"批次 {body.batch_id} 已存在")
        if store.get_product(body.product_id) is None:
            raise HTTPException(404, f"产品 {body.product_id} 不存在")
        if store.get_line(body.line_id) is None:
            raise HTTPException(404, f"产线 {body.line_id} 不存在")
        if store.batch_at_sequence(body.line_id, body.sequence):
            raise HTTPException(409, f"产线 {body.line_id} 上顺序号 {body.sequence} 已被占用")
        return store.create_batch(
            body.batch_id, body.product_id, body.line_id, body.sequence,
            body.started_at, body.allergens,
            [s.model_dump() for s in body.equipment_segments])

    @app.get("/batches/{batch_id}", tags=["批次追溯"])
    def get_batch(batch_id: str, store: Store = Depends(get_store)):
        batch = _batch_or_404(batch_id, store)
        return {**batch,
                "rework_in": store.rework_into(batch_id),
                "rework_out": store.rework_from(batch_id),
                "allocations": store.allocations_for_batch(batch_id),
                "lot_ledger": store.ledger_for_batch(batch_id),
                "packaging_runs": [
                    {"run_id": r["run_id"], "line_id": r["line_id"],
                     "status": r["status"], "planned_quantity": r["planned_quantity"],
                     "labels_per_unit": r["labels_per_unit"],
                     "issued_quantity": sum(i["quantity"] for i in r["issuances"])}
                    for r in store.runs_for_batch(batch_id)]}

    @app.get("/batches/{batch_id}/trace", tags=["批次追溯"])
    def batch_trace(batch_id: str, store: Store = Depends(get_store)):
        """从该批次向前追溯：最近含敏原批次与返工路径的开闭状态及证据缺口。"""
        _batch_or_404(batch_id, store)
        return trace.trace_batch(store, batch_id)

    @app.post("/cleaning-programs", status_code=201, tags=["批次追溯"])
    def create_cleaning_program(body: CleaningProgramCreate, store: Store = Depends(get_store)):
        """登记清洁程序版本：覆盖过敏原、必检采样点、有效期与定量限值。"""
        if body.line_id is not None and store.get_line(body.line_id) is None:
            raise HTTPException(404, f"产线 {body.line_id} 不存在")
        if store.get_cleaning_program(body.program_id, body.version):
            raise HTTPException(409, f"清洁程序 {body.program_id}@{body.version} 已存在")
        return store.create_cleaning_program(
            body.program_id, body.version, body.line_id, body.allergens,
            body.required_points, body.valid_from, body.valid_until, body.limit_ppm)

    @app.post("/cleaning-records", status_code=201, tags=["批次追溯"])
    def create_cleaning_record(body: CleaningRecordCreate, store: Store = Depends(get_store)):
        """登记批次投产前、某设备段上执行的清洁程序版本。"""
        if store.get_cleaning_record(body.record_id):
            raise HTTPException(409, f"清洁记录 {body.record_id} 已存在")
        batch = _batch_or_404(body.batch_id, store)
        if store.get_line(body.line_id) is None:
            raise HTTPException(404, f"产线 {body.line_id} 不存在")
        if body.line_id != batch["line_id"]:
            raise HTTPException(422, f"清洁记录产线 {body.line_id} 与批次产线 "
                                    f"{batch['line_id']} 不一致")
        if not any(s["segment_id"] == body.segment_id for s in batch["segments"]):
            raise HTTPException(404, f"设备段 {body.segment_id} 未登记在批次 "
                                    f"{body.batch_id} 上")
        if store.get_cleaning_program(body.program_id, body.program_version) is None:
            raise HTTPException(404, f"清洁程序 {body.program_id}@{body.program_version} 不存在")
        return store.create_cleaning_record(
            body.record_id, body.line_id, body.batch_id, body.segment_id,
            body.program_id, body.program_version, body.cleaned_at)

    @app.post("/swabs", status_code=201, tags=["批次追溯"])
    def create_swab(body: SwabResultCreate, store: Store = Depends(get_store)):
        """登记拭子采样与定量结果；value_ppm 允许为空（结果待出）。"""
        if store.get_swab(body.swab_id):
            raise HTTPException(409, f"拭子 {body.swab_id} 已存在")
        rec = store.get_cleaning_record(body.record_id)
        if rec is None:
            raise HTTPException(404, f"清洁记录 {body.record_id} 不存在")
        return store.create_swab(
            body.swab_id, body.record_id, body.point_id, body.allergen,
            body.value_ppm, body.sampled_at)

    @app.put("/swabs/{swab_id}/result", tags=["批次追溯"])
    def backfill_swab(swab_id: str, body: SwabBackfill, store: Store = Depends(get_store)):
        """补录拭子定量结果（可能在标签批准之后到达）。

        结果阳性/超限会自动沿返工影响链标记相关标签：草稿/复核中重新分析，
        已批准标签标记 stale 并派生新修订；调用方无需手工触发影响传播。
        阴性补录同样重新分析，可能使原“结果待出”的开放路径关闭并消解发现项。
        """
        swab = store.get_swab(swab_id)
        if swab is None:
            raise HTTPException(404, f"拭子 {swab_id} 不存在")
        rec = store.get_cleaning_record(swab["record_id"])
        if rec is None:
            raise HTTPException(500, f"拭子 {swab_id} 的清洁记录缺失，数据异常")
        program = store.get_cleaning_program(rec["program_id"], rec["program_version"])
        updated = store.set_swab_value(swab_id, body.value_ppm, body.sampled_at)
        impacted = engine.impacted_batches_via_rework(store, [rec["batch_id"]])
        # 仅当结果超过程序限值、且采样点/过敏原确为该程序验证对象时按阳性传播
        applicable = bool(
            program and swab["point_id"] in program["required_points"]
            and engine.norm(swab["allergen"]) in {engine.norm(a) for a in program["allergens"]})
        exceeded = applicable and body.value_ppm > program["limit_ppm"]
        actions = engine.apply_batch_impact(
            store, impacted, positive=exceeded,
            reason=(f"拭子 {swab_id} 补录{'阳性' if exceeded else '阴性'}结果 "
                    f"{body.value_ppm} ppm"
                    + (f"（限值 {program['limit_ppm']} ppm）" if program else "")))
        return {"swab": updated, "exceeded_limit": exceeded,
                "impacted_batches": sorted(impacted), "actions": actions}

    @app.post("/rework-paths", status_code=201, tags=["批次追溯"])
    def add_rework_path(body: ReworkPathCreate, store: Store = Depends(get_store)):
        """登记返工料去向（源批次余料投入目标批次，可跨产品）。"""
        source = _batch_or_404(body.source_batch_id, store)
        target = _batch_or_404(body.target_batch_id, store)
        if any(r["source_batch_id"] == body.source_batch_id
               for r in store.rework_into(body.target_batch_id)):
            raise HTTPException(409, f"返工路径 {body.source_batch_id} -> "
                                    f"{body.target_batch_id} 已存在")
        # 防止返工环
        if body.source_batch_id in engine.impacted_batches_via_rework(
                store, [body.target_batch_id]):
            raise HTTPException(422, f"返工路径将形成环：{body.target_batch_id} 的余料"
                                    f"已（间接地）回流到 {body.source_batch_id}")
        path = store.add_rework_path(
            body.source_batch_id, body.target_batch_id, body.percentage)
        return {"path": path,
                "source_product_id": source["product_id"],
                "target_product_id": target["product_id"]}

    # ------------------------------------------------------------- 来源图与影响
    @app.get("/products/{product_id}/source-graph", tags=["分析"])
    def source_graph(product_id: str, batch_id: str | None = None,
                     store: Store = Depends(get_store)):
        if store.get_product(product_id) is None:
            raise HTTPException(404, f"产品 {product_id} 不存在")
        batch = store.get_batch(batch_id) if batch_id else store.latest_batch_for_product(product_id)
        if batch_id and batch is None:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        resolved_batch_id = batch["batch_id"] if batch else None
        exp = engine.expand_recipe(store, product_id, batch_id=resolved_batch_id)
        derived = engine.derive_declarations(store, product_id, exp,
                                             batch_id=resolved_batch_id)
        return {
            "product_id": product_id,
            "batch_id": resolved_batch_id,
            "batch_trace": trace.trace_batch(store, resolved_batch_id) if batch else None,
            "graph": [n.as_dict() for n in exp.nodes],
            "derived": derived,
            "findings": [f.__dict__ | {"fingerprint": f.fingerprint} for f in exp.findings],
        }

    @app.get("/products/{product_id}/impact", tags=["分析"])
    def product_impact(product_id: str, store: Store = Depends(get_store)):
        """该产品的依赖视图：引用到的原料与各标签修订的过期状态。"""
        if store.get_product(product_id) is None:
            raise HTTPException(404, f"产品 {product_id} 不存在")
        exp = engine.expand_recipe(store, product_id)
        return {
            "product_id": product_id,
            "referenced_ingredients": sorted(exp.referenced),
            "labels": [
                {"label_id": l["id"], "revision": l["revision"],
                 "status": l["status"], "stale": l["stale"]}
                for l in store.labels_for_product(product_id)
            ],
        }

    @app.get("/products/{product_id}/labels/compare", tags=["分析"])
    def compare_label_revisions(product_id: str, from_revision: int, to_revision: int,
                                store: Store = Depends(get_store)):
        """比较两版标签的推导声明：增删项、发现项变化与波及范围。"""
        try:
            return engine.compare_revisions(store, product_id, from_revision, to_revision)
        except KeyError as e:
            raise HTTPException(404, str(e))

    # ------------------------------------------------------------- 标签生命周期
    @app.post("/labels", status_code=201, tags=["标签生命周期"])
    def create_label(body: LabelCreate, store: Store = Depends(get_store)):
        """新建标签草稿（或同一产品的下一修订），创建即自动分析。

        指定 batch_id 时审核针对该批次；缺省取产品最新批次。绑定关系持久化，
        后续分析/批准/影响传播均沿用该批次。
        """
        if store.get_product(body.product_id) is None:
            raise HTTPException(404, f"产品 {body.product_id} 不存在")
        batch = None
        if body.batch_id:
            batch = store.get_batch(body.batch_id)
            if batch is None:
                raise HTTPException(404, f"批次 {body.batch_id} 不存在")
            if batch["product_id"] != body.product_id:
                raise HTTPException(422, f"批次 {body.batch_id} 属于产品 "
                                        f"{batch['product_id']}，与标签产品 "
                                        f"{body.product_id} 不一致")
        label = store.create_label(body.product_id, body.label_copy.model_dump())
        if batch:
            store.set_label_batch(label["id"], batch["batch_id"])
        engine.analyze_label(store, label["id"])
        return _label_view(store, store.get_label(label["id"]))

    @app.get("/labels/{label_id}", tags=["标签生命周期"])
    def get_label(label_id: str, store: Store = Depends(get_store)):
        return _label_view(store, _label_or_404(store, label_id))

    @app.put("/labels/{label_id}/copy", tags=["标签生命周期"])
    def update_copy(label_id: str, body: LabelCopyUpdate, store: Store = Depends(get_store)):
        """修改标签文案（仅草稿/复核中），保存后重新分析。"""
        label = _label_or_404(store, label_id)
        if label["status"] not in ("draft", "in_review"):
            raise HTTPException(409, f"状态 {label['status']} 下不可修改文案")
        store.set_label_copy(label_id, body.label_copy.model_dump())
        engine.analyze_label(store, label_id)
        return _label_view(store, store.get_label(label_id))

    @app.post("/labels/{label_id}/submit", tags=["标签生命周期"])
    def submit(label_id: str, store: Store = Depends(get_store)):
        label = _label_or_404(store, label_id)
        if label["status"] != "draft":
            raise HTTPException(409, f"仅草稿可提交复核，当前状态 {label['status']}")
        engine.analyze_label(store, label_id)
        store.set_label_status(label_id, "in_review")
        return _label_view(store, store.get_label(label_id))

    @app.post("/labels/{label_id}/approve", tags=["标签生命周期"])
    def approve(label_id: str, body: ApproveRequest, store: Store = Depends(get_store)):
        """批准：批准前重新分析，存在未决 blocker（矛盾/资料缺口等）时拒绝。"""
        label = _label_or_404(store, label_id)
        if label["status"] != "in_review":
            raise HTTPException(409, f"仅复核中的标签可批准，当前状态 {label['status']}")
        engine.analyze_label(store, label_id)
        blockers = engine.open_blockers(store, label_id)
        if blockers:
            raise HTTPException(
                409,
                {"error": "存在未决矛盾/阻断项，禁止批准",
                 "open_blockers": [{"id": f["id"], "kind": f["kind"], "message": f["message"]}
                                   for f in blockers]},
            )
        label = store.get_label(label_id)
        batch_id = store.get_label_batch(label_id)
        snapshot = {
            "label_id": label_id,
            "revision": label["revision"],
            "copy": label["copy"],
            "derived": label["derived"],
            "findings": store.findings_for_label(label_id),
        }
        # 冻结逐批次追溯采用的程序版本与检测记录：后续补录不改写已批准快照
        if batch_id:
            snapshot["batch_id"] = batch_id
            snapshot["batch_trace_evidence"] = trace.batch_trace_evidence(store, batch_id)
            # 冻结开工时锁定的投料批号/规格/用量：后续更正不改写已批准快照
            snapshot["material_allocations"] = \
                genealogy.batch_material_evidence(store, batch_id)["allocations"]
        approval = store.add_approval(label_id, body.approved_by, snapshot)
        store.set_label_status(label_id, "approved")
        return {"approval": approval, "label": _label_view(store, store.get_label(label_id))}

    @app.post("/labels/{label_id}/withdraw", tags=["标签生命周期"])
    def withdraw(label_id: str, body: WithdrawRequest, store: Store = Depends(get_store)):
        label = _label_or_404(store, label_id)
        if label["status"] != "approved":
            raise HTTPException(409, f"仅已批准的标签可撤回，当前状态 {label['status']}")
        store.set_label_status(label_id, "withdrawn")
        reason = f"标签撤回：{body.reason}"
        print_freeze = printing.freeze_for_label(store, label_id, reason=reason)
        store.log_event("label_withdrawn", {"label_id": label_id, "reason": body.reason,
                                            "print_freeze": print_freeze})
        view = _label_view(store, store.get_label(label_id))
        view["print_freeze"] = print_freeze
        return view

    @app.post("/labels/{label_id}/reanalyze", tags=["分析"])
    def reanalyze(label_id: str, batch_id: str | None = None,
                  store: Store = Depends(get_store)):
        """重新分析；可用 batch_id 改绑审核批次（草稿/复核中）。"""
        label = _label_or_404(store, label_id)
        if batch_id:
            batch = store.get_batch(batch_id)
            if batch is None:
                raise HTTPException(404, f"批次 {batch_id} 不存在")
            if batch["product_id"] != label["product_id"]:
                raise HTTPException(422, f"批次 {batch_id} 不属于产品 {label['product_id']}")
            store.set_label_batch(label_id, batch_id)
        return engine.analyze_label(store, label_id, batch_id=batch_id)

    @app.get("/labels/{label_id}/analysis", tags=["分析"])
    def analysis(label_id: str, store: Store = Depends(get_store)):
        _label_or_404(store, label_id)
        return engine.analyze_label(store, label_id)

    # ------------------------------------------------------------- 覆盖
    @app.post("/labels/{label_id}/findings/{finding_id}/override", tags=["审核"])
    def override_finding(label_id: str, finding_id: int, body: OverrideRequest,
                         store: Store = Depends(get_store)):
        """审核人覆盖自动结论：必须填写理由并关联可解析的输入证据。"""
        label = _label_or_404(store, label_id)
        if label["status"] not in ("draft", "in_review"):
            raise HTTPException(409, f"状态 {label['status']} 下不可覆盖发现项")
        finding = store.get_finding(finding_id)
        if finding is None or finding["label_id"] != label_id:
            raise HTTPException(404, f"发现项 {finding_id} 不属于标签 {label_id}")
        if finding["status"] == "resolved":
            raise HTTPException(409, "已消解的发现项无需覆盖")
        bad = [r for r in body.evidence_refs if not engine.validate_evidence_ref(store, r)]
        if bad:
            raise HTTPException(422, f"证据引用无法解析：{bad}；"
                                     "支持 spec:原料:版本 / declaration:原料:版本:过敏原 / "
                                     "line:产线 / recipe:产品:版本 / batch:批次 / "
                                     "cleaning:清洁记录 / program:程序@版本 / swab:拭子")
        override = {"reviewer": body.reviewer, "reason": body.reason,
                    "evidence_refs": body.evidence_refs}
        store.set_override(finding_id, override)
        return store.get_finding(finding_id)

    # ------------------------------------------------------------- 印刷标签批次
    def _print_batch_or_404(print_batch_id: str, store: Store) -> dict:
        pb = store.get_print_batch(print_batch_id)
        if pb is None:
            raise HTTPException(404, f"印刷批次 {print_batch_id} 不存在")
        return pb

    def _print_batch_view(store: Store, pb: dict) -> dict:
        return {**pb,
                "issuances": store.issuances_for_print_batch(pb["print_batch_id"]),
                "dispositions": store.dispositions_for_print_batch(pb["print_batch_id"])}

    @app.post("/print-batches", status_code=201, tags=["印刷标签批次"])
    def register_print_batch(body: PrintBatchCreate, store: Store = Depends(get_store)):
        """登记印刷标签批次入库：关联已批准标签修订，固化文案规范化摘要。"""
        if store.get_print_batch(body.print_batch_id):
            raise HTTPException(409, f"印刷批次 {body.print_batch_id} 已存在")
        label = _label_or_404(store, body.label_id)
        if label["status"] != "approved":
            raise HTTPException(409, f"标签修订须为 approved 才能送印，当前状态 "
                                    f"{label['status']}")
        if body.received_at and body.expires_at and body.expires_at < body.received_at:
            raise HTTPException(422, "失效时刻不得早于入库时刻")
        applicable = body.applicable_product_ids or [label["product_id"]]
        missing = [pid for pid in applicable if store.get_product(pid) is None]
        if missing:
            raise HTTPException(404, f"适用产品不存在：{sorted(set(missing))}")
        if label["product_id"] not in applicable:
            raise HTTPException(422, f"适用产品必须包含标签所属产品 {label['product_id']}")
        summary = printing.canonical_copy_summary(label["copy"])
        if body.copy_summary is not None:
            provided = printing.canonical_copy_summary(body.copy_summary.model_dump())
            if provided != summary:
                raise HTTPException(
                    422, {"error": "送印文案摘要与批准文案不一致，旧版/失效文案不得入库",
                          "diff": engine._copy_diff(
                              {"declared_allergens": provided["declared_allergens"],
                               "may_contain": provided["may_contain"],
                               "free_from_claims": provided["free_from_claims"]},
                              {"declared_allergens": summary["declared_allergens"],
                               "may_contain": summary["may_contain"],
                               "free_from_claims": summary["free_from_claims"]}),
                          "provided_ingredients_text": provided["ingredients_text"],
                          "approved_ingredients_text": summary["ingredients_text"]})
        pb = store.create_print_batch(
            body.print_batch_id, label["id"], label["product_id"],
            sorted(set(applicable)), summary, body.quantity_received,
            body.received_at, body.expires_at)
        return _print_batch_view(store, pb)

    @app.get("/print-batches/{print_batch_id}", tags=["印刷标签批次"])
    def get_print_batch(print_batch_id: str, store: Store = Depends(get_store)):
        return _print_batch_view(store, _print_batch_or_404(print_batch_id, store))

    @app.post("/print-batches/{print_batch_id}/issue", tags=["印刷标签批次"])
    def issue_print_batch(print_batch_id: str, body: PrintBatchIssue,
                          store: Store = Depends(get_store)):
        """领用放行：门禁逐项核对通过后绑定生产批次；幂等键重复时复用原结果。

        门禁顺序：标签仍 approved 且无 stale 标记；印刷批次可领用且未失效；
        待包装批次属于适用产品；余量充足；再把当前分析声明与批准快照、
        印刷摘要逐项对照，任一不符 409 并附差异路径。
        """
        prior = store.issuance_by_idempotency_key(body.idempotency_key)
        if prior is not None:
            if prior["print_batch_id"] != print_batch_id:
                raise HTTPException(
                    409, {"error": "幂等键已用于其他印刷批次",
                          "idempotency_key": body.idempotency_key,
                          "original_print_batch_id": prior["print_batch_id"]})
            if prior["production_batch_id"] != body.production_batch_id \
                    or prior["quantity"] != body.quantity:
                raise HTTPException(
                    409, {"error": "幂等键重复但请求内容不一致，已领用数量不可倒扣",
                          "idempotency_key": body.idempotency_key,
                          "original": {"production_batch_id": prior["production_batch_id"],
                                       "quantity": prior["quantity"]},
                          "conflicting": {"production_batch_id": body.production_batch_id,
                                          "quantity": body.quantity}})
            return {"issuance": prior, "reused": True,
                    "analysis_version": prior["analysis_version"]}

        pb = _print_batch_or_404(print_batch_id, store)
        label = store.get_label(pb["label_id"])
        if label is None:
            raise HTTPException(500, f"印刷批次 {print_batch_id} 的标签修订缺失，数据异常")
        # 先核对标签修订本身：撤回 / stale 的标签不得再放行（即使印刷批次尚未冻结）
        if label["status"] != "approved":
            raise HTTPException(409, {"error": "标签修订不再是 approved，禁止放行",
                                     "label_status": label["status"],
                                     "differences": [{
                                         "path": "label.status",
                                         "expected": "approved",
                                         "actual": label["status"]}]})
        if label["stale"]:
            raise HTTPException(409, {"error": "标签修订已带 stale 标记（规格/追溯资料变化），"
                                               "禁止放行",
                                     "differences": [{"path": "label.stale",
                                                      "expected": False, "actual": True}]})
        if pb["status"] != "available":
            raise HTTPException(409, {"error": f"印刷批次当前状态 {pb['status']}，不可领用",
                                     "status": pb["status"],
                                     "frozen_reason": pb.get("frozen_reason")})
        batch = store.get_batch(body.production_batch_id)
        if batch is None:
            raise HTTPException(404, f"生产批次 {body.production_batch_id} 不存在")
        if batch["product_id"] not in pb["applicable_product_ids"]:
            raise HTTPException(409, {"error": "产品不匹配：该印刷批次不适用于待包装批次"
                                               "所属产品",
                                     "differences": [{
                                         "path": "product_match",
                                         "expected": pb["applicable_product_ids"],
                                         "actual": batch["product_id"]}]})
        if pb["expires_at"]:
            # 以批次开工时刻核对失效；开工时刻缺失时按当前日期核对
            reference_date = (batch.get("started_at") or utcnow())[:10]
            if reference_date > pb["expires_at"][:10]:
                raise HTTPException(409, {"error": "印刷批次已失效，不得贴用于新批次",
                                         "differences": [{
                                             "path": "print_batch.expires_at",
                                             "expires_at": pb["expires_at"],
                                             "reference_date": reference_date,
                                             "batch_started_at": batch.get("started_at")}]})
        if body.quantity > pb["remaining_quantity"]:
            raise HTTPException(409, {"error": "领用数量超过印刷批次剩余数量",
                                     "quantity": body.quantity,
                                     "remaining_quantity": pb["remaining_quantity"],
                                     "differences": [{"path": "remaining_quantity",
                                                      "expected": f"<= {pb['remaining_quantity']}",
                                                      "actual": body.quantity}]})
        result = printing.evaluate_release(store, pb, batch, label)
        if not result["ok"]:
            raise HTTPException(409, {"error": "待包装批次当前分析与批准快照/印刷摘要"
                                               "逐项对照不符，禁止放行",
                                     "analysis_version": result["analysis_version"],
                                     "differences": result["differences"]})
        issued = printing.issue(store, pb, batch, body.quantity, body.idempotency_key)
        return {**issued,
                "print_batch": store.get_print_batch(print_batch_id)}

    @app.post("/print-batches/{print_batch_id}/dispose", tags=["印刷标签批次"])
    def dispose_print_batch(print_batch_id: str, body: PrintBatchDispose,
                            store: Store = Depends(get_store)):
        """冻结余量处置：可报废（scrap）或隔离（quarantine），须写明理由。"""
        pb = _print_batch_or_404(print_batch_id, store)
        if pb["status"] != "frozen":
            raise HTTPException(409, f"仅冻结中的印刷批次余量可处置，当前状态 "
                                    f"{pb['status']}")
        if body.quantity > pb["remaining_quantity"]:
            raise HTTPException(409, f"处置数量 {body.quantity} 超过剩余数量 "
                                    f"{pb['remaining_quantity']}")
        updated = store.create_disposition(print_batch_id, body.action,
                                           body.quantity, body.reason)
        return _print_batch_view(store, updated)

    # ------------------------------------------------------------- 投料谱系
    def _lot_view(store: Store, lot: dict) -> dict:
        return {**lot,
                "allocations": store.allocations_for_lot(lot["lot_id"]),
                "ledger": store.ledger_for_lot(lot["lot_id"])}

    @app.post("/lots", status_code=201, tags=["投料谱系"])
    def register_lot(body: MaterialLotCreate, store: Store = Depends(get_store)):
        """登记原料到货批号：供应商批号、对应规格版本、收货量、有效期与质检状态。"""
        if store.get_ingredient(body.ingredient_id) is None:
            raise HTTPException(404, f"原料 {body.ingredient_id} 不存在")
        if store.get_version(body.ingredient_id, body.spec_version) is None:
            raise HTTPException(
                404, f"原料 {body.ingredient_id} 的规格版本 {body.spec_version} 不存在")
        if store.get_lot(body.lot_id):
            raise HTTPException(409, f"原料批号 {body.lot_id} 已存在")
        if store.lot_by_supplier_no(body.ingredient_id, body.supplier_lot_no):
            raise HTTPException(
                409, f"原料 {body.ingredient_id} 的供应商批号 "
                     f"{body.supplier_lot_no} 已登记，不得重复到货")
        if body.received_at and body.expires_at and body.expires_at < body.received_at:
            raise HTTPException(422, "有效期不得早于收货时刻")
        lot = store.create_lot(
            body.lot_id, body.ingredient_id, body.supplier_lot_no, body.spec_version,
            body.quantity_received, body.received_at, body.expires_at, body.status)
        return _lot_view(store, lot)

    @app.get("/lots/{lot_id}", tags=["投料谱系"])
    def get_lot(lot_id: str, store: Store = Depends(get_store)):
        lot = store.get_lot(lot_id)
        if lot is None:
            raise HTTPException(404, f"原料批号 {lot_id} 不存在")
        return _lot_view(store, lot)

    @app.put("/lots/{lot_id}/status", tags=["投料谱系"])
    def set_lot_status(lot_id: str, body: LotStatusUpdate, store: Store = Depends(get_store)):
        """变更批号质检状态（待检/放行/隔离）；仅放行状态可投料。"""
        if store.get_lot(lot_id) is None:
            raise HTTPException(404, f"原料批号 {lot_id} 不存在")
        lot = store.set_lot_status(lot_id, body.status, body.reason)
        return _lot_view(store, lot)

    @app.post("/batches/{batch_id}/allocations", tags=["投料谱系"])
    def allocate_lots(batch_id: str, body: AllocationCreate, response: Response,
                      store: Store = Depends(get_store)):
        """为生产批次分配一个或多个原料批号及用量（开工时锁定投料规格）。

        门禁：批号须已放行、未过期（按批次开工时刻，缺省按当前日期）、余量
        充足、规格符合配方（配方项锁定版本须一致且原料须在配方中）；任一不
        符整体拒绝（409 + failures），不部分扣量。幂等键唯一：同键同内容
        重放复用原结果、不重复扣量；同键内容冲突返回 409。
        """
        batch = _batch_or_404(batch_id, store)
        payload = {"batch_id": batch_id,
                   "items": sorted(({"lot_id": i.lot_id, "quantity": i.quantity}
                                    for i in body.items),
                                   key=lambda x: (x["lot_id"], x["quantity"]))}
        # 幂等检查、门禁与扣量写入在同一事务内原子完成
        outcome = genealogy.allocate_lots_atomic(
            store, batch, body.idempotency_key, payload,
            [i.model_dump() for i in body.items])
        if outcome["outcome"] == "conflict":
            prior = outcome["prior"]
            raise HTTPException(
                409, {"error": "幂等键重复但请求内容不一致，已扣量不可倒扣",
                      "idempotency_key": body.idempotency_key,
                      "original": prior["payload"],
                      "conflicting": payload})
        if outcome["outcome"] == "reused":
            prior = outcome["prior"]
            response.status_code = 200
            return {"request_id": prior["request_id"], "batch_id": batch_id,
                    "allocations": [store.get_allocation(a)
                                    for a in prior["allocation_ids"]],
                    "reused": True}
        if outcome["outcome"] == "failed":
            raise HTTPException(
                409, {"error": "投料分配未通过门禁，未扣量",
                      "failures": outcome["failures"]})
        response.status_code = 201
        return {"request_id": outcome["request_id"], "batch_id": batch_id,
                "allocations": outcome["allocations"], "reused": False}

    @app.post("/allocations/{allocation_id}/reverse", tags=["投料谱系"])
    def reverse_allocation(allocation_id: str, body: AllocationReverse,
                           store: Store = Depends(get_store)):
        """开工前撤销分配：记一笔 reverse 反向流水恢复批号余量。

        批次已开工（started_at 不晚于当前日期）后不可撤销；撤销后该批次若
        不再有任何有效投料记录，来源图回退为现行规格展开。
        """
        allocation = store.get_allocation(allocation_id)
        if allocation is None:
            raise HTTPException(404, f"投料分配 {allocation_id} 不存在")
        if allocation["status"] != "active":
            raise HTTPException(409, f"分配 {allocation_id} 已撤销，不得重复操作")
        batch = store.get_batch(allocation["batch_id"])
        started = (batch.get("started_at") or "")[:10] if batch else ""
        if started and started <= utcnow()[:10]:
            raise HTTPException(
                409, {"error": f"批次 {allocation['batch_id']} 已开工"
                               f"（{batch['started_at']}），不得撤销投料分配",
                      "batch_started_at": batch["started_at"]})
        result = genealogy.reverse_allocation(store, allocation, body.reason)
        # 撤销改变批次实际投料：草稿/复核中标签按同一批次重新分析
        actions = engine.apply_batch_impact(
            store, [allocation["batch_id"]], positive=False,
            reason=f"撤销投料分配 {allocation_id}：{body.reason}")
        return {**result, "actions": actions}

    # ------------------------------------------------------------- 包装执行与卷标结算
    def _run_or_404(run_id: str, store: Store) -> dict:
        run = store.get_packaging_run(run_id)
        if run is None:
            raise HTTPException(404, f"包装运行 {run_id} 不存在")
        return run

    @app.post("/packaging-runs", status_code=201, tags=["包装执行"])
    def start_packaging_run(body: PackagingRunCreate, store: Store = Depends(get_store)):
        """开工登记：绑定生产批次、领用记录、包装线、计划产量与每件用标数，
        并登记清场发现。

        门禁（all-or-nothing，任一不符不登记任何记录）：清场发现旧卷标未隔离；
        领用记录不存在 / 属于其他批次 / 已绑定其他运行；领用横跨不同标签修订；
        卷标已冻结或标签修订已撤回 / 带 stale 标记；印刷批次适用产品不含
        待包装批次产品。
        """
        batch = store.get_batch(body.production_batch_id)
        if batch is None:
            raise HTTPException(404, f"生产批次 {body.production_batch_id} 不存在")
        if store.get_line(body.line_id) is None:
            raise HTTPException(404, f"包装线 {body.line_id} 不存在")
        outcome = packaging.start_run(
            store, body.run_id, batch, body.line_id, body.planned_quantity,
            body.labels_per_unit, body.issuance_ids, body.operator,
            [f.model_dump() for f in body.clearance_findings])
        if outcome["outcome"] == "conflict":
            raise HTTPException(409, f"包装运行 {body.run_id} 已存在")
        if outcome["outcome"] == "failed":
            raise HTTPException(
                409, {"error": "开工门禁未通过，未登记包装运行",
                      "failures": outcome["failures"]})
        return packaging.run_view(store, body.run_id)

    @app.get("/packaging-runs/{run_id}", tags=["包装执行"])
    def get_packaging_run(run_id: str, store: Store = Depends(get_store)):
        """运行完整视图：绑定、清场发现、用标/调整事件、结算记录与实时对账。"""
        _run_or_404(run_id, store)
        return packaging.run_view(store, run_id)

    @app.get("/packaging-runs/{run_id}/reconciliation", tags=["包装执行"])
    def run_reconciliation(run_id: str, store: Store = Depends(get_store)):
        """实时对账（不落结算记录）：两条平衡等式与各数量来源拆分。"""
        run = _run_or_404(run_id, store)
        return packaging.reconciliation(store, run)

    @app.post("/packaging-runs/{run_id}/events", tags=["包装执行"])
    def record_packaging_event(run_id: str, body: PackagingEventCreate,
                               response: Response, store: Store = Depends(get_store)):
        """记录用标事件（合格品贴用/过程损耗/留样/退回隔离），保留操作者与时刻。

        幂等键唯一：同键同内容重放复用原事件（200），同键内容冲突 409。
        已结算运行不再接受用标事件，仅接受盘点调整。退回隔离数量留在领用方
        账上，不补回印刷批次可领用余量。
        """
        run = _run_or_404(run_id, store)
        if run["status"] != "open":
            raise HTTPException(
                409, {"error": f"包装运行已结算（状态 {run['status']}），"
                               f"不再接受用标事件；盘点更正请使用调整事件",
                      "run_status": run["status"]})
        if body.kind == "applied":
            if body.good_units is None:
                raise HTTPException(422, "合格品贴用事件必须给出合格品数 good_units")
            expected = body.good_units * run["labels_per_unit"]
            if body.quantity != expected:
                raise HTTPException(
                    422, {"error": "贴用量与合格品数×每件用标数不符",
                          "quantity": body.quantity,
                          "good_units": body.good_units,
                          "labels_per_unit": run["labels_per_unit"],
                          "expected_quantity": expected})
        elif body.good_units is not None:
            raise HTTPException(422, "仅合格品贴用事件可携带合格品数 good_units")
        outcome = packaging.record_event(
            store, run, body.idempotency_key, body.kind, body.quantity,
            body.good_units, body.operator, body.occurred_at, body.reason)
        if outcome["outcome"] == "conflict":
            prior = outcome["prior"]
            raise HTTPException(
                409, {"error": "幂等键重复但事件内容不一致，已记录事件不可改写",
                      "idempotency_key": body.idempotency_key,
                      "original": {"run_id": prior["run_id"], "kind": prior["kind"],
                                   "quantity": prior["quantity"],
                                   "good_units": prior["good_units"],
                                   "operator": prior["operator"]},
                      "conflicting": {"run_id": run_id, "kind": body.kind,
                                      "quantity": body.quantity,
                                      "good_units": body.good_units,
                                      "operator": body.operator}})
        if outcome["outcome"] == "reused":
            response.status_code = 200
            return {"event": outcome["event"], "reused": True}
        response.status_code = 201
        return {"event": outcome["event"], "reused": False}

    @app.post("/packaging-runs/{run_id}/adjustments", tags=["包装执行"])
    def record_packaging_adjustment(run_id: str, body: PackagingAdjustmentCreate,
                                    response: Response,
                                    store: Store = Depends(get_store)):
        """盘点更正调整事件：结算后记录不可覆盖，更正以有符号增量追加。

        调整必须写明理由；不得使类别合计或合格品数为负。追加后重新结算
        即按最新合计重新判定，历史结算记录保持不可覆盖。
        """
        run = _run_or_404(run_id, store)
        if body.delta == 0 and body.good_units_delta == 0:
            raise HTTPException(422, "delta 与 good_units_delta 不得同时为 0")
        if body.category != "applied" and body.good_units_delta != 0:
            raise HTTPException(422, "仅 category=applied 的调整可携带合格品数增量")
        outcome = packaging.record_adjustment(
            store, run, body.idempotency_key, body.category, body.delta,
            body.good_units_delta, body.reason, body.operator, body.occurred_at)
        if outcome["outcome"] == "conflict":
            prior = outcome["prior"]
            raise HTTPException(
                409, {"error": "幂等键重复但调整内容不一致，已记录事件不可改写",
                      "idempotency_key": body.idempotency_key,
                      "original": {"run_id": prior["run_id"],
                                   "category": prior["category"],
                                   "quantity": prior["quantity"],
                                   "good_units": prior["good_units"],
                                   "operator": prior["operator"]},
                      "conflicting": {"run_id": run_id, "category": body.category,
                                      "delta": body.delta,
                                      "good_units_delta": body.good_units_delta,
                                      "operator": body.operator}})
        if outcome["outcome"] == "failed":
            raise HTTPException(
                409, {"error": "调整未通过门禁，未记录",
                      "failures": outcome["failures"]})
        if outcome["outcome"] == "reused":
            response.status_code = 200
            return {"event": outcome["event"], "reused": True}
        response.status_code = 201
        return {"event": outcome["event"], "reused": False}

    @app.post("/packaging-runs/{run_id}/settle", tags=["包装执行"])
    def settle_packaging_run(run_id: str, body: PackagingSettle,
                             store: Store = Depends(get_store)):
        """卷标结算：两条平衡等式同时满足才落结算记录（append-only）。

          领用量 = 贴用量 + 损耗量 + 留样量 + 退回隔离量
          贴用量 = 合格品数 × 每件用标数

        不平衡时 409 并返回各数量来源（领用记录、用标事件与调整事件分列）；
        盘点更正追加调整事件后重新结算，生成新的结算记录，历史记录不可覆盖。
        """
        _run_or_404(run_id, store)
        outcome = packaging.settle(store, run_id, body.settled_by)
        if outcome["outcome"] == "discrepancy":
            raise HTTPException(
                409, {"error": "结算不平衡：领用量/贴用量与账面记录存在差异",
                      "reconciliation": outcome["reconciliation"]})
        return {"settlement": outcome["settlement"],
                "reconciliation": outcome["reconciliation"],
                "run": packaging.run_view(store, run_id)}

    # ------------------------------------------------------------- 报告
    @app.get("/labels/{label_id}/check-package", tags=["报告"])
    def check_package(label_id: str, store: Store = Depends(get_store)):
        _label_or_404(store, label_id)
        return report.build_check_package(store, label_id)

    @app.get("/labels/{label_id}/review-sheet", response_class=HTMLResponse, tags=["报告"])
    def review_sheet(label_id: str, store: Store = Depends(get_store)):
        _label_or_404(store, label_id)
        return report.build_review_sheet(store, label_id)

    @app.get("/samples/compound-coline", tags=["报告"])
    def sample_compound_coline():
        """含复合原料与共线冲突的完整请求样例（可直接按步骤重放）。"""
        path = SAMPLES_DIR / "compound_coline.json"
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/events", tags=["审核"])
    def events(store: Store = Depends(get_store)):
        return store.events()

    return app


app = create_app()
