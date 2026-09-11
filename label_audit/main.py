"""FastAPI 路由：资料登记、标签生命周期、分析、比较与报告。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import __version__, engine, report
from .db import Store
from .models import (
    ApproveRequest,
    IngredientCreate,
    IngredientVersionCreate,
    LabelCopyUpdate,
    LabelCreate,
    LineCreate,
    OverrideRequest,
    ProductCreate,
    RecipeCreate,
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
    return {
        **label,
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

    # ------------------------------------------------------------- 来源图与影响
    @app.get("/products/{product_id}/source-graph", tags=["分析"])
    def source_graph(product_id: str, store: Store = Depends(get_store)):
        if store.get_product(product_id) is None:
            raise HTTPException(404, f"产品 {product_id} 不存在")
        exp = engine.expand_recipe(store, product_id)
        derived = engine.derive_declarations(store, product_id, exp)
        return {
            "product_id": product_id,
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
        """新建标签草稿（或同一产品的下一修订），创建即自动分析。"""
        if store.get_product(body.product_id) is None:
            raise HTTPException(404, f"产品 {body.product_id} 不存在")
        label = store.create_label(body.product_id, body.label_copy.model_dump())
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
        snapshot = {
            "label_id": label_id,
            "revision": label["revision"],
            "copy": label["copy"],
            "derived": label["derived"],
            "findings": store.findings_for_label(label_id),
        }
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
    def reanalyze(label_id: str, store: Store = Depends(get_store)):
        _label_or_404(store, label_id)
        return engine.analyze_label(store, label_id)

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
                                     "line:产线 / recipe:产品:版本")
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
