from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .util import cell_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS root_versions (
    id INTEGER PRIMARY KEY,
    filename TEXT UNIQUE,
    uploaded_at TEXT,
    uploaded_by TEXT
);
CREATE TABLE IF NOT EXISTS mapping_history (
    id INTEGER PRIMARY KEY,
    batch_id TEXT,
    table_name TEXT,
    source_field TEXT,
    standard_field TEXT,
    standard_cn TEXT,
    status TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mapping_lookup ON mapping_history(table_name, source_field);
"""

CONFIRMED_STATUS = {"已匹配", "采纳推荐", "无需变更", "手动输入", "历史复用"}


def connect(db_path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def record_root_version(conn: sqlite3.Connection, filename: str, uploaded_by: str = "web") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO root_versions(filename, uploaded_at, uploaded_by) VALUES (?, ?, ?)",
        (filename, _now(), uploaded_by),
    )
    conn.commit()


def load_history(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """按 表名+来源字段 取最近一次人工确认结果，供下次自动复用。"""
    rows = conn.execute(
        """
        SELECT table_name, source_field, standard_field, standard_cn, status, confirmed_at
        FROM mapping_history
        WHERE status IN ('已匹配', '采纳推荐', '无需变更', '手动输入', '历史复用')
          AND standard_field <> ''
        ORDER BY confirmed_at DESC, id DESC
        """
    ).fetchall()
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (cell_str(row["table_name"]).lower(), cell_str(row["source_field"]).lower())
        if key not in out:
            out[key] = dict(row)
    return out


def save_history(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: Iterable[dict],
    confirmed_by: str = "web",
) -> int:
    payload = []
    for row in rows:
        standard = cell_str(row.get("标准英文名"))
        source = cell_str(row.get("来源字段英文名"))
        if not standard or not source:
            continue
        payload.append(
            (
                batch_id,
                cell_str(row.get("表名")),
                source,
                standard,
                cell_str(row.get("标准中文名")),
                cell_str(row.get("操作")) or cell_str(row.get("匹配状态")),
                confirmed_by,
                _now(),
            )
        )
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO mapping_history(
            batch_id, table_name, source_field, standard_field, standard_cn, status, confirmed_by, confirmed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def save_mapping_snapshot(directory: Path, batch_id: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{batch_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_root_library(directory: Path, filename: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename or "root_library.xlsx").name
    path = directory / safe_name
    path.write_bytes(data)
    return path


def list_root_libraries(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.glob("*.xlsx") if not p.name.startswith("~$")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
