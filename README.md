# 

面向留学服务群聊的长期服务质量监测、风险预警推送与规则自进化生产后端。系统按日接收群聊切片，通过单客户历史上下文自动预加载与 LangChain Agent（`create_agent` + `BaseTool`）生成每日结构化快照、演进长期记忆状态（温度、画像、申请进度、待办事项），对触发风险的切片推送飞书交互式卡片，并基于主管评估反馈（`count` 计分机制）自动触发教师模型规则修订与最多 5 轮沙盒回归验证。

---

## 一、生产交付待办清单

### 1. 环境变量与密钥配置
在生产服务器或容器环境（`.env` 或 `docker-compose.yml`）中配置以下必需参数：

| 变量名 | 是否必填 | 默认值 / 示例 | 作用说明 |
| :--- | :--- | :--- | :--- |
| **`GEMINI_API_KEY`** | **必填** | `AIzaSy...` | 调用工作模型与教师模型的 API 密钥 |
| **`GEMINI_MODEL`** | 选填 | `gemini-3.5-flash` | 每日群聊切片推理与沙盒回测使用的工作模型 |
| **`TEACHER_MODEL`** | 选填 | `gemini-4-argon` | 规则误报触发阈值时负责诊断与改写细则的教师模型 |
| **`FEISHU_APP_ID`** | 飞书推送必填 | `cli_a9...` | 飞书企业自建应用 App ID |
| **`FEISHU_APP_SECRET`** | 飞书推送必填 | `...` | 飞书企业自建应用 App Secret |
| **`FEISHU_DEFAULT_RECEIVE_ID`** | 选填 | `oc_6b19...` | 默认接收预警卡片的飞书群 `chat_id`（也可在每次请求中传 `feishu_receive_id`） |
| **`ENABLE_FEISHU_WS`** | 选填 | `1`（开启）/ `0`（关闭） | 是否随 HTTP 服务同进程启动飞书 WebSocket 长连接以接收主管按钮回调 |
| **`DB_URL`** | **必填** | `postgresql://postgres:postgres@localhost:5432/znz` | 企业生产 **PostgreSQL** 数据库连接串（兼容 `DATABASE_URL`） |

### 2. 上游业务系统每日调用对接（仅需传当日增量）
上游群聊采集系统每天只需向 `POST /api/v1/analyze` 发送**客户 ID + 当日新增聊天记录**，无需自行维护或回传历史聊天记录、历史快照与长期状态（服务端会自动从数据库按单客户预加载历史上下文）：

```json
{
  "day": "2026-05-06",
  "customer_name": "李同学",
  "feishu_receive_id": "oc_6b19b271c0f571d873f0482b41af5fed",
  "payload": {
    "customer_id": "cust_1001",
    "group_id": "grp_01",
    "days": {
      "2026-05-06": [
        {
          "message_id": "m201",
          "sent_at": "2026-05-06T09:30:00+08:00",
          "sender_name": "李同学",
          "role": "student",
          "text": "王老师，之前答应昨晚8点前给我的初稿怎么到现在还没发？"
        }
      ]
    }
  }
}
```
> **角色枚举约定（`role`）**：客户方严格映射为 `student`（学生）、`parent`（家长）；服务方严格映射为 `advisor`（售后顾问）、`sales`（销售）、`writer`（文案）、`planner`（规划师）、`bot`（系统通知/群管家）。

### 3. 数据库存储（PostgreSQL 原生对接）
- **零成本直连企业 PostgreSQL**：全系统数据库操作已 100% 收口于 `db_adapter.py`（基于 `psycopg[binary]>=3.1`），只需配置环境变量 `DB_URL=postgresql://user:***@host:5432/dbname`，服务启动时会自动执行幂等建表（`CREATE TABLE IF NOT EXISTS`）与 `agent/alert_details.yaml` 规则台账初始化，业务研发 **0 代码改动**。
- **开箱即用容器编排**：`docker-compose.yml` 已内置 `postgres:16-alpine` 服务，本地测试直接执行 `docker compose up --build -d` 即可一键拉起。

### 4. 容器化部署启动
```bash
docker compose up --build -d
```

---

## 二、数据库 5 张核心表说明

| 表名 | 主键 | 核心作用说明 |
| :--- | :--- | :--- |
| **`chat_messages_store`** | `(id_cust, analysis_date)` | 按客户与日期存储每日原始群聊消息列表（`messages_json`），供单客户历史消息下钻工具 `get_past_messages` 与巡查反查使用 |
| **`eval_slice_runs`** | `(id_cust, analysis_date)` | 按客户与日期存储每日推理后的温度、画像（`profile_json`）、待办事项（`items_json`）、申请进度（`applications_json`）、当日快照（`snapshot_json`）与状态变更指令（`state_changes_json`） |
| **`feishu_alert_feedback`** | `alert_id` | 存储推送至飞书的预警记录及主管交互评估结论（`supervisor_verdict`：`发起预警` / `暂不发起` / `误报`） |
| **`playbook_rules_ledger`** | `rule_id` | 维护生效中的预警细则台账、规则计分（`count`：发起预警 `+1` 封顶 `5`，误报 `-1`，暂不发起 `0`）及关联误报 ID（`source_cust_ids`），与 `agent/alert_details.yaml` 双向同步 |
| **`patrol_audit_log`** | `patrol_id` | 记录每次规则 `count <= -3` 触发教师模型修订时的完整诊断、最多 5 轮沙盒重试历史（`rework_history_json`）与最终状态（`APPLIED` / `REJECTED_ROLLED_BACK`） |

---

## 三、项目目录与全部文件职责说明

### 1. 根目录核心服务与配置文件

| 文件路径 | 核心职责说明 |
| :--- | :--- |
| **`api_server.py`** | **生产 HTTP 服务入口**：提供 `GET /api/health`（健康检查）、`POST /api/v1/analyze`（自动从 DB 预加载单客户历史上下文 $\rightarrow$ Agent 推理 $\rightarrow$ 状态演进落库 $\rightarrow$ 飞书推送）与 `POST /api/v1/patrol/run`（手动触发巡查），支持同进程拉起飞书长连接线程 |
| **`db_adapter.py`** | **单一数据库接口层**：全系统读写数据库的唯一通道，管理 5 张核心表建表自检、YAML 与数据库规则双向同步（`seed_playbook_from_yaml` / `render_yaml_from_db`）、单日推理落库（`save_daily_slice_run`）、单客户历史上下文预加载（`load_customer_context_before_day`）及误报样本提取（`load_harmful_cases_for_rule`） |
| **`feishu_bot.py`** | **飞书交互式卡片与回调引擎**：负责内外部扁平字段映射、发送飞书交互式预警卡片、监听长连接按钮点击事件，并在主管点击评估后自动更新 `feishu_alert_feedback` 与 `playbook_rules_ledger.count`（达阈值时异步触发规则重修 Hook） |
| **`feishu_card_config.yaml`** | **飞书卡片与计分配置文件**：定义卡片展示的 6 个扁平字段映射、风险标签配色、评估按钮文案及各结论对应的规则 `count` 分值变化（`score_deltas`） |
| **`SYSTEM_PROMPT.md`** | **工作模型系统提示词**：定义通用巡检方法论、工具调用规范、温度打分准则、状态变更规则以及 **11 类风险预警的 L1 宏观定义** |
| **`Dockerfile`** | **单阶段生产容器镜像构建文件**：基于 `python:3.12-slim` 打包最小化生产运行环境 |
| **`docker-compose.yml`** | **容器编排配置文件**：定义端口映射、环境变量注入与 `./data:/data` 数据库持久化挂载卷 |
| **`requirements.txt`** | **Python 生产依赖清单**：仅包含 `langchain`、`langchain-google-genai`、`flask`、`psycopg[binary]`、`pyyaml`、`lark-oapi` 6 个核心运行时依赖 |

### 2. `agent/`（每日群聊推理与长期记忆引擎）

| 文件路径 | 核心职责说明 |
| :--- | :--- |
| **`agent/__init__.py`** | 导出 Agent 核心外部调用接口（`run_single_slice`、`invoke_rolling_with_metrics`、`apply_state_changes` 等） |
| **`agent/runner.py`** | **Agent 编排与自愈重试核心**：驱动 LangChain `create_agent`（ReAct 步数上限受控），内置 JSON 语法修复与最多 **3 次确定性门禁校验自愈重试**，完成后推进长期状态并注入 `detected_at` |
| **`agent/input_builder.py`** | **渐进披露输入装配器**：将当日消息、昨日精简快照（`summary` + `alert`）与当前长期状态（仅展开未关闭 `open_items` 并裁剪证据 ID 窗口）组装为紧凑的模型输入，节省 Token |
| **`agent/reducer.py`** | **确定性长期状态演进机**：根据模型输出的 `state_changes` 指令，确定性地增删改 `profile`（画像）、`items`（待办/承诺）与 `applications`（选校申请），并计算新温度 |
| **`agent/validator.py`** | **模型输出确定性门禁校验器**：校验消息 ID 真实性、客户今日零发言时的加分/降温限制、预警类型与已调用 `get_alert_type_details` 的一致性、以及 `rule_ids` 与 `types` 的双向覆盖关系 |
| **`agent/contracts.py`** | **业务常量与枚举契约**：定义合法角色集合、11 类风险预警标准名称及默认责任人角色映射（`DEFAULT_ALERT_OWNER_BY_TYPE`） |
| **`agent/config.py`** | **模型运行参数配置**：管理 API Key 加载、默认模型名称、超时时间、多标签开关（`MULTI_LABEL`）及瞬态网络错误识别 |
| **`agent/prompts.py`** | **提示词加载器**：读取 `SYSTEM_PROMPT.md` 并提供 JSON 修复提示词模板 |
| **`agent/alert_details.yaml`** | **L2 预警触发细则库**：按 11 类风险维护具体的 `[rule_id] 细则描述`（与数据库 `playbook_rules_ledger` 表实时同步），供 `get_alert_type_details` 工具按需读取 |
| **`agent/schemas/result.py`** | **Pydantic 结构化输出模型**：严格定义 `ModelProposalOutput`、`DailySnapshot`、`AlertItem`（含 `types, owner, details, message_ids, rule_ids, detected_at`）及各类状态变更指令的 Schema |
| **`agent/tools/__init__.py`** | **客户级检索工具装配工厂（`build_customer_tools`）**：为单次分析装配只读隔离的 3 个工具，将 `< current_day` 的历史快照与历史消息通过 `PrivateAttr` 注入工具内存 |
| **`agent/tools/alert_tools.py`** | **`get_alert_type_details` 工具实现**：按模型传入的 1~3 个风险类别名称，从 `alert_details.yaml` 精准返回对应的 L2 触发细则与 `rule_id` |
| **`agent/tools/snapshot_tools.py`** | **`search_past_snapshots` 工具实现**：在单客户历史快照内存列表中对 `summary` 字段做中文分词匹配排序，返回 Top 1~5 条历史快照摘要 |
| **`agent/tools/message_tools.py`** | **`get_past_messages` 工具实现**：在单客户历史消息内存列表中按 `message_ids` 或关键词检索原始对话，并自动附带每条命中消息的前后各 1 条上下文 |
| **`agent/tools/base.py`** | **工具分词与证据收集底座**：提供中文 2-gram 分词函数 `tokenize_chinese` 与工具返回消息 ID 提取函数 `collect_message_ids` |

### 3. `rule_patrol/`（规则自进化与沙盒回归验证引擎）

| 文件路径 | 核心职责说明 |
| :--- | :--- |
| **`rule_patrol/__init__.py`** | 导出规则巡查与回调触发入口（`run_patrol_once`、`rework_single_rule`、`trigger_rule_rework_hook`） |
| **`rule_patrol/patrol_engine.py`** | **教师模型修规与 5 轮沙盒重试引擎**：当规则 `count <= -3` 时，提取引发误报的完整上下文交由教师模型（Gemini-4）诊断改写，生成临时沙盒 YAML 并重跑误报样本（验证误报消除）+ 随机正常样本（防回归破坏），最多重试 5 轮；全部通过则落库更新 `alert_details.yaml` 并将 `count` 归零，否则回滚保留原规则并将 `count` 归零 |
| **`rule_patrol/config.yaml`** | **巡查与教师模型配置文件**：定义触发阈值（`trigger_count: -3`、`max_count: 5`、`max_rework_attempts: 5`）、回归抽样数及教师模型的系统提示词（主干保留、抽象泛化、180 字上限三原则） |
