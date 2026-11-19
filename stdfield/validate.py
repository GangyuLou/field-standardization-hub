from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .util import cell_str, collapse_view_prefix, is_legal_ident

VIEW_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _row_no(loc: Any, default: int = 0) -> Any:
    try:
        return int(loc) + 2
    except (TypeError, ValueError):
        return loc


def validate_fields(fields: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """上传后立即执行的结构校验：错误阻断，警告放行。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    if fields.empty:
        errors.append({"行": "-", "字段": "-", "问题": "字段清单为空，无法继续"})
        return errors, warnings

    for (_, table), g in fields.groupby(["来源系统缩写", "表名"], dropna=False):
        table = cell_str(table)
        en_seen: dict[str, Any] = {}
        seq_seen: dict[str, Any] = {}

        for loc, row in g.iterrows():
            excel_row = _row_no(loc)
            src_en = cell_str(row.get("来源字段英文名"))
            expr = cell_str(row.get("加工逻辑"))
            required = ["来源系统缩写", "表名", "字段序号"]
            if not cell_str(row.get("来源库名")) and "." not in cell_str(row.get("来源表全名")):
                required.append("来源库名")
            for col in required:
                if not cell_str(row.get(col)):
                    errors.append({"行": excel_row, "字段": src_en or "-", "问题": f"必填列「{col}」为空"})
            if not src_en and not expr:
                errors.append({"行": excel_row, "字段": "-", "问题": "来源字段英文名为空且无加工逻辑"})
            elif src_en:
                lowered = src_en.lower()
                if not is_legal_ident(lowered):
                    errors.append(
                        {
                            "行": excel_row,
                            "字段": src_en,
                            "问题": "来源字段英文名非法（仅小写字母/数字/下划线，不以数字开头，长度≤64）",
                        }
                    )
                if lowered in en_seen:
                    errors.append(
                        {"行": excel_row, "字段": src_en, "问题": f"同表内字段英文名重复（与行 {en_seen[lowered]}）"}
                    )
                else:
                    en_seen[lowered] = excel_row

            seq = cell_str(row.get("字段序号"))
            if seq:
                try:
                    int(float(seq))
                except ValueError:
                    errors.append({"行": excel_row, "字段": src_en or "-", "问题": "字段序号非整数"})
                else:
                    if seq in seq_seen:
                        errors.append(
                            {"行": excel_row, "字段": src_en or "-", "问题": f"同表内字段序号重复（{seq}）"}
                        )
                    else:
                        seq_seen[seq] = excel_row

            if not cell_str(row.get("匹配中文名")):
                warnings.append(
                    {"行": excel_row, "字段": src_en or "-", "问题": "目标字段中文名为空，匹配将标记为低置信"}
                )
            if not cell_str(row.get("来源字段类型")) and not cell_str(row.get("建议目标类型")):
                warnings.append({"行": excel_row, "字段": src_en or "-", "问题": "字段类型为空，输出默认 STRING"})
            if not cell_str(row.get("来源表全名")):
                warnings.append({"行": excel_row, "字段": src_en or "-", "问题": "来源表名为空，FROM 将为空"})
            if not cell_str(row.get("建议标准英文名")):
                warnings.append({"行": excel_row, "字段": src_en or "-", "问题": "字段名为空，将由词根库推荐"})
    return errors, warnings


def normalize_confirmed(df: pd.DataFrame) -> pd.DataFrame:
    """统一标准英文名/视图名的大小写与空白，便于校验和生成。"""
    out = df.copy()
    if "标准英文名" in out.columns:
        out["标准英文名"] = out["标准英文名"].map(lambda v: cell_str(v).lower().replace(" ", "_"))
    if "推荐标准英文名" in out.columns:
        out["推荐标准英文名"] = out["推荐标准英文名"].map(lambda v: cell_str(v).lower().replace(" ", "_"))
    if "目标视图名" in out.columns:
        out["目标视图名"] = out["目标视图名"].map(lambda v: collapse_view_prefix(cell_str(v)))
    return out


def _cn_label(row) -> str:
    """问题清单里的「字段中文名」：优先目标字段中文名，其次标准中文名/来源字段中文名。"""
    for col in ("建议标准中文名", "标准中文名", "来源字段中文名", "来源字段英文名"):
        value = cell_str(row.get(col))
        if value:
            return value
    return "-"


def validate_confirmed(df: pd.DataFrame) -> list[dict]:
    """导出前的最终校验：标准英文名合法且同表内唯一，视图名合法且不重复指向不同表。

    问题清单固定三列：表名 / 字段中文名 / 问题。
    """
    issues: list[dict] = []
    if df.empty:
        return [{"表名": "-", "字段中文名": "-", "问题": "没有可导出的字段"}]

    view_owner: dict[str, tuple[str, str]] = {}
    for (system, db, table), g in df.groupby(["来源系统缩写", "来源库名", "表名"], dropna=False):
        table = cell_str(table)
        seen: dict[str, str] = {}
        for _, row in g.iterrows():
            source = cell_str(row.get("来源字段英文名")) or cell_str(row.get("加工逻辑"))[:24]
            cn_label = _cn_label(row)
            name = cell_str(row.get("标准英文名"))
            if not name:
                issues.append({"表名": table, "字段中文名": cn_label, "问题": "标准英文名为空"})
            elif not is_legal_ident(name):
                issues.append(
                    {
                        "表名": table,
                        "字段中文名": cn_label,
                        "问题": f"标准英文名非法：{name}（仅小写字母/数字/下划线，长度≤64）",
                    }
                )
            elif name in seen:
                issues.append(
                    {"表名": table, "字段中文名": cn_label, "问题": f"同表内标准英文名重复：{name}"}
                )
            else:
                seen[name] = source

        view = cell_str(g.iloc[0].get("目标视图名"))
        source_table = cell_str(g.iloc[0].get("来源表全名"))
        if not view:
            issues.append({"表名": table, "字段中文名": "-", "问题": "目标视图名为空"})
        elif not VIEW_RE.match(view):
            issues.append({"表名": table, "字段中文名": "-", "问题": f"目标视图名非法：{view}"})
        else:
            owner = (cell_str(system), source_table or table)
            previous = view_owner.get(view.lower())
            if previous and previous != owner:
                issues.append(
                    {
                        "表名": table,
                        "字段中文名": "-",
                        "问题": f"视图名 {view} 同时指向多张来源表：{previous[1]} / {owner[1]}",
                    }
                )
            else:
                view_owner[view.lower()] = owner
    return issues
