from __future__ import annotations

from io import BytesIO
from typing import Any

import pandas as pd

from .util import cell_str, is_yes, norm_header, with_view_prefix
from .xlsx import excel_file, read_csv, read_excel

# 与「输入文件模板.xlsx → DWD模型设计」保持一致的 24 列
DWD_COLUMNS = [
    "目标数据库名",
    "表名",
    "表名中文名称",
    "字段名",
    "字段中文名称",
    "字段类型",
    "是否分区字段",
    "主键",
    "非空",
    "更新策略（delete\\update\\insert）",
    "引用代码表",
    "序号",
    "来源系统名称",
    "来源系统中文名",
    "来源库名",
    "来源Schema",
    "来源表名",
    "来源表中文名称",
    "来源字段名",
    "来源字段中文名称",
    "来源字段类型",
    "字段枚举值",
    "关联条件",
    "加工逻辑",
]

# 第一行分组表头（目标 / 源 / 映射关系），与输入模板一致
DWD_GROUP_HEADER = {0: "目标", 12: "源", 22: "映射关系"}

DWD_TEMPLATE_SAMPLES = [
    {
        "目标数据库名": "cdm_pt_prod",
        "表名": "dwd_ast_cust_eqty_cs_estim_df",
        "表名中文名称": "客户服务评价",
        "字段名": "key_id",
        "字段中文名称": "主键id",
        "字段类型": "string",
        "是否分区字段": "N",
        "主键": "Y",
        "非空": "",
        "更新策略（delete\\update\\insert）": "",
        "引用代码表": "",
        "序号": 1,
        "来源系统名称": "ods_pt17cccs",
        "来源系统中文名": "产险2017版客户服务管理系统",
        "来源库名": "ods_pt17cccs",
        "来源Schema": "",
        "来源表名": "ods_pt17cccs.v_oracccs122_cccs_spms_customer_evaluation",
        "来源表中文名称": "客户服务评价表",
        "来源字段名": "id",
        "来源字段中文名称": "主键",
        "来源字段类型": "string",
        "字段枚举值": "",
        "关联条件": "",
        "加工逻辑": "",
    },
    {
        "目标数据库名": "cdm_pt_prod",
        "表名": "dwd_ast_cust_eqty_cs_estim_df",
        "表名中文名称": "客户服务评价",
        "字段名": "",  # 留空 → 由词根库自动推荐
        "字段中文名称": "客户证件号码",
        "字段类型": "",
        "是否分区字段": "N",
        "主键": "",
        "非空": "",
        "更新策略（delete\\update\\insert）": "",
        "引用代码表": "",
        "序号": 2,
        "来源系统名称": "ods_pt17cccs",
        "来源系统中文名": "产险2017版客户服务管理系统",
        "来源库名": "ods_pt17cccs",
        "来源Schema": "",
        "来源表名": "ods_pt17cccs.v_oracccs122_cccs_spms_customer_evaluation",
        "来源表中文名称": "客户服务评价表",
        "来源字段名": "account",
        "来源字段中文名称": "证件号",
        "来源字段类型": "string",
        "字段枚举值": "",
        "关联条件": "",
        "加工逻辑": "",
    },
]

DWD_TEMPLATE_NOTES = [
    ("列", "是否需填写", "说明"),
    ("目标数据库名", "推荐", "目标项目空间，用于 DataWorks 包名与 Outputs，如 cdm_pt_prod"),
    ("表名", "必填", "目标表 / 视图的英文名，即 DataWorks 节点名"),
    ("表名中文名称", "推荐", "表中文描述，作为视图注释与 DDL 注释"),
    ("字段名", "标准化填写", "标准化后的英文名称（小写字母/数字/下划线，≤64，同表唯一）"),
    ("字段中文名称", "推荐", "标准中文名，作为字段注释；留空时取来源字段中文名"),
    ("字段类型", "标准化填写", "标准化后的 MaxCompute 类型，如 string / bigint / decimal(18,2)"),
    ("是否分区字段", "可选", "Y/是 表示分区字段；视图默认不暴露，物理表用于 PARTITION"),
    ("主键", "可选", "Y/是 表示主键"),
    ("非空", "可选", "原样回写（不参与生成）"),
    ("更新策略 / 引用代码表", "可选", "原样回写（不参与生成）"),
    ("序号", "必填", "整数，决定输出列顺序"),
    ("来源系统名称", "必填", "如 ods_pt17cccs；DataWorks 工作流名 = dwd_ + 系统简称"),
    ("来源系统中文名", "可选", "说明用"),
    ("来源库名", "推荐", "来源库名；来源表名已带库名时可空"),
    ("来源Schema", "可选", "预留"),
    ("来源表名", "必填", "来源表全名，如 ods_pt17cccs.v_xxx"),
    ("来源表中文名称", "可选", "说明用"),
    ("来源字段名", "必填", "来源字段英文名"),
    ("来源字段中文名称", "推荐", "用于词根库中文最长匹配"),
    ("来源字段类型", "推荐", "如 varchar(100)、decimal(18,2)；字段类型留空时按此映射"),
    ("字段枚举值 / 关联条件", "可选", "原样回写（不参与生成）"),
    ("加工逻辑", "可选", "常量/表达式，作为 SELECT 表达式（优先于来源字段名），并原样回写到导出结果"),
]


def _header_map(columns) -> dict[str, Any]:
    return {norm_header(c): c for c in columns}


def _pick(hmap: dict[str, Any], *names: str) -> Any | None:
    for name in names:
        key = norm_header(name)
        if key in hmap:
            return hmap[key]
        for hk, col in hmap.items():
            if key and key in hk:
                return col
    return None


def _find_header_row(df_raw: pd.DataFrame, must_have: list[str], max_scan: int = 6) -> int | None:
    for i in range(min(max_scan, len(df_raw))):
        headers = {norm_header(v) for v in df_raw.iloc[i].tolist()}
        if all(any(norm_header(m) in h for h in headers) for m in must_have):
            return i
    return None


def _split_full_name(full_name: str) -> tuple[str, str]:
    if "." in full_name:
        db, table = full_name.rsplit(".", 1)
        return db, table
    return "", full_name


def system_short_name(system: str) -> str:
    """ods_pt17cccs → pt17cccs（用于 DataWorks 工作流名 dwd_<系统简称>）。"""
    text = cell_str(system).lower()
    for prefix in ("ods_", "cdm_", "dwd_", "dws_", "ads_"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def parse_field_list(file_bytes: bytes, filename: str = "") -> pd.DataFrame:
    """解析字段清单（与输入模板「DWD模型设计」一致，也支持同名列的 CSV）。"""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        df = read_csv(file_bytes)
        parsed = _parse_dwd_frame(df, filename, "csv")
        if parsed is None:
            raise ValueError(
                "CSV 缺少必填列：表名、序号、来源字段名。请使用与输入模板一致的列结构。"
            )
        return parsed

    xls = excel_file(file_bytes)
    preferred = [s for s in xls.sheet_names if "DWD" in s.upper() or "模型设计" in s or "字段" in s]
    others = [s for s in xls.sheet_names if s not in preferred]
    for sheet in preferred + others:
        raw = read_excel(file_bytes, sheet_name=sheet, header=None, dtype=object)
        header_row = _find_header_row(raw, ["来源字段名", "序号"])
        if header_row is None:
            header_row = _find_header_row(raw, ["字段名", "表名"])
        if header_row is None:
            continue
        df = raw.iloc[header_row + 1 :].copy()
        df.columns = [cell_str(v) or f"col_{i}" for i, v in enumerate(raw.iloc[header_row].tolist())]
        df = df.dropna(how="all")
        parsed = _parse_dwd_frame(df, filename, sheet)
        if parsed is not None and not parsed.empty:
            for col in [
                "目标项目空间",
                "目标表名",
                "目标表中文名",
                "目标视图名",
                "来源系统缩写",
                "来源系统中文名",
                "来源库名",
                "来源表全名",
                "来源表中文名",
            ]:
                if col in parsed.columns:
                    parsed[col] = parsed[col].replace("", pd.NA).ffill().fillna("")
            return parsed
    raise ValueError(
        "未识别到字段清单。请上传与「输入文件模板.xlsx」一致的表格（需包含「DWD模型设计」或含 表名 / 序号 / 来源字段名 的工作表）。"
    )


def _parse_dwd_frame(df: pd.DataFrame, filename: str, sheet: str) -> pd.DataFrame | None:
    hmap = _header_map(df.columns)
    src_field = _pick(hmap, "来源字段名")
    seq = _pick(hmap, "序号")
    if not src_field or not seq:
        return None

    rows = []
    for _, row in df.iterrows():
        def get(*names: str) -> str:
            col = _pick(hmap, *names)
            return cell_str(row.get(col)) if col else ""

        source_en = get("来源字段名").lower()
        expr = get("加工逻辑")
        source_table = get("来源表名")
        target_table = get("表名")
        if not source_en and not expr and not target_table:
            continue

        parsed_db, parsed_table = _split_full_name(source_table)
        db = get("来源库名") or parsed_db
        full_name = f"{parsed_db}.{parsed_table}" if parsed_db else parsed_table
        system = get("来源系统名称", "来源系统缩写")

        rows.append(
            {
                "目标项目空间": get("目标数据库名"),
                "目标表名": target_table,
                "目标表中文名": get("表名中文名称"),
                "目标视图名": with_view_prefix(target_table) if target_table else "",
                "表名": target_table,
                "目标表全名": f"{get('目标数据库名')}.{target_table}".strip("."),
                "字段序号": get("序号"),
                "来源系统缩写": system,
                "来源系统中文名": get("来源系统中文名"),
                "来源库名": db,
                "来源表全名": full_name,
                "来源表中文名": get("来源表中文名称"),
                "来源字段英文名": source_en,
                "来源字段中文名": get("来源字段中文名称"),
                "来源字段类型": get("来源字段类型"),
                "是否主键": get("主键"),
                "是否分区": get("是否分区字段"),
                "非空": get("非空"),
                "更新策略": get("更新策略"),
                "引用代码表": get("引用代码表"),
                "字段枚举值": get("字段枚举值"),
                "关联条件": get("关联条件"),
                "加工逻辑": expr,
                "建议标准英文名": get("字段名").lower(),
                "建议标准中文名": get("字段中文名称"),
                "建议目标类型": get("字段类型"),
                "来源文件": filename,
                "来源Sheet": sheet,
            }
        )
    return pd.DataFrame(rows) if rows else None


def enrich_fields(fields: pd.DataFrame) -> pd.DataFrame:
    """补齐生成所需的派生列（不依赖任何表级清单）。"""
    if fields.empty:
        return fields
    out = fields.copy()
    out["来源系统简称"] = out["来源系统缩写"].map(system_short_name)
    # 标准化按「目标字段中文名」匹配，缺失时才回退源端中文名
    target_cn = out["建议标准中文名"] if "建议标准中文名" in out.columns else [""] * len(out)
    out["匹配中文名"] = [
        cell_str(target) or cell_str(source) for target, source in zip(target_cn, out["来源字段中文名"])
    ]
    out["表中文描述"] = [
        cell_str(desc) or cell_str(table) for desc, table in zip(out["目标表中文名"], out["表名"])
    ]
    out["来源表全名"] = [
        cell_str(full) or (f"{cell_str(db)}.{cell_str(tbl)}" if cell_str(db) else cell_str(tbl))
        for full, db, tbl in zip(out["来源表全名"], out["来源库名"], out["表名"])
    ]
    out["目标视图名"] = [
        with_view_prefix(cell_str(view) or cell_str(target) or cell_str(tbl))
        for view, target, tbl in zip(out["目标视图名"], out["目标表名"], out["表名"])
    ]
    out["_is_partition"] = [is_yes(v) for v in out["是否分区"]]
    out["_is_pk"] = [is_yes(v) for v in out["是否主键"]]
    return out


def field_template_bytes() -> bytes:
    """字段清单模板：与「输入文件模板.xlsx → DWD模型设计」同样的列与分组表头。"""
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        workbook = writer.book
        sheet = workbook.create_sheet("DWD模型设计")
        writer.sheets["DWD模型设计"] = sheet

        for col_index, label in DWD_GROUP_HEADER.items():
            sheet.cell(row=1, column=col_index + 1, value=label)
        for col_index, name in enumerate(DWD_COLUMNS, start=1):
            sheet.cell(row=2, column=col_index, value=name)
        for row_index, sample in enumerate(DWD_TEMPLATE_SAMPLES, start=3):
            for col_index, name in enumerate(DWD_COLUMNS, start=1):
                sheet.cell(row=row_index, column=col_index, value=sample.get(name, ""))

        for col_index, name in enumerate(DWD_COLUMNS, start=1):
            letter = sheet.cell(row=2, column=col_index).column_letter
            sheet.column_dimensions[letter].width = max(12, min(len(name) * 2 + 4, 32))
        sheet.freeze_panes = "D3"

        pd.DataFrame(DWD_TEMPLATE_NOTES, columns=["列", "是否需填写", "说明"]).to_excel(
            writer, sheet_name="填写说明", index=False
        )
    return buffer.getvalue()
