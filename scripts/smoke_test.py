#!/usr/bin/env python3
"""用仓库内置词根库与输入文件模板跑通解析 → 匹配 → 生成 → 打包全流程。"""
from __future__ import annotations

import sys
import zipfile
import io
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stdfield.dataworks import build_package, import_guide  # noqa: E402
from stdfield.export import build_excel  # noqa: E402
from stdfield.generate import generate_bundle  # noqa: E402
from stdfield.parse import enrich_fields, parse_field_list  # noqa: E402
from stdfield.pipeline import add_technical_fields, apply_matching, editor_view  # noqa: E402
from stdfield.roots import load_default_root_library, parse_root_library  # noqa: E402
from stdfield.validate import normalize_confirmed, validate_confirmed, validate_fields  # noqa: E402

INPUT_XLSX = ROOT / "输入文件模板.xlsx"
if not INPUT_XLSX.exists():  # 仓库里与 raw/ 一起提交
    INPUT_XLSX = ROOT / "raw" / "输入文件模板.xlsx"


def main() -> None:
    library = parse_root_library(*load_default_root_library())
    print("内置词根库:", library.stats())

    input_bytes = INPUT_XLSX.read_bytes()
    fields = enrich_fields(parse_field_list(input_bytes, INPUT_XLSX.name))
    print("字段数:", len(fields), "目标对象数:", fields["表名"].nunique())
    if fields.empty:
        raise SystemExit("解析结果为空，请检查输入文件")

    errors, warnings = validate_fields(fields)
    print("校验错误:", len(errors), "警告:", len(warnings))

    matched = apply_matching(fields, library, {})
    print("匹配状态:", matched["匹配状态"].value_counts().to_dict())

    final = normalize_confirmed(add_technical_fields(editor_view(matched)))
    tech = final[final["_is_tech"].fillna(False).astype(bool)]
    print("技术字段:", tech["标准英文名"].tolist(), tech["SELECT表达式"].tolist())
    # 词根库未覆盖的中文名需要人工补名，这里用来源字段名兜底以便跑通烟雾测试
    final["标准英文名"] = (
        final["标准英文名"].replace("", pd.NA).fillna(final["来源字段英文名"]).fillna("std_col")
    )
    issues = validate_confirmed(final)
    if issues:
        print("导出校验问题:", issues[:5])
        raise SystemExit(1)

    records = final.to_dict(orient="records")
    for mode in ("视图", "物理表"):
        bundle = generate_bundle(
            confirmed=records,
            output_mode=mode,
            expose_partition=True,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        print(f"---- {mode} 脚本 ----")
        for name, content in bundle.items():
            print("====", name, "====")
            print(content[:600])

        package = build_package(records, mode, project="cdm_pt_prod")
        archive = zipfile.ZipFile(io.BytesIO(package.zip_bytes))
        print(f"DataWorks 迁移包（{mode}）:", package.package_name, len(package.nodes), "个节点",
              len(package.zip_bytes), "字节")
        for path in archive.namelist():
            print("   ", path, len(archive.read(path)))
        print(import_guide(package, mode).splitlines()[0])

    excel = build_excel(final)
    print("Excel 字节数:", len(excel))
    print("烟雾测试通过")


if __name__ == "__main__":
    main()
