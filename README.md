# 公共中心字段标准化

将来源表字段清单标准化为 DataWorks / MaxCompute 可用的标准英文字段名，并生成：

1. **标准化结果 Excel**（表目录 / 变更历史 / DWD模型设计，与输入模板结构一致）
2. **建视图脚本**（`CREATE OR REPLACE VIEW`）或**物理表脚本**（DDL + `INSERT OVERWRITE` + PyODPS）
3. **DataWorks 迁移助手导入包**（`.zip`）：导入后在 DataWorks 中自动创建节点，
   视图模式节点内容是建视图语句，物理表模式节点内容是 `INSERT OVERWRITE` 语句

程序**不连接源库、不连接 DataWorks**，全部输入靠上传文件，以 Docker + Nginx 方式部署，浏览器访问。

## 输入文件

| 文件 | 是否必填 | 说明 |
|---|---|---|
| ① 字段清单 | 必填 | 与 `输入文件模板.xlsx → DWD模型设计` 列结构一致（24 列），也支持同名列的 CSV |
| ② 词根库 | 可选 | **默认使用内置词根库** `stdfield/assets/root_library.xlsx`（常用词根库 v2.2）；上传则本次覆盖 |

标准化只需填写两列：

- **字段名**：标准化后的英文名（留空则由词根库推荐）
- **字段类型**：标准化后的 MaxCompute 类型（留空则按「来源字段类型」映射）

其余列（目标数据库名、表名、来源系统名称、来源表名、来源字段名、序号等）按输入模板原样提供即可，合并单元格造成的空值会自动向下填充。侧边栏可下载带填写说明的字段清单模板。

### 默认技术字段（自动补充）

每个目标对象末尾自动补 3 个技术字段，清单中已有同名字段时自动跳过、不重复：

| 字段名 | 中文名 | 类型 | SELECT 表达式 |
|---|---|---|---|
| `fst_load_tm` | 首次加载时间 | `DATETIME` | `getdate()` |
| `lst_load_tm` | 最后一次更新时间 | `DATETIME` | `getdate()` |
| `load_fm` | 来源系统 | `STRING` | `'系统简称'`（如 `'pt17cccs'`） |

侧边栏「技术字段 → 自动补充 3 个技术字段」可关闭；补充后的行可在编辑器中改名/改类型，改动会被保留。

标准化匹配依据是**目标列「字段中文名称」**（缺失时才回退来源字段中文名）；匹配前会剔除中文名中的字母/数字与**括号注释**——中文括号 `（）` 和英文括号 `()` 内的内容一律不参与匹配（支持嵌套/未闭合），如 `客户证件号码（主键）` → 按 `客户证件号码` 匹配；字段注释仍保留原始括号说明。

导出时 `非空`、`更新策略`、`引用代码表`、`字段枚举值`、`关联条件`、`加工逻辑` 等列按输入清单**原样回写**；其中 `加工逻辑` 同时作为 `SELECT` 表达式（优先于来源字段名）。

> 已删除「表级探源清单」及其三元组关联，程序只解析一份字段清单。

## Docker 部署

### 1. 生成本机证书和 Basic Auth

```bash
chmod +x scripts/gen_certs.sh
./scripts/gen_certs.sh
```

默认账号：`admin` / `changeme`（上线前请修改）。

### 2. HTTPS（Nginx 反代 Streamlit）

```bash
docker compose up --build -d
```

- 应用容器只绑定本机 `127.0.0.1:8501`
- 浏览器访问 `https://localhost`（自签证书需信任一次）
- Nginx 已配置 WebSocket `Upgrade`，避免 Streamlit 停在 loading 后无响应

只跑应用容器（不用 Nginx）：

```bash
docker build -t field-std-agent:1.0.0 .
docker run -d -p 8501:8501 -v "$PWD/data:/app/data" field-std-agent:1.0.0
```

### 3. 本机 HTTP 调试（不走 443）

```bash
docker compose -f docker-compose.yml -f docker-compose.http.yml up --build
```

访问 `http://localhost:8080`。

### 4. 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## 页面流程

1. 上传字段清单（词根库用内置默认，可上传覆盖）
2. 解析校验（错误阻断，警告可继续）
3. 表格即最终 `DWD模型设计` 的 24 列，直接改「字段名」「字段中文名称」「字段类型」「是否分区字段」「主键」（多候选 / 未匹配 / 冲突 建议人工确认）；改动即时生效且不会跳回表头，改完产物会提示「已过期，请重新生成」
4. 一键生成并下载全部产物

## 输出产物

| 产物 | 说明 |
|---|---|
| `标准化结果.xlsx` | 3 个 sheet，与输入模板结构一致：`表目录`、`变更历史`、`DWD模型设计`（24 列，字段名/字段类型已填好，可直接回填上传） |
| `odps_views.sql` | 视图模式：按表分组的 `CREATE OR REPLACE VIEW` |
| `odps_ddl.sql` / `odps_insert.sql` | 物理表模式：建表 DDL 与 `INSERT OVERWRITE` |
| `pyodps_create_table.py` | 勾选后生成，使用 `Column` 对象保留字段注释 |
| `dataworks迁移包/<包名>.zip` | **DataWorks 迁移助手导入包**，导入后直接创建节点 |

### DataWorks 迁移包结构

与 DataWorks 数据开发导出包（V1 XML）一致，参考 `raw/package/cdm_gp_prod_20261116`：

```
<项目名>_<yyyymmdd>/
├── project.xml
└── src/workflows/BUSINESS_dwd_<系统简称>/workflow.xml
```

- **按目标项目空间分包**：清单里有几个「目标数据库名」就生成几个包（如 `cdm_pt_prod_20261118.zip`、`cdm_hl_prod_20261118.zip`），分别导入各自的项目空间
- 包目录名 = 目标项目空间 + `_` + 生成日期
- 每个来源系统一个工作流（`dwd_<系统简称>`，如 `ods_pt17cccs` → `dwd_pt17cccs`），也可选择全部节点放同一个工作流
- 每个目标对象对应 `workflow.xml` 中的一个 `ODPS_SQL` 节点，**节点名 = 目标对象名去掉全部 `v_` 前缀**（`v_dwd_x_df` → `dwd_x_df`、`v_v_dwd_x_df` → `dwd_x_df`），重名自动追加序号；视图名本身保留单层 `v_`（禁止 `v_v_表名`）
- 视图默认**带分区字段**（侧边栏「视图中暴露分区字段」默认勾选），分区列排在视图最后一列
- **视图模式**：节点内容 = `CREATE OR REPLACE VIEW ...`
- **物理表模式**：节点内容 = `INSERT OVERWRITE TABLE ...`（建表 DDL 在 `odps_ddl.sql`，需先执行）
- `Outputs/Output@data` = `<项目名>.<节点名>`
- 侧边栏可配置目标项目空间、工作流组织方式、节点目录、owner、Cron、资源组、重跑策略

### 项目空间一律用调度参数

脚本和节点内容里的项目空间都写成 DataWorks 调度参数，参数名与取值同项目空间名：

```sql
CREATE OR REPLACE VIEW ${cdm_gp_prod}.v_dwd_x_df (...) AS
SELECT ... FROM ${ods_gp17ccs}.v_src_tbl;
```

- 目标对象前缀 = `${目标项目空间}`（清单「目标数据库名」；迁移包内以包的项目空间为准）
- 来源表前缀 = `${来源项目空间}`（来源表全名中 `.` 之前的部分）
- `CREATE TABLE IF NOT EXISTS`、`INSERT OVERWRITE TABLE`、PyODPS `o.create_table(...)` 同样带参数
- `导入说明.txt` 的「三、调度参数」列出包内全部参数，需在节点「调度配置 → 调度参数」中确认已定义

导入步骤：DataWorks 控制台 → 数据开发 → 迁移助手 → DataWorks迁移 → 导入 → 上传 `dataworks迁移包/<项目名>_<yyyymmdd>.zip`。

## 数据目录

```
data/
├── app.db              # 词根库版本与映射历史（SQLite）
├── root_library/       # 上传过的词根库版本
├── mappings/           # 每次生成的映射快照 JSON
└── exports/            # 导出 ZIP 归档
```

相同来源字段的已确认结果会在下次生成时自动复用（历史复用）。

## 校验与测试

```bash
python3 -m unittest discover -s tests -v   # 单元测试
python3 scripts/smoke_test.py              # 用内置词根库 + 输入文件模板做端到端烟雾测试
```

## 约束

- 不连源库、不连 DataWorks，全部文件驱动
- 词根库内置默认版本，未上传词根库不阻断流程
- 技术字段（`fst_load_tm` / `lst_load_tm` / `load_fm`）按目标对象自动补齐，已有同名字段则跳过
- 标准英文名只允许小写字母/数字/下划线，不以数字开头，长度 ≤64，同表内唯一
- 静态分区不进 `SELECT`；动态分区列必须位于 `SELECT` 末尾
- `INSERT OVERWRITE` 不指定列，DDL 与 INSERT 必须由同一份确认后的映射生成，保证列顺序一致
