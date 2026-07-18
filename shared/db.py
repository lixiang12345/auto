"""Shared data layer for the auto-deploy system.

A single SQLite database is the source of truth for accounts. Both the
registration service (any-auto-register) and the relay read/write here, so the
relay can serve accounts that were registered automatically without any
out-of-band sync step.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Honors DB_PATH env (set in Docker to /data/accounts.db) but defaults to a
# local path so the relay runs without Docker for testing.
_DB_ENV = os.environ.get("DB_PATH")
DB_PATH = Path(_DB_ENV) if _DB_ENV else (Path(__file__).resolve().parent.parent / "data" / "accounts.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provider    TEXT NOT NULL,            -- claude | codex | gemini | grok
            email       TEXT,
            auth_type   TEXT NOT NULL DEFAULT 'oauth', -- oauth | apikey | sso
            creds_json  TEXT NOT NULL,            -- provider-specific secret material
            proxy       TEXT,
            status      TEXT NOT NULL DEFAULT 'active', -- active | disabled | dead | pending
            note        TEXT,
            source      TEXT NOT NULL DEFAULT 'manual',-- manual | auto-register
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            last_used   TEXT,
            last_error  TEXT,
            fail_count  INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_provider_status
            ON accounts(provider, status);
        """
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["creds"] = json.loads(d.pop("creds_json") or "{}")
    except (json.JSONDecodeError, KeyError):
        d["creds"] = {}
    return d


def list_accounts(
    provider: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM accounts WHERE 1=1"
        params: list[Any] = []
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY provider, id"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_account(account_id: int) -> Optional[dict[str, Any]]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_account(
    *,
    provider: str,
    auth_type: str,
    creds: dict[str, Any],
    email: Optional[str] = None,
    proxy: Optional[str] = None,
    source: str = "manual",
    note: Optional[str] = None,
) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO accounts
                (provider, email, auth_type, creds_json, proxy, source, note,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                email,
                auth_type,
                json.dumps(creds, ensure_ascii=False),
                proxy,
                source,
                note,
                _now_iso(),
                _now_iso(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_account(
    account_id: int,
    *,
    status: Optional[str] = None,
    proxy: Optional[str] = None,
    note: Optional[str] = None,
    creds: Optional[dict[str, Any]] = None,
) -> None:
    conn = get_conn()
    try:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if proxy is not None:
            fields.append("proxy = ?")
            params.append(proxy)
        if note is not None:
            fields.append("note = ?")
            params.append(note)
        if creds is not None:
            fields.append("creds_json = ?")
            params.append(json.dumps(creds, ensure_ascii=False))
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(_now_iso())
        params.append(account_id)
        conn.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def record_result(account_id: int, success: bool, error: Optional[str] = None) -> None:
    conn = get_conn()
    try:
        if success:
            conn.execute(
                "UPDATE accounts SET success_count = success_count + 1, "
                "fail_count = 0, last_used = ?, last_error = NULL, status = "
                "CASE WHEN status = 'dead' THEN 'active' ELSE status END WHERE id = ?",
                (_now_iso(), account_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET fail_count = fail_count + 1, last_used = ?, "
                "last_error = ? WHERE id = ?",
                (_now_iso(), error, account_id),
            )
            # Auto-disable after 5 consecutive failures.
            row = conn.execute(
                "SELECT fail_count FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row and row["fail_count"] >= 5:
                conn.execute(
                    "UPDATE accounts SET status = 'dead' WHERE id = ?", (account_id,)
                )
        conn.commit()
    finally:
        conn.close()


def import_csv(path: str) -> int:
    """Import registered accounts from a CSV produced by any-auto-register.

    Expected columns: provider,email,auth_type,proxy,creds_json
    """
    count = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            creds = row.get("creds_json") or "{}"
            try:
                json.loads(creds)
            except json.JSONDecodeError:
                creds = "{}"
            create_account(
                provider=row["provider"],
                email=row.get("email"),
                auth_type=row.get("auth_type", "oauth"),
                creds=json.loads(creds),
                proxy=row.get("proxy"),
                source="auto-register",
            )
            count += 1
    return count
