"""生成 DataWorks 迁移助手可直接导入的迁移包（V1 XML 包格式）。

包结构与 DataWorks 数据开发导出包一致：

    <项目名>_<yyyymmdd>/
    ├── project.xml
    └── src/workflows/BUSINESS_dwd_<系统简称>/workflow.xml

每个目标对象（视图 / 物理表）对应 workflow.xml 中的一个 ODPS_SQL 节点：
视图模式节点内容是 CREATE OR REPLACE VIEW 语句，
物理表模式节点内容是 INSERT OVERWRITE 语句。

导入方式：DataWorks 控制台 → 数据开发 → 迁移助手 → DataWorks迁移 → 导入，上传该 ZIP。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

from .generate import GeneratedTarget, GenerationError, build_targets
from .util import cell_str, strip_view_prefix

DEFAULT_OWNER = "5285418240614129999"
DEFAULT_CRON = "00 24 00 * * ?"
DEFAULT_START_EFFECT = "-28800000"
DEFAULT_END_EFFECT = "253370772751000"
DEFAULT_RESOURCE_GROUP = "sys_default"
DEFAULT_RESOURCE_GROUP_NAME = "Default Group"
DEFAULT_CONNECTION = "odps_first"
DEFAULT_RERUN_TIME = "0"
DEFAULT_RERUN_INTERVAL = "120000"
WORKFLOW_PREFIX = "dwd_"

_NAME_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")
_MAX_NAME = 128


@dataclass
class DataWorksNode:
    """workflow.xml 中的一个节点。"""

    name: str
    workflow: str
    code: str
    output: str
    folder: str
    node_type: str = "ODPS_SQL"

    @property
    def spec_path(self) -> str:
        return f"src/workflows/BUSINESS_{self.workflow}/workflow.xml"


@dataclass
class DataWorksPackage:
    zip_bytes: bytes
    package_name: str
    project: str
    nodes: list[DataWorksNode] = field(default_factory=list)

    @property
    def workflows(self) -> list[str]:
        seen: list[str] = []
        for node in self.nodes:
            if node.workflow not in seen:
                seen.append(node.workflow)
        return seen

    def file_map(self, **options) -> dict[str, str]:
        """包内 路径 -> 文本内容（含 project.xml 与各 workflow.xml）。"""
        files = {f"{self.package_name}/project.xml": project_xml(self.project, **options)}
        for workflow in self.workflows:
            nodes = [n for n in self.nodes if n.workflow == workflow]
            files[f"{self.package_name}/src/workflows/BUSINESS_{workflow}/workflow.xml"] = workflow_xml(
                workflow, nodes, **options
            )
        return files


def sanitize_name(value: str, fallback: str = "node") -> str:
    """把表名 / 视图名转换成 DataWorks 可接受的节点名。"""
    text = _NAME_SAFE.sub("_", cell_str(value)).strip("_")
    text = re.sub(r"_{2,}", "_", text)
    if not text:
        return fallback
    if text[0].isdigit():
        text = f"n_{text}"
    return text[:_MAX_NAME]


def _unique(name: str, used: set[str]) -> str:
    candidate = name
    index = 2
    while candidate.lower() in used:
        candidate = f"{name}_{index}"
        index += 1
    used.add(candidate.lower())
    return candidate


def node_name_for(target: GeneratedTarget, view_prefix: str = "v_") -> str:
    """节点名 = 目标对象名，且去掉全部视图名前缀（v_v_dwd_x_df → dwd_x_df）。"""
    name = strip_view_prefix(target.target_name, view_prefix)
    if not name and target.columns:
        name = strip_view_prefix(target.columns[0].get("表名"), view_prefix)
    return name


def _default_folder(workflow: str, table_name: str) -> str:
    """业务流程/<工作流>/folderMaxCompute/<主题>，主题取 dwd_<主题>_xxx 中的主题段。"""
    match = re.match(r"^(?:dwd|dim|dws|ads)_([a-z0-9]+)_", cell_str(table_name).lower())
    theme = match.group(1) if match else "default"
    return f"业务流程/{workflow}/folderMaxCompute/{theme}"


def workflow_name_for(system: str) -> str:
    """来源系统 → 工作流名：ods_pt17cccs → dwd_pt17cccs。"""
    from .parse import system_short_name

    short = system_short_name(system)
    return f"{WORKFLOW_PREFIX}{short}" if short else f"{WORKFLOW_PREFIX}default"


def build_nodes(
    confirmed: list[dict],
    output_mode: str,
    project: str,
    view_name_prefix: str = "v_",
    expose_partition: bool = True,
    view_comment_override: str = "",
    partition_mode: str = "static",
    partition_value: str = "${bizdate}",
    lifecycle: int = 36000,
    workflow_mode: str = "per_system",
    workflow_name: str = "",
    folder_override: str = "",
) -> list[DataWorksNode]:
    """由确认后的映射生成节点列表。

    workflow_mode:
      per_system —— 每个来源系统一个工作流（默认，dwd_<系统简称>）
      single     —— 全部节点放入指定工作流
    """
    targets: list[GeneratedTarget] = build_targets(
        confirmed=confirmed,
        output_mode=output_mode,
        expose_partition=expose_partition,
        view_comment_override=view_comment_override,
        partition_mode=partition_mode,
        partition_value=partition_value,
        lifecycle=lifecycle,
        view_name_prefix=view_name_prefix,
    )

    used_by_workflow: dict[str, set[str]] = {}
    nodes: list[DataWorksNode] = []
    single = sanitize_name(workflow_name or f"{WORKFLOW_PREFIX}all", "dwd_all")
    for target in targets:
        system = cell_str(target.columns[0].get("来源系统缩写")) if target.columns else ""
        workflow = single if workflow_mode == "single" else workflow_name_for(system)
        used = used_by_workflow.setdefault(workflow, set())
        node_name = _unique(sanitize_name(node_name_for(target, view_name_prefix), "node"), used)
        node_id = f"{project}.{node_name}"
        target_table = cell_str(target.columns[0].get("表名")) if target.columns else target.target_name
        nodes.append(
            DataWorksNode(
                name=node_name,
                workflow=workflow,
                code=target.script,
                output=node_id,
                folder=cell_str(folder_override) or _default_folder(workflow, target_table),
            )
        )
    if not nodes:
        raise GenerationError("没有生成任何 DataWorks 节点")
    return nodes


def project_xml(project: str, owner: str = DEFAULT_OWNER, **_: object) -> str:
    return f'<Project dmObjectType="PROJECT"  name={quoteattr(project)} owner={quoteattr(cell_str(owner) or DEFAULT_OWNER)} />\n'


def _node_xml(node: DataWorksNode, **options: object) -> str:
    cron = cell_str(options.get("cron")) or DEFAULT_CRON
    start_effect = cell_str(options.get("start_effect_date")) or DEFAULT_START_EFFECT
    end_effect = cell_str(options.get("end_effect_date")) or DEFAULT_END_EFFECT
    resource_group = cell_str(options.get("resource_group")) or DEFAULT_RESOURCE_GROUP
    resource_group_name = cell_str(options.get("resource_group_name")) or DEFAULT_RESOURCE_GROUP_NAME
    connection = cell_str(options.get("connection")) or DEFAULT_CONNECTION
    rerun_mode = cell_str(options.get("rerun_mode")) or "ALL_ALLOWED"
    rerun_time = cell_str(options.get("task_rerun_time")) or DEFAULT_RERUN_TIME
    rerun_interval = cell_str(options.get("task_rerun_interval")) or DEFAULT_RERUN_INTERVAL

    attrs = {
        "dmObjectType": "NODE",
        "name": node.name,
        "type": node.node_type,
        "cronExpress": cron,
        "startEffectDate": start_effect,
        "isAutoParse": "1",
        "endEffectDate": end_effect,
        "resourceGroup": resource_group,
        "resourceGroupName": resource_group_name,
        "startRightNow": "false",
        "rerunMode": rerun_mode,
        "pauseSchedule": "false",
        "useType": "SCHEDULED",
        "folder": node.folder,
        "isRoot": "false",
        "connection": connection,
        "taskRerunTime": rerun_time,
        "taskRerunInterval": rerun_interval,
        "dependentType": "0",
    }
    attr_text = " ".join(f"{key}={quoteattr(str(value))}" for key, value in attrs.items())
    code = node.code.replace("]]>", "]]]]><![CDATA[>")
    return (
        f"            <Node {attr_text}>\n"
        "              <root>false</root>\n"
        f"              <Code><![CDATA[{code}]]></Code>\n"
        "              <Outputs>\n"
        f'                <Output dmObjectType="NODE_IO"  data={quoteattr(node.output)} parseType="1" />\n'
        "              </Outputs>\n"
        "            </Node>\n"
    )


def workflow_xml(workflow: str, nodes: list[DataWorksNode], **options: object) -> str:
    body = "".join(_node_xml(node, **options) for node in nodes)
    return (
        f'<Workflow dmObjectType="WORKFLOW"  name={quoteattr(workflow)} version="V3" scheduled="true" >\n'
        "    <Nodes>\n"
        f"{body}"
        "        \n"
        "    </Nodes>\n"
        "</Workflow>\n"
    )


def package_name_for(project: str, date: datetime | None = None) -> str:
    stamp = (date or datetime.now()).strftime("%Y%m%d")
    return f"{sanitize_name(project, 'dataworks')}_{stamp}"


def build_package(
    confirmed: list[dict],
    output_mode: str,
    project: str,
    package_date: datetime | None = None,
    **options: object,
) -> DataWorksPackage:
    """生成 DataWorks 迁移包（ZIP 字节 + 节点清单）。"""
    node_option_keys = {
        "view_name_prefix",
        "expose_partition",
        "view_comment_override",
        "partition_mode",
        "partition_value",
        "lifecycle",
        "workflow_mode",
        "workflow_name",
        "folder_override",
    }
    node_options = {k: v for k, v in options.items() if k in node_option_keys}
    xml_options = {k: v for k, v in options.items() if k not in node_option_keys}

    project_name = sanitize_name(project, "dataworks")
    nodes = build_nodes(
        confirmed=confirmed,
        output_mode=output_mode,
        project=project_name,
        **node_options,
    )
    name = package_name_for(project_name, package_date)

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/project.xml", project_xml(project_name, **xml_options))
        for workflow in dict.fromkeys(n.workflow for n in nodes):
            group = [n for n in nodes if n.workflow == workflow]
            zf.writestr(
                f"{name}/src/workflows/BUSINESS_{workflow}/workflow.xml",
                workflow_xml(workflow, group, **xml_options),
            )
    return DataWorksPackage(zip_bytes=buffer.getvalue(), package_name=name, project=project_name, nodes=nodes)


def project_of(row: dict, default: str = "cdm_pt_prod") -> str:
    """记录所属目标项目空间（输入清单「目标数据库名」）。"""
    return cell_str(row.get("目标项目空间")) or default


def build_packages(
    confirmed: list[dict],
    output_mode: str,
    project: str = "",
    package_date: datetime | None = None,
    **options: object,
) -> list[DataWorksPackage]:
    """按目标项目空间分组生成迁移包：每个项目空间一个 ZIP。

    - 显式指定 project 时，全部节点放进该项目空间的一个包；
    - 未指定时按记录的「目标项目空间」拆分，如 cdm_pt_prod / cdm_hl_prod 各一个包。
    """
    if not confirmed:
        raise GenerationError("没有可生成的字段")
    override = cell_str(project)
    if override:
        return [build_package(confirmed, output_mode, override, package_date, **options)]

    groups: dict[str, list[dict]] = {}
    for row in confirmed:
        groups.setdefault(project_of(row), []).append(row)
    if not groups:
        raise GenerationError("没有可生成的字段")
    return [
        build_package(rows, output_mode, name, package_date, **options)
        for name, rows in groups.items()
    ]


def _as_packages(packages) -> list[DataWorksPackage]:
    if isinstance(packages, DataWorksPackage):
        return [packages]
    return list(packages)


def import_guide(packages, output_mode: str) -> str:
    """导入说明（放在汇总 ZIP 里，不放进迁移包内部），支持多项目空间多个包。"""
    items = _as_packages(packages)
    content_label = (
        "CREATE OR REPLACE VIEW 视图创建语句" if output_mode == "视图" else "INSERT OVERWRITE 数据装载语句"
    )
    blocks: list[str] = []
    for package in items:
        workflow_lines = "\n".join(
            f"- BUSINESS_{wf}/workflow.xml（{sum(1 for n in package.nodes if n.workflow == wf)} 个节点）"
            for wf in package.workflows
        )
        blocks.append(
            f"""迁移包：{package.package_name}.zip
目标项目空间：{package.project}
节点数：{len(package.nodes)}
节点名：目标视图名 / 表名去掉视图名前缀（v_dwd_x_df → dwd_x_df、v_v_dwd_x_df → dwd_x_df）

包结构
{package.package_name}/
├── project.xml
└── src/workflows/
{workflow_lines}"""
        )
    summary = "\n\n".join(blocks)
    total = sum(len(p.nodes) for p in items)
    return f"""DataWorks 迁移助手导入说明
==============================

迁移包数量：{len(items)}
节点总数：{total}
节点类型：ODPS_SQL（MaxCompute SQL）
节点内容：{content_label}
项目空间：{", ".join(p.project for p in items)}

一、导入步骤
1. 登录 DataWorks 控制台，进入对应项目空间（如需新建，请先创建项目）；
2. 打开「数据开发」→「迁移助手」→「DataWorks迁移」→「导入」；
3. 依次上传本目录下的每个 ZIP 包（每个目标项目空间一个包，需在其对应项目空间中导入）；
4. 在导入预览中确认工作流与节点映射关系后执行导入；
5. 导入完成后在「业务流程」下按需提交、发布节点。

二、包明细
{summary}

三、注意事项
1. 视图模式节点内容为建视图语句；物理表模式节点内容为 INSERT OVERWRITE 语句；
2. 物理表模式请先执行产物中的 odps_ddl.sql 建表，再运行节点；
3. 节点名已去掉全部视图名前缀 v_（视图本身仍保留单层 v_），同一工作流内重名会自动追加序号；
4. Outputs 为「项目名.节点名」，与 project.xml 中的 name 保持一致；
5. 多项目空间的节点已按项目空间拆成多个包，请勿把 A 项目的包导入 B 项目；
6. 需要调整调度周期、资源组、责任人时，可在页面「DataWorks 迁移包」配置中修改后重新生成。
"""
