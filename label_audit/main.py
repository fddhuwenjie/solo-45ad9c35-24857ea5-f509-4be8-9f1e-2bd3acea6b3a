"""FastAPI 路由：资料登记、标签生命周期、分析、比较与报告。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import __version__, engine, report, trace
from .db import Store
from .models import (
    ApproveRequest,
    BatchCreate,
    CleaningProgramCreate,
    CleaningRecordCreate,
    IngredientCreate,
    IngredientVersionCreate,
    LabelCopyUpdate,
    LabelCreate,
    LineCreate,
    OverrideRequest,
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
        """登记新规格版本（如供应商更换），随后沿依赖图标记受影响产品。"""
        if store.get_ingredient(ingredient_id) is None:
            raise HTTPException(404, f"原料 {ingredient_id} 不存在")
        if store.get_version(ingredient_id, body.version):
            raise HTTPException(409, f"版本 {body.version} 已存在；规格版本不可变，请登记新版本号")
        ver = store.add_ingredient_version(
            ingredient_id, body.version,
            [s.model_dump() for s in body.sub_components],
            [d.model_dump() for d in body.supplier_declarations],
        )
        impacted = engine.impacted_products(store, [ingredient_id])
        actions = engine.apply_impact(store, impacted, reason=f"新规格版本 {ingredient_id}@{body.version}")
        return {"version": ver, "impacted_products": sorted(impacted), "actions": actions}

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
                "rework_out": store.rework_from(batch_id)}

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
        exp = engine.expand_recipe(store, product_id)
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
        approval = store.add_approval(label_id, body.approved_by, snapshot)
        store.set_label_status(label_id, "approved")
        return {"approval": approval, "label": _label_view(store, store.get_label(label_id))}

    @app.post("/labels/{label_id}/withdraw", tags=["标签生命周期"])
    def withdraw(label_id: str, body: WithdrawRequest, store: Store = Depends(get_store)):
        label = _label_or_404(store, label_id)
        if label["status"] != "approved":
            raise HTTPException(409, f"仅已批准的标签可撤回，当前状态 {label['status']}")
        store.set_label_status(label_id, "withdrawn")
        store.log_event("label_withdrawn", {"label_id": label_id, "reason": body.reason})
        return _label_view(store, store.get_label(label_id))

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
