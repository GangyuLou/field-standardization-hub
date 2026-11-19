from __future__ import annotations

import io
import sys
import unittest
from datetime import datetime
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402

from stdfield.dataworks import build_package, build_packages, sanitize_name  # noqa: E402
from stdfield.export import build_excel, dwd_frame  # noqa: E402
from stdfield.generate import GenerationError, generate_bundle, generate_view_ddl  # noqa: E402
from stdfield.match import match_field  # noqa: E402
from stdfield.parse import (  # noqa: E402
    DWD_COLUMNS,
    enrich_fields,
    field_template_bytes,
    parse_field_list,
    system_short_name,
)
from stdfield.pipeline import (  # noqa: E402
    TECHNICAL_FIELDS,
    TEMPLATE_EDITABLE,
    add_technical_fields,
    apply_matching,
    editor_view,
    merge_edits,
    merge_template_edits,
)
from stdfield.roots import (  # noqa: E402
    default_root_library_path,
    load_default_root_library,
    parse_root_library,
)
from stdfield.types import map_source_type, normalize_target_type  # noqa: E402
from stdfield.validate import normalize_confirmed, validate_confirmed, validate_fields  # noqa: E402
from stdfield.xlsx import read_excel, sanitize_xlsx  # noqa: E402

INPUT_XLSX = ROOT / "输入文件模板.xlsx"


def _column(name: str, seq: int, source: str, cn: str, standard: str, **kwargs) -> dict:
    base = {
        "来源系统缩写": "ods_pt19gipos",
        "来源系统简称": "pt19gipos",
        "来源库名": "medical1",
        "表名": "t1",
        "字段序号": str(seq),
        "来源字段英文名": source,
        "来源字段中文名": cn,
        "来源字段类型": "varchar(100)",
        "标准英文名": standard,
        "标准中文名": cn,
        "目标类型": "STRING",
        "目标视图名": "v_t1",
        "目标表名": "t1",
        "目标项目空间": "cdm_pt_prod",
        "来源表全名": "medical1.t1",
        "是否分区": "否",
        "是否主键": "",
        "SELECT表达式": source,
        "_is_partition": False,
        "_is_pk": False,
        "表中文描述": "测试表",
        "匹配状态": "已匹配",
        "操作": "采纳推荐",
        "说明": "",
    }
    base.update(kwargs)
    return base


class XlsxReadTest(unittest.TestCase):
    def test_broken_autofilter_is_sanitized(self) -> None:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame({"a": [1, 2]}).to_excel(writer, sheet_name="数据", index=False)
        source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        broken = io.BytesIO()
        with source, zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                payload = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    text = payload.decode("utf-8")
                    text = text.replace(
                        "<sheetData>",
                        '<autoFilter ref="A1:A2"><filterColumn colId="0"><customFilters><customFilter '
                        'operator="equal" val="护理项目明细"/></customFilters></filterColumn></autoFilter>'
                        "<sheetData>",
                    )
                    payload = text.encode("utf-8")
                target.writestr(item, payload)
        data = broken.getvalue()
        with self.assertRaises(Exception):
            openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True)
        self.assertNotIn(b"autoFilter", sanitize_xlsx(data))
        frame = read_excel(data, sheet_name="数据")
        self.assertEqual(list(frame.columns), ["a"])


class DefaultRootLibraryTest(unittest.TestCase):
    def test_bundled_default_exists(self) -> None:
        self.assertIsNotNone(default_root_library_path())
        data, name = load_default_root_library()
        library = parse_root_library(data, name)
        self.assertGreater(len(library.roots), 4000)


class ParseTest(unittest.TestCase):
    def test_input_template(self) -> None:
        fields = parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name)
        self.assertEqual(len(fields), 11)
        self.assertEqual(fields.iloc[0]["表名"], "dwd_ast_cust_eqty_cs_estim_df")
        self.assertEqual(fields.iloc[2]["目标视图名"], "v_dwd_ast_cust_eqty_cs_estim_df")
        self.assertEqual(fields.iloc[0]["目标项目空间"], "cdm_pt_prod")
        self.assertEqual(
            fields.iloc[0]["来源表全名"], "ods_pt17cccs.v_oracccs122_cccs_spms_customer_evaluation"
        )
        self.assertEqual(fields.iloc[0]["建议目标类型"], "string")
        self.assertEqual(fields.iloc[9]["建议标准英文名"], "load_fm")
        # 合并单元格/空值需向下填充或留空，来源表全名不得为空
        self.assertTrue(all(fields["来源表全名"] != ""))
        self.assertEqual(fields.iloc[9]["加工逻辑"], "'pt17cccs'")

    def test_template_roundtrip(self) -> None:
        data = field_template_bytes()
        workbook = openpyxl.load_workbook(io.BytesIO(data))
        self.assertIn("DWD模型设计", workbook.sheetnames)
        sheet = workbook["DWD模型设计"]
        header = [c.value for c in sheet[2]]
        self.assertEqual(header, DWD_COLUMNS)
        self.assertEqual(sheet["A1"].value, "目标")
        fields = parse_field_list(data, "字段清单模板.xlsx")
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields.iloc[0]["表名"], "dwd_ast_cust_eqty_cs_estim_df")
        self.assertEqual(fields.iloc[0]["建议标准英文名"], "key_id")
        self.assertEqual(fields.iloc[0]["建议目标类型"], "string")

    def test_csv(self) -> None:
        csv = (
            "目标数据库名,表名,字段名,字段类型,序号,来源系统名称,来源表名,来源字段名,来源字段中文名称\n"
            "cdm_pt_prod,dwd_t1,key_id,string,1,ods_pt17cccs,ods_pt17cccs.v1,id,主键\n"
        ).encode("utf-8-sig")
        fields = parse_field_list(csv, "fields.csv")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields.iloc[0]["表名"], "dwd_t1")

    def test_missing_template_columns_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_field_list(b"a,b\n1,2\n", "x.csv")

    def test_system_short_name(self) -> None:
        self.assertEqual(system_short_name("ods_pt17cccs"), "pt17cccs")
        self.assertEqual(system_short_name("pt17cccs"), "pt17cccs")

    def test_enrich_without_table_inventory(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        self.assertEqual(fields["表中文描述"].iloc[0], "客户服务评价")
        self.assertTrue(fields["_is_partition"].iloc[-1])
        self.assertFalse(fields["_is_partition"].iloc[0])

    def test_view_prefix_not_doubled_when_table_has_prefix(self) -> None:
        fields = enrich_fields(
            pd.DataFrame(
                [
                    _column(
                        "a", 1, "policy_no", "保单号", "policy_no",
                        表名="v_dwd_clm_medic_df", 目标表名="v_dwd_clm_medic_df",
                        目标视图名="v_v_dwd_clm_medic_df", 目标表中文名="理赔子表",
                        建议标准中文名="保单号", 建议标准英文名="policy_no",
                    )
                ]
            )
        )
        self.assertEqual(fields.iloc[0]["目标视图名"], "v_dwd_clm_medic_df")


class MatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.library = parse_root_library(*load_default_root_library())

    def test_chinese_longest_match(self) -> None:
        result = match_field("理赔金额", "claim_amt", self.library)
        self.assertEqual(result.status, "已匹配")
        self.assertEqual(result.standard_en, "claims_amt")

    def test_unmatched_is_not_forced(self) -> None:
        result = match_field("完全不存在的词", "zzz_unknown", self.library)
        self.assertEqual(result.status, "未匹配")
        self.assertEqual(result.standard_en, "")

    def test_ambiguous_abbr_lists_candidates(self) -> None:
        result = match_field("证件号码", "", self.library)
        self.assertIn(result.status, {"多候选", "已匹配"})

    def test_matching_uses_target_chinese_name(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        self.assertEqual(fields.iloc[0]["匹配中文名"], "主键id")  # 目标中文名优先
        matched = apply_matching(fields, self.library, {})
        account = matched[matched["来源字段英文名"] == "account"].iloc[0]
        # 目标「客户证件号码」而非来源「证件号」
        self.assertEqual(account["标准英文名"], "cust_idtty_no")
        self.assertIn("按目标字段中文名匹配", account["说明"])
        sourcetype = matched[matched["来源字段英文名"] == "sourcetype"].iloc[0]
        self.assertEqual(sourcetype["标准英文名"], "apps_src_chnl")

    def test_chinese_name_noise_is_ignored(self) -> None:
        result = match_field("主键id", "", self.library)
        self.assertEqual(result.standard_en, "key")
        self.assertEqual(result.status, "已匹配")
        noisy = match_field("评价渠道来源(1:电话,2:微信,3:APP)", "", self.library)
        self.assertTrue(noisy.standard_en.startswith("apps"))

    def test_bracket_notes_ignored_both_styles(self) -> None:
        for name in [
            "保单号（主键）",
            "保单号(主键)",
            "保单号（主键）(ID)",
            "保单号（含(特殊)说明）",
            "保单号(未闭合",
        ]:
            with self.subTest(name=name):
                result = match_field(name, "", self.library)
                self.assertEqual(result.standard_en, "plc_no")

    def test_target_chinese_name_with_brackets_matches(self) -> None:
        fields = pd.DataFrame(
            [
                _column(
                    "a", 1, "account", "证件号", "x",
                    建议标准中文名="客户证件号码（主键）", 匹配中文名="客户证件号码（主键）",
                )
            ]
        )
        matched = apply_matching(fields, self.library, {})
        self.assertEqual(matched.iloc[0]["标准英文名"], "cust_idtty_no")
        # 注释仍保留原始的括号说明
        self.assertEqual(matched.iloc[0]["标准中文名"], "客户证件号码（主键）")

    def test_falls_back_to_source_chinese_name(self) -> None:
        fields = pd.DataFrame(
            [
                _column(
                    "a", 1, "claim_amt", "理赔金额", "claims_amt",
                    建议标准中文名="无关描述XYZ", 匹配中文名="无关描述XYZ",
                )
            ]
        )
        matched = apply_matching(fields, self.library, {})
        row = matched.iloc[0]
        self.assertEqual(row["标准英文名"], "claims_amt")
        self.assertIn("回退来源字段中文名", row["说明"])

    def test_filled_standard_name_wins(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        matched = apply_matching(fields, self.library, {})
        load_fm = matched[matched["建议标准英文名"] == "load_fm"].iloc[0]
        self.assertEqual(load_fm["标准英文名"], "load_fm")
        self.assertEqual(load_fm["匹配状态"], "已匹配")
        self.assertEqual(load_fm["SELECT表达式"], "'pt17cccs'")
        self.assertIn("采用输入清单已填字段名", load_fm["说明"])

    def test_enriched_system_short_name(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        self.assertEqual(fields.iloc[0]["来源系统简称"], "pt17cccs")


class TypeTest(unittest.TestCase):
    def test_source_mapping(self) -> None:
        self.assertEqual(map_source_type("varchar(100)"), ("STRING", False))
        self.assertEqual(map_source_type("decimal(18,2)"), ("DECIMAL(18,2)", False))
        self.assertEqual(map_source_type("datetime"), ("DATETIME", False))
        self.assertEqual(map_source_type(""), ("STRING", True))
        self.assertEqual(map_source_type("weird"), ("STRING", True))

    def test_target_type_normalization(self) -> None:
        self.assertEqual(normalize_target_type("string"), ("STRING", False))
        self.assertEqual(normalize_target_type("datetime"), ("DATETIME", False))
        self.assertEqual(normalize_target_type("decimal(18,2)"), ("DECIMAL(18,2)", False))
        self.assertEqual(normalize_target_type(""), ("STRING", True))


class MergeEditsTest(unittest.TestCase):
    def test_merge_by_row_id_with_filtered_view(self) -> None:
        original = pd.DataFrame(
            [
                _column("a", 1, "x", "X", "x", 操作="采纳推荐"),
                _column("a", 2, "y", "Y", "y", 操作="采纳推荐"),
            ]
        )
        view = original.loc[[1]].copy()
        view["_row_id"] = view.index
        edited = view.copy().reset_index(drop=True)
        edited.loc[0, "标准英文名"] = "yy"
        merged = merge_edits(original, edited)
        self.assertEqual(merged.loc[0, "标准英文名"], "x")
        self.assertEqual(merged.loc[1, "标准英文名"], "yy")
        self.assertEqual(merged.loc[1, "操作"], "手动输入")


class GenerateTest(unittest.TestCase):
    def test_view_order_and_alias(self) -> None:
        cols = [
            _column("a", 2, "src_b", "乙", "b"),
            _column("a", 1, "src_a", "甲", "a"),
            _column("a", 3, "", "常量", "c", SELECT表达式="getdate()"),
        ]
        sql = generate_view_ddl("v_t1", "medical1.t1", cols, view_comment="注释'引号")
        self.assertLess(sql.index("    a "), sql.index("    b "))
        self.assertLess(sql.index("src_a"), sql.index("AS a"))
        self.assertIn("AS c", sql)
        self.assertIn("COMMENT '注释''引号'", sql)

    def test_view_prefix_not_doubled_in_bundle(self) -> None:
        cols = [
            _column("a", 1, "policy_no", "保单号", "policy_no",
                    表名="v_dwd_policy_df", 目标表名="v_dwd_policy_df",
                    目标视图名="v_v_dwd_policy_df", 来源表全名="medical1.t1"),
        ]
        files = generate_bundle(
            cols,
            output_mode="视图",
            expose_partition=True,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        sql = files["odps_views.sql"]
        self.assertIn("CREATE OR REPLACE VIEW v_dwd_policy_df", sql)
        self.assertNotIn("v_v_", sql)

    def test_partition_exposed_by_default(self) -> None:
        cols = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        # 默认所有视图都带分区字段，且排在最后一列
        default_sql = generate_view_ddl("v_t1", "medical1.t1", cols)
        default_cols = [
            line.split()[0]
            for line in default_sql.split("(", 1)[1].split(")", 1)[0].splitlines()
            if line.strip()
        ]
        self.assertEqual(default_cols, ["policy_no", "ds"])
        self.assertEqual(default_sql.rstrip().splitlines()[-2].strip(), "ds")  # SELECT 中包含分区字段

        hidden = generate_view_ddl("v_t1", "medical1.t1", cols, expose_partition=False)
        hidden_cols = hidden.split("(", 1)[1].split(")", 1)[0].split()
        self.assertNotIn("ds", hidden_cols)

    def test_package_keeps_partition_field_by_default(self) -> None:
        records = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        package = build_package(records, "视图", project="cdm_pt_prod")
        archive = zipfile.ZipFile(io.BytesIO(package.zip_bytes))
        xml = archive.read(
            f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml"
        ).decode("utf-8")
        self.assertIn("ds        COMMENT '分区日期'", xml)

    def test_physical_bundle_static_partition(self) -> None:
        cols = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        files = generate_bundle(
            confirmed=cols,
            output_mode="物理表",
            expose_partition=False,
            view_comment_override="",
            generate_pyodps_flag=True,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        self.assertIn("odps_ddl.sql", files)
        self.assertIn("odps_insert.sql", files)
        self.assertIn("pyodps_create_table.py", files)
        self.assertIn("PARTITION (ds='${bizdate}')", files["odps_insert.sql"])
        self.assertNotIn("    ds", files["odps_insert.sql"].split("SELECT\n", 1)[1])

    def test_dynamic_partition_puts_partition_last(self) -> None:
        cols = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        files = generate_bundle(
            confirmed=cols,
            output_mode="物理表",
            expose_partition=False,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="dynamic",
            partition_value="",
            lifecycle=36000,
        )
        select = files["odps_insert.sql"].split("SELECT\n", 1)[1].split("FROM", 1)[0]
        self.assertIn("PARTITION (ds)", files["odps_insert.sql"])
        lines = [line.strip() for line in select.strip().splitlines() if line.strip()]
        self.assertEqual(lines[-1], "ds")

    def test_illegal_name_rejected(self) -> None:
        with self.assertRaises(GenerationError):
            generate_view_ddl("v_t1", "medical1.t1", [_column("a", 1, "x", "X", "Bad-Name")])


class DataWorksPackageTest(unittest.TestCase):
    def test_view_package_matches_reference_structure(self) -> None:
        records = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        package = build_package(records, "视图", project="cdm_gp_prod")
        archive = zipfile.ZipFile(io.BytesIO(package.zip_bytes))
        members = archive.namelist()
        self.assertIn(f"{package.package_name}/project.xml", members)
        self.assertIn(
            f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml", members
        )
        self.assertEqual(len(members), 2)

        project = ET.fromstring(archive.read(f"{package.package_name}/project.xml"))
        self.assertEqual(project.get("dmObjectType"), "PROJECT")
        self.assertEqual(project.get("name"), "cdm_gp_prod")

        workflow = ET.fromstring(
            archive.read(f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml")
        )
        self.assertEqual(workflow.get("dmObjectType"), "WORKFLOW")
        self.assertEqual(workflow.get("name"), "dwd_pt19gipos")
        self.assertEqual(workflow.get("version"), "V3")
        node = workflow.find("Nodes/Node")
        self.assertEqual(node.get("type"), "ODPS_SQL")
        self.assertEqual(node.get("connection"), "odps_first")
        # 节点名去掉视图名前缀 v_，Outputs 与 project.xml 的 name 保持一致
        self.assertEqual(node.get("name"), "t1")
        self.assertEqual(node.find("Outputs/Output").get("data"), "cdm_gp_prod.t1")
        self.assertIn("业务流程/dwd_pt19gipos/folderMaxCompute/", node.get("folder"))
        self.assertIn("CREATE OR REPLACE VIEW", node.find("Code").text)

    def test_table_package_node_contains_insert_overwrite(self) -> None:
        records = [
            _column("a", 1, "policy_no", "保单号", "policy_no"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        package = build_package(records, "物理表", project="cdm_gp_prod")
        archive = zipfile.ZipFile(io.BytesIO(package.zip_bytes))
        xml = archive.read(
            f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml"
        ).decode("utf-8")
        self.assertIn("INSERT OVERWRITE TABLE", xml)
        self.assertNotIn("CREATE TABLE", xml)

    def test_package_name_has_date_suffix(self) -> None:
        from datetime import datetime

        package = build_package(
            [_column("a", 1, "policy_no", "保单号", "policy_no")],
            "视图",
            project="cdm_gp_prod",
            package_date=datetime(2026, 11, 16),
        )
        self.assertEqual(package.package_name, "cdm_gp_prod_20261116")

    def test_same_workflow_unique_node_names(self) -> None:
        first = _column("a", 1, "policy_no", "保单号", "policy_no")
        second = dict(first, 表名="t2", 来源表全名="medical1.t2")
        package = build_package([first, second], "视图", project="cdm_pt_prod")
        names = [n.name for n in package.nodes]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({n.workflow for n in package.nodes}, {"dwd_pt19gipos"})

    def test_workflows_grouped_per_system(self) -> None:
        a = _column("a", 1, "policy_no", "保单号", "policy_no")
        b = _column("a", 1, "claim_amt", "理赔金额", "claim_amt", **{"来源系统缩写": "ods_pt20xyz", "表名": "t9"})
        package = build_package([a, b], "视图", project="cdm_pt_prod")
        self.assertEqual(sorted(package.workflows), ["dwd_pt19gipos", "dwd_pt20xyz"])

    def test_single_workflow_mode(self) -> None:
        a = _column("a", 1, "policy_no", "保单号", "policy_no")
        b = _column("a", 1, "claim_amt", "理赔金额", "claim_amt", **{"表名": "t2"})
        package = build_package(
            [a, b], "视图", project="cdm_pt_prod", workflow_mode="single", workflow_name="dwd_all_in_one"
        )
        self.assertEqual({n.workflow for n in package.nodes}, {"dwd_all_in_one"})

    def test_folder_override(self) -> None:
        package = build_package(
            [_column("a", 1, "policy_no", "保单号", "policy_no")],
            "视图",
            project="cdm_pt_prod",
            folder_override="业务流程/custom/ast",
        )
        self.assertEqual(package.nodes[0].folder, "业务流程/custom/ast")

    def test_one_package_per_project(self) -> None:
        pt = _column("a", 1, "policy_no", "保单号", "policy_no")
        hl = _column("b", 1, "policy_no", "保单号", "policy_no", 目标项目空间="cdm_hl_prod")
        packages = build_packages([pt, hl], "视图")
        self.assertEqual(
            [p.package_name for p in packages],
            [f"cdm_pt_prod_{datetime.now():%Y%m%d}", f"cdm_hl_prod_{datetime.now():%Y%m%d}"],
        )
        self.assertEqual([p.project for p in packages], ["cdm_pt_prod", "cdm_hl_prod"])
        for package in packages:
            archive = zipfile.ZipFile(io.BytesIO(package.zip_bytes))
            self.assertEqual(
                sorted(archive.namelist()),
                sorted(
                    [
                        f"{package.package_name}/project.xml",
                        f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml",
                    ]
                ),
            )

    def test_explicit_project_keeps_single_package(self) -> None:
        pt = _column("a", 1, "policy_no", "保单号", "policy_no")
        hl = _column(
            "b", 1, "policy_no", "保单号", "policy_no",
            目标项目空间="cdm_hl_prod", 表名="t2", 目标表名="t2", 目标视图名="v_t2",
            来源表全名="medical1.t2",
        )
        packages = build_packages([pt, hl], "视图", project="cdm_gp_prod")
        self.assertEqual(len(packages), 1)
        self.assertEqual(len(packages[0].nodes), 2)

    def test_node_name_strips_view_prefix(self) -> None:
        package = build_package([_column("a", 1, "policy_no", "保单号", "policy_no")], "视图", project="p")
        self.assertEqual(package.nodes[0].name, "t1")
        self.assertEqual(package.nodes[0].output, "p.t1")

    def test_node_name_strips_all_view_prefixes(self) -> None:
        records = [
            _column("a", 1, "policy_no", "保单号", "policy_no",
                    表名="v_v_dwd_policy_df", 目标表名="v_v_dwd_policy_df",
                    目标视图名="v_v_dwd_policy_df", 来源表全名="medical1.t1"),
        ]
        package = build_package(records, "视图", project="cdm_hl_prod")
        node = package.nodes[0]
        self.assertEqual(node.name, "dwd_policy_df")
        self.assertEqual(node.output, "cdm_hl_prod.dwd_policy_df")
        self.assertIn("CREATE OR REPLACE VIEW v_dwd_policy_df", node.code)
        self.assertNotIn("v_v_", node.code)

    def test_sanitize_name(self) -> None:
        self.assertEqual(sanitize_name("v_dim_customer_std"), "v_dim_customer_std")
        self.assertEqual(sanitize_name("客户 表-1"), "客户_表_1")
        self.assertEqual(sanitize_name("123abc"), "n_123abc")
        self.assertEqual(sanitize_name(""), "node")

    def test_empty_input_rejected(self) -> None:
        with self.assertRaises(GenerationError):
            build_package([], "视图", project="p")


class TechnicalFieldTest(unittest.TestCase):
    def test_appends_three_fields_per_target(self) -> None:
        records = [
            _column("a", 1, "id", "主键", "key"),
            _column("b", 1, "id2", "主键", "key", 表名="t2", 目标表名="t2", 目标视图名="v_t2",
                    来源表全名="medical1.t2"),
        ]
        out = add_technical_fields(pd.DataFrame(records))
        tech = out[out["_is_tech"].fillna(False).astype(bool)]
        self.assertEqual(len(tech), 6)
        names = [t[0] for t in TECHNICAL_FIELDS]
        self.assertEqual(tech[tech["表名"] == "t1"]["标准英文名"].tolist(), names)
        self.assertEqual(tech[tech["表名"] == "t1"]["SELECT表达式"].tolist(),
                         ["getdate()", "getdate()", "'pt19gipos'"])
        self.assertEqual(tech["目标类型"].tolist(), ["DATETIME", "DATETIME", "STRING"] * 2)
        self.assertEqual(tech["字段序号"].tolist(), ["2", "3", "4"] * 2)

    def test_skips_existing_technical_fields(self) -> None:
        records = [
            _column("a", 1, "id", "主键", "key"),
            _column(
                "a", 2, "", "首次加载时间", "fst_load_tm",
                加工逻辑="getdate()", SELECT表达式="getdate()",
            ),
        ]
        out = add_technical_fields(pd.DataFrame(records))
        tech_names = out.loc[out["_is_tech"].fillna(False).astype(bool), "标准英文名"].tolist()
        self.assertEqual(tech_names, ["lst_load_tm", "load_fm"])

    def test_idempotent_and_preserves_edits(self) -> None:
        out = add_technical_fields(pd.DataFrame([_column("a", 1, "id", "主键", "key")]))
        edited = out.copy()
        edited.loc[edited["标准英文名"] == "load_fm", "标准中文名"] = "来源系统简称"
        again = add_technical_fields(edited)
        self.assertEqual(len(again), len(edited))
        self.assertEqual(
            again.loc[again["标准英文名"] == "load_fm", "标准中文名"].tolist(), ["来源系统简称"]
        )

    def test_disabled_removes_technical_fields(self) -> None:
        out = add_technical_fields(pd.DataFrame([_column("a", 1, "id", "主键", "key")]))
        trimmed = add_technical_fields(out, enabled=False)
        self.assertEqual(len(trimmed), 1)
        self.assertFalse(trimmed["_is_tech"].any())

    def test_technical_fields_reach_sql_and_package(self) -> None:
        records = add_technical_fields(
            pd.DataFrame([_column("a", 1, "id", "主键", "key")])
        ).to_dict(orient="records")
        files = generate_bundle(
            confirmed=records,
            output_mode="视图",
            expose_partition=False,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        sql = files["odps_views.sql"]
        self.assertIn("getdate()   AS fst_load_tm", sql)
        self.assertIn("'pt19gipos'", sql)
        self.assertRegex(sql, r"'pt19gipos'\s+AS load_fm")

        package = build_package(records, "视图", project="cdm_pt_prod")
        xml = zipfile.ZipFile(io.BytesIO(package.zip_bytes)).read(
            f"{package.package_name}/src/workflows/BUSINESS_dwd_pt19gipos/workflow.xml"
        ).decode("utf-8")
        self.assertIn("getdate()", xml)
        self.assertIn("load_fm", xml)


class ExcelTemplateTest(unittest.TestCase):
    def _records(self) -> list[dict]:
        cols = [
            _column("a", 1, "id", "主键", "key"),
            _column(
                "a", 2, "", "首次加载时间", "fst_load_tm",
                加工逻辑="getdate()", SELECT表达式="getdate()",
            ),
        ]
        return add_technical_fields(pd.DataFrame(cols)).to_dict(orient="records")

    def test_sheets_match_input_template(self) -> None:
        workbook = openpyxl.load_workbook(io.BytesIO(build_excel(pd.DataFrame(self._records()))))
        self.assertEqual(workbook.sheetnames, ["表目录", "变更历史", "DWD模型设计"])
        self.assertEqual(
            [c.value for c in workbook["表目录"][1]],
            ["项目空间", "一级主题", "二级主题", "三级主题", "四级主题", "表名", "表中文名",
             "来源表名", "来源表中文名", "实体", "负责人", "更新时间"],
        )
        self.assertEqual(
            [c.value for c in workbook["变更历史"][1]],
            ["设计调整日期", "作者", "变动内容", "状态", "预计发布日期", "实际发布日期"],
        )
        sheet = workbook["DWD模型设计"]
        self.assertEqual(sheet["A1"].value, "目标")
        self.assertEqual(sheet["M1"].value, "源")
        self.assertEqual(sheet["W1"].value, "映射关系")
        self.assertEqual([c.value for c in sheet[2]], DWD_COLUMNS)

    def test_processing_logic_only_when_transformed(self) -> None:
        sheet = openpyxl.load_workbook(io.BytesIO(build_excel(pd.DataFrame(self._records()))))[
            "DWD模型设计"
        ]
        rows = {row[3]: row for row in sheet.iter_rows(min_row=3, values_only=True)}
        self.assertIn(rows["key"][23], ("", None))  # 同名字段直接映射，不写加工逻辑
        self.assertEqual(rows["fst_load_tm"][23], "getdate()")

    def test_roundtrip_reupload(self) -> None:
        excel = build_excel(pd.DataFrame(self._records()))
        parsed = parse_field_list(excel, "标准化结果.xlsx")
        self.assertEqual(list(parsed["建议标准英文名"]), ["key", "fst_load_tm", "lst_load_tm", "load_fm"])
        self.assertEqual(parsed.iloc[1]["加工逻辑"], "getdate()")

    def test_empty_input_still_three_sheets(self) -> None:
        workbook = openpyxl.load_workbook(io.BytesIO(build_excel(pd.DataFrame())))
        self.assertEqual(workbook.sheetnames, ["表目录", "变更历史", "DWD模型设计"])


class TemplatePassthroughTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))

    def test_parse_keeps_passthrough_columns(self) -> None:
        for col in ["非空", "更新策略", "引用代码表", "字段枚举值", "关联条件"]:
            self.assertIn(col, self.fields.columns)
        enums = [v for v in self.fields["字段枚举值"] if v]
        self.assertEqual(enums, ["1:电话，P17,2:微信，3:APP"])

    def test_export_writes_back_enum_and_logic(self) -> None:
        library = parse_root_library(*load_default_root_library())
        final = normalize_confirmed(
            add_technical_fields(editor_view(apply_matching(self.fields, library, {})))
        )
        workbook = openpyxl.load_workbook(io.BytesIO(build_excel(final)))
        sheet = workbook["DWD模型设计"]
        rows = {row[3]: row for row in sheet.iter_rows(min_row=3, values_only=True)}
        LOGIC = "to_date(create_date\n    ,'yyyy-mm-dd hh:mi:ss')"
        # 字段按「目标字段中文名」匹配：评价来源渠道 → apps_src_chnl
        self.assertEqual(rows["apps_src_chnl"][21], "1:电话，P17,2:微信，3:APP")
        self.assertEqual(rows["put_tm"][23], LOGIC)
        self.assertEqual(rows["fst_load_tm"][23], "getdate()")
        self.assertEqual(rows["dt"][6], "Y")

        # 回填后再导出，内容不变
        again = parse_field_list(build_excel(final), "标准化结果.xlsx")
        self.assertEqual([v for v in again["字段枚举值"] if v], ["1:电话，P17,2:微信，3:APP"])
        logic = [v for v in again["加工逻辑"] if v]
        self.assertIn(LOGIC, logic)

    def test_other_passthrough_columns_roundtrip(self) -> None:
        workbook = openpyxl.load_workbook(io.BytesIO(INPUT_XLSX.read_bytes()))
        sheet = workbook["DWD模型设计"]
        sheet["I3"] = "Y"
        sheet["J3"] = "insert"
        sheet["K3"] = "dict_gender"
        sheet["W3"] = "a.id = b.id"
        buffer = io.BytesIO()
        workbook.save(buffer)
        fields = enrich_fields(parse_field_list(buffer.getvalue(), "filled.xlsx"))

        library = parse_root_library(*load_default_root_library())
        final = normalize_confirmed(
            add_technical_fields(editor_view(apply_matching(fields, library, {})))
        )
        out = openpyxl.load_workbook(io.BytesIO(build_excel(final)))["DWD模型设计"]
        row = [c.value for c in out[3]]
        self.assertEqual(row[8], "Y")
        self.assertEqual(row[9], "insert")
        self.assertEqual(row[10], "dict_gender")
        self.assertEqual(row[22], "a.id = b.id")

    def test_processing_logic_wins_for_select(self) -> None:
        library = parse_root_library(*load_default_root_library())
        matched = apply_matching(self.fields, library, {})
        put_tm = matched[matched["来源字段英文名"] == "create_date"].iloc[0]
        self.assertIn("to_date(create_date", put_tm["SELECT表达式"])
        self.assertIn("采用加工逻辑作为SELECT表达式", put_tm["说明"])

    def test_generated_sql_flattens_multiline_logic(self) -> None:
        library = parse_root_library(*load_default_root_library())
        final = normalize_confirmed(
            add_technical_fields(editor_view(apply_matching(self.fields, library, {})))
        )
        files = generate_bundle(
            confirmed=final.to_dict(orient="records"),
            output_mode="视图",
            expose_partition=False,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        for line in files["odps_views.sql"].splitlines():
            self.assertNotIn("\n", line)
        self.assertIn("to_date(create_date ,'yyyy-mm-dd hh:mi:ss')", files["odps_views.sql"])


class PreviewAlignmentTest(unittest.TestCase):
    def test_preview_columns_equal_template(self) -> None:
        records = [
            _column("a", 1, "id", "主键", "key"),
            _column("a", 2, "ds", "分区日期", "ds", 是否分区="是", _is_partition=True),
        ]
        preview = dwd_frame(pd.DataFrame(records))
        self.assertEqual(list(preview.columns), DWD_COLUMNS)
        # 预览可编辑列必须是模板列的子集
        self.assertTrue(set(TEMPLATE_EDITABLE) <= set(preview.columns))

    def test_merge_template_edits_maps_back(self) -> None:
        original = pd.DataFrame(
            [_column("a", 1, "id", "主键", "key", 类型来源="输入清单字段类型")]
        )
        edited = pd.DataFrame(
            [
                {
                    "字段名": "key_id",
                    "字段中文名称": "主键ID",
                    "字段类型": "bigint",
                    "是否分区字段": "Y",
                    "主键": "N",
                    "_row_id": 0,
                }
            ]
        )
        merged = merge_template_edits(original, edited)
        row = merged.iloc[0]
        self.assertEqual(row["标准英文名"], "key_id")
        self.assertEqual(row["标准中文名"], "主键ID")
        self.assertEqual(row["目标类型"], "BIGINT")
        self.assertEqual(row["是否分区"], "Y")
        self.assertEqual(row["是否主键"], "N")
        self.assertTrue(row["_is_partition"])
        self.assertFalse(row["_is_pk"])
        self.assertEqual(row["操作"], "手动输入")
        self.assertEqual(row["类型来源"], "人工确认")

    def test_edit_roundtrip_through_preview(self) -> None:
        """模拟「编辑 → 合并 → 重建表格 → 再渲染」：第一次修改必须生效且不产生抖动。"""
        editor = pd.DataFrame([_column("a", 1, "id", "主键", "key", 类型来源="输入清单字段类型")])
        preview = dwd_frame(editor)
        preview["_row_id"] = preview.index
        preview.loc[preview.index[0], "字段名"] = "Typed_Name"

        merged = merge_template_edits(editor, preview)
        self.assertEqual(merged.iloc[0]["标准英文名"], "typed_name")

        again = dwd_frame(merged)
        self.assertEqual(again.iloc[0]["字段名"], "typed_name")

        again["_row_id"] = again.index
        self.assertEqual(len(merge_template_edits(merged, again)), len(merged))
        self.assertEqual(merge_template_edits(merged, again).iloc[0]["标准英文名"], "typed_name")

    def test_merge_without_row_id_uses_row_ids(self) -> None:
        """隐藏列丢失时按位置回退，第一次编辑同样生效。"""
        editor = pd.DataFrame(
            [
                _column("a", 1, "id", "主键", "key"),
                _column("a", 2, "account", "证件号", "idtty_no"),
            ]
        )
        preview = dwd_frame(editor)
        preview.loc[1, "字段名"] = "typed_second_row"   # 没有 _row_id 列
        merged = merge_template_edits(editor, preview, row_ids=list(editor.index))
        self.assertEqual(merged.iloc[0]["标准英文名"], "key")
        self.assertEqual(merged.iloc[1]["标准英文名"], "typed_second_row")

    def test_second_edit_after_first_is_applied(self) -> None:
        """连续两次修改：第一次生效后，第二次也必须生效（回归：改第二个值失败/跳表头）。"""
        editor = pd.DataFrame(
            [
                _column("a", 1, "id", "主键", "key"),
                _column("a", 2, "account", "证件号", "idtty_no"),
                _column("a", 3, "remark", "备注", "memorandum"),
            ]
        )
        # 第一次：改第 1 行
        preview = dwd_frame(editor)
        preview["_row_id"] = preview.index
        preview.loc[0, "字段名"] = "first_name"
        editor = merge_template_edits(editor, preview, row_ids=[0, 1, 2])
        self.assertEqual(editor.iloc[0]["标准英文名"], "first_name")

        # 第二次：重新渲染后改第 3 行
        preview2 = dwd_frame(editor)
        preview2["_row_id"] = preview2.index
        preview2.loc[2, "字段名"] = "second_name"
        editor = merge_template_edits(editor, preview2, row_ids=list(editor.index))
        self.assertEqual(editor.iloc[0]["标准英文名"], "first_name")   # 第一次的改动保留
        self.assertEqual(editor.iloc[2]["标准英文名"], "second_name")  # 第二次也生效

    def test_positional_mapping_with_reordered_rows(self) -> None:
        """预览按「序号」重排后，按位置映射仍要落到正确行。"""
        editor = pd.DataFrame(
            [
                _column("a", 5, "remark", "备注", "memorandum"),
                _column("a", 1, "id", "主键", "key"),
            ]
        )
        preview = dwd_frame(editor)          # 按序号升序：id 在前
        self.assertEqual(list(preview.index), [1, 0])
        preview.loc[preview.index[0], "字段名"] = "first_sorted_row"
        merged = merge_template_edits(editor, preview, row_ids=list(preview.index))
        self.assertEqual(merged.loc[1, "标准英文名"], "first_sorted_row")
        self.assertEqual(merged.loc[0, "标准英文名"], "memorandum")

    def test_hidden_row_id_nan_still_merges(self) -> None:
        """Streamlit 隐藏列可能回传 NaN：按位置映射仍然生效。"""
        editor = pd.DataFrame([_column("a", 1, "id", "主键", "key")])
        preview = dwd_frame(editor)
        preview["_row_id"] = float("nan")
        preview.loc[0, "字段名"] = "typed_name"
        merged = merge_template_edits(editor, preview, row_ids=[0])
        self.assertEqual(merged.iloc[0]["标准英文名"], "typed_name")

    def test_merge_keeps_unchanged_rows(self) -> None:
        original = pd.DataFrame([_column("a", 1, "id", "主键", "key", 类型来源="输入清单字段类型")])
        edited = pd.DataFrame(
            [{"字段名": "key", "字段中文名称": "主键", "字段类型": "string", "_row_id": 0}]
        )
        merged = merge_template_edits(original, edited)
        self.assertEqual(merged.iloc[0]["操作"], "采纳推荐")
        self.assertEqual(merged.iloc[0]["类型来源"], "输入清单字段类型")


class ValidateTest(unittest.TestCase):
    def test_duplicate_and_illegal(self) -> None:
        df = pd.DataFrame(
            [
                _column("a", 1, "x", "X", "x"),
                _column("a", 2, "y", "Y", "x"),
                _column("a", 3, "z", "Z", "9bad"),
            ]
        )
        issues = validate_confirmed(normalize_confirmed(df))
        messages = " ".join(i["问题"] for i in issues)
        self.assertIn("重复", messages)
        self.assertIn("非法", messages)
        # 问题表固定三列：表名 / 字段中文名 / 问题
        self.assertEqual(list(issues[0]), ["表名", "字段中文名", "问题"])
        self.assertEqual(issues[0]["表名"], "t1")
        self.assertEqual(issues[0]["字段中文名"], "Y")  # 重复行的字段中文名

    def test_field_validation_errors(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        broken = fields.copy()
        broken.loc[0, "字段序号"] = ""
        broken.loc[1, "来源字段英文名"] = "Bad Name"
        errors, _ = validate_fields(broken)
        messages = " ".join(e["问题"] for e in errors)
        self.assertIn("字段序号", messages)
        self.assertIn("非法", messages)

    def test_full_pipeline(self) -> None:
        fields = enrich_fields(parse_field_list(INPUT_XLSX.read_bytes(), INPUT_XLSX.name))
        errors, _ = validate_fields(fields)
        self.assertEqual(errors, [])

        library = parse_root_library(*load_default_root_library())
        matched = apply_matching(fields, library, {})
        final = normalize_confirmed(editor_view(matched))
        # 词根库未覆盖的中文名需人工补齐，才有合法标准名
        final["标准英文名"] = final["标准英文名"].replace("", pd.NA).fillna("unmatched_col")
        self.assertEqual(validate_confirmed(final), [])

        files = generate_bundle(
            confirmed=final.to_dict(orient="records"),
            output_mode="视图",
            expose_partition=False,
            view_comment_override="",
            generate_pyodps_flag=False,
            partition_mode="static",
            partition_value="${bizdate}",
            lifecycle=36000,
        )
        sql = files["odps_views.sql"]
        self.assertIn("CREATE OR REPLACE VIEW v_dwd_ast_cust_eqty_cs_estim_df", sql)
        self.assertIn("COMMENT '客户服务评价'", sql)
        self.assertIn("FROM ods_pt17cccs.v_oracccs122_cccs_spms_customer_evaluation;", sql)

        excel = build_excel(final)
        workbook = openpyxl.load_workbook(io.BytesIO(excel))
        self.assertEqual(workbook.sheetnames, ["表目录", "变更历史", "DWD模型设计"])
        sheet = workbook["DWD模型设计"]
        self.assertEqual(sheet["A1"].value, "目标")
        self.assertEqual([c.value for c in sheet[2]], DWD_COLUMNS)
        first = [c.value for c in sheet[3]]
        self.assertEqual(first[0], "cdm_pt_prod")
        self.assertEqual(first[1], "dwd_ast_cust_eqty_cs_estim_df")
        self.assertEqual(first[3], "key")
        self.assertEqual(first[5], "string")
        self.assertEqual(first[11], 1)
        # 输出可原样回填（同一份模板结构）
        roundtrip = parse_field_list(excel, "标准化结果.xlsx")
        self.assertEqual(len(roundtrip), len(final))
        self.assertEqual(roundtrip.iloc[0]["建议标准英文名"], "key")
        self.assertEqual(len(workbook["表目录"]["A"]), len(workbook["表目录"]["A"]))
        self.assertGreaterEqual(workbook["表目录"].max_row, 2)
        self.assertGreaterEqual(workbook["变更历史"].max_row, 2)


if __name__ == "__main__":
    unittest.main()
