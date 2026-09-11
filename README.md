# 食品标签审核 API

供应商更换后，复合原料的次级配料与交叉接触声明会变化，旧标签未必同步。本服务为标签审核提供：

- **来源图展开**：从配方出发递归展开复合原料，定位每种过敏原的来源路径；
- **声明推导**：由供应商声明与产线共线情况推导应声明项与交叉接触提示；
- **问题检测**：资料缺口、别名混用、循环引用、无法展开的复合原料、“无某过敏原”宣称与资料的矛盾；
- **生命周期**：草拟 → 复核 → 批准 → 撤回，未决矛盾阻止批准；
- **影响传播**：配方/规格更新沿依赖图标记受影响产品，批准记录只读并派生新修订；
- **审核覆盖**：审核人可覆盖自动结论，但必须填写理由并关联输入证据；
- **报告**：两版声明比较（增删 + 波及范围）、JSON 核对包、可打印审查单、请求样例。

## 运行

```bash
pip install -r requirements.txt
uvicorn label_audit.main:app --reload          # 默认内存库
# 持久化：LABEL_DB 未内置环境变量，使用 create_app("audit.db") 指定 SQLite 文件
python -m pytest tests/                        # 15 个端到端测试
```

交互文档：`http://localhost:8000/docs`。

## 数据流

```
配方(版本) ──┐
原料规格版本 ─┼─> 来源图展开 ─> 过敏原证据(带路径) ─> 推导应声明项 ─┐
供应商声明 ──┘      │                                            ├─> 发现项
产线共线 ──────────┴────────────────────────────────────────────┘
                                        标签文案 ──> 比对 ──> 批准/阻断
```

## 检测规则

| kind | 级别 | 含义 |
|---|---|---|
| `data_gap` | blocker | 未知原料引用 / 缺规格版本 / 缺供应商声明 / 过敏原状态未知 |
| `alias_conflict` | blocker / warning | 引用命中多个原料（blocker）；同一名称或别名被多个原料使用（warning） |
| `circular_reference` | blocker | 复合原料子成分沿路径回到祖先 |
| `unexpandable_compound` | blocker | 标记为复合原料但无子成分拆分 |
| `missing_declaration` | blocker | 推导应声明而标签未声明 |
| `missing_cross_contact` | warning | 交叉接触风险未在标签提示 |
| `unnecessary_declaration` | warning | 标签声明了推导不出的过敏原 |
| `claim_contradiction` | blocker | “无某过敏原”宣称与原料/共线资料矛盾 |

批准前自动重新分析；存在 `open` 状态的 blocker 即拒绝（409）。

## 覆盖（override）

`POST /labels/{id}/findings/{fid}/override`，必须给出 `reason` 与可解析的 `evidence_refs`：

```
spec:{ingredient_id}:{version}                 原料规格版本
declaration:{ingredient_id}:{version}:{allergen}   某条供应商声明
line:{line_id}                                 产线
recipe:{product_id}:{version}                  配方版本
```

覆盖按发现项指纹（kind + 主体）保留，重新分析不会丢失；资料变化导致发现项消解时自动转为 `resolved`。

## 影响传播与只读批准记录

- 新规格版本 / 新配方 / 共线变更 → 沿依赖图找出引用该产品或原料的所有标签；
- 草稿、复核中的标签标记 `stale`；已批准标签标记 `stale` 并**派生新修订**（草稿、重新分析）；
- `approvals` 表只插不改，批准快照（文案 + 推导 + 发现项）永久保留。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingredients` · `/ingredients/{id}/versions` | 原料与规格版本（含子成分、供应商声明） |
| POST | `/products` · `/products/{id}/recipes` | 产品与配方版本 |
| POST | `/lines` · `/products/{id}/lines/{line_id}` | 产线与共线登记 |
| GET | `/products/{id}/source-graph` | 来源图 + 推导 + 发现项 |
| GET | `/products/{id}/impact` | 依赖视图与标签过期状态 |
| GET | `/products/{id}/labels/compare?from_revision=&to_revision=` | 两版声明增删 + 波及范围 |
| POST | `/labels` · `/labels/{id}/submit` · `/approve` · `/withdraw` | 生命周期 |
| PUT | `/labels/{id}/copy` | 修改文案（草稿/复核中） |
| GET | `/labels/{id}/analysis` · POST `/labels/{id}/reanalyze` | 分析 |
| POST | `/labels/{id}/findings/{fid}/override` | 覆盖自动结论 |
| GET | `/labels/{id}/check-package` | JSON 核对包 |
| GET | `/labels/{id}/review-sheet` | 可打印审查单（HTML） |
| GET | `/samples/compound-coline` | 复合原料 + 共线冲突请求样例 |
| GET | `/events` | 审计事件日志 |

## 请求样例

`samples/compound_coline.json`：巧克力曲奇——巧克力豆（复合原料）的子成分大豆磷脂含大豆，供应商声明可能含奶，产线共线处理奶与花生，而标签宣称“无奶”。按 `steps` 顺序重放即可复现：应声明缺失、宣称矛盾、交叉接触未提示、资料缺口，以及批准被 409 阻断。也可直接 `GET /samples/compound-coline` 获取。

## 目录

```
label_audit/
  main.py     FastAPI 路由与生命周期状态机
  models.py   Pydantic 请求校验
  engine.py   规则引擎：展开、推导、检测、影响、比较
  db.py       SQLite 持久层（approvals 只读）
  report.py   JSON 核对包 / 可打印审查单
samples/compound_coline.json   请求样例
tests/test_api.py              15 个端到端测试
```
