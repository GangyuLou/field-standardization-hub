# skill: field-standardization-agent

## 🎯 能力

上传「字段清单」（Excel/CSV）→ 按词根库标准化字段英文名 → 人工确认 → 一次导出：

1. `标准化结果.xlsx`（表目录 / 变更历史 / DWD模型设计，与输入模板结构一致，可回填）
2. `odps_views.sql`｜`odps_ddl.sql` + `odps_insert.sql` + `pyodps_create_table.py`
3. `dataworks迁移包/<项目>_<yyyymmdd>.zip`（迁移助手导入包，每个目标项目空间一个）

不连源库、不连 DataWorks；词根库内置；Docker 部署，浏览器访问。

## 📥 输入

| 文件 | 必填 | 说明 |
|---|---|---|
| 字段清单 | 是 | 工作表 `DWD模型设计`：第 1 行分组表头（A=`目标`、M=`源`、W=`映射关系`），第 2 行 24 列名，第 3 行起数据；CSV 需含同名列 |
| 词根库 | 否 | 默认 `stdfield/assets/root_library.xlsx`（列：中文别名/英文全称/英文缩写/日期/修改人），上传则覆盖 |

**标准化只填两列**：`字段名`（标准化英文名）、`字段类型`（MaxCompute 类型，如 `string`/`decimal(18,2)`）。

模板 24 列：`目标数据库名 表名 表名中文名称 字段名 字段中文名称 字段类型 是否分区字段 主键 非空 更新策略(delete\update\insert) 引用代码表 序号 ｜ 来源系统名称 来源系统中文名 来源库名 来源Schema 来源表名 来源表中文名称 来源字段名 来源字段中文名称 来源字段类型 ｜ 字段枚举值 关联条件 加工逻辑`

- 合并单元格空值自动 `ffill`；`序号` 决定列顺序；`是否分区字段` = Y/是 即分区字段；
- `字段名` 已填 → 直接采用（状态「已匹配」）；`字段类型` 已填 → 规范化大写，空则按 `来源字段类型` 映射；
- `表名` 可能自带 `v_` 前缀（历史产物回填）→ 视为「已带视图前缀」，派生视图名时**不再叠加** `v_`；
- 除可编辑列外，其余列（非空/更新策略/引用代码表/字段枚举值/关联条件/加工逻辑）**原样回写，不得丢弃**。

**默认技术字段**（每目标对象末尾自动补，已有同名字段则跳过，开关可关）：

| 字段 | 中文 | 类型 | SELECT |
|---|---|---|---|
| `fst_load_tm` | 首次加载时间 | DATETIME | `getdate()` |
| `lst_load_tm` | 最后一次更新时间 | DATETIME | `getdate()` |
| `load_fm` | 来源系统 | STRING | `'系统简称'`（`ods_pt17cccs`→`'pt17cccs'`） |

## 🔄 处理流程

1. **解析词根库**：中文别名按 `,，、/` 拆分；建 `中文别名/英文全称/英文缩写` 三索引；检测多义缩写；`@st.cache_data` 按 `file_bytes` 缓存。
2. **解析字段清单**：优先含 `DWD`/`模型设计`/`字段` 的工作表；前 6 行内定位表头（含 `来源字段名`+`序号` 或 `字段名`+`表名`）；`enrich_fields()` 派生 `来源系统简称/表中文描述/目标视图名/匹配中文名/_is_partition/_is_pk`；`目标视图名` = `v_` + **已剥掉 `v_` 的**表名（前缀幂等）。
3. **结构校验（阻断项）**：系统名/表名/序号为空；来源库名空且来源表名不带库名；来源字段名空且加工逻辑空；来源字段名非法或同表重复；序号非整数或同表重复。**警告**：目标中文名为空、类型缺失（默认 STRING）、来源表名为空。
4. **字段匹配**（依据 **`字段中文名称`（目标）**，空则回退来源中文名）：
   - 清洗：剔除括号注释（`（）`/`()`，含嵌套、未闭合）与字母数字标点 → `主键id`→`主键`、`客户证件号码（主键）`→`客户证件号码`；注释列仍保留原文；
   - 中文别名最长匹配、完整词条优先；目标名未命中 → 回退来源中文名（说明标注）；
   - 无中文名 → 英文按 `_` 拆段查索引，标记「低置信」；
   - 多义缩写 → 「多候选」必须人工确认；未命中 → 「未匹配」，不硬拼；同表重名 → 「冲突」；历史命中 → 复用；
   - `SELECT` 表达式优先级：`加工逻辑` ＞ `来源字段名` ＞ `NULL`；多行表达式折叠为单行。
5. **类型**：`normalize_target_type()`（`string`/`bigint`/`decimal(p,s)`/`datetime`/`date`/`double`…）；空或未知 → `STRING` + 待确认。
6. **补技术字段**：按目标对象（`目标视图名+来源表全名+表名+表中文描述`）分组，序号接最大值；幂等、保留人工修改。
7. **人工确认**：`st.data_editor` 直接展示 `DWD模型设计` 24 列（预览即产物）；可编辑 = `字段名/字段中文名称/字段类型/是否分区字段/主键`，其余只读。批量：采纳推荐、标记无需变更（仅处理有来源字段名的行）、按前缀重算视图名（**先剥 `v_` 再补，幂等**）、下载草稿 CSV。
8. **生成导出**：同一份确认映射驱动全部产物，`validate_confirmed()` 不过则阻断；问题表固定三列 `表名 / 字段中文名 / 问题`。

## 📤 输出

汇总 ZIP：`标准化结果.xlsx` + SQL/PyODPS + `dataworks迁移包/`（多包 + `导入说明.txt`）+ `README.txt`。

**Excel 3 sheet**：`表目录`（项目空间/主题层级/表名/表中文名/来源表名/来源表中文名/实体/负责人/更新时间）、`变更历史`（日期/作者/变动内容/状态/计划与实际上线）、`DWD模型设计`（24 列，`字段名`=标准英文名、`字段类型`=小写标准类型、`加工逻辑`=原值或技术字段表达式、`字段名` 同来源名时 `加工逻辑` 留空）。

**视图**：`CREATE OR REPLACE VIEW ${cdm_pt_prod}.v_x (col COMMENT '...', ..., ds COMMENT '分区') ... AS SELECT src AS col, ..., ds FROM ${ods_pt17cccs}.源表;`
**视图名只允许一层 `v_`**：`v_x` / `v_dwd_x_df`；表名已带 `v_` 时保持 `v_dwd_x_df`，**禁止 `v_v_dwd_x_df`**。
**物理表**：`CREATE TABLE IF NOT EXISTS ${cdm_pt_prod}.x ... PARTITIONED BY (dt STRING) LIFECYCLE n`；`INSERT OVERWRITE TABLE ${cdm_pt_prod}.x PARTITION (dt='${bizdate}') SELECT ... FROM ${ods_pt17cccs}.源表;`（静态分区列不进 SELECT；动态分区列排 SELECT 末尾）。
**项目空间一律写成调度参数**：目标对象前缀 `${目标项目空间}`、来源表前缀 `${来源项目空间}`（见 `raw/package` 参考包），参数名 = 取值 = 项目空间名；脚本、节点内容、PyODPS 三处一致，禁止写死项目空间名。

## 📦 DataWorks 迁移包（V1 XML，对齐 `raw/package/cdm_gp_prod_20261116`）

```
<项目>_<yyyymmdd>/
├── project.xml            <Project dmObjectType="PROJECT" name="cdm_pt_prod" owner="5285418240614129999" />
└── src/workflows/BUSINESS_dwd_<系统简称>/workflow.xml
```

- **按目标项目空间分包**：清单里有几个 `目标数据库名` 就出几个包（`cdm_pt_prod_*.zip`、`cdm_hl_prod_*.zip`）；侧边栏显式填项目空间则合并为一包；
- 工作流名 = `dwd_` + 系统简称（`ods_pt17cccs`→`dwd_pt17cccs`），可切换为单工作流；
- 节点名 = 目标对象名**去掉全部 `v_` 前缀**（`v_dwd_x_df`→`dwd_x_df`、`v_v_dwd_x_df`→`dwd_x_df`），同工作流内重名加 `_2`；节点名与 `CREATE` 视图名不同名属正常（视图名保留单层 `v_`）；
- 节点：`<Node dmObjectType="NODE" name=... type="ODPS_SQL" cronExpress="00 24 00 * * ?" startEffectDate="-28800000" isAutoParse="1" endEffectDate="253370772751000" resourceGroup="sys_default" resourceGroupName="Default Group" startRightNow="false" rerunMode="ALL_ALLOWED" pauseSchedule="false" useType="SCHEDULED" folder="业务流程/<wf>/folderMaxCompute/<主题>" isRoot="false" connection="odps_first" taskRerunTime="0" taskRerunInterval="120000" dependentType="0">` + `<root>false</root>` + `<Code><![CDATA[…]]></Code>` + `<Outputs><Output dmObjectType="NODE_IO" data="<项目>.<节点>" parseType="1"/></Outputs>`；
- **节点内容**：视图 → 建视图语句；物理表 → `INSERT OVERWRITE` 语句（建表 DDL 不进节点）；SQL 用 `${目标项目空间}` / `${来源项目空间}` 引用项目空间；
- `导入说明.txt` 列出包内用到的项目空间参数（`参数名 = 取值`），提示在节点「调度配置 → 调度参数」中补齐；
- ZIP 路径用 `/`；属性值 `quoteattr`；`]]>` 拆分转义。

## 🖥️ 页面

主区：① 上传（字段清单必填、词根库可选）② 词根库指标/多义冲突/校验 ③ 人工确认（24 列预览）④ 生成导出（ZIP、单包下载、SQL/包预览）。

侧边栏：输出模式（视图/物理表）、视图名前缀 `v_`（幂等：已带前缀不叠加）、视图注释、**视图中暴露分区字段（默认勾选）**、按表拆分脚本、PyODPS、**自动补充 3 个技术字段（默认勾选）**、分区模式/静态分区值/LIFECYCLE、迁移包（目标项目空间、工作流组织方式、统一工作流名、节点目录、owner、Cron、资源组、重跑）、词根库版本、下载模板。

## ⚠️ 关键约束

1. 不连源库/DataWorks，全部文件驱动；词根库内置，未上传不阻断。
2. 无表级探源清单、无三元组关联。
3. 只填两列；其余列缺失自动派生，**模板 24 列零丢失**。
4. 多义必确认；未匹配不硬拼；类型缺失给 STRING。
5. 标准英文名：小写字母/数字/下划线，非数字开头，≤64，同表唯一。
6. 视图**默认带分区字段**且排最后；物理表静态分区不进 SELECT，动态分区列排末尾。
7. `INSERT OVERWRITE` 不指定列 → 列顺序即唯一依据，**Excel/DDL/INSERT/节点内容必须同源同序**。
8. 匹配依据 = 目标 `字段中文名称`，括号注释不参与匹配。
9. 迁移包：按项目空间分包、节点名去掉**全部** `v_` 前缀、`Outputs@data = <项目>.<节点>`、与 `project.xml@name` 一致。
10. Nginx 必须转发 WebSocket；`maxUploadSize` 与 `client_max_body_size` 一致。
11. 镜像需包含 `stdfield/assets/root_library.xlsx`（`.dockerignore` 放行）；Dockerfile 不装 apt，HEALTHCHECK 用标准库。
12. **改 `stdfield/*` 后必须重启 Streamlit 进程**（模块驻留 `sys.modules`，否则报签名不匹配如 `build_package() missing argument`）。
13. **视图名前缀幂等**：目标视图名 = `v_` + 剥掉已有 `v_` 的表名（`dwd_x_df`→`v_dwd_x_df`、`v_dwd_x_df`→`v_dwd_x_df`，**禁止 `v_v_dwd_x_df`**）；解析派生、按前缀重算、空名兜底三条路径同一规则。`目标视图名` 以 `v_v_` 开头即错误。
14. **项目空间参数化**：`CREATE OR REPLACE VIEW` / `CREATE TABLE` / `INSERT OVERWRITE` / PyODPS 的目标对象前缀 = `${目标项目空间}`，来源表前缀 = `${来源项目空间}`（例 `${cdm_gp_prod}`、`${ods_gp17ccs}`）；显式传入的项目空间（迁移包）优先于清单里的「目标项目空间」。

## 🧨 已踩过的坑（勿回退）

- **`st.data_editor` 数据源必须稳定**：`st.session_state.draft` 是表格唯一数据源，只在解析/批量操作后由 `dwd_frame(editor)` 重建；**编辑后绝不把返回值写回 draft**，否则表格重建、内部滚动条跳回第一行。编辑值由组件状态保存，每轮从返回值合并进 `st.session_state.editor`。
- **行映射按位置**：`row_ids = draft_ids`；隐藏列 `_row_id` 会被 Streamlit 回传 NaN，不能依赖。
- **编辑不要 `_reset_export()`/`st.rerun()`**：会删掉产物区导致页面高度骤变、视口跳顶；改为标记「产物已过期」提示。
- **视图前缀/表名取值**：`表名` 取目标表，`来源表全名` 取来源表名，勿混用。
- **`v_` 前缀只能有一层**：历史回填的 `表名` 自带 `v_`（如 `v_dwd_clm_...`），若派生视图名 / 「按前缀重算视图名」再拼一层，就会生成 `CREATE OR REPLACE VIEW v_v_dwd_...`；若节点名只剥一层，节点名还会残留 `v_dwd_...`。规则：**前缀幂等（先剥再补）+ 节点名循环剥掉全部 `v_`**。
- **项目空间别写死**：早期脚本把项目空间写进 SQL（`FROM ods_hl17cc.v_x`、`CREATE OR REPLACE VIEW v_x`），换环境就要改文件。规则：目标 `${目标项目空间}.对象名`、来源 `${来源项目空间}.源表`，参数名=取值=项目空间名，参考 `raw/package` 里 `${ods_pt17cccs}` / `${cdm_pt_prod}` 的写法。
- **openpyxl 读模板**：`输入文件模板.xlsx` 含中文 `autoFilter` 会导致 `ValueError`，读入前需 `sanitize_xlsx()` 剥离 `<autoFilter>`。
- **`requirements.txt`** 只需 `streamlit pandas openpyxl`（导出用 openpyxl 手工写 3 sheet，不用 xlsxwriter）。

## 🪙 开发过程 Token 约束（强制执行）

### 1. 先契约，后代码
- 写任何模块前，先输出该模块的**函数签名 + 数据结构**（≤15 行），等确认后再写实现。
- 禁止一次性生成整个项目。按模块顺序交付：matcher → validators → generator → app。
- 每个模块只依赖已确认的契约，不自行新增未声明的接口。

### 2. 禁止探索式读文件
- 项目结构固定为：app.py / matcher.py / validators.py / generator.py / requirements.txt。
- 只读当前任务相关的文件。禁止扫描目录、禁止读取无关模块。
- 需要跨模块信息时，看契约，不读实现。

### 3. 输出纪律
- 不写解释性长文。代码前后最多各 2 行说明。   S
- 不写显而易见的注释（如 `# 导入 pandas`）。
- 不重复贴已确认过的代码。修改时只给 diff 或改动函数，不给全文。
- 不生成示例数据、测试用例、README，除非明确要求。

### 4. 固定技术决策，不再讨论
| 决策 | 结论 |
|---|---|
| Web 框架 | Streamlit |
| 数据处理 | pandas |
| Excel 读写 | openpyxl 读 / xlsxwriter 写 |
| 匹配 | 纯 Python 字典 + 最长匹配 |
| 持久化 | SQLite |
| 部署 | Docker Compose + Nginx |
| 输出模式 | CREATE OR REPLACE VIEW（默认） |
以上决策已定，禁止提议替代方案，禁止比较选型。

### 5. 最小实现优先
- 第一版只实现主流程，不做抽象层、不做插件化、不做配置化。
- 禁止引入设计模式（工厂、策略、观察者等），除非明确要求。
- 禁止预判未来需求写扩展点。
- 函数超过 40 行必须拆分，但不为拆分而拆分。

### 6. 错误处理最小化
- 一次只修一个 bug。给出：现象 1 行 + 原因 1 行 + 修复代码。
- 不输出完整 traceback，只贴关键行。
- 不在修复时顺手重构无关代码。

### 7. 上下文纪律
- 每次任务开始时，只复述与本任务相关的 1–2 条约束，不复述全项目背景。
- 长对话中主动提示："以下只讨论 X 模块，其余上下文可丢弃。"
- 不重复确认已确认过的内容。