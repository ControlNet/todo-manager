"""Run records: every entry-point invocation, its wake reasons and outcome."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import clock, ids
from .db import dumps, loads, tx


def start(conn: sqlite3.Connection, entry: str) -> str:
    run_id = ids.new_id("run")
    with tx(conn):
        conn.execute("INSERT INTO runs(id, entry, started_at, status) VALUES (?,?,?, 'running')",
                     (run_id, entry, clock.iso()))
    return run_id


def finish(conn: sqlite3.Connection, run_id: str, *, status: str, wake: bool = False,
           reasons: list[str] | None = None, summary: dict[str, Any] | None = None, error: str | None = None,
           released: bool = False) -> None:
    """``released``: the run claims nothing (a problems-only wake), so it is settled at once; it
    does not wait for a `run done` that its prompt does not ask for."""
    now = clock.iso()
    with tx(conn):
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, wake=?, wake_reasons=?, summary=?, error=?,"
            " released_at=COALESCE(released_at, ?) WHERE id=?",
            (now, status, 1 if wake else 0, dumps(reasons or []), dumps(summary or {}), error,
             now if released else None, run_id))


def get(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["wake_reasons"] = loads(row["wake_reasons"], [])
    result["summary"] = loads(row["summary"], {})
    return result


def run_dir(home: Path, run_id: str) -> Path:
    path = home / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(home: Path, run_id: str, name: str, data: Any) -> Path:
    path = run_dir(home, run_id) / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def write_text(home: Path, run_id: str, name: str, text: str) -> Path:
    path = run_dir(home, run_id) / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def read_text(home: Path, run_id: str, name: str) -> str | None:
    path = home / "runs" / run_id / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_json(home: Path, run_id: str, name: str) -> Any:
    path = home / "runs" / run_id / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def recent(conn: sqlite3.Connection, entry: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    sql = "SELECT * FROM runs"
    params: list[Any] = []
    if entry:
        sql += " WHERE entry=?"
        params.append(entry)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    return [{**dict(r), "wake_reasons": loads(r["wake_reasons"], [])} for r in conn.execute(sql, params)]
