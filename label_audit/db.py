"""SQLite 持久层：原料规格、配方、产线、标签、发现项、批准记录与事件日志。

所有 JSON 字段以 TEXT 存储，读取时解码。批准记录（approvals）只插不改，
保证已批准标签的审核轨迹不可变。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
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
    value_ppm REAL NOT NULL,
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
"""

# 标签生命周期：draft -> in_review -> approved -> withdrawn
LABEL_STATUSES = ("draft", "in_review", "approved", "withdrawn")


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
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ 基础
    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()):
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

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
