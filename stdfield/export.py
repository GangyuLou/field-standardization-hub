from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .parse import DWD_COLUMNS, DWD_GROUP_HEADER
from .util import cell_str

SHEET_CATALOG = "表目录"
SHEET_HISTORY = "变更历史"
SHEET_DWD = "DWD模型设计"

CATALOG_COLUMNS = [
    "项目空间",
    "一级主题",
    "二级主题",
    "三级主题",
    "四级主题",
    "表名",
    "表中文名",
    "来源表名",
    "来源表中文名",
    "实体",
    "负责人",
    "更新时间",
]

HISTORY_COLUMNS = ["设计调整日期", "作者", "变动内容", "状态", "预计发布日期", "实际发布日期"]

HEADER_FILL = PatternFill("solid", fgColor="DCE6F1")
GROUP_FILL = PatternFill("solid", fgColor="F2F2F2")


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([""] * len(df), index=df.index, dtype=object)


def _row_sort_key(df: pd.DataFrame) -> pd.Series:
    order: dict[str, int] = {}
    for table in _col(df, "表名").map(cell_str):
        order.setdefault(table, len(order))
    def seq(value: object) -> float:
        try:
            return float(cell_str(value))
        except ValueError:
            return 0.0
    return pd.Series(
        [
            (order.get(cell_str(t), 0), seq(s))
            for t, s in zip(_col(df, "表名"), _col(df, "字段序号"))
        ],
        index=df.index,
    )


def _as_int(value: object) -> object:
    text = cell_str(value)
    try:
        return int(float(text))
    except ValueError:
        return text


def dwd_frame(confirmed: pd.DataFrame) -> pd.DataFrame:
    """把确认后的映射还原成输入模板「DWD模型设计」的 24 列。"""
    if confirmed is None or confirmed.empty:
        return pd.DataFrame(columns=DWD_COLUMNS)
    df = confirmed.loc[confirmed.index[_row_sort_key(confirmed).argsort(kind="stable")]] if len(confirmed) else confirmed
    source_en = _col(df, "来源字段英文名").map(cell_str)
    # 加工逻辑原样回写；清单未填且由工具补的技术字段使用其 SELECT 表达式
    logic = [
        cell_str(raw) or (cell_str(expr) if not cell_str(src) else "")
        for raw, expr, src in zip(_col(df, "加工逻辑"), _col(df, "SELECT表达式"), source_en)
    ]
    partition = _col(df, "是否分区").map(lambda v: cell_str(v) or "N")
    frame = pd.DataFrame(
        {
            "目标数据库名": _col(df, "目标项目空间").map(cell_str),
            "表名": _col(df, "表名").map(cell_str),
            "表名中文名称": _col(df, "目标表中文名").map(cell_str),
            "字段名": _col(df, "标准英文名").map(cell_str),
            "字段中文名称": _col(df, "标准中文名").map(cell_str),
            "字段类型": _col(df, "目标类型").map(lambda v: cell_str(v).lower()),
            "是否分区字段": partition,
            "主键": _col(df, "是否主键").map(cell_str),
            "非空": _col(df, "非空").map(cell_str),
            "更新策略（delete\\update\\insert）": _col(df, "更新策略").map(cell_str),
            "引用代码表": _col(df, "引用代码表").map(cell_str),
            "序号": _col(df, "字段序号").map(_as_int),
            "来源系统名称": _col(df, "来源系统缩写").map(cell_str),
            "来源系统中文名": _col(df, "来源系统中文名").map(cell_str),
            "来源库名": _col(df, "来源库名").map(cell_str),
            "来源Schema": _col(df, "来源Schema").map(cell_str),
            "来源表名": _col(df, "来源表全名").map(cell_str),
            "来源表中文名称": _col(df, "来源表中文名").map(cell_str),
            "来源字段名": source_en,
            "来源字段中文名称": _col(df, "来源字段中文名").map(cell_str),
            "来源字段类型": _col(df, "来源字段类型").map(cell_str),
            "字段枚举值": _col(df, "字段枚举值").map(cell_str),
            "关联条件": _col(df, "关联条件").map(cell_str),
            "加工逻辑": logic,
        }
    )
    return frame[DWD_COLUMNS]


def table_catalog(confirmed: pd.DataFrame, meta: dict | None = None) -> pd.DataFrame:
    """表目录：每个目标对象一行。"""
    if confirmed is None or confirmed.empty:
        return pd.DataFrame(columns=CATALOG_COLUMNS)
    stamp = datetime.now().strftime("%Y-%m-%d")
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _, row in confirmed.iterrows():
        key = (cell_str(row.get("表名")), cell_str(row.get("目标视图名")))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "项目空间": cell_str(row.get("目标项目空间")),
                "一级主题": "",
                "二级主题": "",
                "三级主题": "",
                "四级主题": "",
                "表名": cell_str(row.get("表名")),
                "表中文名": cell_str(row.get("目标表中文名")) or cell_str(row.get("表中文描述")),
                "来源表名": cell_str(row.get("来源表全名")),
                "来源表中文名": cell_str(row.get("来源表中文名")),
                "实体": "",
                "负责人": "",
                "更新时间": stamp,
            }
        )
    return pd.DataFrame(rows, columns=CATALOG_COLUMNS)


def change_history(confirmed: pd.DataFrame, meta: dict | None = None) -> pd.DataFrame:
    """变更历史：表头 + 模板自带的「必填」提示行 + 本次导出记录。"""
    stamp = datetime.now().strftime("%Y-%m-%d")
    tables = len({cell_str(v) for v in _col(confirmed, "表名")}) if confirmed is not None else 0
    fields = 0 if confirmed is None else len(confirmed)
    rows = [
        {c: "必填" for c in HISTORY_COLUMNS},
        {
            "设计调整日期": stamp,
            "作者": "字段标准化工具",
            "变动内容": f"标准化导出：{tables} 个表 / {fields} 个字段"
            + (f"（{meta.get('output_mode')}）" if meta and meta.get("output_mode") else ""),
            "状态": "已生成",
            "预计发布日期": "",
            "实际发布日期": "上线后补充",
        },
    ]
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def _auto_width(series: pd.Series) -> int:
    lengths = [len(str(v)) for v in series.head(300) if str(v) not in {"", "nan", "None"}]
    return min(max(lengths + [10]) + 2, 46)


def _write_sheet(sheet, frame: pd.DataFrame, group_header: bool = False) -> None:
    start = 1
    if group_header:
        for col_index, label in DWD_GROUP_HEADER.items():
            cell = sheet.cell(row=1, column=col_index + 1, value=label)
            cell.font = Font(bold=True)
            cell.fill = GROUP_FILL
        start = 2
    for col_index, name in enumerate(frame.columns, start=1):
        cell = sheet.cell(row=start, column=col_index, value=name)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        letter = get_column_letter(col_index)
        sheet.column_dimensions[letter].width = max(12, min(len(str(name)) * 2 + 4, 32))
    for row_index, (_, row) in enumerate(frame.iterrows(), start=start + 1):
        for col_index, name in enumerate(frame.columns, start=1):
            value = row[name]
            sheet.cell(row=row_index, column=col_index, value=None if pd.isna(value) else value)
        for col_index, name in enumerate(frame.columns, start=1):
            letter = get_column_letter(col_index)
            width = _auto_width(frame[name])
            if width > (sheet.column_dimensions[letter].width or 0):
                sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = sheet.cell(row=start + 1, column=1).coordinate


def build_excel(confirmed: pd.DataFrame, meta: dict | None = None) -> bytes:
    """输出标准化结果 Excel：与输入模板一致的表目录 / 变更历史 / DWD模型设计。"""
    workbook = Workbook()
    catalog = table_catalog(confirmed, meta)
    history = change_history(confirmed, meta)
    dwd = dwd_frame(confirmed)

    first = workbook.active
    first.title = SHEET_CATALOG
    _write_sheet(first, catalog)
    _write_sheet(workbook.create_sheet(SHEET_HISTORY), history)
    _write_sheet(workbook.create_sheet(SHEET_DWD), dwd, group_header=True)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
