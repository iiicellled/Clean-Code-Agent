# Clean Code Agent

`Clean Code Agent` 是一个基于 [iiicellled/Clean-Code-Qwen](https://github.com/iiicellled/Clean-Code-Qwen) 的代码生成智能体应用。项目使用 FastAPI 实现后端 agent 编排，通过 OpenAI-compatible 接口调用远程 vLLM 部署的 Clean-Code-Qwen 模型，并提供一个 Web 页面用于展示对话、代码生成、代码快照和 Python 运行等能力。

`Clean Code Qwen` 是一个基于 `Qwen/Qwen2.5-Coder-7B-Instruct` 进行 SFT + DPO LoRA 微调的项目，并且得到一个训练完毕的模型。本项目侧重模型应用层：将已经 merge 后的模型部署为远程 coder 模型，并在后端实现主模型路由、意图识别、代码生成、代码审阅、会话记忆、代码工作区和运行验证等 agent 功能。

![Web UI](figures/web.png)

## 当前状态

- 主模型：`Qwen3.7-Max`
- 远程 coder 模型：`cleancode_qwen/output_models/qwen-coder-simplifier-dpo-merged`
- 模型来源：[iiicellled/Clean-Code-Qwen](https://github.com/iiicellled/Clean-Code-Qwen)
- 后端框架：FastAPI
- 推理服务：vLLM + OpenAI-compatible `/v1/chat/completions`
- 前端页面：Vue 3 + Monaco Editor
- 数据库：MySQL / SQLAlchemy，可选启用
- 主要代码语言：Python

## 项目主要功能

### 1. 双模型 Agent 架构

项目中有两个模型角色：

| 模型角色 | 代码对象 | 默认用途 | 配置方式 |
|---|---|---|---|
| 主模型 | `primary_chat_model` | 普通聊天、意图识别、槽位抽取、代码审阅与整理 | `PRIMARY_MODEL_URL`、`PRIMARY_MODEL_NAME`、`PRIMARY_API_KEY` |
| coder 模型 | `coder_chat_model` | 代码生成 | `CODER_MODEL_URL`、`CODER_MODEL_NAME`、`CODER_API_KEY` |

coder 模型实际部署的是 Clean-Code-Qwen merge 后的完整模型：

```text
cleancode_qwen/output_models/qwen-coder-simplifier-dpo-merged
```

默认情况下，`MODEL_ROUTING_ENABLED=false`，用户请求会直接发送给 coder 模型。开启 `MODEL_ROUTING_ENABLED=true` 后，后端会先调用主模型做意图识别，再根据识别结果决定是否进入代码生成链路。

### 2. 后端 Agent 流程

开启路由后，一次对话请求的大致流程如下：

```mermaid
flowchart TD
    A[User message] --> B[conversation_service 保存用户消息]
    B --> C[加载最近对话与代码工作区]
    C --> D[intent_service 调用主模型识别意图]
    D --> E{intent}
    E -->|general_chat| F[chatbot_service 调用主模型回答]
    E -->|write_code 且缺少槽位| G[主模型生成追问]
    E -->|write_code 且槽位完整| H[coder_service 构造 coder prompt]
    H --> I[coder_chat_model 调用 Clean-Code-Qwen]
    I --> J[code_review_service 把 raw code 交给主模型审阅]
    J --> K[主模型检查需求、接口、边界和输出格式]
    K --> L[conversation_service 保存助手消息]
    L --> M[抽取 Markdown code block 为代码快照]
    M --> N[Web 代码区展示 / 可继续修改]
```

对应的核心代码位置：

- `app/services/model_router_service.py`：决定走普通聊天、追问、代码生成还是代码审阅链路
- `app/services/intent_service.py`：构造意图识别 prompt，要求主模型返回严格 JSON
- `app/services/coder_service.py`：将主模型抽取出的结构化任务转成 coder 模型输入
- `app/services/code_review_service.py`：将 coder 生成的 raw code 交给主模型复核和整理
- `app/services/conversation_service.py`：负责消息、上下文、任务状态和代码快照

### 3. 主模型的意图识别

主模型不是直接把原始用户问题转给 coder 模型，而是先把对话压缩为一个结构化决策。当前意图集合包括：

```text
general_chat
write_code
unknown
```

当识别到 `write_code` 时，主模型需要抽取这些槽位：

| 槽位 | 是否必需 | 含义 |
|---|---:|---|
| `language` | 是 | 用户希望生成的编程语言，例如 Python |
| `task` | 是 | 具体要实现的函数、算法、脚本或功能 |
| `constraints` | 否 | 额外约束，例如复杂度、输入输出格式、边界条件 |

主模型返回的数据格式类似：

```json
{
  "intent": "write_code",
  "confidence": 0.92,
  "slots": {
    "language": "Python",
    "task": "实现一个函数，返回列表中的第二大元素",
    "constraints": "需要处理重复元素和空列表"
  },
  "missing_slots": [],
  "follow_up_question": null
}
```

如果 `language` 或 `task` 缺失，后端不会立即调用 coder 模型，而是保存当前任务状态并让主模型生成追问。例如用户只说“帮我写个函数”，系统会继续询问语言和具体功能。

### 4. 主模型如何告知 coder 模型

主模型和 coder 模型之间是通过后端的结构化中间层衔接：

1. `intent_service` 调用主模型，得到 `IntentDecision`。
2. `model_router_service` 判断 `IntentDecision.ready_to_execute`。
3. `coder_service` 读取 `language`、`task`、`constraints` 和最新用户消息。
4. 后端用 `CODER_USER_PROMPT_TEMPLATE` 重新组织成面向 coder 模型的 prompt。
5. `coder_chat_model` 通过 OpenAI-compatible HTTP 请求调用远程 Clean-Code-Qwen。

即，主模型负责理解和拆解任务，coder 模型负责生成代码，两者间传递的是后端定义的结构化任务而不是简单拼接完整对话。

### 5. 主模型如何审阅和改进 coder 输出

当 coder 模型生成初稿后，系统会进入审阅阶段：

1. `coder_service.generate_code()` 得到 `raw_code`。
2. `code_review_service` 将 `raw_code`、用户需求、语言、任务和约束一起放入审阅 prompt。
3. 主模型检查代码是否满足用户需求、接口是否正确、边界条件是否明显遗漏、输出格式是否适合前端抽取。
4. 主模型返回最终 Markdown code block。
5. `conversation_service` 从最终回复中抽取代码块，生成代码快照并返回给前端。

这一步的作用是在应用层做一次轻量 verifier / reviewer，使最终展示给用户的代码更稳定，也方便后端抽取为代码文件。

### 6. 会话记忆与工作区上下文

配置数据库后，系统会保存历史消息、任务状态和代码快照；每次请求时，后端会重新组装上下文发给模型。

当前上下文策略：

- 最多取最近 `20` 条消息作为对话上下文
- 如果当前有代码工作区，会将文件路径、语言、内容和 active file 写入 system message
- 如果存在未补全的写代码任务，会从 `conversation_tasks` 中恢复已填槽位和缺失槽位
- 用户在右侧代码区修改后的文件，会随下一轮请求一起提交给后端

这使得系统可以支持连续交互，例如：

```text
生成函数 -> 用户修改代码 -> 让模型补边界条件 -> 运行 -> 继续优化
```

### 7. MySQL 数据表

配置 `DATABASE_URL` 后，SQLAlchemy 会自动创建 4 张表：

| 表名 | 作用 |
|---|---|
| `conversations` | 保存会话标题、创建时间和更新时间 |
| `messages` | 保存用户、助手和 system 消息 |
| `conversation_tasks` | 保存意图路由中的任务状态、slots 和 missing_slots |
| `code_snapshots` | 保存每轮对话产生或提交的代码文件快照 |

表之间的关系：

```mermaid
erDiagram
    conversations ||--o{ messages : contains
    conversations ||--o{ conversation_tasks : tracks
    conversations ||--o{ code_snapshots : stores
    messages ||--o{ code_snapshots : may_create

    conversations {
        int id
        string title
        datetime created_at
        datetime updated_at
    }
    messages {
        int id
        int conversation_id
        string role
        text content
        datetime created_at
    }
    conversation_tasks {
        int id
        int conversation_id
        string intent
        string status
        json slots_json
        json missing_slots_json
    }
    code_snapshots {
        int id
        int conversation_id
        int message_id
        json files_json
        string active_file
        string created_by
    }
```

如果不配置数据库，历史会话接口会返回不可用，前端自动进入无历史模式，但基础聊天接口仍可使用。

### 8. Python 代码运行

后端提供 `/api/code/run` 接口运行 Python 代码。执行时会在临时目录中生成脚本，并使用隔离子进程运行：

- 使用当前 Python 解释器
- 使用 `-I -X utf8` 启动
- 支持 stdin
- 支持额外调用片段 `call_code`
- 支持超时限制
- 对 stdout/stderr 做长度截断

该能力用于基本验证生成代码的行为，不建议执行不可信或高风险代码。

### 9. Web 页面展示

前端主要用于展示和调试后端 agent 能力，包含：

- 聊天窗口
- SSE 流式显示
- 历史会话侧边栏
- 右侧代码区
- Monaco Editor 编辑体验
- 代码文件 tab
- 代码复制
- Python 运行结果展示
- Markdown、代码高亮和公式渲染

## 整体架构

```text
Clean-Code-Qwen / sft_lora_coder
  Qwen2.5-Coder-7B-Instruct
        + SFT LoRA
        + DPO LoRA
        -> merge_lora.py
        -> output_models/qwen-coder-simplifier-dpo-merged

Remote Linux GPU Server
  vLLM + serve_remote.py
  POST /v1/chat/completions

Local / Application Server
  FastAPI backend
    - primary model client
    - coder model client
    - intent routing
    - code review
    - conversation storage
    - code extraction
    - Python runner
  Web frontend
    - chat UI
    - code workspace
```

## 目录结构

```text
coder_agent/
  app/
    main.py                         # FastAPI 入口、API 路由、静态资源挂载
    config.py                       # 环境变量配置
    database.py                     # SQLAlchemy 初始化
    models.py                       # 会话、消息、任务、代码快照模型
    schemas.py                      # Pydantic API schema
    model_service.py                # 主模型和 coder 模型客户端
    services/
      chatbot_service.py            # 普通聊天服务，由主模型处理
      coder_service.py              # 将结构化任务转给 coder 模型
      code_review_service.py        # 主模型审阅和整理 coder 输出
      code_runner_service.py        # Python 代码运行
      conversation_service.py       # 会话、上下文、任务状态、代码快照
      intent_service.py             # 主模型意图识别、槽位抽取、追问
      model_router_service.py       # 模型路由与任务编排
      service_configs.py            # 各服务模型参数和提示词
  web/
    index.html                      # Web 页面
    app.js                          # Vue、SSE、Monaco、会话逻辑
    styles.css                      # 页面样式
    github.css                      # Markdown 样式
    googlecode.css                  # 高亮样式
    highlight.min.js                # highlight.js
  figures/
    web.png                         # Web 页面截图
  requirements.txt
  README.md
```

## 环境依赖

本地 agent 依赖：

```text
fastapi
uvicorn[standard]
httpx
python-dotenv
SQLAlchemy
PyMySQL
langchain-openai
```

远程模型训练、merge 和 vLLM 部署依赖可参考 `Clean-Code-Qwen` 的 requirements，其中包括：

```text
torch
transformers
peft
trl
datasets
accelerate
bitsandbytes
vllm
```

## 模型准备与部署

### 1. 准备 Clean-Code-Qwen 模型

本项目基于已经发布的 Clean-Code-Qwen，需要将 LoRA adapter 合并为完整模型。

默认训练产物：

```text
models/Qwen2.5-Coder-7B-Instruct
output_models/qwen-coder-simplifier-lora
output_models/qwen-coder-simplifier-dpo-lora
```

在远程 Linux GPU 服务器上安装依赖：

```bash
cd cleancode_qwen
pip install -r requirements.txt
```

执行 merge：

```bash
python merge_lora.py \
  --base-model models/Qwen2.5-Coder-7B-Instruct \
  --sft-adapter output_models/qwen-coder-simplifier-lora \
  --dpo-adapter output_models/qwen-coder-simplifier-dpo-lora \
  --output-dir output_models/qwen-coder-simplifier-dpo-merged \
  --merge-strategy final_adapter \
  --dtype float16 \
  --overwrite
```

当前 DPO adapter 是在 SFT adapter 基础上继续训练保存的最终 LoRA，因此默认使用 `final_adapter`。如果 DPO adapter 是相对 SFT-merged 模型的增量，可根据 `merge_lora.py` 的说明改用 `sequential`。

### 2. 启动远程 vLLM 服务

使用 merged 模型启动服务：

```bash
uvicorn serve_remote_vllm:app --host 127.0.0.1 --port 9000 --log-level info --no-access-log
```

可以在本地使用 SSH 隧道连接服务器：

```bash
ssh -L 9000:127.0.0.1:9000 user@REMOTE_SERVER_IP
```

## 本地启动

```powershell
cd cleancode_agent
pip install -r requirements.txt
```

创建 `.env`：

```env
# Remote Clean-Code-Qwen served by vLLM
CODER_MODEL_URL=http://127.0.0.1:9000/v1/chat/completions
CODER_MODEL_NAME=qwen-coder-simplifier-dpo-merged
CODER_API_KEY=change-me-into-your-coder-api-key
CODER_TIMEOUT_SECONDS=300
VERIFY_CODER_TLS=true

# Optional primary model for intent routing, general chat, and code review.
MODEL_ROUTING_ENABLED=false
PRIMARY_MODEL_URL=https://api.openai.com/v1
PRIMARY_MODEL_NAME=
PRIMARY_API_KEY=
PRIMARY_TIMEOUT_SECONDS=300

# Optional database. Without this, history APIs are disabled.
DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/coder_agent?charset=utf8mb4
```

启动后端和页面：

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

访问：

```text
http://127.0.0.1:8001
```

## API

### 状态接口

```http
GET /api/health
GET /api/model/status
```

### 普通聊天

```http
POST /api/chat
POST /api/chat/stream
```

请求示例：

```json
{
  "messages": [
    {
      "role": "user",
      "content": "写一个 Python 函数，返回列表中的第二大元素。"
    }
  ]
}
```

### 会话聊天

需要配置 `DATABASE_URL`。

```http
GET    /api/conversations
POST   /api/conversations
GET    /api/conversations/{conversation_id}
DELETE /api/conversations/{conversation_id}
POST   /api/conversations/{conversation_id}/chat
POST   /api/conversations/{conversation_id}/chat/stream
```

会话请求可以携带当前代码区状态：

```json
{
  "content": "把这个函数改成支持空列表，并补充类型注解。",
  "current_files": [
    {
      "path": "solution.py",
      "language": "python",
      "content": "def second_largest(nums):\n    return sorted(set(nums))[-2]"
    }
  ],
  "active_file": "solution.py"
}
```

### 代码运行

```http
POST /api/code/run
```

请求示例：

```json
{
  "language": "python",
  "code": "def add(a, b):\n    return a + b",
  "call_code": "add(1, 2)",
  "stdin": "",
  "timeout_seconds": 5
}
```

返回示例：

```json
{
  "stdout": "3\n",
  "stderr": "",
  "exit_code": 0,
  "timeout": false,
  "duration_ms": 42
}
```

## 与 Clean-Code-Qwen 的关系

- `Clean Code Qwen` 保存训练、评测、merge 和远程部署脚本，并提供经过 SFT + DPO 优化的 `Qwen2.5-Coder-7B-Instruct` 模型。
- `Clean Code Agent` 使用 merge 后的模型作为远程 coder 后端，并实现应用层 agent 能力。

整体流程为：

```text
SFT 数据 -> SFT LoRA
DPO 偏好数据 -> DPO LoRA
merge_lora.py -> qwen-coder-simplifier-dpo-merged
vLLM 服务 -> Clean Code Agent 后端 -> Web 页面/API
```

## 当前限制

- 内置代码运行目前只支持 Python。
- Python 运行器用于基本验证，不是完整安全沙箱。
- 会话历史依赖外部数据库，不配置 `DATABASE_URL` 时不会保存历史。
- `serve_remote.py` 当前可以接受 `stream: true`，但具体是否逐 token 输出取决于远程服务实现。

## 后续计划

- 增加多文件项目级代码修改能力
- 增加测试生成与自动运行
- 增加 Git diff 展示和补丁应用
- 增加更严格的代码运行沙箱
- 增加更多语言的运行支持
