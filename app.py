from __future__ import annotations

import traceback
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import streamlit as st

from stdfield.dataworks import build_packages, import_guide
from stdfield.export import build_excel, dwd_frame
from stdfield.generate import GenerationError, generate_bundle
from stdfield.parse import DWD_COLUMNS, enrich_fields, field_template_bytes, parse_field_list
from stdfield.pipeline import (
    TEMPLATE_EDITABLE,
    add_technical_fields,
    apply_matching,
    editor_view,
    merge_template_edits,
)
from stdfield.roots import DEFAULT_ROOT_LIBRARY_NAME, default_root_library_path, parse_root_library
from stdfield.store import (
    connect,
    list_root_libraries,
    load_history,
    record_root_version,
    save_history,
    save_mapping_snapshot,
    save_root_library,
)
from stdfield.util import cell_str, with_view_prefix
from stdfield.validate import normalize_confirmed, validate_confirmed, validate_fields

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "app.db"
EXPORT_DIR = DATA_DIR / "exports"
ROOT_DIR = DATA_DIR / "root_library"
MAPPING_DIR = DATA_DIR / "mappings"

EDITABLE = TEMPLATE_EDITABLE
# 预览表格与导出文件完全一致：直接使用「DWD模型设计」的 24 列
DISPLAY_COLUMNS = list(DWD_COLUMNS)
DISPLAY_WIDTHS = {
    "目标数据库名": "small",
    "表名": "medium",
    "表名中文名称": "medium",
    "字段名": "medium",
    "字段中文名称": "medium",
    "字段类型": "small",
    "是否分区字段": "small",
    "主键": "small",
    "非空": "small",
    "更新策略（delete\\update\\insert）": "medium",
    "引用代码表": "medium",
    "序号": "small",
    "来源系统名称": "medium",
    "来源系统中文名": "medium",
    "来源库名": "medium",
    "来源Schema": "small",
    "来源表名": "large",
    "来源表中文名称": "medium",
    "来源字段名": "medium",
    "来源字段中文名称": "medium",
    "来源字段类型": "small",
    "字段枚举值": "medium",
    "关联条件": "medium",
    "加工逻辑": "large",
}
PENDING_STATUS = ["多候选", "未匹配", "冲突", "低置信"]

st.set_page_config(page_title="字段标准化", page_icon="📐", layout="wide")


def _init_state() -> None:
    defaults = {
        "matched": None,
        "editor": None,
        "errors": [],
        "warnings": [],
        "root_stats": {},
        "root_conflicts": [],
        "parse_token": None,
        "editor_version": 0,
        "draft": None,
        "draft_ids": [],
        "draft_version": -1,
        "parse_meta": {},
        "export_zip": None,
        "export_name": "",
        "export_files": {},
        "export_dw_packages": [],
        "export_dw_files": {},
        "export_dw_files": {},
        "export_signature": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


@st.cache_data(show_spinner=False)
def cached_parse_roots(file_bytes: bytes, filename: str):
    return parse_root_library(file_bytes, filename=filename)


@st.cache_data(show_spinner=False, ttl=60)
def _cached_root_files(folder: str) -> list[str]:
    return [p.name for p in list_root_libraries(Path(folder))]


@st.cache_resource(show_spinner=False)
def _safe_connect():
    try:
        for folder in (DATA_DIR, EXPORT_DIR, ROOT_DIR, MAPPING_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        return connect(DB_PATH, check_same_thread=False)
    except Exception as exc:  # noqa: BLE001 - 只降级持久化，不影响生成
        st.sidebar.warning(f"持久化不可用（{exc}），本次仍可正常生成与下载。")
        return None


def _read_chosen_root(uploaded, existing_name: str) -> tuple[bytes, str]:
    """词根库优先级：本次上传 > 已保存版本 > 镜像内置默认词根库。"""
    if uploaded is not None:
        return uploaded.getvalue(), uploaded.name
    if existing_name:
        path = ROOT_DIR / existing_name
        if path.is_file():
            return path.read_bytes(), path.name
    path = default_root_library_path()
    if path is not None:
        return path.read_bytes(), DEFAULT_ROOT_LIBRARY_NAME
    raise ValueError("未找到内置词根库，请上传词根库文件。")


def _reset_export() -> None:
    st.session_state.export_zip = None
    st.session_state.export_name = ""
    st.session_state.export_files = {}
    st.session_state.export_dw_packages = []
    st.session_state.export_dw_files = {}
    st.session_state.export_signature = None


def _run_pipeline(
    root_bytes: bytes,
    root_name: str,
    field_bytes: bytes,
    field_name: str,
    conn,
) -> None:
    library = cached_parse_roots(root_bytes, root_name)
    if conn is not None:
        record_root_version(conn, root_name)
        try:
            save_root_library(ROOT_DIR, root_name, root_bytes)
            _cached_root_files.clear()
        except OSError:
            pass

    fields_df = enrich_fields(parse_field_list(field_bytes, field_name))
    if fields_df.empty:
        raise ValueError("未从字段清单中识别到字段行，请确认使用模板且表头完整。")

    errors, warnings = validate_fields(fields_df)
    history = load_history(conn) if conn is not None else {}
    matched = apply_matching(fields_df, library, history) if not errors else pd.DataFrame()

    st.session_state.errors = errors
    st.session_state.warnings = warnings
    st.session_state.root_stats = library.stats()
    st.session_state.root_conflicts = library.conflicts
    st.session_state.matched = matched
    st.session_state.editor = editor_view(matched) if not matched.empty else None
    st.session_state.parse_token = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.session_state.editor_version += 1
    if st.session_state.editor is not None:
        _rebuild_draft(st.session_state.editor)
    else:
        st.session_state.draft = None
        st.session_state.draft_ids = []
    project_name = ""
    if "目标项目空间" in fields_df.columns:
        projects = [cell_str(v) for v in fields_df["目标项目空间"] if cell_str(v)]
        if projects:
            project_name = max(set(projects), key=projects.count)
    st.session_state.parse_meta = {
        "root_library": root_name,
        "field_file": field_name,
        "project_name": project_name,
    }
    _reset_export()


def _render_uploads() -> tuple:
    st.subheader("1. 上传输入")
    builtin = default_root_library_path() is not None
    hint = "内置词根库已启用" if builtin else "需上传词根库"
    st.caption(
        f"字段清单按输入模板填写，标准化只需填写「字段名」和「字段类型」。{hint}；如需更换词根库可在此上传覆盖。"
    )
    c1, c2 = st.columns(2)
    with c1:
        field_upload = st.file_uploader(
            "① 字段清单（.xlsx / .csv，与输入模板一致）", type=["xlsx", "csv"], key="field_upload"
        )
    with c2:
        root_upload = st.file_uploader(
            "② 词根库（可选，默认使用内置词根库）", type=["xlsx"], key="root_upload"
        )
    if root_upload is not None:
        st.caption(f"本次使用上传的词根库：{root_upload.name}")
    elif builtin:
        st.caption(f"本次使用内置词根库：{DEFAULT_ROOT_LIBRARY_NAME}")
    return field_upload, root_upload


def _render_root_stats() -> None:
    stats = st.session_state.root_stats
    if not stats:
        return
    st.subheader("2. 解析结果")
    cols = st.columns(5)
    for col, key in zip(cols, ["词根条数", "中文别名数", "英文全称数", "英文缩写数", "多义缩写数"]):
        col.metric(key, stats.get(key, 0))
    if st.session_state.root_conflicts:
        with st.expander(f"多义缩写冲突（{len(st.session_state.root_conflicts)} 个，命中时需人工确认）"):
            st.dataframe(pd.DataFrame(st.session_state.root_conflicts), use_container_width=True, height=240)


def _render_issues() -> None:
    errors = st.session_state.errors or []
    warnings = st.session_state.warnings or []
    if errors:
        st.error(f"校验未通过，已阻断生成：{len(errors)} 个错误。请先修正字段清单。")
        st.dataframe(pd.DataFrame(errors), use_container_width=True, height=240)
    if warnings:
        with st.expander(f"警告 {len(warnings)} 条（可继续）", expanded=False):
            st.dataframe(pd.DataFrame(warnings), use_container_width=True, height=240)


def _rebuild_draft(editor: pd.DataFrame) -> None:
    """重建预览草稿（模板 24 列），只在解析 / 批量操作后执行。"""
    draft = dwd_frame(editor)
    st.session_state.draft = draft
    st.session_state.draft_ids = list(draft.index)
    st.session_state.draft_version = st.session_state.editor_version


def _draft_changed(before: pd.DataFrame, after: pd.DataFrame) -> bool:
    """对比草稿与表格返回值（可编辑列）。"""
    if after is None or len(before) != len(after):
        return True
    for col in EDITABLE:
        if col not in before.columns or col not in after.columns:
            continue
        if not before[col].astype(str).reset_index(drop=True).equals(
            after[col].astype(str).reset_index(drop=True)
        ):
            return True
    return False


def _batch_apply(action: str, prefix: str) -> None:
    editor = st.session_state.editor
    if editor is None:
        return
    df = editor.copy()
    if action == "采纳推荐":
        df["标准英文名"] = df["推荐标准英文名"].map(cell_str)
        df["匹配状态"] = df["匹配状态"].map(lambda s: "已匹配" if cell_str(s) in {"已匹配", "低置信"} else s)
        df["操作"] = "采纳推荐"
    elif action == "无需变更":
        # 只处理有来源字段名的行：技术字段、加工逻辑字段保持原有标准名
        tech = df["_is_tech"].fillna(False).astype(bool) if "_is_tech" in df.columns else pd.Series(False, index=df.index)
        has_source = df["来源字段英文名"].map(lambda v: cell_str(v) != "")
        target = (~tech) & has_source
        df.loc[target, "标准英文名"] = df.loc[target, "来源字段英文名"].map(lambda v: cell_str(v).lower())
        df.loc[target, "标准中文名"] = df.loc[target, "来源字段中文名"]
        df.loc[target, "匹配状态"] = "已匹配"
        df.loc[target, "操作"] = "无需变更"
    else:
        base = df["目标表名"].map(cell_str) if "目标表名" in df.columns else pd.Series("", index=df.index)
        fallback = df["表名"].map(cell_str)
        base = base.where(base != "", fallback)
        df["目标视图名"] = [with_view_prefix(cell_str(v), prefix) for v in base]
        df["操作"] = df["操作"].map(lambda v: cell_str(v) or "手动输入")
    st.session_state.editor = df
    st.session_state.editor_version += 1
    _rebuild_draft(df)
    _reset_export()
    st.rerun()


TEMPLATE_EDITABLE_COLUMNS = ["标准英文名", "标准中文名", "目标类型", "是否分区", "是否主键"]


def _template_changed(before: pd.DataFrame, after: pd.DataFrame) -> bool:
    """判断人工确认表格里的可编辑列是否被改动。"""
    if len(before) != len(after):
        return True
    for col in TEMPLATE_EDITABLE_COLUMNS:
        if col not in before.columns or col not in after.columns:
            continue
        if not before[col].astype(str).reset_index(drop=True).equals(
            after[col].astype(str).reset_index(drop=True)
        ):
            return True
    return False


def _render_editor(prefix: str, tech_enabled: bool = True) -> tuple[pd.DataFrame | None, list[dict]]:
    editor = add_technical_fields(st.session_state.editor, enabled=tech_enabled)
    if editor is None or editor.empty:
        st.info("没有可确认的字段。")
        return None, []

    editor = normalize_confirmed(editor)
    st.session_state.editor = editor

    # 预览草稿 = 表格数据源：只在解析 / 批量操作后重建，编辑后直接沿用组件返回值，
    # 保证「传给组件的值」与「组件自身状态」始终一致（否则 Streamlit 会重置表格）。
    if (
        st.session_state.draft is None
        or st.session_state.draft_version != st.session_state.editor_version
        or len(st.session_state.draft) != len(editor)
    ):
        _rebuild_draft(editor)
    draft: pd.DataFrame = st.session_state.draft

    total = len(editor)
    matched = int((editor["标准英文名"].map(cell_str) != "").sum())
    pending = int(editor["匹配状态"].isin(PENDING_STATUS).sum())
    m = st.columns(4)
    m[0].metric("字段总数", total)
    m[1].metric("已给出标准名", matched)
    m[2].metric("待人工确认", pending)
    m[3].metric("涉及表数", int(editor["表名"].nunique()))

    b1, b2, b3, b4 = st.columns([1, 1, 1.4, 1.6])
    if b1.button("批量采纳推荐", use_container_width=True):
        _batch_apply("采纳推荐", prefix)
    if b2.button("批量标记无需变更", use_container_width=True):
        _batch_apply("无需变更", prefix)
    if b3.button("按前缀重算视图名", use_container_width=True):
        _batch_apply("重算视图名", prefix)
    b4.download_button(
        "下载当前确认表 CSV",
        data=draft.to_csv(index=False).encode("utf-8-sig"),
        file_name="DWD模型设计_草稿.csv",
        mime="text/csv",
        use_container_width=True,
    )

    editable = [c for c in EDITABLE if c in draft.columns]
    disabled = [c for c in draft.columns if c not in editable]
    edited = st.data_editor(
        draft,
        use_container_width=True,
        height=min(620, 140 + 35 * len(draft)),
        disabled=disabled,
        column_config={
            **{
                name: st.column_config.TextColumn(name, width=DISPLAY_WIDTHS.get(name, "medium"))
                for name in DISPLAY_COLUMNS
            },
            "字段名": st.column_config.TextColumn(
                "字段名",
                help="标准化后的英文名：仅小写字母/数字/下划线，不以数字开头，长度≤64，同表内不重复",
                width="medium",
                required=True,
            ),
            "字段类型": st.column_config.TextColumn(
                "字段类型",
                help="标准化后的 MaxCompute 类型，如 string / bigint / decimal(18,2) / datetime",
                width="small",
            ),
            "加工逻辑": st.column_config.TextColumn(
                "加工逻辑", help="常量或表达式，作为 SELECT 表达式；原样回写到导出结果", width="large"
            ),
        },
        hide_index=True,
        key=f"editor_{st.session_state.parse_token}_{st.session_state.draft_version}",
    )
    if _draft_changed(draft, edited):
        # 注意：**不要**把 edited 写回 draft。draft 是表格的数据源，一旦每轮都换成新对象，
        # Streamlit 会重建表格并把内部滚动条重置到第一行。编辑值由组件自身状态保存，
        # 每轮通过返回值 `edited` 增量合并进 session_state.editor 即可。
        merged = merge_template_edits(
            editor, edited, row_ids=st.session_state.draft_ids
        )
        st.session_state.editor = merged
        editor = merged

    issues = validate_confirmed(editor)
    if issues:
        st.warning(f"导出前仍有 {len(issues)} 个问题需要修正（见下表）。")
        st.dataframe(
            pd.DataFrame(issues, columns=["表名", "字段中文名", "问题"]),
            use_container_width=True,
            height=220,
            column_config={
                "表名": st.column_config.TextColumn("表名", width="medium"),
                "字段中文名": st.column_config.TextColumn("字段中文名", width="medium"),
                "问题": st.column_config.TextColumn("问题", width="large"),
            },
        )
    else:
        st.success("当前映射通过导出校验。")
    return editor, issues


def _build_zip(
    excel: bytes,
    files: dict[str, str],
    meta: dict,
    dw_packages: list[tuple[str, bytes]] | None = None,
    dw_guide: str = "",
) -> bytes:
    note = [
        "字段标准化产物",
        f"生成时间：{meta.get('generated_at', '')}",
        f"词根库：{meta.get('root_library', '')}",
        f"字段清单：{meta.get('field_file', '')}",
        f"输出模式：{meta.get('output_mode', '')}",
        "",
        "文件说明：",
        "- 标准化结果.xlsx：表目录 / 变更历史 / DWD模型设计（结构与输入模板一致，可直接回填上传）",
    ]
    for name in files:
        note.append(f"- {name}")
    for dw_name, _ in dw_packages or []:
        note.append(f"- dataworks迁移包/{dw_name}.zip：DataWorks 迁移助手导入包（该包对应项目空间）")
    if dw_packages:
        note.append("- dataworks迁移包/导入说明.txt：迁移助手导入步骤与注意事项")
    note.append("")
    note.append(
        "注意：视图脚本与 Excel 来自同一份确认后的映射，列顺序一致；"
        "脚本以 ${目标项目空间}.对象名、${来源项目空间}.来源表 引用，运行前需在调度参数中定义同名参数。"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as zf:
        zf.writestr("标准化结果.xlsx", excel)
        for name, content in files.items():
            zf.writestr(name, content)
        for dw_name, dw_zip in dw_packages or []:
            zf.writestr(f"dataworks迁移包/{dw_name}.zip", dw_zip)
        if dw_packages:
            zf.writestr("dataworks迁移包/导入说明.txt", dw_guide)
        zf.writestr("README.txt", "\n".join(note) + "\n")
    return buffer.getvalue()


def _signature(config: dict, editor: pd.DataFrame | None) -> tuple:
    version = st.session_state.parse_token or ""
    content = 0
    if editor is not None and not editor.empty:
        cols = [
            c
            for c in ("标准英文名", "标准中文名", "目标类型", "是否分区", "是否主键", "字段序号")
            if c in editor.columns
        ]
        if cols:
            content = int(pd.util.hash_pandas_object(editor[cols].astype(str), index=True).sum())
    return (version, st.session_state.editor_version, tuple(sorted(config.items())), len(editor) if editor is not None else 0, content)


def _render_export(
    config: dict,
    final: pd.DataFrame | None,
    issues: list[dict],
    conn,
    stale: bool = False,
) -> None:
    st.subheader("4. 生成导出")
    blocked = bool(st.session_state.errors) or bool(issues) or final is None or final.empty
    if blocked:
        st.caption("修正校验问题后即可生成。")
    if st.button("生成并下载全部产物", type="primary", disabled=blocked):
        records = final.to_dict(orient="records")
        try:
            bundle = generate_bundle(
                confirmed=records,
                output_mode=config["output_mode"],
                expose_partition=config["expose_partition"],
                view_comment_override=config["view_comment"],
                generate_pyodps_flag=config["generate_py"],
                partition_mode=config["partition_mode"],
                partition_value=config["partition_value"],
                lifecycle=config["lifecycle"],
                split_by_table=config["split_by_table"],
                view_name_prefix=config["view_prefix"],
                project=cell_str(config["dw_project"]),
            )
            generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            meta = {
                "generated_at": generated_at,
                "root_library": st.session_state.parse_meta.get("root_library", ""),
                "input_file": st.session_state.parse_meta.get("input_file", ""),
                "output_mode": config["output_mode"],
            }
            excel = build_excel(final, meta=meta)

            dw_packages: list[tuple[str, bytes]] = []
            dw_files: dict[str, str] = {}
            dw_guide = ""
            dw_error = ""
            if config["dw_enabled"]:
                try:
                    packages = build_packages(
                        confirmed=records,
                        output_mode=config["output_mode"],
                        project=cell_str(config["dw_project"]),
                        view_name_prefix=config["view_prefix"],
                        expose_partition=config["expose_partition"],
                        view_comment_override=config["view_comment"],
                        partition_mode=config["partition_mode"],
                        partition_value=config["partition_value"],
                        lifecycle=config["lifecycle"],
                        workflow_mode=config["dw_workflow_mode"],
                        workflow_name=config["dw_workflow_name"],
                        folder_override=config["dw_folder"],
                        owner=config["dw_owner"],
                        cron=config["dw_cron"],
                        resource_group=config["dw_resgroup"],
                        resource_group_name=config["dw_resgroup_name"],
                        rerun_mode=config["dw_rerun_mode"],
                        task_rerun_interval=config["dw_rerun_interval"],
                    )
                    dw_packages = [(p.package_name, p.zip_bytes) for p in packages]
                    dw_guide = import_guide(packages, config["output_mode"])
                    for package in packages:
                        for path, content in package.file_map(
                            owner=config["dw_owner"],
                            cron=config["dw_cron"],
                            resource_group=config["dw_resgroup"],
                            resource_group_name=config["dw_resgroup_name"],
                            rerun_mode=config["dw_rerun_mode"],
                            task_rerun_interval=config["dw_rerun_interval"],
                        ).items():
                            dw_files[f"{package.package_name}.zip!{path}"] = content
                except (GenerationError, ValueError) as exc:
                    dw_error = str(exc)

            zip_bytes = _build_zip(excel, bundle, meta, dw_packages, dw_guide)
            name = f"{batch_id}_字段标准化产物.zip"

            if conn is not None:
                try:
                    save_history(conn, batch_id, records)
                    save_mapping_snapshot(
                        MAPPING_DIR,
                        batch_id,
                        {
                            "batch_id": batch_id,
                            "generated_at": generated_at,
                            "meta": st.session_state.parse_meta,
                            "output_mode": config["output_mode"],
                            "records": records,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"映射历史写入失败：{exc}")
            try:
                EXPORT_DIR.mkdir(parents=True, exist_ok=True)
                (EXPORT_DIR / name).write_bytes(zip_bytes)
            except OSError as exc:
                st.warning(f"导出归档失败（不影响下载）：{exc}")

            st.session_state.export_zip = zip_bytes
            st.session_state.export_name = name
            st.session_state.export_files = bundle
            st.session_state.export_dw_packages = dw_packages
            st.session_state.export_dw_files = dw_files
            st.session_state.export_signature = _signature(config, final)
            packages_note = (
                "（含 DataWorks 迁移包 "
                + "、".join(f"{pkg}.zip" for pkg, _ in dw_packages)
                + "）"
                if dw_packages
                else ""
            )
            st.success(f"已生成 {name}{packages_note}")
            if dw_error:
                st.error(f"DataWorks 迁移包未生成：{dw_error}（其余产物已正常导出）")
        except (GenerationError, ValueError) as exc:
            st.error(f"生成失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"生成失败：{exc}")
            with st.expander("错误详情"):
                st.code(traceback.format_exc())

    if st.session_state.export_zip:
        if stale:
            st.warning("映射已修改，下面的产物是上一次生成的结果，请重新「生成并下载全部产物」。")
        dw_packages = st.session_state.export_dw_packages or []
        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "⬇️ 下载全部产物 ZIP",
                data=st.session_state.export_zip,
                file_name=st.session_state.export_name,
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
        with d2:
            if dw_packages:
                names = [f"{name}.zip" for name, _ in dw_packages]
                pick = st.selectbox("选择迁移包", names, index=0, key="dw_pick")
                data = dw_packages[names.index(pick)][1]
                st.download_button(
                    "⬇️ 单独下载所选迁移包",
                    data=data,
                    file_name=pick,
                    mime="application/zip",
                    use_container_width=True,
                )
            else:
                st.caption("未生成迁移助手导入包。")

        code_files = dict(st.session_state.export_files)
        dw_files = st.session_state.export_dw_files or {}
        tabs = st.tabs(["SQL / PyODPS"] + (["DataWorks 迁移包"] if dw_files else []))
        with tabs[0]:
            if code_files:
                sub = st.tabs(list(code_files))
                for tab, (name, content) in zip(sub, code_files.items()):
                    with tab:
                        language = "python" if name.endswith(".py") else "sql"
                        st.code(content, language=language)
            else:
                st.caption("没有 SQL 产物。")
        if dw_files:
            with tabs[1]:
                st.caption(
                    "每个目标项目空间生成一个包：<项目名>_<yyyymmdd>/project.xml + "
                    "<项目名>_<yyyymmdd>/src/workflows/BUSINESS_dwd_<系统简称>/workflow.xml；"
                    "每个目标对象一个 ODPS_SQL 节点，**节点名 = 目标对象名去掉 v_ 前缀**，"
                    "节点内容为建视图或 INSERT OVERWRITE 语句。"
                )
                st.dataframe(
                    pd.DataFrame(
                        [{"包内路径": path, "字节数": len(content.encode("utf-8"))} for path, content in dw_files.items()]
                    ),
                    use_container_width=True,
                    height=min(320, 80 + 28 * len(dw_files)),
                )
                pick = st.selectbox("预览文件", list(dw_files), index=0)
                content = dw_files[pick]
                st.code(content, language="xml" if pick.endswith(".xml") else "sql")


def main() -> None:
    _init_state()
    st.title("公共中心标准化")
    st.caption(
        "不连源库、不连 DataWorks。上传字段清单 → 校验 → 人工确认 → 导出标准化结果、建视图脚本与迁移助手导入包。"
    )

    conn = _safe_connect()
    root_files = _cached_root_files(str(ROOT_DIR)) if ROOT_DIR.exists() else []

    with st.sidebar:
        st.header("导出配置")
        output_mode = st.radio("输出模式", ["视图", "物理表"], index=0)
        view_prefix = st.text_input("视图名前缀", value="v_", help="默认 v_ + 表名；输入清单已带视图名时优先使用")
        view_comment = st.text_input("视图注释（空=表名中文名称）", value="")
        expose_partition = st.checkbox(
            "视图中暴露分区字段",
            value=True,
            help="默认勾选：所有视图都带分区字段，且分区字段排在视图最后一列（取消勾选则不输出分区字段）",
        )
        split_by_table = st.checkbox("按表拆分视图脚本", value=False)
        generate_py = st.checkbox("生成 PyODPS 建表脚本", value=False, help="视图模式通常不需要")
        st.divider()
        st.subheader("技术字段")
        tech_fields = st.checkbox(
            "自动补充 3 个技术字段",
            value=True,
            help="每个目标对象末尾补 fst_load_tm=getdate()、lst_load_tm=getdate()、load_fm='系统简称'；"
            "清单中已有同名字段时自动跳过",
        )
        st.caption("fst_load_tm 首次加载时间 / lst_load_tm 最后一次更新时间 / load_fm 来源系统")
        st.divider()
        partition_mode = st.selectbox("分区写入模式（物理表）", ["static", "dynamic"], index=0)
        partition_value = st.text_input("静态分区值", value="${bizdate}")
        lifecycle = st.number_input("LIFECYCLE", min_value=1, value=30, step=1)
        st.divider()
        st.subheader("DataWorks 迁移包")
        dw_enabled = st.checkbox(
            "生成迁移助手导入包（ZIP）", value=True, help="导入 DataWorks 迁移助手后直接创建节点"
        )
        dw_project = st.text_input(
            "目标项目空间", value="", placeholder="留空则取输入清单「目标数据库名」", help="同时作为迁移包目录名前缀"
        )
        dw_workflow_mode = st.selectbox(
            "工作流组织方式",
            ["per_system", "single"],
            index=0,
            format_func=lambda v: "每个来源系统一个工作流（dwd_<系统简称>）" if v == "per_system" else "全部节点一个工作流",
        )
        dw_workflow_name = st.text_input(
            "统一工作流名（仅单工作流时生效）", value="", placeholder="留空则用 dwd_all"
        )
        dw_folder = st.text_input(
            "节点目录（可选）", value="", placeholder="留空则为 业务流程/<工作流>/folderMaxCompute/<主题>"
        )
        with st.expander("调度与责任人设置", expanded=False):
            dw_owner = st.text_input("项目 owner", value="", placeholder="DataWorks 账号 ID，留空用默认值")
            dw_cron = st.text_input("调度 Cron", value="00 24 00 * * ?")
            dw_resgroup = st.text_input("调度资源组", value="", placeholder="留空为 sys_default")
            dw_resgroup_name = st.text_input("资源组名称", value="", placeholder="留空为 Default Group")
            dw_rerun_mode = st.selectbox("重跑模式", ["ALL_ALLOWED", "FAILURE_ALLOWED", "ALL_DENIED"], index=0)
            dw_rerun_interval = st.text_input("重跑间隔（毫秒）", value="120000")
        st.divider()
        st.subheader("词根库版本")
        existing = "(使用内置默认词根库)"
        root_choice = st.selectbox("已保存词根库", [existing] + root_files, index=0)
        st.download_button(
            "下载字段清单模板",
            data=field_template_bytes(),
            file_name="字段清单模板.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        if conn is not None:
            st.caption(f"数据目录：{DATA_DIR}")

    field_upload, root_upload = _render_uploads()

    config = {
        "output_mode": output_mode,
        "view_prefix": view_prefix,
        "view_comment": view_comment,
        "expose_partition": expose_partition,
        "split_by_table": split_by_table,
        "generate_py": generate_py,
        "tech_fields": tech_fields,
        "partition_mode": partition_mode,
        "partition_value": partition_value,
        "lifecycle": int(lifecycle),
        "dw_enabled": dw_enabled,
        "dw_project": dw_project,
        "dw_workflow_mode": dw_workflow_mode,
        "dw_workflow_name": dw_workflow_name,
        "dw_owner": dw_owner,
        "dw_cron": dw_cron,
        "dw_resgroup": dw_resgroup,
        "dw_resgroup_name": dw_resgroup_name,
        "dw_rerun_mode": dw_rerun_mode,
        "dw_rerun_interval": dw_rerun_interval,
        "dw_folder": dw_folder,
    }

    if st.button("解析并匹配", type="primary"):
        try:
            root_bytes, root_name = _read_chosen_root(
                root_upload, "" if root_choice == existing else root_choice
            )
            if field_upload is None:
                raise ValueError("请上传字段清单（与输入模板同样的列结构）。")
            with st.spinner("解析词根库与字段清单…"):
                _run_pipeline(
                    root_bytes=root_bytes,
                    root_name=root_name,
                    field_bytes=field_upload.getvalue(),
                    field_name=field_upload.name,
                    conn=conn,
                )
        except Exception as exc:  # noqa: BLE001
            st.error(f"解析失败：{exc}")
            with st.expander("错误详情"):
                st.code(traceback.format_exc())

    _render_root_stats()
    _render_issues()

    editor = st.session_state.editor
    if editor is None or (isinstance(editor, pd.DataFrame) and editor.empty):
        if st.session_state.parse_token and not st.session_state.errors:
            st.info("没有可确认的字段。")
        return

    st.subheader("3. 人工确认")
    st.caption(
        "表格即最终「DWD模型设计」内容：可直接修改「字段名」「字段中文名称」「字段类型」「是否分区字段」「主键」"
        "（字段名会自动转小写、空格转下划线）；多候选 / 未匹配 / 冲突 建议人工确认。"
    )
    final, issues = _render_editor(view_prefix, config["tech_fields"])

    current = _signature(config, final)
    stale = bool(st.session_state.export_zip) and st.session_state.export_signature != current

    _render_export(config, final, issues, conn, stale=stale)


if __name__ == "__main__":
    main()
