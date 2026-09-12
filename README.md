# 食品标签审核 API

供应商更换后，复合原料的次级配料与交叉接触声明会变化，旧标签未必同步。同一条产线做过花生产品，后续批次也不必永久标注“可能含花生”；反过来，清洁记录残缺时，单凭一张阴性拭子单同样不足以消除风险。本服务为标签审核提供：

- **来源图展开**：从配方出发递归展开复合原料，定位每种过敏原的来源路径；
- **逐批次共线追溯**：登记生产顺序、设备段、清洁程序版本及有效期、程序覆盖的过敏原、必检采样点、拭子定量结果与返工料去向，从待审批次向前追溯最近含敏原批次与返工路径；
- **声明推导**：由供应商声明与逐批次追溯结果推导应声明项与交叉接触提示；
- **问题检测**：资料缺口、别名混用、循环引用、无法展开的复合原料、“无某过敏原”宣称与资料的矛盾；
- **生命周期**：草拟 → 复核 → 批准 → 撤回，未决矛盾阻止批准；
- **影响传播**：配方/规格更新沿依赖图标记受影响产品；拭子结果补录沿返工影响链标记批次标签；批准记录只读并派生新修订；
- **投料谱系**：到货批号记录供应商批号、对应规格、收货量、有效期与待检/放行/隔离状态；为生产批次分配一个或多个批号及用量（未放行/已过期/余量不足/规格不符配方均拒绝，幂等键防重复扣量）；来源图按开工锁定的投料规格展开，用料缺项记证据空白；供应商更正沿扣料关系圈出涉事成品、标签与已发放卷标；开工前可撤销分配并记反向流水；
- **印刷批次领用放行**：印刷卷标按批准修订入库（规范化文案摘要），领用时核对标签仍 approved、无 stale、产品匹配、未失效、余量充足，并把待包装批次当前分析与批准快照/印刷摘要逐项对照；撤回、规格变化、阳性拭子冻结剩余卷标并列出处置批次；
- **包装执行与卷标结算**：开工绑定生产批次、领用记录、包装线、计划产量与每件用标数并登记清场发现（旧卷标未隔离、领用横跨不同标签修订、卷标已冻结、适用产品不符均阻止开工）；合格品贴用/过程损耗/留样/退回隔离以幂等事件追加并保留操作者与时刻；结算同时满足「领用量=贴用+损耗+留样+退回隔离」与「贴用量=合格品数×每件用标数」，差异返回数量来源，退回量不补回可领用余量；结算记录不可覆盖，盘点更正追加调整事件并重新判定；撤回/更正时列出未结算现场余量、已包装数量与待隔离批次；
- **审核覆盖**：审核人可覆盖自动结论，但必须填写理由并关联输入证据；
- **报告**：两版声明比较（增删 + 波及范围）、JSON 核对包、可打印审查单、请求样例。

## 运行

```bash
pip install -r requirements.txt
uvicorn label_audit.main:app --reload          # 默认内存库
# 持久化：LABEL_DB 未内置环境变量，使用 create_app("audit.db") 指定 SQLite 文件
python -m pytest tests/                        # 端到端测试（95 个，含 22 个包装执行测试）
```

交互文档：`http://localhost:8000/docs`。

## 数据流

```
配方(版本) ──┐
原料规格版本 ─┼─> 来源图展开 ─> 过敏原证据(带路径) ─> 推导应声明项 ─┐
供应商声明 ──┘        ↑ 批次有投料记录时按开工锁定规格展开          ├─> 发现项
前序含敏原批次 ─┐                                            ┌─┘
清洁程序(有效期/覆盖过敏原) ─┼─> 逐批次追溯 ─> 开放/关闭路径 ─┘
清洁记录/拭子定量(必检点) ─┤            ↑ 返工料去向（可跨产品、可传递）
到货批号 ─> 投料分配(锁定规格/扣量流水) ─> 供应商更正沿扣料关系波及
标签文案 ───────────────────────────────> 比对 ──> 批准/阻断
印刷批次 ─> 领用放行(逐项对照) ─> 包装运行(清场/开工绑定) ─> 用标事件 ─> 卷标结算
```

## 逐批次追溯

一条（过敏原 → 来源批次 → 设备段/返工）路径只有在以下条件**全部**满足时才关闭：

1. 批次生产顺序明确（同产线按 `sequence` 可定位最近含敏原批次）；
2. 待审批次登记的每个相关设备段都有清洁执行记录，记录产线与批次产线一致；
3. 清洁引用的程序版本存在、适用于该产线（`line_id` 为空表示通用；指定 L2 的程序不能关闭 L1 路径）、覆盖该过敏原，且清洁日期处于程序有效期内；
4. 程序声明的每个必检采样点都按对应过敏原采样（漏采即开放）；
5. 拭子定量结果已出具且不超过程序限值（等于限值合格；待出/超限均开放，超限为 blocker）。

返工料另作区分：源批次**成品本身含**的过敏原属于组分携带（`component_carried_over`），目标批次设备清洁只处理残留、不能去除已混入的组分，路径恒开放；源批次自身未关闭的设备残留路径则沿返工链（可跨产品、可传递）传播。

| evidence_gap 代码 | 含义 |
|---|---|
| `order_unknown` | 批次生产顺序不明，无法定位最近含敏原批次 |
| `missing_cleaning` | 设备段无清洁记录 / 批次未登记设备段 |
| `program_unknown` | 清洁记录引用的程序版本不存在 |
| `program_wrong_line` | 程序或清洁记录属于其他产线 |
| `program_not_covering` | 程序不覆盖该过敏原 |
| `validation_expired` | 清洁时程序不在有效期 / 清洁日期缺失无法核对 |
| `missing_swab` | 必检采样点漏采 |
| `swab_pending` | 已采样但定量结果未出 |
| `swab_exceeded` | 拭子定量结果超限（blocker） |
| `component_carried_over` | 返工源批次成品含该过敏原，清洁无法去除组分 |

未登记任何批次时，推导回退为旧的产线 `allergens_handled` 静态保守推导。

## 检测规则

| kind | 级别 | 含义 |
|---|---|---|
| `data_gap` | blocker | 未知原料引用 / 缺规格版本 / 缺供应商声明 / 过敏原状态未知 / 拭子阳性超限（`missing=cleaning_validation`）/ 批次用料记录缺项（`missing=material_allocation`） |
| `alias_conflict` | blocker / warning | 引用命中多个原料（blocker）；同一名称或别名被多个原料使用（warning） |
| `circular_reference` | blocker | 复合原料子成分沿路径回到祖先 |
| `unexpandable_compound` | blocker | 标记为复合原料但无子成分拆分 |
| `missing_declaration` | blocker | 推导应声明而标签未声明 |
| `missing_cross_contact` | warning | 开放交叉接触路径未在标签提示 |
| `unnecessary_declaration` | warning | 标签声明了推导不出的过敏原 |
| `claim_contradiction` | blocker | “无某过敏原”宣称与原料资料或开放共线/返工路径矛盾 |

批准前自动重新分析；存在 `open` 状态的 blocker 即拒绝（409）。

## 覆盖（override）

`POST /labels/{id}/findings/{fid}/override`，必须给出 `reason` 与可解析的 `evidence_refs`：

```
spec:{ingredient_id}:{version}                 原料规格版本
declaration:{ingredient_id}:{version}:{allergen}   某条供应商声明
line:{line_id}                                 产线
recipe:{product_id}:{version}                  配方版本
batch:{batch_id}                               生产批次
program:{program_id}@{version}                 清洁程序版本
cleaning:{record_id}                           清洁执行记录
swab:{swab_id}                                 拭子结果
```

覆盖按发现项指纹（kind + 主体）保留，重新分析不会丢失；资料变化导致发现项消解时自动转为 `resolved`。

## 影响传播与只读批准记录

- 新规格版本 / 新配方 / 共线变更 → 沿依赖图找出引用该产品或原料的所有标签；
- 草稿、复核中的标签标记 `stale`；已批准标签标记 `stale` 并**派生新修订**（草稿、重新分析）；
- `PUT /swabs/{id}/result` 补录拭子定量结果时自动处理：草稿/复核中标签立即重新分析（阴性补录可消解“结果待出”的开放路径）；结果超限且采样点/过敏原属程序验证对象时，沿返工影响链传播，已批准标签标记 `stale` 并派生新修订，调用方无需手工触发；
- `approvals` 表只插不改；批准快照永久保留文案、推导、发现项，以及**逐批次追溯采用的清洁程序版本、清洁记录与拭子结果**——后续补录不再改写已冻结快照。

## 投料谱系

同名原料的到货批号可能分别采用不同规格版本；只记配方版本时，供应商更正过敏原声明后无法圈出真正消耗过该批原料的成品。投料谱系把“哪一批原料投进了哪个生产批次”落成可审计的扣量关系：

1. `POST /lots` 登记到货批号：供应商批号（同一原料下唯一）、对应规格版本、收货量、有效期与质检状态（`pending` 待检 / `released` 放行 / `quarantined` 隔离）；`PUT /lots/{id}/status` 变更质检状态。
2. `POST /batches/{batch_id}/allocations` 为生产批次分配一个或多个批号及用量，门禁逐项核对：
   - 批号须为 `released`（待检/隔离拒绝，`not_released`）；
   - 未过期（按批次开工时刻，缺省按当前日期，`expired`）；
   - 余量充足（同一请求内相同批号用量合并核对，`insufficient_quantity`）；
   - 规格符合配方：原料须在当前配方中（`ingredient_not_in_recipe`），配方项锁定版本时批号规格须一致（`spec_mismatch`）。

   任一不符整体拒绝（409 + `failures`），不部分扣量。幂等键唯一：同一幂等键的**幂等检查、门禁评估（含余量读取）、请求登记、分配与库存流水在同一个串行化事务内原子提交**——并发同键请求只扣量一次、只留一组分配与流水；同键同内容重放复用原结果（不重复扣量），同键内容冲突 409——已扣量只增不减，没有倒扣入口；冲突与门禁失败路径不残留任何副作用。
3. **锁定规格展开**：批次一旦存在有效投料记录，来源图（`/source-graph`、标签分析、放行门禁、核对包）按开工时锁定的投料规格展开，证据带 `lot_ids`；配方项缺用料记录时记 `data_gap`（`missing=material_allocation`，blocker）证据空白，**不回退最新规格**。批次完全没有有效投料记录时，只要配方中任一原料已启用批号管理（存在到货批号），同样逐项记 `material_allocation` 证据空白、推导为空，**不得回退当前或最新规格**；仅当配方原料均未启用批号管理时才保持旧的现行规格展开（兼容旧资料）。
4. **供应商更正波及链**：登记新规格版本时可用 `corrects_version` 显式指定本次被更正的旧规格（缺省取登记前最新版本）；若该原料已有到货批号，则**只沿被更正规格对应批号的实际扣料关系**定位批次——其他规格批号不纳入 `affected_lots`。受影响标签按批次影响链处理（草稿/复核中重新分析，已批准标 stale 并派生新修订），该修订下仍有余量的印刷批次冻结；响应 `correction` 列明被更正规格（`corrected_versions`）、涉事批号与用量（`affected_lots.consumed_by`）、声明差异（`declaration_changes`）与处置边界（`disposition_boundary`：受影响标签、冻结卷标、已发放到包装现场的生产批次）。引用该原料但批次无有效投料记录的产品无法证明未消耗，列入 `consumption_unknown`（`material_allocation` 证据空白），**不得判为未受影响**；只有每个批次都能以投料记录证明未消耗被更正规格的产品才进入 `unaffected_products` 保持原状态。原料尚无到货批号时回退旧的依赖图影响传播。
5. `POST /allocations/{id}/reverse` 开工前撤销分配：记一笔 `reverse` 反向流水恢复批号余量；批次已开工（`started_at` 不晚于当前日期）后不可撤销。全部撤销后批次回到“无有效投料记录”，来源图按上条规则记证据空白。
6. 审计还原：批次查询（`GET /batches/{id}`）带 `allocations` 与 `lot_ledger`；批准快照冻结当时的投料批号/规格/用量（`material_allocations`）；核对包 `material_genealogy` 汇总分配、批号、扣量流水与波及该批次的更正事件；审查单含投料谱系小节；事件日志记录 `lot_registered / lot_status_changed / lots_allocated / allocation_reversed / supplier_correction`。

## 印刷标签批次领用放行

现场若只核对产品名，换版或阳性拭子补录后旧版卷标仍可能被贴上新产品批次。印刷卷标按“印刷批次”单独管理，放行是批准快照与待包装批次当前分析之间的最后核对：

1. `POST /print-batches` 入库登记：关联**已批准**标签修订；服务端按批准文案生成规范化摘要（过敏原大小写归一、排序去重，配料表压缩空白），请求自带 `copy` 摘要时必须一致，否则 422；登记数量、入库/失效时刻与适用产品（缺省仅标签所属产品，且必须包含它）。
2. `POST /print-batches/{id}/issue` 领用放行，门禁依次核对：
   - 标签修订仍为 `approved`、无 `stale` 标记（撤回/规格变化/阳性传播后即失效）；
   - 印刷批次 `available`、未失效（按批次开工时刻，缺省按当前日期）、余量充足；
   - 待包装批次所属产品在适用产品清单内；
   - 待包装批次**所属产品**（跨产品领用时按该产品当前配方/规格，而非标签修订所属产品）**当前重新推导**的应声明/交叉接触项与批准快照逐项一致，印刷摘要与批准文案逐项一致，且没有新增开放 blocker（阳性拭子等硬证据失败）。

   任一不符返回 409，`detail.differences` 给出差异路径（如 `derived.may_contain.extra[peanut]`、`print_summary.declared_allergens.missing[milk]`、`label.stale`、`print_batch.expires_at`、`product_match`、`remaining_quantity`），不写领用记录。
3. 领用记录绑定生产批次、实际数量与**幂等键**：同键重放复用原领用结果（含原 `issuance_id`/分析版本，不重复扣减）；同键内容冲突返回 409——已领用数量只增不减，没有倒扣入口；余量归零自动结案。
4. 标签撤回、规格/配方变化或阳性拭子补录触发影响传播时，自动冻结该修订下仍有余量的印刷批次（`frozen`，附冻结原因），并在响应中列出**已领用它的生产批次**及各自领用数量/分析版本，进入处置评估。
5. `POST /print-batches/{id}/dispose` 处置冻结余量：`scrap`（报废）或 `quarantine`（隔离），必须写明理由；部分处置后仍冻结，余量归零结案。
6. 每次放行记录冻结采用的标签修订、分析版本（当前推导结构含待包装批次所属产品的哈希 `ana-…`）、数量变化；核对包的 `print_control` 汇总入库/领用/处置数量、冻结原因与每条领用；审查单含印刷批次小节；事件日志记录 `print_batch_registered / issued / frozen / disposed`。
7. stale 标记不可被“重新分析”清除：仅草稿/复核中标签能凭重新分析消解 stale；已批准/已撤回修订的 stale 只能保留——`/reanalyze`、`/analysis`、审查单生成（内部重跑分析）或新登记印刷批次都不能让它恢复可领用。

## 包装执行与卷标结算

包装线领出卷标后，系统不再只知道“数量去了哪个生产批次”：每一枚卷标的贴用、损耗、留样与退回都落成可审计事件，线边混入旧卷标时获批文案不再能贴错批。

1. `POST /packaging-runs` 开工登记：绑定生产批次、领用记录（`issuance_ids`）、包装线、计划产量与每件用标数，并登记清场发现（`clearance_findings`）。门禁（任一不符整体 409 + `failures`，不登记任何记录）：
   - 清场发现旧卷标未隔离（`old_rolls_not_isolated`）；
   - 领用记录不存在（`issuance_unknown`）、属于其他生产批次（`issuance_batch_mismatch`）或已绑定其他运行（`issuance_already_bound`，防重复计量）；
   - 领用横跨不同标签修订（`mixed_label_revisions`，同线混用是贴错批的典型来源）；
   - 卷标已冻结（`print_batch_frozen`）或标签修订已撤回/带 stale 标记（`label_revision_blocked`）；
   - 印刷批次适用产品不含待包装批次产品（`product_not_applicable`）。
2. `POST /packaging-runs/{id}/events` 记录用标事件：`applied`（合格品贴用，须带 `good_units` 且满足 贴用量=合格品数×每件用标数）、`wasted`（过程损耗）、`sampled`（留样）、`returned`（退回隔离）；每条事件保留操作者与时刻，幂等键唯一（同键同内容重放复用、同键冲突 409）；事件只增不改。**退回隔离数量留在领用方账上，不补回印刷批次可领用余量。**
3. `POST /packaging-runs/{id}/settle` 卷标结算：同时满足「领用量 = 贴用 + 损耗 + 留样 + 退回隔离」与「贴用量 = 合格品数 × 每件用标数」才落结算记录（append-only）并置 `settled`；不平衡时 409，`detail.reconciliation` 给出两条等式的差异与各数量来源（领用记录逐条、各类别的用标事件量与调整量分列）。`GET /packaging-runs/{id}/reconciliation` 提供不落记录的实时对账。
4. **盘点更正**：结算后用标事件即封闭，记录不可覆盖；`POST /packaging-runs/{id}/adjustments` 以有符号增量追加调整事件（必须写明理由，不得使类别合计或合格品数为负）。**调整事件写入后按最新事件账重新判定**：两条结算等式不再成立时运行回退为 `open`（事件日志记 `packaging_run_reopened`），不得继续按 `settled` 处理；恢复平衡后需重新结算才回升 `settled`——重新结算生成新的结算记录，历史结算快照永不改写。
5. **撤回/更正波及**：标签撤回或供应商规格更正触发影响传播时，`print_freeze` 按生产批次列出未结算现场余量（领出未上线 + 未结算运行的线边余量，**按实时对账判定**——仅当前仍满足两条结算等式的已结算运行才视为现场余量清零）、已包装数量（贴用量与合格品数）与 `pending_isolation_batches` 待隔离批次；已包装成品始终进入处置评估。
6. 审计串联：核对包 `packaging_execution` 汇总该标签修订下全部包装运行（清场发现、用标/调整事件、结算记录与实时对账）及波及清单；批次查询带 `packaging_runs`；审查单含包装执行小节；事件日志记录 `packaging_run_started / packaging_event_recorded / packaging_run_settled / packaging_run_reopened`。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingredients` · `/ingredients/{id}/versions` | 原料与规格版本（含子成分、供应商声明） |
| POST | `/products` · `/products/{id}/recipes` | 产品与配方版本 |
| POST | `/lines` · `/products/{id}/lines/{line_id}` | 产线与共线登记 |
| POST | `/batches` | 生产批次（产线、顺序号、设备段、本批次过敏原） |
| GET | `/batches/{id}` · `/batches/{id}/trace` | 批次详情（含返工进出）/ 逐批追溯开闭路径 |
| POST | `/cleaning-programs` | 清洁程序版本（覆盖过敏原、必检点、有效期、限值） |
| POST | `/cleaning-records` | 清洁执行记录（批次、设备段、程序版本、清洁日期） |
| POST | `/swabs` · PUT `/swabs/{id}/result` | 拭子登记（`value_ppm` 可空=待出）/ 定量结果补录（自动影响传播） |
| POST | `/rework-paths` | 返工料去向（可跨产品，拒绝成环） |
| POST | `/lots` · PUT `/lots/{id}/status` · GET `/lots/{id}` | 到货批号登记（供应商批号/规格/收货量/有效期/质检状态）· 状态变更 · 详情（余量/分配/流水） |
| POST | `/batches/{id}/allocations` · POST `/allocations/{id}/reverse` | 投料分配（门禁 + 幂等）· 开工前撤销（反向流水） |
| GET | `/products/{id}/source-graph?batch_id=` | 来源图 + 批次追溯 + 推导 + 发现项 |
| GET | `/products/{id}/impact` | 依赖视图与标签过期状态 |
| GET | `/products/{id}/labels/compare?from_revision=&to_revision=` | 两版声明增删 + 波及范围 |
| POST | `/labels`（可带 `batch_id`）· `/submit` · `/approve` · `/withdraw` | 生命周期（批次绑定持久化） |
| PUT | `/labels/{id}/copy` · POST `/labels/{id}/reanalyze?batch_id=` | 修改文案 / 重新分析（可改绑批次） |
| POST | `/labels/{id}/findings/{fid}/override` | 覆盖自动结论 |
| POST | `/print-batches` · `/{id}/issue` · `/{id}/dispose` | 印刷批次入库（批准文案摘要）/ 领用放行（幂等，逐项对照）/ 冻结余量报废或隔离 |
| GET | `/print-batches/{id}` | 印刷批次详情（领用与处置流水） |
| POST | `/packaging-runs` · GET `/packaging-runs/{id}` | 包装运行开工登记（清场 + 门禁）/ 运行完整视图 |
| POST | `/packaging-runs/{id}/events` · `/{id}/adjustments` | 用标事件（幂等，贴用/损耗/留样/退回隔离）/ 盘点更正调整 |
| POST | `/packaging-runs/{id}/settle` · GET `/packaging-runs/{id}/reconciliation` | 卷标结算（双等式平衡）/ 实时对账 |
| GET | `/labels/{id}/check-package` | JSON 核对包（含批次追溯证据索引与印刷放行控制） |
| GET | `/labels/{id}/review-sheet` | 可打印审查单（HTML，含追溯小节） |
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
  trace.py    逐批次追溯：路径开闭、返工链、批准快照证据
  printing.py 印刷批次：文案规范化摘要、放行逐项对照、影响冻结
  packaging.py 包装执行：开工门禁、幂等用标事件、双等式结算、盘点更正、撤回波及清单
  genealogy.py 投料谱系：分配门禁、锁定规格、更正波及链、反向流水
  db.py       SQLite 持久层（approvals 只读）
  report.py   JSON 核对包 / 可打印审查单
samples/compound_coline.json   请求样例
tests/test_api.py              16 个端到端测试
tests/test_batch_trace.py      22 个逐批追溯/补录传播回归测试
tests/test_print_batches.py    21 个印刷批次领用放行/冻结/处置回归测试
tests/test_genealogy.py        14 个投料谱系/更正波及/撤销流水/并发幂等测试
```
