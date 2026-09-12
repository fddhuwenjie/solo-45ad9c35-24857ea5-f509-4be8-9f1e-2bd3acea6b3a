"""SQLite 持久层：原料规格、配方、产线、标签、发现项、批准记录与事件日志。

所有 JSON 字段以 TEXT 存储，读取时解码。批准记录（approvals）只插不改，
保证已批准标签的审核轨迹不可变。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    is_compound INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingredient_versions (
    ingredient_id TEXT NOT NULL,
    version TEXT NOT NULL,
    sub_components TEXT NOT NULL DEFAULT '[]',
    supplier_declarations TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (ingredient_id, version)
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipes (
    product_id TEXT NOT NULL,
    version TEXT NOT NULL,
    items TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (product_id, version)
);
CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    allergens_handled TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS product_lines (
    product_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    PRIMARY KEY (product_id, line_id)
);
CREATE TABLE IF NOT EXISTS labels (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    copy TEXT NOT NULL,
    derived TEXT,
    stale INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (product_id, revision)
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    message TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    override_json TEXT,
    UNIQUE (label_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label_id TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
-- 逐批次共线追溯：批次、设备段、清洁程序版本、清洁/拭子记录、返工去向
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    started_at TEXT,
    allergens TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (line_id, sequence)
);
CREATE TABLE IF NOT EXISTS batch_segments (
    batch_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (batch_id, segment_id)
);
CREATE TABLE IF NOT EXISTS cleaning_programs (
    program_id TEXT NOT NULL,
    version TEXT NOT NULL,
    line_id TEXT,
    allergens TEXT NOT NULL DEFAULT '[]',
    required_points TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    limit_ppm REAL NOT NULL DEFAULT 2.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (program_id, version)
);
CREATE TABLE IF NOT EXISTS cleaning_records (
    record_id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    program_id TEXT NOT NULL,
    program_version TEXT NOT NULL,
    cleaned_at TEXT
);
CREATE TABLE IF NOT EXISTS swab_results (
    swab_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    point_id TEXT NOT NULL,
    allergen TEXT NOT NULL,
    value_ppm REAL,
    sampled_at TEXT,
    FOREIGN KEY (record_id) REFERENCES cleaning_records(record_id)
);
CREATE TABLE IF NOT EXISTS rework_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_batch_id TEXT NOT NULL,
    target_batch_id TEXT NOT NULL,
    percentage REAL,
    created_at TEXT NOT NULL,
    UNIQUE (source_batch_id, target_batch_id)
);
-- 标签修订分析时实际采用的批次（缺省取产品最新批次）
CREATE TABLE IF NOT EXISTS label_batches (
    label_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL
);
-- 印刷标签批次：按已批准标签修订印刷的实物卷标入库与领用
CREATE TABLE IF NOT EXISTS print_batches (
    print_batch_id TEXT PRIMARY KEY,
    label_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    applicable_product_ids TEXT NOT NULL DEFAULT '[]',
    copy_summary TEXT NOT NULL,
    quantity_received INTEGER NOT NULL,
    received_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    frozen_reason TEXT,
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS print_issuances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuance_id TEXT NOT NULL UNIQUE,
    print_batch_id TEXT NOT NULL,
    production_batch_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    label_id TEXT NOT NULL,
    label_revision INTEGER NOT NULL,
    analysis_version TEXT NOT NULL,
    analysis_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS print_dispositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    print_batch_id TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 投料谱系：原料到货批号、投料分配（开工锁定规格）与扣量流水
CREATE TABLE IF NOT EXISTS material_lots (
    lot_id TEXT PRIMARY KEY,
    ingredient_id TEXT NOT NULL,
    supplier_lot_no TEXT NOT NULL,
    spec_version TEXT NOT NULL,
    quantity_received REAL NOT NULL,
    received_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE (ingredient_id, supplier_lot_no)
);
CREATE TABLE IF NOT EXISTS lot_allocation_requests (
    request_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    allocation_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_allocations (
    allocation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    ingredient_id TEXT NOT NULL,
    spec_version TEXT NOT NULL,
    quantity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    allocation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity REAL NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
-- 包装执行与卷标结算：包装运行、领用绑定、清场发现、用标事件与结算记录
CREATE TABLE IF NOT EXISTS packaging_runs (
    run_id TEXT PRIMARY KEY,
    production_batch_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    planned_quantity INTEGER NOT NULL,
    labels_per_unit INTEGER NOT NULL,
    expected_label_quantity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packaging_run_issuances (
    run_id TEXT NOT NULL,
    issuance_id TEXT NOT NULL,
    print_batch_id TEXT NOT NULL,
    label_id TEXT NOT NULL,
    label_revision INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (run_id, issuance_id)
);
CREATE TABLE IF NOT EXISTS clearance_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    finding TEXT NOT NULL,
    old_rolls_found INTEGER NOT NULL DEFAULT 0,
    isolated INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packaging_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    good_units INTEGER,
    operator TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packaging_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    result TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    settled_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 成品换标处置：处置单、箱/托盘标识、处置事件（幂等追加）、审核/结案快照、
-- 原包装运行锁定与新卷标预留/消耗/退回账
CREATE TABLE IF NOT EXISTS relabel_dispositions (
    disposition_id TEXT PRIMARY KEY,
    affected_batch_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    source_reason TEXT,
    new_label_id TEXT NOT NULL,
    new_print_batch_id TEXT NOT NULL,
    labels_per_unit INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    current_epoch INTEGER NOT NULL DEFAULT 0,
    invalidated_reason TEXT,
    invalidated_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS relabel_items (
    item_id TEXT PRIMARY KEY,
    disposition_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    identifier_kind TEXT NOT NULL,
    units INTEGER NOT NULL,
    isolated INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (disposition_id, identifier)
);
CREATE TABLE IF NOT EXISTS relabel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    disposition_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    labels_used INTEGER NOT NULL DEFAULT 0,
    epoch INTEGER NOT NULL,
    operator TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relabel_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    new_label_id TEXT,
    new_print_batch_id TEXT,
    analysis_version TEXT NOT NULL,
    approved_copy_snapshot TEXT NOT NULL,
    print_copy_summary TEXT NOT NULL,
    basis_derived TEXT NOT NULL,
    item_checks TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relabel_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    closure_id TEXT NOT NULL UNIQUE,
    disposition_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relabel_run_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (disposition_id, run_id)
);
CREATE TABLE IF NOT EXISTS relabel_label_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_id TEXT NOT NULL,
    print_batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
"""

# 标签生命周期：draft -> in_review -> approved -> withdrawn
LABEL_STATUSES = ("draft", "in_review", "approved", "withdrawn")
# 印刷批次状态：available 可领用 -> frozen（影响传播冻结剩余）/ closed（余量归零）
PRINT_BATCH_STATUSES = ("available", "frozen", "closed")
PRINT_DISPOSITION_ACTIONS = ("scrap", "quarantine")
# 原料批号质检状态：pending 待检 -> released 放行 / quarantined 隔离（隔离可再放行）
LOT_STATUSES = ("pending", "released", "quarantined")
# 投料分配状态：active 有效 -> reversed 已撤销（仅开工前，记反向流水）
ALLOCATION_STATUSES = ("active", "reversed")
LEDGER_KINDS = ("allocate", "reverse")
# 包装运行状态：open 进行中 -> settled 已结算（结算后仅接受盘点调整事件）
PACKAGING_RUN_STATUSES = ("open", "settled")
# 用标事件：applied 合格品贴用 / wasted 过程损耗 / sampled 留样 / returned 退回隔离；
# adjustment 盘点调整（结算后追加更正，category 指向调整对象，quantity 为有符号增量）
PACKAGING_EVENT_KINDS = ("applied", "wasted", "sampled", "returned", "adjustment")
PACKAGING_CATEGORIES = ("applied", "wasted", "sampled", "returned")
# 结算结果：balanced 平衡（不平衡时不落结算记录，仅返回差异与数量来源）
SETTLEMENT_RESULTS = ("balanced",)
# 换标处置单状态：draft 草拟 -> approved 审核通过可开工 -> closed 已结案（只读）/
# invalidated 在办失效（规则/标签再变动，待复核重审）
RELABEL_STATUSES = ("draft", "approved", "closed", "invalidated")
# 标识类型：case 外箱 / pallet 托盘
RELABEL_IDENTIFIER_KINDS = ("case", "pallet")
# 处置事件：removed 拆标 / relabelled 重贴合格 / scrapped 报废 /
# inspection_failed 抽检失败 / released 放行
RELABEL_EVENT_KINDS = ("removed", "relabelled", "scrapped",
                       "inspection_failed", "released")
# 新卷标账：reserved 审核通过时预留 / consumed 重贴消耗 / returned 退回可领余量
RELABEL_LEDGER_KINDS = ("reserved", "consumed", "returned")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _loads(text, default):
    if text is None:
        return default
    return json.loads(text)


class Store:
    """对 SQLite 的薄封装；所有方法返回已解码 JSON 字段的普通 dict。"""

    def __init__(self, path: str = ":memory:"):
        # 单连接 + RLock：写操作串行化；transaction() 期间其他线程的写被阻塞，
        # 保证“幂等检查 -> 门禁 -> 扣量 -> 登记”这类复合写不会被并发交错。
        self._lock = threading.RLock()
        self._txn_depth = 0
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        self.conn.commit()

    def _migrate_columns(self) -> None:
        """旧库轻量列迁移：为既有换标审核快照补候选标签/印刷批次列。"""
        existing = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(relabel_reviews)").fetchall()}
        for col in ("new_label_id", "new_print_batch_id"):
            if col not in existing:
                self.conn.execute(
                    f"ALTER TABLE relabel_reviews ADD COLUMN {col} TEXT")

    # ------------------------------------------------------------------ 基础
    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()):
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            if self._txn_depth == 0:
                self.conn.commit()
            return cur

    @contextmanager
    def transaction(self):
        """串行化写事务：块内所有 _exec 不逐条提交，随块结束原子 COMMIT，
        异常时整体 ROLLBACK（冲突/失败路径不残留副作用）。可嵌套（仅最外层
        真正开启事务）；持锁期间其他线程的写操作被阻塞。"""
        with self._lock:
            if self._txn_depth > 0:
                self._txn_depth += 1
                try:
                    yield self
                finally:
                    self._txn_depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._txn_depth = 1
            try:
                yield self
            except Exception:
                self._txn_depth = 0
                self.conn.rollback()
                raise
            else:
                self._txn_depth = 0
                self.conn.commit()

    def log_event(self, kind: str, payload: dict) -> None:
        self._exec(
            "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
            (utcnow(), kind, json.dumps(payload, ensure_ascii=False)),
        )

    def events(self) -> list[dict]:
        return [
            {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": _loads(r["payload"], {})}
            for r in self._q("SELECT * FROM events ORDER BY id")
        ]

    # ------------------------------------------------------------------ 原料
    def create_ingredient(self, ing_id: str, name: str, aliases: list[str], is_compound: bool) -> dict:
        self._exec(
            "INSERT INTO ingredients (id, name, aliases, is_compound) VALUES (?,?,?,?)",
            (ing_id, name, json.dumps(aliases, ensure_ascii=False), int(is_compound)),
        )
        self.log_event("ingredient_created", {"ingredient_id": ing_id})
        return self.get_ingredient(ing_id)

    def get_ingredient(self, ing_id: str) -> dict | None:
        r = self._one("SELECT * FROM ingredients WHERE id = ?", (ing_id,))
        return self._decode_ingredient(r) if r else None

    def all_ingredients(self) -> list[dict]:
        return [self._decode_ingredient(r) for r in self._q("SELECT * FROM ingredients ORDER BY id")]

    def find_ingredients_by_term(self, term: str) -> list[dict]:
        """按名称或别名（规范化后）精确匹配原料，用于别名解析与别名混用检测。"""
        from .engine import norm  # 延迟导入避免循环依赖

        t = norm(term)
        out = []
        for ing in self.all_ingredients():
            terms = {norm(ing["name"])} | {norm(a) for a in ing["aliases"]}
            if t in terms:
                out.append(ing)
        return out

    @staticmethod
    def _decode_ingredient(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "name": r["name"],
            "aliases": _loads(r["aliases"], []),
            "is_compound": bool(r["is_compound"]),
        }

    def add_ingredient_version(
        self,
        ing_id: str,
        version: str,
        sub_components: list[dict],
        supplier_declarations: list[dict],
    ) -> dict:
        self._exec(
            "INSERT INTO ingredient_versions VALUES (?,?,?,?,?)",
            (
                ing_id,
                version,
                json.dumps(sub_components, ensure_ascii=False),
                json.dumps(supplier_declarations, ensure_ascii=False),
                utcnow(),
            ),
        )
        self.log_event(
            "ingredient_version_added",
            {"ingredient_id": ing_id, "version": version,
             "declaration_allergens": [d.get("allergen") for d in supplier_declarations]},
        )
        return self.get_version(ing_id, version)

    def get_version(self, ing_id: str, version: str) -> dict | None:
        r = self._one(
            "SELECT * FROM ingredient_versions WHERE ingredient_id = ? AND version = ?",
            (ing_id, version),
        )
        return self._decode_version(r) if r else None

    def current_version(self, ing_id: str) -> dict | None:
        r = self._one(
            "SELECT * FROM ingredient_versions WHERE ingredient_id = ? ORDER BY rowid DESC LIMIT 1",
            (ing_id,),
        )
        return self._decode_version(r) if r else None

    def versions_of(self, ing_id: str) -> list[dict]:
        return [
            self._decode_version(r)
            for r in self._q("SELECT * FROM ingredient_versions WHERE ingredient_id = ? ORDER BY rowid", (ing_id,))
        ]

    @staticmethod
    def _decode_version(r: sqlite3.Row) -> dict:
        return {
            "ingredient_id": r["ingredient_id"],
            "version": r["version"],
            "sub_components": _loads(r["sub_components"], []),
            "supplier_declarations": _loads(r["supplier_declarations"], []),
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 产品与配方
    def create_product(self, pid: str, name: str) -> dict:
        self._exec("INSERT INTO products (id, name) VALUES (?,?)", (pid, name))
        self.log_event("product_created", {"product_id": pid})
        return self.get_product(pid)

    def get_product(self, pid: str) -> dict | None:
        r = self._one("SELECT * FROM products WHERE id = ?", (pid,))
        return {"id": r["id"], "name": r["name"]} if r else None

    def all_products(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"]} for r in self._q("SELECT * FROM products ORDER BY id")]

    def add_recipe(self, product_id: str, version: str, items: list[dict]) -> dict:
        self._exec(
            "INSERT INTO recipes VALUES (?,?,?,?)",
            (product_id, version, json.dumps(items, ensure_ascii=False), utcnow()),
        )
        self.log_event("recipe_added", {"product_id": product_id, "version": version})
        return self.get_recipe(product_id, version)

    def get_recipe(self, product_id: str, version: str) -> dict | None:
        r = self._one("SELECT * FROM recipes WHERE product_id = ? AND version = ?", (product_id, version))
        return self._decode_recipe(r) if r else None

    def current_recipe(self, product_id: str) -> dict | None:
        r = self._one(
            "SELECT * FROM recipes WHERE product_id = ? ORDER BY rowid DESC LIMIT 1", (product_id,)
        )
        return self._decode_recipe(r) if r else None

    @staticmethod
    def _decode_recipe(r: sqlite3.Row) -> dict:
        return {
            "product_id": r["product_id"],
            "version": r["version"],
            "items": _loads(r["items"], []),
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 产线
    def create_line(self, line_id: str, name: str, allergens_handled: list[str]) -> dict:
        self._exec(
            "INSERT INTO lines VALUES (?,?,?)",
            (line_id, name, json.dumps(allergens_handled, ensure_ascii=False)),
        )
        self.log_event("line_created", {"line_id": line_id})
        return self.get_line(line_id)

    def get_line(self, line_id: str) -> dict | None:
        r = self._one("SELECT * FROM lines WHERE id = ?", (line_id,))
        return self._decode_line(r) if r else None

    def assign_line(self, product_id: str, line_id: str) -> None:
        self._exec("INSERT OR IGNORE INTO product_lines VALUES (?,?)", (product_id, line_id))
        self.log_event("line_assigned", {"product_id": product_id, "line_id": line_id})

    def lines_for_product(self, product_id: str) -> list[dict]:
        return [
            self._decode_line(r)
            for r in self._q(
                "SELECT l.* FROM lines l JOIN product_lines pl ON pl.line_id = l.id WHERE pl.product_id = ?",
                (product_id,),
            )
        ]

    @staticmethod
    def _decode_line(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "name": r["name"], "allergens_handled": _loads(r["allergens_handled"], [])}

    # ------------------------------------------------------------------ 标签
    def create_label(self, product_id: str, copy: dict, parent_id: str | None = None) -> dict:
        r = self._one("SELECT MAX(revision) AS m FROM labels WHERE product_id = ?", (product_id,))
        revision = ((r["m"] or 0) + 1) if r else 1
        label_id = new_id("lbl")
        self._exec(
            "INSERT INTO labels (id, product_id, revision, status, copy, derived, stale, parent_id, created_at)"
            " VALUES (?,?,?,?,?,NULL,0,?,?)",
            (label_id, product_id, revision, "draft", json.dumps(copy, ensure_ascii=False), parent_id, utcnow()),
        )
        self.log_event("label_created", {"label_id": label_id, "product_id": product_id, "revision": revision})
        return self.get_label(label_id)

    def get_label(self, label_id: str) -> dict | None:
        r = self._one("SELECT * FROM labels WHERE id = ?", (label_id,))
        return self._decode_label(r) if r else None

    def get_label_by_revision(self, product_id: str, revision: int) -> dict | None:
        r = self._one(
            "SELECT * FROM labels WHERE product_id = ? AND revision = ?", (product_id, revision)
        )
        return self._decode_label(r) if r else None

    def labels_for_product(self, product_id: str) -> list[dict]:
        return [
            self._decode_label(r)
            for r in self._q("SELECT * FROM labels WHERE product_id = ? ORDER BY revision", (product_id,))
        ]

    def set_label_status(self, label_id: str, status: str) -> None:
        assert status in LABEL_STATUSES
        self._exec("UPDATE labels SET status = ? WHERE id = ?", (status, label_id))
        self.log_event("label_status", {"label_id": label_id, "status": status})

    def set_label_copy(self, label_id: str, copy: dict) -> None:
        self._exec(
            "UPDATE labels SET copy = ? WHERE id = ?", (json.dumps(copy, ensure_ascii=False), label_id)
        )

    def set_label_derived(self, label_id: str, derived: dict) -> None:
        self._exec(
            "UPDATE labels SET derived = ? WHERE id = ?",
            (json.dumps(derived, ensure_ascii=False), label_id),
        )

    def set_stale(self, label_id: str, stale: bool) -> None:
        self._exec("UPDATE labels SET stale = ? WHERE id = ?", (int(stale), label_id))

    @staticmethod
    def _decode_label(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "product_id": r["product_id"],
            "revision": r["revision"],
            "status": r["status"],
            "copy": _loads(r["copy"], {}),
            "derived": _loads(r["derived"], None),
            "stale": bool(r["stale"]),
            "parent_id": r["parent_id"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 发现项
    def sync_findings(self, label_id: str, findings: list) -> None:
        """把一次分析结果写入 findings 表。

        - 新指纹插入为 open；
        - 已存在的指纹保留原状态（open / overridden），从而保留审核人的覆盖；
        - 曾标为 resolved 的指纹若被重新检出，恢复为 open（刷新说明与细节），
          重新参与批准门禁；
        - 不再出现的指纹标记为 resolved（保留历史，不参与阻断）。
        """
        existing = {r["fingerprint"]: r for r in self._findings_rows(label_id)}
        incoming = {}
        for f in findings:
            incoming.setdefault(f.fingerprint, f)
        for fp, f in incoming.items():
            row = existing.get(fp)
            if row is None:
                self._exec(
                    "INSERT INTO findings (label_id, fingerprint, kind, severity, status, message, detail)"
                    " VALUES (?,?,?,?,'open',?,?)",
                    (label_id, fp, f.kind, f.severity, f.message,
                     json.dumps(f.detail, ensure_ascii=False)),
                )
            elif row["status"] == "resolved":
                self._exec(
                    "UPDATE findings SET status = 'open', message = ?, detail = ? WHERE id = ?",
                    (f.message, json.dumps(f.detail, ensure_ascii=False), row["id"]),
                )
        for fp, row in existing.items():
            if fp not in incoming and row["status"] != "resolved":
                self._exec("UPDATE findings SET status = 'resolved' WHERE id = ?", (row["id"],))

    def _findings_rows(self, label_id: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM findings WHERE label_id = ? ORDER BY id", (label_id,))

    def findings_for_label(self, label_id: str, include_resolved: bool = True) -> list[dict]:
        rows = self._findings_rows(label_id)
        out = [self._decode_finding(r) for r in rows]
        if not include_resolved:
            out = [f for f in out if f["status"] != "resolved"]
        return out

    def get_finding(self, finding_id: int) -> dict | None:
        r = self._one("SELECT * FROM findings WHERE id = ?", (finding_id,))
        return self._decode_finding(r) if r else None

    def set_override(self, finding_id: int, override: dict) -> None:
        self._exec(
            "UPDATE findings SET status = 'overridden', override_json = ? WHERE id = ?",
            (json.dumps(override, ensure_ascii=False), finding_id),
        )
        self.log_event("finding_overridden", {"finding_id": finding_id, **override})

    @staticmethod
    def _decode_finding(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "label_id": r["label_id"],
            "fingerprint": r["fingerprint"],
            "kind": r["kind"],
            "severity": r["severity"],
            "status": r["status"],
            "message": r["message"],
            "detail": _loads(r["detail"], {}),
            "override": _loads(r["override_json"], None),
        }

    # ------------------------------------------------------------------ 批次与逐批追溯
    def create_batch(self, batch_id: str, product_id: str, line_id: str, sequence: int,
                     started_at: str | None, allergens: list[str],
                     segments: list[dict]) -> dict:
        self._exec(
            "INSERT INTO batches (batch_id, product_id, line_id, sequence, started_at,"
            " allergens, created_at) VALUES (?,?,?,?,?,?,?)",
            (batch_id, product_id, line_id, sequence, started_at,
             json.dumps(allergens, ensure_ascii=False), utcnow()),
        )
        for seg in segments:
            self._exec(
                "INSERT INTO batch_segments (batch_id, segment_id, name) VALUES (?,?,?)",
                (batch_id, seg["segment_id"], seg.get("name")),
            )
        self.log_event("batch_created", {"batch_id": batch_id, "product_id": product_id,
                                         "line_id": line_id, "sequence": sequence,
                                         "allergens": allergens})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict | None:
        r = self._one("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        return self._decode_batch(r) if r else None

    def _decode_batch(self, r: sqlite3.Row) -> dict:
        return {
            "batch_id": r["batch_id"], "product_id": r["product_id"],
            "line_id": r["line_id"], "sequence": r["sequence"],
            "started_at": r["started_at"], "allergens": _loads(r["allergens"], []),
            "segments": self.segments_for_batch(r["batch_id"]),
        }

    def segments_for_batch(self, batch_id: str) -> list[dict]:
        return [
            {"segment_id": r["segment_id"], "name": r["name"]}
            for r in self._q(
                "SELECT segment_id, name FROM batch_segments WHERE batch_id = ? ORDER BY rowid",
                (batch_id,))
        ]

    def batches_for_product(self, product_id: str) -> list[dict]:
        return [
            self._decode_batch(r)
            for r in self._q("SELECT * FROM batches WHERE product_id = ? ORDER BY sequence",
                             (product_id,))
        ]

    def latest_batch_for_product(self, product_id: str) -> dict | None:
        r = self._one(
            "SELECT * FROM batches WHERE product_id = ? ORDER BY sequence DESC, rowid DESC LIMIT 1",
            (product_id,))
        return self._decode_batch(r) if r else None

    def batches_on_line(self, line_id: str) -> list[dict]:
        return [
            self._decode_batch(r)
            for r in self._q("SELECT * FROM batches WHERE line_id = ? ORDER BY sequence", (line_id,))
        ]

    def batch_at_sequence(self, line_id: str, sequence: int) -> dict | None:
        r = self._one(
            "SELECT * FROM batches WHERE line_id = ? AND sequence = ?", (line_id, sequence))
        return self._decode_batch(r) if r else None

    def previous_batches(self, batch: dict) -> list[dict]:
        """同一产线上序号严格更小的批次（按序号倒序），用于向前追溯含敏原批次。"""
        return [
            self._decode_batch(r)
            for r in self._q(
                "SELECT * FROM batches WHERE line_id = ? AND sequence < ? ORDER BY sequence DESC",
                (batch["line_id"], batch["sequence"]))
        ]

    # --------------------------------------------------------- 清洁程序版本与有效期
    def create_cleaning_program(self, program_id: str, version: str, line_id: str | None,
                                allergens: list[str], required_points: list[str],
                                valid_from: str | None, valid_until: str | None,
                                limit_ppm: float) -> dict:
        self._exec(
            "INSERT INTO cleaning_programs VALUES (?,?,?,?,?,?,?,?,?)",
            (program_id, version, line_id, json.dumps(allergens, ensure_ascii=False),
             json.dumps(required_points, ensure_ascii=False), valid_from, valid_until,
             limit_ppm, utcnow()),
        )
        self.log_event("cleaning_program_created",
                       {"program_id": program_id, "version": version, "allergens": allergens})
        return self.get_cleaning_program(program_id, version)

    def get_cleaning_program(self, program_id: str, version: str) -> dict | None:
        r = self._one(
            "SELECT * FROM cleaning_programs WHERE program_id = ? AND version = ?",
            (program_id, version))
        return self._decode_program(r) if r else None

    @staticmethod
    def _decode_program(r: sqlite3.Row) -> dict:
        return {
            "program_id": r["program_id"], "version": r["version"], "line_id": r["line_id"],
            "allergens": _loads(r["allergens"], []),
            "required_points": _loads(r["required_points"], []),
            "valid_from": r["valid_from"], "valid_until": r["valid_until"],
            "limit_ppm": r["limit_ppm"],
        }

    # --------------------------------------------------------- 清洁执行记录与拭子
    def create_cleaning_record(self, record_id: str, line_id: str, batch_id: str,
                               segment_id: str, program_id: str, program_version: str,
                               cleaned_at: str | None) -> dict:
        self._exec(
            "INSERT INTO cleaning_records VALUES (?,?,?,?,?,?,?)",
            (record_id, line_id, batch_id, segment_id, program_id, program_version, cleaned_at),
        )
        self.log_event("cleaning_record_created",
                       {"record_id": record_id, "batch_id": batch_id, "segment_id": segment_id,
                        "program": f"{program_id}@{program_version}"})
        return self.get_cleaning_record(record_id)

    def get_cleaning_record(self, record_id: str) -> dict | None:
        r = self._one("SELECT * FROM cleaning_records WHERE record_id = ?", (record_id,))
        return self._decode_cleaning_record(r) if r else None

    @staticmethod
    def _decode_cleaning_record(r: sqlite3.Row) -> dict:
        return {
            "record_id": r["record_id"], "line_id": r["line_id"], "batch_id": r["batch_id"],
            "segment_id": r["segment_id"], "program_id": r["program_id"],
            "program_version": r["program_version"], "cleaned_at": r["cleaned_at"],
        }

    def cleaning_records_for_batch(self, batch_id: str, segment_id: str | None = None) -> list[dict]:
        if segment_id is None:
            rows = self._q(
                "SELECT * FROM cleaning_records WHERE batch_id = ? ORDER BY rowid", (batch_id,))
        else:
            rows = self._q(
                "SELECT * FROM cleaning_records WHERE batch_id = ? AND segment_id = ? ORDER BY rowid",
                (batch_id, segment_id))
        return [self._decode_cleaning_record(r) for r in rows]

    def create_swab(self, swab_id: str, record_id: str, point_id: str, allergen: str,
                    value_ppm: float | None, sampled_at: str | None) -> dict:
        self._exec(
            "INSERT INTO swab_results VALUES (?,?,?,?,?,?)",
            (swab_id, record_id, point_id, allergen, value_ppm, sampled_at),
        )
        self.log_event("swab_created", {"swab_id": swab_id, "record_id": record_id,
                                        "point_id": point_id, "allergen": allergen,
                                        "value_ppm": value_ppm})
        return self.get_swab(swab_id)

    def get_swab(self, swab_id: str) -> dict | None:
        r = self._one("SELECT * FROM swab_results WHERE swab_id = ?", (swab_id,))
        return self._decode_swab(r) if r else None

    def set_swab_value(self, swab_id: str, value_ppm: float, sampled_at: str | None) -> dict:
        """实验室定量结果补录（可能在标签批准之后）。"""
        self._exec(
            "UPDATE swab_results SET value_ppm = ?, sampled_at = COALESCE(?, sampled_at)"
            " WHERE swab_id = ?",
            (value_ppm, sampled_at, swab_id),
        )
        self.log_event("swab_backfilled", {"swab_id": swab_id, "value_ppm": value_ppm})
        return self.get_swab(swab_id)

    def swabs_for_record(self, record_id: str) -> list[dict]:
        return [
            self._decode_swab(r)
            for r in self._q("SELECT * FROM swab_results WHERE record_id = ? ORDER BY rowid",
                             (record_id,))
        ]

    @staticmethod
    def _decode_swab(r: sqlite3.Row) -> dict:
        return {
            "swab_id": r["swab_id"], "record_id": r["record_id"], "point_id": r["point_id"],
            "allergen": r["allergen"], "value_ppm": r["value_ppm"], "sampled_at": r["sampled_at"],
        }

    # ------------------------------------------------------------------ 返工去向
    def add_rework_path(self, source_batch_id: str, target_batch_id: str,
                        percentage: float | None) -> dict:
        self._exec(
            "INSERT INTO rework_paths (source_batch_id, target_batch_id, percentage, created_at)"
            " VALUES (?,?,?,?)",
            (source_batch_id, target_batch_id, percentage, utcnow()),
        )
        self.log_event("rework_path_added",
                       {"source_batch_id": source_batch_id, "target_batch_id": target_batch_id,
                        "percentage": percentage})
        return {"source_batch_id": source_batch_id, "target_batch_id": target_batch_id,
                "percentage": percentage}

    def rework_into(self, batch_id: str) -> list[dict]:
        """余料投入本批次的返工路径。"""
        return [
            {"source_batch_id": r["source_batch_id"], "target_batch_id": r["target_batch_id"],
             "percentage": r["percentage"]}
            for r in self._q(
                "SELECT * FROM rework_paths WHERE target_batch_id = ? ORDER BY id", (batch_id,))
        ]

    def rework_from(self, batch_id: str) -> list[dict]:
        """本批次余料的去向（用于阳性结果影响链）。"""
        return [
            {"source_batch_id": r["source_batch_id"], "target_batch_id": r["target_batch_id"],
             "percentage": r["percentage"]}
            for r in self._q(
                "SELECT * FROM rework_paths WHERE source_batch_id = ? ORDER BY id", (batch_id,))
        ]

    # ------------------------------------------------------------------ 标签-批次绑定
    def set_label_batch(self, label_id: str, batch_id: str) -> None:
        self._exec(
            "INSERT INTO label_batches (label_id, batch_id) VALUES (?,?)"
            " ON CONFLICT(label_id) DO UPDATE SET batch_id = excluded.batch_id",
            (label_id, batch_id),
        )

    def get_label_batch(self, label_id: str) -> str | None:
        r = self._one("SELECT batch_id FROM label_batches WHERE label_id = ?", (label_id,))
        return r["batch_id"] if r else None

    def labels_for_batch(self, batch_id: str) -> list[str]:
        return [r["label_id"] for r in self._q(
            "SELECT label_id FROM label_batches WHERE batch_id = ?", (batch_id,))]

    # ------------------------------------------------------------------ 批准记录（只读）
    def add_approval(self, label_id: str, approved_by: str, snapshot: dict) -> dict:
        cur = self._exec(
            "INSERT INTO approvals (label_id, approved_by, approved_at, snapshot) VALUES (?,?,?,?)",
            (label_id, approved_by, utcnow(), json.dumps(snapshot, ensure_ascii=False)),
        )
        self.log_event("label_approved", {"label_id": label_id, "approved_by": approved_by})
        return self.get_approval(cur.lastrowid)

    def get_approval(self, approval_id: int) -> dict | None:
        r = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._decode_approval(r) if r else None

    def approvals_for_label(self, label_id: str) -> list[dict]:
        return [
            self._decode_approval(r)
            for r in self._q("SELECT * FROM approvals WHERE label_id = ? ORDER BY id", (label_id,))
        ]

    @staticmethod
    def _decode_approval(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "label_id": r["label_id"],
            "approved_by": r["approved_by"],
            "approved_at": r["approved_at"],
            "snapshot": _loads(r["snapshot"], {}),
        }

    # ------------------------------------------------------------------ 印刷标签批次
    def create_print_batch(self, print_batch_id: str, label_id: str, product_id: str,
                           applicable_product_ids: list[str], copy_summary: dict,
                           quantity_received: int, received_at: str | None,
                           expires_at: str | None) -> dict:
        self._exec(
            "INSERT INTO print_batches (print_batch_id, label_id, product_id,"
            " applicable_product_ids, copy_summary, quantity_received, received_at,"
            " expires_at, status, created_at) VALUES (?,?,?,?,?,?,?,?,'available',?)",
            (print_batch_id, label_id, product_id,
             json.dumps(applicable_product_ids, ensure_ascii=False),
             json.dumps(copy_summary, ensure_ascii=False),
             quantity_received, received_at, expires_at, utcnow()),
        )
        self.log_event("print_batch_registered", {
            "print_batch_id": print_batch_id, "label_id": label_id,
            "product_id": product_id, "applicable_product_ids": applicable_product_ids,
            "quantity_received": quantity_received,
            "received_at": received_at, "expires_at": expires_at,
            "copy_summary": copy_summary})
        return self.get_print_batch(print_batch_id)

    def get_print_batch(self, print_batch_id: str) -> dict | None:
        r = self._one("SELECT * FROM print_batches WHERE print_batch_id = ?",
                      (print_batch_id,))
        return self._decode_print_batch(r) if r else None

    def print_batches_for_label(self, label_id: str) -> list[dict]:
        return [
            self._decode_print_batch(r)
            for r in self._q("SELECT * FROM print_batches WHERE label_id = ? ORDER BY rowid",
                             (label_id,))
        ]

    def issued_quantity(self, print_batch_id: str) -> int:
        r = self._one(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM print_issuances"
            " WHERE print_batch_id = ?",
            (print_batch_id,))
        return int(r["s"])

    def disposed_quantity(self, print_batch_id: str) -> int:
        r = self._one(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM print_dispositions"
            " WHERE print_batch_id = ?",
            (print_batch_id,))
        return int(r["s"])

    def issuances_for_print_batch(self, print_batch_id: str) -> list[dict]:
        return [
            self._decode_issuance(r)
            for r in self._q("SELECT * FROM print_issuances WHERE print_batch_id = ?"
                            " ORDER BY id", (print_batch_id,))
        ]

    def issuance_by_idempotency_key(self, key: str) -> dict | None:
        r = self._one("SELECT * FROM print_issuances WHERE idempotency_key = ?", (key,))
        return self._decode_issuance(r) if r else None

    def create_issuance(self, issuance_id: str, print_batch_id: str,
                        production_batch_id: str, quantity: int, idempotency_key: str,
                        label_id: str, label_revision: int, analysis_version: str,
                        analysis_snapshot: dict) -> dict:
        self._exec(
            "INSERT INTO print_issuances (issuance_id, print_batch_id, production_batch_id,"
            " quantity, idempotency_key, label_id, label_revision, analysis_version,"
            " analysis_snapshot, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (issuance_id, print_batch_id, production_batch_id, quantity, idempotency_key,
             label_id, label_revision, analysis_version,
             json.dumps(analysis_snapshot, ensure_ascii=False), utcnow()),
        )
        # 余量归零：剩余印刷批次不可倒扣，自动结案，不再接受领用/处置
        pb = self.get_print_batch(print_batch_id)
        if pb and pb["remaining_quantity"] == 0 and pb["status"] == "available":
            self._exec("UPDATE print_batches SET status = 'closed' WHERE print_batch_id = ?",
                       (print_batch_id,))
        self.log_event("print_batch_issued", {
            "issuance_id": issuance_id, "print_batch_id": print_batch_id,
            "production_batch_id": production_batch_id, "quantity": quantity,
            "idempotency_key": idempotency_key, "label_id": label_id,
            "label_revision": label_revision, "analysis_version": analysis_version})
        return self.get_issuance(issuance_id)

    def get_issuance(self, issuance_id: str) -> dict | None:
        r = self._one("SELECT * FROM print_issuances WHERE issuance_id = ?", (issuance_id,))
        return self._decode_issuance(r) if r else None

    def freeze_print_batch(self, print_batch_id: str, reason: str) -> None:
        """冻结剩余印刷批次；已结案（余量归零）的批次不再改变状态。"""
        pb = self.get_print_batch(print_batch_id)
        if pb is None or pb["status"] == "closed":
            return
        self._exec(
            "UPDATE print_batches SET status = 'frozen', frozen_reason = ?, frozen_at = ?"
            " WHERE print_batch_id = ?",
            (reason, utcnow(), print_batch_id))
        self.log_event("print_batch_frozen",
                       {"print_batch_id": print_batch_id, "reason": reason})

    def freeze_available_print_batches_for_label(self, label_id: str, reason: str) -> list[dict]:
        """冻结某标签修订下所有仍可领用的印刷批次，返回被冻结批次（含处置清单依据）。"""
        frozen = []
        for pb in self.print_batches_for_label(label_id):
            if pb["status"] != "available" or pb["remaining_quantity"] <= 0:
                continue
            self.freeze_print_batch(pb["print_batch_id"], reason)
            frozen.append(self.get_print_batch(pb["print_batch_id"]))
        return frozen

    def create_disposition(self, print_batch_id: str, action: str, quantity: int,
                           reason: str) -> dict:
        self._exec(
            "INSERT INTO print_dispositions (print_batch_id, action, quantity, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (print_batch_id, action, quantity, reason, utcnow()),
        )
        pb = self.get_print_batch(print_batch_id)
        if pb and pb["remaining_quantity"] == 0:
            self._exec("UPDATE print_batches SET status = 'closed' WHERE print_batch_id = ?",
                       (print_batch_id,))
        self.log_event("print_batch_disposed", {
            "print_batch_id": print_batch_id, "action": action,
            "quantity": quantity, "reason": reason})
        return self.get_print_batch(print_batch_id)

    def dispositions_for_print_batch(self, print_batch_id: str) -> list[dict]:
        return [
            {"id": r["id"], "action": r["action"], "quantity": r["quantity"],
             "reason": r["reason"], "created_at": r["created_at"]}
            for r in self._q("SELECT * FROM print_dispositions WHERE print_batch_id = ?"
                            " ORDER BY id", (print_batch_id,))
        ]

    @staticmethod
    def _decode_issuance(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "issuance_id": r["issuance_id"],
            "print_batch_id": r["print_batch_id"],
            "production_batch_id": r["production_batch_id"],
            "quantity": r["quantity"],
            "idempotency_key": r["idempotency_key"],
            "label_id": r["label_id"],
            "label_revision": r["label_revision"],
            "analysis_version": r["analysis_version"],
            "analysis_snapshot": _loads(r["analysis_snapshot"], {}),
            "created_at": r["created_at"],
        }

    def _decode_print_batch(self, r: sqlite3.Row) -> dict:
        issued = self.issued_quantity(r["print_batch_id"])
        disposed = self.disposed_quantity(r["print_batch_id"])
        reserved = self.reserved_quantity(r["print_batch_id"])
        consumed = self.reserved_consumed_quantity(r["print_batch_id"])
        returned = self.reserved_returned_quantity(r["print_batch_id"])
        # 未消耗预留（在办处置单占住、结案/失效会退回）；已重贴消耗部分不回补
        outstanding = reserved - consumed - returned
        received = r["quantity_received"]
        status = r["status"]
        # 可领用余量 = 入库 - 领用 - 处置 - 预留 + 预留退回。
        # 退回只回补未消耗预留；已重贴消耗部分（= 预留-退回-未消耗）不回补。
        remaining = received - issued - disposed - reserved + returned
        # 数据层兜底结案：可领用余量为零且无未消耗预留（不会再有退回）时才
        # 呈现 closed。仅被预留占满（哪怕入库量恰好等于预留量）不算 closed——
        # 预留来自在办处置单，首笔重贴要能正常消耗。
        if outstanding == 0 and remaining == 0 and status == "available":
            status = "closed"
        return {
            "print_batch_id": r["print_batch_id"],
            "label_id": r["label_id"],
            "product_id": r["product_id"],
            "applicable_product_ids": _loads(r["applicable_product_ids"], []),
            "copy_summary": _loads(r["copy_summary"], {}),
            "quantity_received": received,
            "issued_quantity": issued,
            "disposed_quantity": disposed,
            "reserved_quantity": reserved,
            "reserved_consumed_quantity": consumed,
            "reserved_returned_quantity": returned,
            "reserved_outstanding_quantity": reserved - consumed - returned,
            "remaining_quantity": remaining,
            "received_at": r["received_at"],
            "expires_at": r["expires_at"],
            "status": status,
            "frozen_reason": r["frozen_reason"],
            "frozen_at": r["frozen_at"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 原料到货批号
    def create_lot(self, lot_id: str, ingredient_id: str, supplier_lot_no: str,
                   spec_version: str, quantity_received: float,
                   received_at: str | None, expires_at: str | None, status: str) -> dict:
        assert status in LOT_STATUSES
        self._exec(
            "INSERT INTO material_lots (lot_id, ingredient_id, supplier_lot_no,"
            " spec_version, quantity_received, received_at, expires_at, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (lot_id, ingredient_id, supplier_lot_no, spec_version, quantity_received,
             received_at, expires_at, status, utcnow()),
        )
        self.log_event("lot_registered", {
            "lot_id": lot_id, "ingredient_id": ingredient_id,
            "supplier_lot_no": supplier_lot_no, "spec_version": spec_version,
            "quantity_received": quantity_received, "status": status})
        return self.get_lot(lot_id)

    def get_lot(self, lot_id: str) -> dict | None:
        r = self._one("SELECT * FROM material_lots WHERE lot_id = ?", (lot_id,))
        return self._decode_lot(r) if r else None

    def lot_by_supplier_no(self, ingredient_id: str, supplier_lot_no: str) -> dict | None:
        r = self._one(
            "SELECT * FROM material_lots WHERE ingredient_id = ? AND supplier_lot_no = ?",
            (ingredient_id, supplier_lot_no))
        return self._decode_lot(r) if r else None

    def lots_for_ingredient(self, ingredient_id: str) -> list[dict]:
        return [
            self._decode_lot(r)
            for r in self._q(
                "SELECT * FROM material_lots WHERE ingredient_id = ? ORDER BY rowid",
                (ingredient_id,))
        ]

    def set_lot_status(self, lot_id: str, status: str, reason: str | None = None) -> dict:
        assert status in LOT_STATUSES
        old = self.get_lot(lot_id)
        self._exec("UPDATE material_lots SET status = ? WHERE lot_id = ?", (status, lot_id))
        self.log_event("lot_status_changed", {
            "lot_id": lot_id, "old_status": old["status"] if old else None,
            "new_status": status, "reason": reason})
        return self.get_lot(lot_id)

    def allocated_quantity(self, lot_id: str) -> float:
        """批号当前有效（未撤销）投料总量；撤销的分配由反向流水恢复余量。"""
        r = self._one(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM lot_allocations"
            " WHERE lot_id = ? AND status = 'active'",
            (lot_id,))
        return float(r["s"])

    def _decode_lot(self, r: sqlite3.Row) -> dict:
        allocated = self.allocated_quantity(r["lot_id"])
        return {
            "lot_id": r["lot_id"],
            "ingredient_id": r["ingredient_id"],
            "supplier_lot_no": r["supplier_lot_no"],
            "spec_version": r["spec_version"],
            "quantity_received": r["quantity_received"],
            "allocated_quantity": allocated,
            "remaining_quantity": r["quantity_received"] - allocated,
            "received_at": r["received_at"],
            "expires_at": r["expires_at"],
            "status": r["status"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 投料分配
    def create_allocation_request(self, request_id: str, idempotency_key: str,
                                  batch_id: str, payload: dict,
                                  allocation_ids: list[str]) -> dict:
        self._exec(
            "INSERT INTO lot_allocation_requests"
            " (request_id, idempotency_key, batch_id, payload, allocation_ids, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (request_id, idempotency_key, batch_id,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             json.dumps(allocation_ids, ensure_ascii=False), utcnow()),
        )
        return self.allocation_request_by_key(idempotency_key)

    def allocation_request_by_key(self, idempotency_key: str) -> dict | None:
        r = self._one(
            "SELECT * FROM lot_allocation_requests WHERE idempotency_key = ?",
            (idempotency_key,))
        if not r:
            return None
        return {
            "request_id": r["request_id"],
            "idempotency_key": r["idempotency_key"],
            "batch_id": r["batch_id"],
            "payload": _loads(r["payload"], {}),
            "allocation_ids": _loads(r["allocation_ids"], []),
            "created_at": r["created_at"],
        }

    def create_allocation(self, allocation_id: str, request_id: str, batch_id: str,
                          lot_id: str, ingredient_id: str, spec_version: str,
                          quantity: float) -> dict:
        self._exec(
            "INSERT INTO lot_allocations (allocation_id, request_id, batch_id, lot_id,"
            " ingredient_id, spec_version, quantity, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,'active',?)",
            (allocation_id, request_id, batch_id, lot_id, ingredient_id, spec_version,
             quantity, utcnow()),
        )
        return self.get_allocation(allocation_id)

    def get_allocation(self, allocation_id: str) -> dict | None:
        r = self._one("SELECT * FROM lot_allocations WHERE allocation_id = ?",
                      (allocation_id,))
        return self._decode_allocation(r) if r else None

    def allocations_for_batch(self, batch_id: str, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM lot_allocations WHERE batch_id = ?"
        if active_only:
            sql += " AND status = 'active'"
        return [self._decode_allocation(r)
                for r in self._q(sql + " ORDER BY rowid", (batch_id,))]

    def allocations_for_lot(self, lot_id: str, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM lot_allocations WHERE lot_id = ?"
        if active_only:
            sql += " AND status = 'active'"
        return [self._decode_allocation(r)
                for r in self._q(sql + " ORDER BY rowid", (lot_id,))]

    def set_allocation_reversed(self, allocation_id: str) -> dict:
        self._exec("UPDATE lot_allocations SET status = 'reversed' WHERE allocation_id = ?",
                   (allocation_id,))
        return self.get_allocation(allocation_id)

    @staticmethod
    def _decode_allocation(r: sqlite3.Row) -> dict:
        return {
            "allocation_id": r["allocation_id"],
            "request_id": r["request_id"],
            "batch_id": r["batch_id"],
            "lot_id": r["lot_id"],
            "ingredient_id": r["ingredient_id"],
            "spec_version": r["spec_version"],
            "quantity": r["quantity"],
            "status": r["status"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 扣量流水
    def add_ledger_entry(self, lot_id: str, batch_id: str, allocation_id: str,
                         kind: str, quantity: float, reason: str | None = None) -> dict:
        assert kind in LEDGER_KINDS
        cur = self._exec(
            "INSERT INTO lot_ledger (lot_id, batch_id, allocation_id, kind, quantity,"
            " reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (lot_id, batch_id, allocation_id, kind, quantity, reason, utcnow()),
        )
        r = self._one("SELECT * FROM lot_ledger WHERE id = ?", (cur.lastrowid,))
        return self._decode_ledger(r)

    def ledger_for_lot(self, lot_id: str) -> list[dict]:
        return [self._decode_ledger(r)
                for r in self._q("SELECT * FROM lot_ledger WHERE lot_id = ? ORDER BY id",
                                 (lot_id,))]

    def ledger_for_batch(self, batch_id: str) -> list[dict]:
        return [self._decode_ledger(r)
                for r in self._q("SELECT * FROM lot_ledger WHERE batch_id = ? ORDER BY id",
                                 (batch_id,))]

    @staticmethod
    def _decode_ledger(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "lot_id": r["lot_id"],
            "batch_id": r["batch_id"],
            "allocation_id": r["allocation_id"],
            "kind": r["kind"],
            "quantity": r["quantity"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 包装运行
    def create_packaging_run(self, run_id: str, batch_id: str, line_id: str,
                             planned_quantity: int, labels_per_unit: int,
                             issuances: list[dict], operator: str,
                             clearance_findings: list[dict]) -> dict:
        """开工登记：绑定生产批次、领用记录、包装线、计划产量与每件用标数，
        并登记清场发现。issuances 为门禁通过后的领用记录快照。"""
        self._exec(
            "INSERT INTO packaging_runs (run_id, production_batch_id, line_id,"
            " planned_quantity, labels_per_unit, expected_label_quantity, status,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,'open',?,?)",
            (run_id, batch_id, line_id, planned_quantity, labels_per_unit,
             planned_quantity * labels_per_unit, operator, utcnow()),
        )
        for iss in issuances:
            self._exec(
                "INSERT INTO packaging_run_issuances (run_id, issuance_id,"
                " print_batch_id, label_id, label_revision, quantity)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, iss["issuance_id"], iss["print_batch_id"],
                 iss["label_id"], iss["label_revision"], iss["quantity"]),
            )
        for f in clearance_findings:
            self._exec(
                "INSERT INTO clearance_findings (run_id, finding, old_rolls_found,"
                " isolated, note, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, f["finding"], f.get("old_rolls_found", 0),
                 int(f.get("isolated", True)), f.get("note"), utcnow()),
            )
        self.log_event("packaging_run_started", {
            "run_id": run_id, "production_batch_id": batch_id, "line_id": line_id,
            "planned_quantity": planned_quantity, "labels_per_unit": labels_per_unit,
            "expected_label_quantity": planned_quantity * labels_per_unit,
            "issuances": [{"issuance_id": i["issuance_id"],
                           "print_batch_id": i["print_batch_id"],
                           "label_id": i["label_id"],
                           "label_revision": i["label_revision"],
                           "quantity": i["quantity"]} for i in issuances],
            "clearance_findings": clearance_findings,
            "operator": operator})
        return self.get_packaging_run(run_id)

    def get_packaging_run(self, run_id: str) -> dict | None:
        r = self._one("SELECT * FROM packaging_runs WHERE run_id = ?", (run_id,))
        return self._decode_packaging_run(r) if r else None

    def runs_for_batch(self, batch_id: str) -> list[dict]:
        return [
            self._decode_packaging_run(r)
            for r in self._q(
                "SELECT * FROM packaging_runs WHERE production_batch_id = ?"
                " ORDER BY rowid", (batch_id,))
        ]

    def issuance_bound_run(self, issuance_id: str) -> str | None:
        """领用记录已绑定的包装运行 ID；未绑定返回 None（领出未上线）。"""
        r = self._one(
            "SELECT run_id FROM packaging_run_issuances WHERE issuance_id = ?",
            (issuance_id,))
        return r["run_id"] if r else None

    def set_packaging_run_status(self, run_id: str, status: str) -> None:
        assert status in PACKAGING_RUN_STATUSES
        self._exec("UPDATE packaging_runs SET status = ? WHERE run_id = ?",
                   (status, run_id))

    def _decode_packaging_run(self, r: sqlite3.Row) -> dict:
        run_id = r["run_id"]
        return {
            "run_id": run_id,
            "production_batch_id": r["production_batch_id"],
            "line_id": r["line_id"],
            "planned_quantity": r["planned_quantity"],
            "labels_per_unit": r["labels_per_unit"],
            "expected_label_quantity": r["expected_label_quantity"],
            "status": r["status"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "issuances": [
                {"issuance_id": i["issuance_id"],
                 "print_batch_id": i["print_batch_id"],
                 "label_id": i["label_id"],
                 "label_revision": i["label_revision"],
                 "quantity": i["quantity"]}
                for i in self._q(
                    "SELECT issuance_id, print_batch_id, label_id, label_revision,"
                    " quantity FROM packaging_run_issuances WHERE run_id = ?"
                    " ORDER BY rowid", (run_id,))],
            "clearance_findings": [
                {"id": f["id"], "finding": f["finding"],
                 "old_rolls_found": f["old_rolls_found"],
                 "isolated": bool(f["isolated"]), "note": f["note"],
                 "created_at": f["created_at"]}
                for f in self._q(
                    "SELECT * FROM clearance_findings WHERE run_id = ? ORDER BY id",
                    (run_id,))],
        }

    # ------------------------------------------------------------------ 用标事件（只增不改）
    def create_packaging_event(self, event_id: str, run_id: str,
                               idempotency_key: str, kind: str, category: str,
                               quantity: int, good_units: int | None,
                               operator: str, occurred_at: str | None,
                               reason: str | None) -> dict:
        assert kind in PACKAGING_EVENT_KINDS
        assert category in PACKAGING_CATEGORIES
        occurred = occurred_at or utcnow()
        self._exec(
            "INSERT INTO packaging_events (event_id, run_id, idempotency_key, kind,"
            " category, quantity, good_units, operator, occurred_at, reason,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, run_id, idempotency_key, kind, category, quantity,
             good_units, operator, occurred, reason, utcnow()),
        )
        self.log_event("packaging_event_recorded", {
            "event_id": event_id, "run_id": run_id, "kind": kind,
            "category": category, "quantity": quantity, "good_units": good_units,
            "operator": operator, "occurred_at": occurred, "reason": reason,
            "idempotency_key": idempotency_key})
        return self.get_packaging_event(event_id)

    def get_packaging_event(self, event_id: str) -> dict | None:
        r = self._one("SELECT * FROM packaging_events WHERE event_id = ?",
                      (event_id,))
        return self._decode_packaging_event(r) if r else None

    def packaging_event_by_key(self, idempotency_key: str) -> dict | None:
        r = self._one("SELECT * FROM packaging_events WHERE idempotency_key = ?",
                      (idempotency_key,))
        return self._decode_packaging_event(r) if r else None

    def events_for_run(self, run_id: str) -> list[dict]:
        return [
            self._decode_packaging_event(r)
            for r in self._q(
                "SELECT * FROM packaging_events WHERE run_id = ? ORDER BY id",
                (run_id,))
        ]

    @staticmethod
    def _decode_packaging_event(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "event_id": r["event_id"],
            "run_id": r["run_id"],
            "idempotency_key": r["idempotency_key"],
            "kind": r["kind"],
            "category": r["category"],
            "quantity": r["quantity"],
            "good_units": r["good_units"],
            "operator": r["operator"],
            "occurred_at": r["occurred_at"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 结算记录（只增不改）
    def add_settlement(self, run_id: str, result: str, snapshot: dict,
                       settled_by: str) -> dict:
        """追加一条结算记录；历史结算永不改写，盘点更正后重新结算即追加新记录。"""
        assert result in SETTLEMENT_RESULTS
        settlement_id = new_id("stl")
        self._exec(
            "INSERT INTO packaging_settlements (settlement_id, run_id, result,"
            " snapshot, settled_by, created_at) VALUES (?,?,?,?,?,?)",
            (settlement_id, run_id, result,
             json.dumps(snapshot, ensure_ascii=False), settled_by, utcnow()),
        )
        self.log_event("packaging_run_settled", {
            "settlement_id": settlement_id, "run_id": run_id, "result": result,
            "settled_by": settled_by,
            "issued_quantity": snapshot["balances"]["issuance_balance"]["issued_quantity"],
            "applied_quantity": snapshot["balances"]["application_balance"]["applied_quantity"],
            "good_units": snapshot["balances"]["application_balance"]["good_units"]})
        return self.get_settlement(settlement_id)

    def get_settlement(self, settlement_id: str) -> dict | None:
        r = self._one("SELECT * FROM packaging_settlements WHERE settlement_id = ?",
                      (settlement_id,))
        return self._decode_settlement(r) if r else None

    def settlements_for_run(self, run_id: str) -> list[dict]:
        return [
            self._decode_settlement(r)
            for r in self._q(
                "SELECT * FROM packaging_settlements WHERE run_id = ? ORDER BY id",
                (run_id,))
        ]

    @staticmethod
    def _decode_settlement(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "settlement_id": r["settlement_id"],
            "run_id": r["run_id"],
            "result": r["result"],
            "snapshot": _loads(r["snapshot"], {}),
            "settled_by": r["settled_by"],
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------------ 成品换标处置
    def create_relabel_disposition(self, disposition_id: str, batch_id: str,
                                   source_kind: str, source_ref: str | None,
                                   source_reason: str | None, new_label_id: str,
                                   new_print_batch_id: str, labels_per_unit: int,
                                   items: list[dict], run_ids: list[str],
                                   created_by: str) -> dict:
        """登记处置单（草拟）：逐项登记箱/托盘标识，并锁定原包装运行。

        原包装运行锁定是只增记录：即使处置单失效/结案也不解锁，保证处置期间
        与处置完成后原包装账不可再被用标事件改动。
        """
        with self.transaction():
            self._exec(
                "INSERT INTO relabel_dispositions (disposition_id, affected_batch_id,"
                " source_kind, source_ref, source_reason, new_label_id,"
                " new_print_batch_id, labels_per_unit, status, current_epoch,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,'draft',0,?,?)",
                (disposition_id, batch_id, source_kind, source_ref, source_reason,
                 new_label_id, new_print_batch_id, labels_per_unit,
                 created_by, utcnow()))
            for it in items:
                self._exec(
                    "INSERT INTO relabel_items (item_id, disposition_id, identifier,"
                    " identifier_kind, units, isolated, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (it["item_id"], disposition_id, it["identifier"],
                     it["identifier_kind"], it["units"],
                     int(it.get("isolated", True)), utcnow()))
            for run_id in sorted(set(run_ids)):
                self._exec(
                    "INSERT OR IGNORE INTO relabel_run_locks (disposition_id, run_id,"
                    " created_at) VALUES (?,?,?)",
                    (disposition_id, run_id, utcnow()))
            self.log_event("relabel_disposition_created", {
                "disposition_id": disposition_id, "affected_batch_id": batch_id,
                "source_kind": source_kind, "source_ref": source_ref,
                "new_label_id": new_label_id,
                "new_print_batch_id": new_print_batch_id,
                "labels_per_unit": labels_per_unit,
                "items": [{"item_id": i["item_id"], "identifier": i["identifier"],
                           "identifier_kind": i["identifier_kind"],
                           "units": i["units"],
                           "isolated": bool(i.get("isolated", True))}
                          for i in items],
                "locked_run_ids": sorted(set(run_ids)),
                "created_by": created_by})
        return self.get_relabel_disposition(disposition_id)

    def get_relabel_disposition(self, disposition_id: str) -> dict | None:
        r = self._one(
            "SELECT * FROM relabel_dispositions WHERE disposition_id = ?",
            (disposition_id,))
        return self._decode_relabel_disposition(r) if r else None

    def all_relabel_dispositions(self, statuses: list[str] | None = None) -> list[dict]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._q(
                f"SELECT * FROM relabel_dispositions WHERE status IN ({marks})"
                f" ORDER BY rowid", tuple(statuses))
        else:
            rows = self._q("SELECT * FROM relabel_dispositions ORDER BY rowid")
        return [self._decode_relabel_disposition(r) for r in rows]

    def relabel_dispositions_for_batch(self, batch_id: str) -> list[dict]:
        return [
            self._decode_relabel_disposition(r)
            for r in self._q(
                "SELECT * FROM relabel_dispositions WHERE affected_batch_id = ?"
                " ORDER BY rowid", (batch_id,))
        ]

    def relabel_dispositions_for_new_label(self, label_id: str) -> list[dict]:
        return [
            self._decode_relabel_disposition(r)
            for r in self._q(
                "SELECT * FROM relabel_dispositions WHERE new_label_id = ?"
                " ORDER BY rowid", (label_id,))
        ]

    def relabel_dispositions_for_print_batch(self, print_batch_id: str) -> list[dict]:
        return [
            self._decode_relabel_disposition(r)
            for r in self._q(
                "SELECT * FROM relabel_dispositions WHERE new_print_batch_id = ?"
                " ORDER BY rowid", (print_batch_id,))
        ]

    @staticmethod
    def _decode_relabel_disposition(r: sqlite3.Row) -> dict:
        return {
            "disposition_id": r["disposition_id"],
            "affected_batch_id": r["affected_batch_id"],
            "source_kind": r["source_kind"],
            "source_ref": r["source_ref"],
            "source_reason": r["source_reason"],
            "new_label_id": r["new_label_id"],
            "new_print_batch_id": r["new_print_batch_id"],
            "labels_per_unit": r["labels_per_unit"],
            "status": r["status"],
            "current_epoch": r["current_epoch"],
            "invalidated_reason": r["invalidated_reason"],
            "invalidated_at": r["invalidated_at"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "approved_by": r["approved_by"],
            "approved_at": r["approved_at"],
        }

    # --------------------------------------------------------------- 箱/托盘标识
    def relabel_items(self, disposition_id: str) -> list[dict]:
        return [
            {"item_id": r["item_id"], "disposition_id": r["disposition_id"],
             "identifier": r["identifier"], "identifier_kind": r["identifier_kind"],
             "units": r["units"], "isolated": bool(r["isolated"]),
             "created_at": r["created_at"]}
            for r in self._q(
                "SELECT * FROM relabel_items WHERE disposition_id = ? ORDER BY rowid",
                (disposition_id,))
        ]

    def relabel_item(self, item_id: str) -> dict | None:
        r = self._one("SELECT * FROM relabel_items WHERE item_id = ?", (item_id,))
        if r is None:
            return None
        return {"item_id": r["item_id"], "disposition_id": r["disposition_id"],
                "identifier": r["identifier"], "identifier_kind": r["identifier_kind"],
                "units": r["units"], "isolated": bool(r["isolated"]),
                "created_at": r["created_at"]}

    def active_owner_of_identifier(self, identifier: str) -> dict | None:
        """标识是否已被在办（未结案）处置单占用；已结案处置占用释放，可重新登记。"""
        r = self._one(
            "SELECT d.* FROM relabel_items i JOIN relabel_dispositions d"
            " ON d.disposition_id = i.disposition_id WHERE i.identifier = ?"
            " AND d.status != 'closed' ORDER BY d.rowid LIMIT 1",
            (identifier,))
        return self._decode_relabel_disposition(r) if r else None

    def run_relabel_lock(self, run_id: str) -> dict | None:
        """原包装运行是否已被任一处置单锁定（含已结案/已失效，锁只增不删）。"""
        r = self._one(
            "SELECT * FROM relabel_run_locks WHERE run_id = ? ORDER BY rowid LIMIT 1",
            (run_id,))
        if r is None:
            return None
        return {"run_id": run_id, "disposition_id": r["disposition_id"],
                "created_at": r["created_at"]}

    def run_locks_for_disposition(self, disposition_id: str) -> list[dict]:
        return [
            {"run_id": r["run_id"], "created_at": r["created_at"]}
            for r in self._q(
                "SELECT run_id, created_at FROM relabel_run_locks"
                " WHERE disposition_id = ? ORDER BY rowid", (disposition_id,))
        ]

    # --------------------------------------------------------------- 处置事件（追加）
    def create_relabel_event(self, event_id: str, disposition_id: str, item_id: str,
                             idempotency_key: str, kind: str, quantity: int,
                             labels_used: int, epoch: int, operator: str,
                             occurred_at: str | None, reason: str | None) -> dict:
        assert kind in RELABEL_EVENT_KINDS
        occurred = occurred_at or utcnow()
        with self.transaction():
            self._exec(
                "INSERT INTO relabel_events (event_id, disposition_id, item_id,"
                " idempotency_key, kind, quantity, labels_used, epoch, operator,"
                " occurred_at, reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, disposition_id, item_id, idempotency_key, kind,
                 quantity, labels_used, epoch, operator, occurred, reason,
                 utcnow()))
            if labels_used:
                self._exec(
                    "INSERT INTO relabel_label_ledger (disposition_id, print_batch_id,"
                    " kind, quantity, epoch, reason, created_at)"
                    " SELECT ?, new_print_batch_id, 'consumed', ?, ?, ?, ?"
                    " FROM relabel_dispositions WHERE disposition_id = ?",
                    (disposition_id, labels_used, epoch, reason, utcnow(),
                     disposition_id))
            self.log_event("relabel_event_recorded", {
                "event_id": event_id, "disposition_id": disposition_id,
                "item_id": item_id, "kind": kind, "quantity": quantity,
                "labels_used": labels_used, "epoch": epoch,
                "operator": operator, "occurred_at": occurred, "reason": reason,
                "idempotency_key": idempotency_key})
        return self.get_relabel_event(event_id)

    def get_relabel_event(self, event_id: str) -> dict | None:
        r = self._one("SELECT * FROM relabel_events WHERE event_id = ?", (event_id,))
        return self._decode_relabel_event(r) if r else None

    def relabel_event_by_key(self, idempotency_key: str) -> dict | None:
        r = self._one("SELECT * FROM relabel_events WHERE idempotency_key = ?",
                      (idempotency_key,))
        return self._decode_relabel_event(r) if r else None

    def relabel_events(self, disposition_id: str, current_epoch: bool = False,
                       epoch: int | None = None) -> list[dict]:
        sql = "SELECT * FROM relabel_events WHERE disposition_id = ?"
        params: list = [disposition_id]
        if current_epoch:
            sql += " AND epoch = (SELECT current_epoch FROM relabel_dispositions" \
                   " WHERE disposition_id = ?)"
            params.append(disposition_id)
        elif epoch is not None:
            sql += " AND epoch = ?"
            params.append(epoch)
        return [self._decode_relabel_event(r)
                for r in self._q(sql + " ORDER BY id", tuple(params))]

    @staticmethod
    def _decode_relabel_event(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "event_id": r["event_id"],
            "disposition_id": r["disposition_id"],
            "item_id": r["item_id"],
            "idempotency_key": r["idempotency_key"],
            "kind": r["kind"],
            "quantity": r["quantity"],
            "labels_used": r["labels_used"],
            "epoch": r["epoch"],
            "operator": r["operator"],
            "occurred_at": r["occurred_at"],
            "reason": r["reason"],
            "created_at": r["created_at"],
        }

    # --------------------------------------------------------------- 审核快照与状态
    def set_relabel_candidate(self, disposition_id: str, new_label_id: str,
                              new_print_batch_id: str) -> None:
        self._exec(
            "UPDATE relabel_dispositions SET new_label_id = ?,"
            " new_print_batch_id = ? WHERE disposition_id = ?",
            (new_label_id, new_print_batch_id, disposition_id))

    def approve_relabel_review(self, disposition_id: str, reviewer: str,
                               epoch: int, review: dict,
                               ledger: list[dict]) -> None:
        """审核通过：落审核快照（append-only）、抬升 epoch、预留新卷标、置 approved。

        预留账（reserved）只增；上一轮在办失效时其未消耗预留先退回（returned），
        由 :meth:`invalidate_relabel_disposition` 处理。
        """
        with self.transaction():
            self._exec(
                "INSERT INTO relabel_reviews (disposition_id, epoch, reviewer,"
                " new_label_id, new_print_batch_id, analysis_version,"
                " approved_copy_snapshot, print_copy_summary, basis_derived,"
                " item_checks, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (disposition_id, epoch, reviewer, review["new_label_id"],
                 review["new_print_batch_id"], review["analysis_version"],
                 json.dumps(review["approved_copy"], ensure_ascii=False),
                 json.dumps(review["print_summary"], ensure_ascii=False),
                 json.dumps(review["basis_derived"], ensure_ascii=False),
                 json.dumps(review["item_checks"], ensure_ascii=False), utcnow()))
            for entry in ledger:
                self._exec(
                    "INSERT INTO relabel_label_ledger (disposition_id, print_batch_id,"
                    " kind, quantity, epoch, reason, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (disposition_id, entry["print_batch_id"], entry["kind"],
                     entry["quantity"], epoch, entry.get("reason"), utcnow()))
            self._exec(
                "UPDATE relabel_dispositions SET status = 'approved',"
                " current_epoch = ?, approved_by = ?, approved_at = ?,"
                " invalidated_reason = NULL, invalidated_at = NULL"
                " WHERE disposition_id = ?",
                (epoch, reviewer, utcnow(), disposition_id))
            self.log_event("relabel_review_approved", {
                "disposition_id": disposition_id, "epoch": epoch,
                "reviewer": reviewer, "analysis_version": review["analysis_version"],
                "reserved": [e for e in ledger if e["kind"] == "reserved"]})

    def relabel_reviews(self, disposition_id: str) -> list[dict]:
        return [
            {"id": r["id"], "disposition_id": r["disposition_id"],
             "epoch": r["epoch"], "reviewer": r["reviewer"],
             "new_label_id": r["new_label_id"],
             "new_print_batch_id": r["new_print_batch_id"],
             "analysis_version": r["analysis_version"],
             "approved_copy_snapshot": _loads(r["approved_copy_snapshot"], {}),
             "print_copy_summary": _loads(r["print_copy_summary"], {}),
             "basis_derived": _loads(r["basis_derived"], {}),
             "item_checks": _loads(r["item_checks"], []),
             "created_at": r["created_at"]}
            for r in self._q(
                "SELECT * FROM relabel_reviews WHERE disposition_id = ? ORDER BY id",
                (disposition_id,))
        ]

    def latest_relabel_review(self, disposition_id: str, epoch: int) -> dict | None:
        r = self._one(
            "SELECT * FROM relabel_reviews WHERE disposition_id = ? AND epoch = ?"
            " ORDER BY id DESC LIMIT 1", (disposition_id, epoch))
        if r is None:
            return None
        return {"id": r["id"], "epoch": r["epoch"], "reviewer": r["reviewer"],
                "new_label_id": r["new_label_id"],
                "new_print_batch_id": r["new_print_batch_id"],
                "analysis_version": r["analysis_version"],
                "approved_copy_snapshot": _loads(r["approved_copy_snapshot"], {}),
                "print_copy_summary": _loads(r["print_copy_summary"], {}),
                "basis_derived": _loads(r["basis_derived"], {}),
                "item_checks": _loads(r["item_checks"], []),
                "created_at": r["created_at"]}

    def invalidate_relabel_disposition(self, disposition_id: str, reason: str,
                                       print_batch_id: str, epoch: int,
                                       outstanding_reserved: int) -> None:
        """在办处置失效：置 invalidated，退回该轮未消耗的新卷标预留。

        处置事件全部保留（带 epoch），待复核重审后按新一轮事件核平。
        """
        with self.transaction():
            self._exec(
                "UPDATE relabel_dispositions SET status = 'invalidated',"
                " invalidated_reason = ?, invalidated_at = ?"
                " WHERE disposition_id = ?",
                (reason, utcnow(), disposition_id))
            if outstanding_reserved > 0:
                self._exec(
                    "INSERT INTO relabel_label_ledger (disposition_id, print_batch_id,"
                    " kind, quantity, epoch, reason, created_at)"
                    " VALUES (?,?,'returned',?,?,?,?)",
                    (disposition_id, print_batch_id, outstanding_reserved, epoch,
                     reason, utcnow()))
            self.log_event("relabel_disposition_invalidated", {
                "disposition_id": disposition_id, "reason": reason,
                "epoch": epoch, "returned_reserved": outstanding_reserved})

    # --------------------------------------------------------------- 新卷标预留账
    def _relabel_ledger_sum(self, print_batch_id: str, kind: str) -> int:
        r = self._one(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM relabel_label_ledger"
            " WHERE print_batch_id = ? AND kind = ?", (print_batch_id, kind))
        return int(r["s"])

    def reserved_quantity(self, print_batch_id: str) -> int:
        return self._relabel_ledger_sum(print_batch_id, "reserved")

    def reserved_consumed_quantity(self, print_batch_id: str) -> int:
        return self._relabel_ledger_sum(print_batch_id, "consumed")

    def reserved_returned_quantity(self, print_batch_id: str) -> int:
        return self._relabel_ledger_sum(print_batch_id, "returned")

    def relabel_label_ledger(self, disposition_id: str) -> list[dict]:
        return [
            {"id": r["id"], "print_batch_id": r["print_batch_id"],
             "kind": r["kind"], "quantity": r["quantity"], "epoch": r["epoch"],
             "reason": r["reason"], "created_at": r["created_at"]}
            for r in self._q(
                "SELECT * FROM relabel_label_ledger WHERE disposition_id = ?"
                " ORDER BY id", (disposition_id,))
        ]

    # --------------------------------------------------------------- 结案（只读）
    def close_relabel_disposition(self, disposition_id: str, epoch: int,
                                  snapshot: dict, closed_by: str) -> dict:
        closure_id = new_id("cls")
        with self.transaction():
            self._exec(
                "INSERT INTO relabel_closures (closure_id, disposition_id, epoch,"
                " snapshot, closed_by, created_at) VALUES (?,?,?,?,?,?)",
                (closure_id, disposition_id, epoch,
                 json.dumps(snapshot, ensure_ascii=False), closed_by, utcnow()))
            self._exec(
                "UPDATE relabel_dispositions SET status = 'closed'"
                " WHERE disposition_id = ?", (disposition_id,))
            rec = snapshot["reconciliation"]
            self.log_event("relabel_disposition_closed", {
                "closure_id": closure_id, "disposition_id": disposition_id,
                "epoch": epoch, "closed_by": closed_by,
                "isolated_units": rec["unit_balance"]["isolated_units"],
                "relabelled_units": rec["unit_balance"]["relabelled_units"],
                "scrapped_units": rec["unit_balance"]["scrapped_units"],
                "still_quarantined_units":
                    rec["unit_balance"]["still_quarantined_units"],
                "labels_consumed":
                    rec["label_balance"]["labels_consumed"]})
        return self.get_relabel_closure(closure_id)

    def get_relabel_closure(self, closure_id: str) -> dict | None:
        r = self._one("SELECT * FROM relabel_closures WHERE closure_id = ?",
                      (closure_id,))
        return self._decode_relabel_closure(r) if r else None

    def relabel_closures(self, disposition_id: str) -> list[dict]:
        return [
            self._decode_relabel_closure(r)
            for r in self._q(
                "SELECT * FROM relabel_closures WHERE disposition_id = ? ORDER BY id",
                (disposition_id,))
        ]

    @staticmethod
    def _decode_relabel_closure(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "closure_id": r["closure_id"],
            "disposition_id": r["disposition_id"],
            "epoch": r["epoch"],
            "snapshot": _loads(r["snapshot"], {}),
            "closed_by": r["closed_by"],
            "created_at": r["created_at"],
        }
