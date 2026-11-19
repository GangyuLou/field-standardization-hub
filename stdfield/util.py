from __future__ import annotations

import re
from typing import Any

VIEW_PREFIX = "v_"

YES_VALUES = {"是", "y", "yes", "true", "1", "Y"}
NO_VALUES = {"否", "n", "no", "false", "0", "N"}
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def norm_header(value: Any) -> str:
    return re.sub(r"\s+", "", cell_str(value))


def is_yes(value: Any) -> bool:
    return cell_str(value) in YES_VALUES


def is_no(value: Any) -> bool:
    return cell_str(value) in NO_VALUES


def is_legal_ident(name: str) -> bool:
    return bool(IDENT_RE.match(name or ""))


def strip_view_prefix(name: str, prefix: str = VIEW_PREFIX) -> str:
    """循环剥掉全部视图前缀：v_v_dwd_x_df → dwd_x_df。"""
    text = cell_str(name)
    pre = cell_str(prefix).lower()
    if not pre:
        return text
    while text.lower().startswith(pre):
        text = text[len(pre) :]
    return text


def with_view_prefix(name: str, prefix: str = VIEW_PREFIX) -> str:
    """幂等地补视图前缀：dwd_x_df → v_dwd_x_df，v_dwd_x_df 保持，v_v_… 折成一层。"""
    text = cell_str(name)
    pre = cell_str(prefix)
    if not pre:
        return text
    return f"{pre}{strip_view_prefix(text, pre)}"


def collapse_view_prefix(name: str, prefix: str = VIEW_PREFIX) -> str:
    """只折叠重复叠加的前缀，不改动其他命名：v_v_dwd_x → v_dwd_x。"""
    text = cell_str(name)
    pre = cell_str(prefix)
    if not pre:
        return text
    doubled = (pre + pre).lower()
    while text.lower().startswith(doubled):
        text = text[len(pre) :]
    return text


def sql_comment(text: str) -> str:
    return cell_str(text).replace("'", "''")


def table_key(system: str, db: str, table: str) -> tuple[str, str, str]:
    return (cell_str(system).lower(), cell_str(db).lower(), cell_str(table).lower())


def split_aliases(text: str) -> list[str]:
    parts = re.split(r"[,，、/|;｜]+", cell_str(text))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        alias = part.strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
    return out
