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
