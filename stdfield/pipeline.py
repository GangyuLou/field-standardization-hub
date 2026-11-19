from __future__ import annotations

import pandas as pd

from .match import match_field
from .roots import AliasTrie
from .types import map_source_type, normalize_target_type
from .util import cell_str, is_legal_ident, is_yes


STATUS_ORDER = ["已匹配", "低置信", "多候选", "未匹配", "冲突", "待补类型"]
EDITABLE_FIELDS = ["标准英文名", "标准中文名", "目标类型", "目标视图名", "匹配状态", "操作"]

# 预览表格使用「DWD模型设计」的 24 列，可编辑列 → 内部列
TEMPLATE_EDIT_MAP = {
    "字段名": "标准英文名",
    "字段中文名称": "标准中文名",
    "字段类型": "目标类型",
    "是否分区字段": "是否分区",
    "主键": "是否主键",
}
TEMPLATE_EDITABLE = list(TEMPLATE_EDIT_MAP)

# 默认补充的技术字段：(标准英文名, 标准中文名, MaxCompute 类型, SELECT 表达式)
# 表达式中 {system} 会替换为来源系统简称（如 pt17cccs），{bizdate} 原样保留给调度替换。
TECHNICAL_FIELDS = [
    ("fst_load_tm", "首次加载时间", "DATETIME", "getdate()"),
    ("lst_load_tm", "最后一次更新时间", "DATETIME", "getdate()"),
    ("load_fm", "来源系统", "STRING", "'{system}'"),
]
_TECH_GROUP_KEY = ("目标视图名", "来源表全名", "表名", "表中文描述")


def merge_template_edits(
    original: pd.DataFrame,
    edited: pd.DataFrame | None,
    mapping: dict[str, str] | None = None,
    row_ids: list | pd.Index | None = None,
) -> pd.DataFrame:
    """把「DWD模型设计」形式的编辑结果合并回内部列。

    行对应关系**优先按位置**（`row_ids` 为表格数据行的原始索引，顺序与 data_editor
    返回顺序一致）；隐藏列 `_row_id` 仅作兜底——Streamlit 对隐藏列可能回传 NaN，
    早期依赖它会导致第一次编辑之后的再次编辑被丢弃。
    """
    merged = original.copy()
    if edited is None or edited.empty:
        return merged
    if row_ids is not None and len(row_ids) == len(edited):
        ids = pd.Index(list(row_ids), dtype="int64")
        aligned = edited.copy()
        aligned.index = ids
    elif "_row_id" in edited.columns:
        raw = pd.to_numeric(edited["_row_id"], errors="coerce").dropna()
        if raw.empty or len(raw) != len(edited):
            return merged
        ids = pd.Index(raw.astype(int))
        aligned = edited.set_axis(ids)
    else:
        return merged
    if ids.empty:
        return merged
    before_name = (
        merged.loc[ids, "标准英文名"].map(cell_str).str.lower()
        if "标准英文名" in merged.columns
        else pd.Series("", index=ids)
    )
    for template_col, internal in (mapping or TEMPLATE_EDIT_MAP).items():
        if template_col not in aligned.columns or internal not in merged.columns:
            continue
        values = aligned[template_col]
        if internal == "标准英文名":
            # 与 normalize_confirmed 保持一致，避免表格状态与数据不一致
            values = values.map(lambda v: cell_str(v).lower().replace(" ", "_"))
        if internal == "目标类型":
            values = values.map(lambda v: normalize_target_type(v)[0])
            if "类型来源" in merged.columns:
                changed_type = values.map(cell_str).str.upper() != merged.loc[ids, "目标类型"].map(
                    cell_str
                ).str.upper()
                merged.loc[ids[changed_type.values], "类型来源"] = "人工确认"
        merged.loc[ids, internal] = values
    if "是否分区" in merged.columns:
        merged["_is_partition"] = merged["是否分区"].map(is_yes)
    if "是否主键" in merged.columns:
        merged["_is_pk"] = merged["是否主键"].map(is_yes)
    name_col = next((t for t, i in (mapping or TEMPLATE_EDIT_MAP).items() if i == "标准英文名"), None)
    if name_col and name_col in aligned.columns:
        changed = aligned[name_col].map(cell_str).str.lower().values != before_name.values
        auto = ids[changed]
        if len(auto):
            for col, value in (("操作", "手动输入"), ("匹配状态", "已匹配")):
                if col in merged.columns:
                    merged.loc[auto, col] = value
    return merged


def merge_edits(original: pd.DataFrame, edited: pd.DataFrame, editable: list[str] | None = None) -> pd.DataFrame:
    """把 data_editor 的编辑结果按 _row_id 合并回完整表格。"""
    fields = editable or EDITABLE_FIELDS
    merged = original.copy()
    if "_row_id" in edited.columns:
        ids = pd.Index(edited["_row_id"].map(int))
        aligned = edited.set_axis(ids)
    else:
        ids = edited.index
        aligned = edited
    for col in fields:
        if col in aligned.columns:
            merged.loc[ids, col] = aligned[col]
    if "标准英文名" in aligned.columns:
        changed = aligned["标准英文名"].map(cell_str) != original.loc[ids, "标准英文名"].map(cell_str)
        action_same = (
            aligned["操作"].map(cell_str) == original.loc[ids, "操作"].map(cell_str)
            if "操作" in aligned.columns
            else pd.Series(True, index=ids)
        )
        auto = ids[changed & action_same]
        if len(auto):
            merged.loc[auto, "操作"] = "手动输入"
    return merged


def _is_tech_mask(df: pd.DataFrame) -> pd.Series:
    if "_is_tech" not in df.columns:
        return pd.Series(False, index=df.index)
    return df["_is_tech"].fillna(False).astype(bool)


def _existing_names(group: pd.DataFrame) -> set[str]:
    names: set[str] = set()
    for col in ("标准英文名", "推荐标准英文名", "建议标准英文名", "来源字段英文名"):
        if col in group.columns:
            names.update(cell_str(v).lower() for v in group[col] if cell_str(v))
    return names


def _max_seq(group: pd.DataFrame) -> int:
    seqs = []
    for value in group.get("字段序号", []):
        try:
            seqs.append(int(float(cell_str(value))))
        except (TypeError, ValueError):
            continue
    return max(seqs) if seqs else len(group)


def add_technical_fields(
    fields: pd.DataFrame | None,
    enabled: bool = True,
    specs: list[tuple[str, str, str, str]] | None = None,
) -> pd.DataFrame | None:
    """为每个目标对象补齐技术字段（fst_load_tm / lst_load_tm / load_fm）。

    幂等：已存在的技术字段行原样保留（用户手改过的值不会被覆盖），只补缺失项；
    目标对象已有同名字段（含清单中原生提供的）时自动跳过，不会产生重复列。
    """
    if fields is None or fields.empty:
        return fields
    out = fields.copy()
    out["_is_tech"] = _is_tech_mask(fields)
    if not enabled:
        trimmed = out.loc[~out["_is_tech"]].copy()
        trimmed["_is_tech"] = False
        return trimmed

    specs = specs or TECHNICAL_FIELDS
    keys = [k for k in _TECH_GROUP_KEY if k in out.columns]
    rows: list[dict] = []
    grouped = out.groupby(keys, sort=False, dropna=False) if keys else [("", out)]
    for _, full_group in grouped:
        plain = full_group.loc[~_is_tech_mask(full_group)]
        if plain.empty:
            continue
        template = plain.iloc[0].to_dict()
        existing = _existing_names(full_group)
        seq = _max_seq(plain)
        system = cell_str(template.get("来源系统简称")) or cell_str(template.get("来源系统缩写"))
        for name, cn, type_name, expr in specs:
            if name in existing:
                continue
            seq += 1
            rows.append(
                {
                    **template,
                    "字段序号": str(seq),
                    "来源字段英文名": "",
                    "来源字段中文名": cn,
                    "来源字段类型": "",
                    "是否分区": "否",
                    "是否主键": "",
                    "加工逻辑": expr.format(system=system),
                    "建议标准英文名": name,
                    "建议标准中文名": cn,
                    "建议目标类型": type_name,
                    "推荐标准英文名": name,
                    "标准英文名": name,
                    "标准中文名": cn,
                    "注释": cn,
                    "匹配词根": "",
                    "候选": "",
                    "匹配状态": "已匹配",
                    "操作": "采纳推荐",
                    "目标类型": type_name,
                    "类型来源": "技术字段默认",
                    "类型待确认": False,
                    "SELECT表达式": expr.format(system=system),
                    "说明": "技术字段自动补充",
                    "_is_partition": False,
                    "_is_pk": False,
                    "_is_tech": True,
                }
            )
    if not rows:
        return out
    return pd.concat([out, pd.DataFrame(rows)], ignore_index=True)


def apply_matching(fields: pd.DataFrame, library, history_lookup: dict[tuple[str, str], dict]) -> pd.DataFrame:
    rows = []
    trie = AliasTrie(library.alias_to_roots)
    for _, row in fields.iterrows():
        table = cell_str(row.get("表名"))
        src_en = cell_str(row.get("来源字段英文名"))
        hist = history_lookup.get((table.lower(), src_en.lower()), {})
        suggested_en = cell_str(row.get("建议标准英文名"))
        suggested_cn = cell_str(row.get("建议标准中文名"))
        # 标准化依据：目标字段中文名（DWD模型设计 E 列），缺失时回退来源字段中文名
        match_cn = (
            cell_str(row.get("匹配中文名"))
            or suggested_cn
            or cell_str(row.get("来源字段中文名"))
        )
        result = match_field(
            match_cn,
            src_en,
            library,
            history_en=hist.get("standard_field", ""),
            history_cn=hist.get("standard_cn", ""),
            trie=trie,
        )
        source_cn = cell_str(row.get("来源字段中文名"))
        notes_fallback = ""
        if result.status == "未匹配" and source_cn and source_cn != match_cn:
            fallback = match_field(
                source_cn,
                src_en,
                library,
                history_en=hist.get("standard_field", ""),
                history_cn=hist.get("standard_cn", ""),
                trie=trie,
            )
            if fallback.standard_en:
                result = fallback
                notes_fallback = f"目标中文名未命中，回退来源字段中文名（{source_cn}）"
        else:
            notes_fallback = ""
        target_type, type_pending = normalize_target_type(row.get("建议目标类型"))
        type_source = "输入清单字段类型"
        if type_pending:
            target_type, type_pending = map_source_type(row.get("来源字段类型"))
            type_source = "按来源类型映射"
        status = result.status
        notes = [result.note] if result.note else []
        if notes_fallback:
            notes.append(notes_fallback)
        if suggested_cn and suggested_cn != cell_str(row.get("来源字段中文名")):
            notes.append("按目标字段中文名匹配")
        if not match_cn and status == "已匹配":
            status = "低置信"
        if type_pending:
            notes.append("待补类型")

        suggested_en = suggested_en.lower().replace(" ", "_")
        standard_en = result.standard_en
        standard_cn = (
            suggested_cn
            or result.standard_cn
            or cell_str(row.get("来源字段中文名"))
            or match_cn
        )
        if suggested_en and is_legal_ident(suggested_en):
            notes.append("采用输入清单已填字段名")
            if result.standard_en and result.standard_en != suggested_en:
                notes.append(f"词根推荐：{result.standard_en}")
            standard_en = suggested_en
            # 用户已填写标准化英文名，视为已确认
            status = "已匹配"

        # SELECT 表达式：加工逻辑（常量 / 表达式）优先，其次来源字段名，都没有则 NULL
        logic = cell_str(row.get("加工逻辑"))
        if logic:
            select_expr = logic
            if src_en and logic.lower() != src_en.lower():
                notes.append("采用加工逻辑作为SELECT表达式")
        else:
            select_expr = src_en or "NULL"

        rows.append(
            {
                **{k: row.get(k) for k in fields.columns},
                "推荐标准英文名": standard_en,
                "标准英文名": standard_en,
                "标准中文名": standard_cn,
                "注释": standard_cn,
                "匹配词根": result.roots_text,
                "匹配状态": status,
                "候选": result.candidates,
                "目标类型": target_type,
                "类型来源": type_source,
                "类型待确认": type_pending,
                "说明": "；".join(n for n in notes if n),
                "操作": "采纳推荐" if standard_en else "手动输入",
                "SELECT表达式": select_expr,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = _sanitize_names(out)
    out = _mark_conflicts(out)
    return out


def _mark_conflicts(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    conflict_idx = []
    for _, g in out.groupby(["来源系统缩写", "来源库名", "表名"], dropna=False):
        counts = g["标准英文名"].replace("", pd.NA).dropna().str.lower().value_counts()
        dups = set(counts[counts > 1].index)
        for idx, row in g.iterrows():
            name = cell_str(row.get("标准英文名")).lower()
            if name and name in dups:
                conflict_idx.append(idx)
    for idx in conflict_idx:
        out.at[idx, "匹配状态"] = "冲突"
        extra = cell_str(out.at[idx, "说明"])
        out.at[idx, "说明"] = "；".join([x for x in [extra, "同表内推荐结果重复"] if x])
    return out


def _sanitize_names(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for idx, row in out.iterrows():
        name = cell_str(row.get("标准英文名")).lower().replace(" ", "_")
        out.at[idx, "标准英文名"] = name
        out.at[idx, "推荐标准英文名"] = cell_str(row.get("推荐标准英文名")).lower().replace(" ", "_")
        if name and not is_legal_ident(name):
            extra = cell_str(out.at[idx, "说明"])
            out.at[idx, "说明"] = "；".join([x for x in [extra, "标准英文名非法"] if x])
            if out.at[idx, "匹配状态"] == "已匹配":
                out.at[idx, "匹配状态"] = "低置信"
    return out


EDITOR_COLUMNS = [
    "来源系统缩写",
    "来源库名",
    "表名",
    "字段序号",
    "来源字段英文名",
    "来源字段中文名",
    "来源字段类型",
    "推荐标准英文名",
    "标准英文名",
    "标准中文名",
    "匹配词根",
    "匹配状态",
    "候选",
    "目标类型",
    "操作",
    "说明",
    "是否分区",
    "是否主键",
    "来源表全名",
    "目标表名",
    "目标视图名",
    "表类型",
    "表中文描述",
    "SELECT表达式",
    "类型待确认",
    "_is_partition",
]


def editor_view(df: pd.DataFrame) -> pd.DataFrame:
    existing = [c for c in EDITOR_COLUMNS if c in df.columns]
    extra = [
        c
        for c in df.columns
        if c not in existing and c not in {"_key", "_is_pk"} and not c.startswith("Unnamed")
    ]
    return df[existing + extra].copy()
