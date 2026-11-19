from __future__ import annotations

import re
from typing import Any

from .util import cell_str

_TYPE_RE = re.compile(r"^\s*([a-zA-Z]+)(?:\s*\(\s*([^)]+)\s*\))?\s*$")

_BASE_MAP = {
    "varchar": "STRING",
    "char": "STRING",
    "nvarchar": "STRING",
    "nchar": "STRING",
    "text": "STRING",
    "clob": "STRING",
    "string": "STRING",
    "str": "STRING",
    "int": "BIGINT",
    "integer": "BIGINT",
    "bigint": "BIGINT",
    "smallint": "BIGINT",
    "tinyint": "TINYINT",
    "decimal": "DECIMAL",
    "numeric": "DECIMAL",
    "number": "DECIMAL",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "real": "DOUBLE",
    "datetime": "DATETIME",
    "timestamp": "DATETIME",
    "timestamptz": "DATETIME",
    "date": "DATE",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
}


def map_source_type(source_type: Any) -> tuple[str, bool]:
    """返回 (MaxCompute 类型, 是否待确认)。"""
    raw = cell_str(source_type)
    if not raw:
        return "STRING", True
    m = _TYPE_RE.match(raw)
    if not m:
        return "STRING", True
    base = m.group(1).lower()
    args = (m.group(2) or "").strip()
    mapped = _BASE_MAP.get(base)
    if mapped is None:
        return "STRING", True
    if mapped == "DECIMAL":
        if args:
            return f"DECIMAL({args.replace(' ', '')})", False
        return "DECIMAL(18,2)", False
    return mapped, False


def normalize_target_type(value: Any) -> tuple[str, bool]:
    """标准化目标类型：用户已填的类型按 MaxCompute 规范大写，未填则按来源类型映射。"""
    raw = cell_str(value)
    if not raw:
        return "STRING", True
    m = _TYPE_RE.match(raw)
    if not m:
        return "STRING", True
    base = m.group(1).lower()
    args = (m.group(2) or "").strip().replace(" ", "")
    mapped = _BASE_MAP.get(base)
    if mapped is None:
        return raw.upper(), False
    if mapped == "DECIMAL":
        return (f"DECIMAL({args})" if args else "DECIMAL(18,2)"), False
    if mapped in {"STRING", "BIGINT", "TINYINT", "DATETIME", "DATE", "DOUBLE", "BOOLEAN"}:
        return mapped, False
    return raw.upper(), False
