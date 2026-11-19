from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .util import cell_str, collapse_view_prefix, is_legal_ident, is_yes, sql_comment, with_view_prefix


class GenerationError(ValueError):
    """映射不满足生成条件时抛出。"""


def _seq(row: dict) -> int:
    try:
        return int(float(cell_str(row.get("字段序号")) or 0))
    except ValueError:
        return 0


def _is_partition(col: dict) -> bool:
    return bool(col.get("_is_partition")) or is_yes(col.get("是否分区"))


def _column_type(col: dict) -> str:
    return cell_str(col.get("目标类型")) or "STRING"


def _column_comment(col: dict) -> str:
    return sql_comment(col.get("标准中文名") or col.get("来源字段中文名") or "")


def _valid_columns(rows: Iterable[dict]) -> list[dict]:
    cols = [c for c in rows if cell_str(c.get("标准英文名"))]
    invalid = [cell_str(c.get("标准英文名")) for c in cols if not is_legal_ident(cell_str(c.get("标准英文名")))]
    if invalid:
        raise GenerationError(f"存在非法的标准英文名，无法生成脚本：{', '.join(sorted(set(invalid))[:8])}")
    return cols


def _sorted_columns(rows: list[dict], expose_partition: bool) -> list[dict]:
    cols = sorted(_valid_columns(rows), key=_seq)
    if expose_partition:
        normals = [c for c in cols if not _is_partition(c)]
        parts = [c for c in cols if _is_partition(c)]
        return normals + parts
    return [c for c in cols if not _is_partition(c)]


def _select_expr(col: dict) -> str:
    src = cell_str(col.get("SELECT表达式")) or cell_str(col.get("来源字段英文名")) or "NULL"
    # 加工逻辑常带换行/缩进，折叠成单行，避免 SELECT 列表错位
    return re.sub(r"\s*\n\s*", " ", src).strip()


def _select_item(col: dict, alias_width: int = 0) -> str:
    src = _select_expr(col)
    tgt = cell_str(col.get("标准英文名"))
    if src.upper() == "NULL":
        return f"    NULL{' ' * max(alias_width - 4, 0)} AS {tgt}"
    if src.lower() == tgt.lower():
        return f"    {src}"
    pad = " " * max(alias_width - len(src), 1)
    return f"    {src}{pad} AS {tgt}"


def _resolve_view_name(view_name: str, table: str, prefix: str) -> str:
    """显式视图名只折叠重复前缀；空则按「前缀 + 已剥前缀的表名」幂等派生。"""
    name = cell_str(view_name)
    if name:
        return collapse_view_prefix(name, prefix)
    return with_view_prefix(table, prefix)


def _source_label(source_table: str, columns: list[dict]) -> str:
    system = cell_str(columns[0].get("来源系统缩写")) if columns else ""
    return f"{source_table}（{system}，{len(columns)} 个字段）" if system else f"{source_table}（{len(columns)} 个字段）"


def generate_view_ddl(
    view_name: str,
    source_table: str,
    columns: list[dict],
    expose_partition: bool = True,
    view_comment: str = "标准化视图",
) -> str:
    """生成 CREATE OR REPLACE VIEW 脚本，列顺序与确认后的字段序号一致。"""
    cols = _sorted_columns(columns, expose_partition)
    if not cols:
        raise GenerationError(f"视图 {view_name} 没有可输出的字段")
    width = max([len(cell_str(c["标准英文名"])) for c in cols] + [1])
    col_defs = [
        f"    {cell_str(c['标准英文名']):<{width}} COMMENT '{_column_comment(c)}'" for c in cols
    ]
    alias_width = max(len(_select_expr(c)) for c in cols)
    select_items = [_select_item(c, alias_width) for c in cols]

    header = f"-- 来源表：{_source_label(source_table, columns)}"
    sql = f"{header}\nCREATE OR REPLACE VIEW {view_name} (\n"
    sql += ",\n".join(col_defs)
    sql += f"\n) COMMENT '{sql_comment(view_comment)}'\nAS\nSELECT\n"
    sql += ",\n".join(select_items)
    sql += f"\nFROM {source_table};\n"
    return sql


def generate_table_ddl(
    table_name: str,
    columns: list[dict],
    lifecycle: int = 36000,
    partition_fields: Iterable[str] | None = None,
) -> str:
    cols = _sorted_columns(columns, expose_partition=False)
    if not cols:
        raise GenerationError(f"表 {table_name} 没有可输出的字段")
    width = max([len(cell_str(c["标准英文名"])) for c in cols] + [1])
    defs = []
    for c in cols:
        typ = _column_type(c)
        defs.append(f"    {cell_str(c['标准英文名']):<{width}} {typ:<14} COMMENT '{_column_comment(c)}'")
    comment = sql_comment(cell_str(columns[0].get("表中文描述")) or table_name)

    parts = [p for p in (partition_fields or []) if p]
    sql = f"-- 来源表：{_source_label(table_name, columns)}\nCREATE TABLE IF NOT EXISTS {table_name} (\n"
    sql += ",\n".join(defs)
    sql += f"\n)\nCOMMENT '{comment}'"
    if parts:
        sql += "\nPARTITIONED BY (" + ", ".join(f"{p} STRING" for p in parts) + ")"
    sql += f"\nLIFECYCLE {lifecycle};\n"
    return sql


def generate_insert_overwrite(
    table_name: str,
    source_table: str,
    columns: list[dict],
    partition_mode: str = "static",
    partition_fields: Iterable[str] | None = None,
    partition_value: str = "${bizdate}",
) -> str:
    parts = [p for p in (partition_fields or []) if p]
    all_cols = _valid_columns(columns)
    if not all_cols:
        raise GenerationError(f"表 {table_name} 没有可输出的字段")
    alias_width = max(len(_select_expr(c)) for c in all_cols)
    if not parts:
        select_items = [_select_item(c, alias_width) for c in _sorted_columns(columns, expose_partition=False)]
        sql = f"-- 来源表：{source_table}\nINSERT OVERWRITE TABLE {table_name}\nSELECT\n"
        sql += ",\n".join(select_items)
        return sql + f"\nFROM {source_table};\n"

    if partition_mode == "dynamic":
        # 动态分区：分区列必须位于 SELECT 末尾，顺序与 PARTITION 声明一致
        ordered = _sorted_columns(columns, expose_partition=True)
        normal = [c for c in ordered if not _is_partition(c)]
        part_cols = [c for c in ordered if _is_partition(c)]
        order = {name: idx for idx, name in enumerate(parts)}
        part_cols.sort(key=lambda c: order.get(cell_str(c["标准英文名"]), len(order)))
        missing = [p for p in parts if p not in {cell_str(c["标准英文名"]) for c in part_cols}]
        if missing:
            raise GenerationError(f"动态分区缺少分区列：{', '.join(missing)}，请在字段清单中补齐后再生成")
        select_items = [_select_item(c, alias_width) for c in normal + part_cols]
        sql = f"-- 来源表：{source_table}\nINSERT OVERWRITE TABLE {table_name} PARTITION ({', '.join(parts)})\nSELECT\n"
        sql += ",\n".join(select_items)
        return sql + f"\nFROM {source_table};\n"

    # 静态分区：分区列不进 SELECT
    select_items = [_select_item(c, alias_width) for c in _sorted_columns(columns, expose_partition=False)]
    assignments = ", ".join(f"{p}='{sql_comment(partition_value)}'" for p in parts)
    sql = f"-- 来源表：{source_table}\nINSERT OVERWRITE TABLE {table_name} PARTITION ({assignments})\nSELECT\n"
    sql += ",\n".join(select_items)
    return sql + f"\nFROM {source_table};\n"


def generate_pyodps(
    table_name: str,
    columns: list[dict],
    lifecycle: int = 36000,
    partition_fields: Iterable[str] | None = None,
) -> str:
    cols = _sorted_columns(columns, expose_partition=False)
    if not cols:
        raise GenerationError(f"表 {table_name} 没有可输出的字段")
    col_lines = []
    for c in cols:
        en = cell_str(c["标准英文名"])
        typ = _column_type(c).lower()
        cn = sql_comment(_column_comment(c)).replace('"', '\\"')
        col_lines.append(f'    Column(name="{en}", type="{typ}", comment="{cn}"),')
    parts = [p for p in (partition_fields or []) if p]
    body = "from odps.models import Schema, Column, Partition\n\n" "columns = [\n" + "\n".join(col_lines) + "\n]\n"
    if parts:
        part_lines = "\n".join(
            f'    Partition(name="{name}", type="string", comment="{name}"),' for name in parts
        )
        body += f"partitions = [\n{part_lines}\n]\n"
        body += "schema = Schema(columns=columns, partitions=partitions)\n"
    else:
        body += "schema = Schema(columns=columns, partitions=None)\n"
    body += f"o.create_table('{table_name}', schema, if_not_exists=True, lifecycle={lifecycle})\n"
    return body


def _partition_fields(columns: list[dict]) -> list[str]:
    names: list[str] = []
    for c in _sorted_columns(columns, expose_partition=True):
        if _is_partition(c):
            name = cell_str(c.get("标准英文名"))
            if name and name not in names:
                names.append(name)
    return names


def _group_rows(confirmed: list[dict]) -> dict[tuple[str, str, str, str], list[dict]]:
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in confirmed:
        key = (
            cell_str(row.get("目标视图名")),
            cell_str(row.get("来源表全名")),
            cell_str(row.get("表名")),
            cell_str(row.get("表中文描述")),
        )
        groups.setdefault(key, []).append(row)
    return groups


def generate_bundle(
    confirmed: list[dict],
    output_mode: str,
    expose_partition: bool,
    view_comment_override: str,
    generate_pyodps_flag: bool,
    partition_mode: str,
    partition_value: str,
    lifecycle: int,
    split_by_table: bool = False,
    view_name_prefix: str = "v_",
) -> dict[str, str]:
    """由同一份确认后的映射生成全部脚本，保证 DDL/INSERT 列顺序一致。"""
    if not confirmed:
        raise GenerationError("没有可生成的字段")
    files: dict[str, str] = {}
    groups = _group_rows(confirmed)

    view_parts: list[str] = []
    insert_parts: list[str] = []
    table_parts: list[str] = []
    py_parts: list[str] = []
    for (view_name, source_table, table, desc), cols in groups.items():
        view_name = _resolve_view_name(view_name, table, view_name_prefix)
        source_table = source_table or (f"{cell_str(cols[0].get('来源库名'))}.{table}".strip("."))
        comment = view_comment_override or desc or f"标准化视图 {view_name}"
        if output_mode == "视图":
            ddl = generate_view_ddl(
                view_name=view_name,
                source_table=source_table,
                columns=cols,
                expose_partition=expose_partition,
                view_comment=comment,
            )
            if split_by_table:
                files[f"views/{view_name}.sql"] = ddl
            else:
                view_parts.append(ddl)
            continue

        physical = cell_str(cols[0].get("目标表名")) or table
        parts = _partition_fields(cols)
        table_parts.append(
            generate_table_ddl(physical, cols, lifecycle=lifecycle, partition_fields=parts)
        )
        insert_parts.append(
            generate_insert_overwrite(
                table_name=physical,
                source_table=source_table,
                columns=cols,
                partition_mode=partition_mode,
                partition_fields=parts,
                partition_value=partition_value,
            )
        )
        if generate_pyodps_flag:
            py_parts.append(generate_pyodps(physical, cols, lifecycle=lifecycle, partition_fields=parts))

    if view_parts:
        files["odps_views.sql"] = "\n".join(view_parts)
    if table_parts:
        files["odps_ddl.sql"] = "\n".join(table_parts)
    if insert_parts:
        files["odps_insert.sql"] = "\n".join(insert_parts)
    if py_parts:
        files["pyodps_create_table.py"] = "\n".join(py_parts)
    if not files:
        raise GenerationError("没有生成任何脚本，请检查字段与输出配置")
    return files


@dataclass
class GeneratedTarget:
    """一个 DataWorks 节点目标：视图名或物理表名 + 节点内容（脚本）。"""

    target_name: str
    source_table: str
    columns: list[dict]
    script: str
    comment: str


def build_targets(
    confirmed: list[dict],
    output_mode: str,
    expose_partition: bool = True,
    view_comment_override: str = "",
    partition_mode: str = "static",
    partition_value: str = "${bizdate}",
    lifecycle: int = 36000,
    view_name_prefix: str = "v_",
) -> list[GeneratedTarget]:
    """按“一个目标对象一个节点”生成节点内容。

    视图模式：节点内容为 CREATE OR REPLACE VIEW 语句；
    物理表模式：节点内容为 INSERT OVERWRITE 语句（建表 DDL 单独输出）。
    """
    if not confirmed:
        raise GenerationError("没有可生成的字段")

    targets: list[GeneratedTarget] = []
    for (view_name, source_table, table, desc), cols in _group_rows(confirmed).items():
        view_name = _resolve_view_name(view_name, table, view_name_prefix)
        source_table = source_table or f"{cell_str(cols[0].get('来源库名'))}.{table}".strip(".")
        comment = view_comment_override or desc or ""
        if output_mode == "视图":
            targets.append(
                GeneratedTarget(
                    target_name=view_name,
                    source_table=source_table,
                    columns=cols,
                    script=generate_view_ddl(
                        view_name=view_name,
                        source_table=source_table,
                        columns=cols,
                        expose_partition=expose_partition,
                        view_comment=comment or f"标准化视图 {view_name}",
                    ),
                    comment=comment,
                )
            )
            continue

        physical = cell_str(cols[0].get("目标表名")) or table
        parts = _partition_fields(cols)
        targets.append(
            GeneratedTarget(
                target_name=physical,
                source_table=source_table,
                columns=cols,
                script=generate_insert_overwrite(
                    table_name=physical,
                    source_table=source_table,
                    columns=cols,
                    partition_mode=partition_mode,
                    partition_fields=parts,
                    partition_value=partition_value,
                ),
                comment=comment,
            )
        )
    if not targets:
        raise GenerationError("没有生成任何节点，请检查字段与输出配置")
    return targets
