# Clean Code Agent

`Clean Code Agent` 是一个基于 [iiicellled/Clean-Code-Qwen](https://github.com/iiicellled/Clean-Code-Qwen) 的代码生成与代码修改智能体应用。项目使用 FastAPI 实现后端 agent 编排，通过 OpenAI-compatible 接口调用远程 vLLM 部署的 Clean-Code-Qwen 模型，并提供一个 Web 页面用于会话、读取本地项目文件、在 Monaco Editor 中编辑代码、生成函数级 patch、运行 Python 代码。

`Clean Code Qwen` 是一个基于 `Qwen/Qwen2.5-Coder-7B-Instruct` 进行 SFT + DPO LoRA 微调的代码模型。本项目侧重模型应用层：将已经 merge 后的模型部署为远程 coder 模型，并在后端实现主模型路由、意图识别、当前文件代码搜索、代码生成、代码审阅、patch 提议、会话记忆和运行验证等 agent 功能。

![Web UI](figures/web.png)

## 当前状态

- 主模型：`Qwen3.7-Max`
- 远程 coder 模型：`cleancode_qwen/output_models/qwen-coder-simplifier-dpo-merged`
- 模型来源：[iiicellled/Clean-Code-Qwen](https://github.com/iiicellled/Clean-Code-Qwen)
- 后端框架：FastAPI
- 推理服务：vLLM + OpenAI-compatible `/v1/chat/completions`
- 前端页面：Vue 3 + Monaco Editor
- 数据库：MySQL / SQLAlchemy，必须配置 `DATABASE_URL`
- 本地项目读取：浏览器 File System Access API
- 主要代码语言：Python

## 项目主要功能

### 1. 双模型 Agent 架构

项目中有两个模型角色：

| 模型角色 | 代码对象 | 默认用途 | 配置方式 |
|---|---|---|---|
| 主模型 | `primary_chat_model` | 普通聊天、意图识别、槽位抽取、代码审阅与整理、patch 前整理 | `PRIMARY_MODEL_URL`、`PRIMARY_MODEL_NAME`、`PRIMARY_API_KEY` |
| coder 模型 | `coder_chat_model` | 根据结构化任务生成代码或函数替换实现 | `CODER_MODEL_URL`、`CODER_MODEL_NAME`、`CODER_API_KEY` |

coder 模型实际部署的是 Clean-Code-Qwen merge 后的完整模型：

```text
cleancode_qwen/output_models/qwen-coder-simplifier-dpo-merged
```

开启 `MODEL_ROUTING_ENABLED=true` 后，后端会先调用主模型做意图识别，再根据识别结果决定进入普通聊天、追问、写代码、写函数或改函数链路。该版本建议启用模型路由；本地文件修改、函数级 patch 和追问补槽都依赖主模型的结构化决策。

### 2. 后端 Agent 流程

一次会话请求的大致流程如下：

```mermaid
flowchart TD
    A[User message] --> B[conversation_service 保存用户消息]
    B --> C[加载最近对话和当前前端工作区]
    C --> D[intent_service 调用主模型识别意图]
    D --> E{intent}
    E -->|general_chat| F[chatbot_service 调用主模型回答]
    E -->|code intent 且缺少槽位| G[返回追问]
    E -->|code intent 且槽位完整| H[current_file_search_tool 搜索当前活动文件]
    H --> I[coder_service 构造 coder prompt]
    I --> J[coder_chat_model 调用 Clean-Code-Qwen]
    J --> K[code_review_service 调用主模型审阅和整理]
    K --> L[patch_service 生成函数级 patch 提议]
    L --> M[conversation_service 保存助手消息并返回 patch]
    M --> N[Web 代码区一键应用 / 保存本地文件]
```

对应的核心代码位置：

- `app/services/model_router_service.py`：决定走普通聊天、追问、代码生成还是代码审阅链路
- `app/services/intent_service.py`：构造意图识别 prompt，要求主模型返回严格 JSON
- `app/tools/current_file_search_tool.py`：在当前活动文件中搜索目标函数、类、调用点或关键词片段
- `app/services/coder_service.py`：将结构化任务和当前文件上下文转成 coder 模型输入
- `app/services/code_review_service.py`：将 coder 生成的 raw code 交给主模型复核和整理
- `app/services/patch_service.py`：把审阅后的代码整理成可一键应用的函数级 patch
- `app/services/conversation_service.py`：负责消息、上下文、任务状态和 patch 返回

### 3. 主模型的意图识别

主模型把最近对话压缩为一个结构化决策。当前意图集合包括：

```text
general_chat
write_code
create_function
modify_function
unknown
```

各意图的边界如下：

| 意图 | 使用场景 | 不适用场景 / 边界 |
|---|---|---|
| `general_chat` | 普通问答、概念解释、项目讨论、代码讲解、方案比较，或用户只是询问而不要求产出代码修改。 | 如果用户明确要求生成代码、添加函数、修改已有函数，不应归为该意图。 |
| `write_code` | 生成一段独立代码、脚本、类、算法、工具函数组或示例代码；任务不绑定当前活动文件中的某个目标函数。 | 不承诺生成可一键应用的 patch；如果用户明确要“在当前文件新增某个函数”，应归为 `create_function`；如果用户明确要改已有函数，应归为 `modify_function`。 |
| `create_function` | 在当前文件或当前任务上下文中新增一个单独函数，目标函数通常尚不存在，需要抽取函数名、参数和具体功能。 | 不用于生成完整脚本、多个函数组合、类定义或泛泛写代码；如果目标函数已经存在并要改写，应归为 `modify_function`。 |
| `modify_function` | 修改、修复、补全、重构当前活动文件中的某个已有函数，目标函数名应能确定，并优先围绕该函数生成替换实现。 | 不用于新增不存在的函数；不适合跨文件修改、整文件重写或无法定位目标函数的请求，这类请求应退回 `write_code` 或追问。 |
| `unknown` | 用户意图无法判断，或输入过短、缺少上下文，不能可靠决定是否需要代码生成或修改。 | 如果能通过追问补齐代码任务槽位，优先保留相应代码意图并返回 `missing_slots`，不要轻易使用 `unknown`。 |

代码相关意图会抽取这些槽位：

| 槽位 | 常见意图 | 含义 |
|---|---|---|
| `language` | `write_code`，可选用于函数任务 | 编程语言，例如 Python |
| `task` | `write_code`、`create_function`、`modify_function` | 具体要实现或修改的功能 |
| `function_name` | `create_function`、`modify_function` | 目标函数名 |
| `parameters` | `create_function` | 新函数参数列表 |
| `constraints` | 所有代码意图 | 额外约束，例如复杂度、输入输出格式、兼容性要求 |
| `search_symbols` | 所有代码意图 | 需要优先在当前文件里检索的函数名或类名 |

主模型返回的数据格式类似：

```json
{
  "intent": "modify_function",
  "confidence": 0.92,
  "slots": {
    "language": "Python",
    "task": "为空列表返回 None，并保留原来的第二大元素逻辑",
    "function_name": "second_largest",
    "parameters": null,
    "constraints": "保持调用兼容",
    "search_symbols": "second_largest"
  },
  "missing_slots": [],
  "follow_up_question": null
}
```

如果必填槽位缺失，后端不会立即调用 coder 模型，而是保存当前任务状态并让主模型生成追问。例如用户只说“帮我新增一个函数”，系统会继续询问函数名、参数和具体功能。

### 4. 当前文件搜索与工作区上下文

右侧代码区读取本地项目文件，但文件内容不由后端主动扫描本机磁盘。前端通过浏览器的 File System Access API 让用户选择项目目录，再读取用户打开的文本文件。每次用户发送消息时，前端会把当前代码区文件列表和 `active_file` 一起提交给会话接口。

后端收到当前工作区后，会先做一次意图识别，拿到 `function_name` 或 `search_symbols`，再调用 `current_file_search_tool.search_current_file()` 只搜索当前活动文件。搜索结果会包含：

- 目标函数或类的完整定义块
- 目标函数的调用片段
- 关键词命中的上下文窗口
- 文件头部 import 区域或前若干行兜底上下文

这样可以避免把整个大文件都塞进模型上下文，同时让 coder 模型更稳定地生成局部修改。

### 5. 主模型如何告知 coder 模型

主模型和 coder 模型之间通过后端的结构化中间层衔接：

1. `intent_service` 调用主模型，得到 `IntentDecision`。
2. `conversation_service` 根据 `function_name`、`search_symbols` 和用户请求搜索当前活动文件。
3. `model_router_service` 判断 `IntentDecision.ready_to_execute`。
4. `coder_service` 读取 `language`、`task`、`constraints`、`function_name`、`parameters`、当前文件搜索结果和最新用户消息。
5. 后端用 `CODER_USER_PROMPT_TEMPLATE` 重新组织成面向 coder 模型的 prompt。
6. `coder_chat_model` 通过 OpenAI-compatible HTTP 请求调用远程 Clean-Code-Qwen。

对于 `create_function`，coder 模型被要求只返回完整的新目标函数。对于 `modify_function`，coder 模型被要求只返回目标函数的完整替换实现，并尽量保持函数名和调用兼容。

### 6. 主模型审阅与 patch 提议

当 coder 模型生成初稿后，系统会进入审阅阶段：

1. `coder_service.generate_code()` 得到 `raw_code`。
2. `code_review_service` 将 `raw_code`、用户需求、语言、任务和约束一起放入审阅 prompt。
3. 主模型检查代码是否满足用户需求、接口是否正确、边界条件是否明显遗漏。
4. 主模型返回最终 Markdown code block 和简短说明。
5. `patch_service` 从最终代码中提取目标函数，基于当前活动文件生成 `old/new` patch。
6. 前端展示 patch 提议，用户点击“应用”后在编辑器内替换，再点击保存写回本地文件。

当前 patch 主要支持 Python 函数级修改：

- `modify_function`：用 AST 查找当前文件中唯一同名函数，并用新函数整体替换旧函数。
- `create_function`：当当前文件中不存在目标函数时，将新函数插入到 import / 文件头部之后。
- 其它代码意图：会尝试根据返回代码中的函数名匹配当前文件中的同名定义。

Patch 应用发生在前端编辑器内。只有用户点击保存后，浏览器才会通过本地文件句柄覆写磁盘文件。

### 7. 会话记忆与数据库

本版本必须配置 `DATABASE_URL`。没有数据库时，后端启动会失败，前端也不会退回无历史模式。

数据库保存：

- 会话标题、创建时间和更新时间
- 用户 / 助手 / system 消息
- 未完成代码任务的意图、slots 和 missing_slots

数据库不再保存代码快照，也不保存本地文件内容。当前工作区状态来自前端当前打开的文件，并随每次会话请求提交给后端。

当前上下文策略：

- 最多取最近 `20` 条消息作为对话上下文
- 当前活动文件会经过 `current_file_search_tool` 搜索后写入 system message
- 如果存在未补全的代码任务，会从 `conversation_tasks` 中恢复已填槽位和缺失槽位
- 用户在右侧代码区修改后的文件内容，会随下一轮请求一起提交给后端

这使得系统可以支持连续交互，例如：

```text
选择项目文件夹 -> 打开文件 -> 让模型修改函数 -> 应用 patch -> 保存文件 -> 运行验证 -> 继续优化
```

### 8. MySQL 数据表

配置 `DATABASE_URL` 后，SQLAlchemy 会自动创建 3 张表：

| 表名 | 作用 |
|---|---|
| `conversations` | 保存会话标题、创建时间和更新时间 |
| `messages` | 保存用户、助手和 system 消息 |
| `conversation_tasks` | 保存意图路由中的任务状态、slots 和 missing_slots |

表之间的关系：

```mermaid
erDiagram
    conversations ||--o{ messages : contains
    conversations ||--o{ conversation_tasks : tracks

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
```

如果你从旧版本升级，数据库里可能残留旧的 `code_snapshots` 表。当前代码不会再读取或写入它；确认不需要旧数据后可以手动删除：

```sql
DROP TABLE code_snapshots;
```

### 9. Python 代码运行

后端提供 `/api/code/run` 接口运行 Python 代码。默认执行后端为 Docker：后端会把用户代码写入一次性临时目录，再使用 `docker run --rm` 启动隔离容器运行脚本。

默认容器运行策略：

- 镜像：`python:3.12-slim`
- 工作目录：容器内 `/workspace`
- 代码挂载：临时目录只读挂载到容器
- 网络：默认 `--network none`
- 资源限制：默认 `256m` 内存、`1` CPU、`64` pids
- 超时：沿用接口请求里的 `timeout_seconds`
- 输出：stdout/stderr 会按长度截断，避免前端被超大输出卡住

该能力用于基本验证生成代码的行为，不建议执行不可信或高风险代码。如果本机暂时没有 Docker，开发调试时可以通过 `.env` 切回本地 Python runner。

### 10. Web 页面展示

前端主要用于展示和调试后端 agent 能力，包含：

- 聊天窗口
- SSE 流式显示
- 历史会话侧边栏
- 右侧代码区
- 本地项目文件夹选择
- 本地文本文件读取与保存
- Monaco Editor 编辑体验
- 代码文件 tab
- Patch 提议、一键应用和取消
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
    - current-file search
    - code generation
    - code review
    - function-level patch proposal
    - conversation storage
    - Python runner
  Web frontend
    - chat UI
    - local folder picker
    - Monaco code workspace
    - patch apply/save workflow
```

## 目录结构

```text
coder_agent/
  app/
    main.py                         # FastAPI 入口、API 路由、静态资源挂载
    config.py                       # 环境变量配置
    database.py                     # SQLAlchemy 初始化，要求配置 DATABASE_URL
    models.py                       # 会话、消息、任务状态模型
    schemas.py                      # Pydantic API schema
    model_service.py                # 主模型和 coder 模型客户端
    services/
      chatbot_service.py            # 普通聊天服务，由主模型处理
      coder_service.py              # 将结构化任务转给 coder 模型
      code_review_service.py        # 主模型审阅和整理 coder 输出
      code_runner_service.py        # Python 代码运行
      conversation_service.py       # 会话、上下文、任务状态、当前工作区和 patch 编排
      intent_service.py             # 主模型意图识别、槽位抽取、追问
      model_router_service.py       # 模型路由与任务编排
      patch_service.py              # 函数级 patch 提议
      service_configs.py            # 各服务模型参数和提示词
    tools/
      current_file_search_tool.py   # 当前活动文件搜索工具
  web/
    index.html                      # Web 页面
    app.js                          # Vue、SSE、Monaco、本地文件和 patch 逻辑
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
cd coder_agent
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

# Primary model for intent routing, general chat, code review, and patch-oriented cleanup.
MODEL_ROUTING_ENABLED=true
PRIMARY_MODEL_URL=https://api.openai.com/v1
PRIMARY_MODEL_NAME=
PRIMARY_API_KEY=
PRIMARY_TIMEOUT_SECONDS=300

# Required database. The app does not support database-less mode.
DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/coder_agent?charset=utf8mb4
```

启动后端和页面：

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 18001
```

访问：

```text
http://127.0.0.1:18001
```

使用本地项目工作区时，请用 Chrome 或 Edge 打开页面，并在右侧代码区点击文件夹按钮选择项目目录。浏览器会请求本地目录读写权限；只有用户打开的文件会进入代码区并随请求发送给后端。

## Docker 代码运行

Web 右侧代码区通过后端接口 `POST /api/code/run` 执行 Python。默认执行后端为 Docker：后端会把用户代码写入一次性临时目录，再使用 `docker run --rm` 启动隔离容器运行脚本，而不是直接调用宿主机 Python。

默认容器运行策略：

- 镜像：`python:3.12-slim`
- 工作目录：容器内 `/workspace`
- 代码挂载：临时目录只读挂载到容器
- 网络：默认 `--network none`
- 资源限制：默认 `256m` 内存、`1` CPU、`64` pids
- 超时：沿用接口请求里的 `timeout_seconds`
- 输出：stdout/stderr 会按长度截断，避免前端被超大输出卡住

首次使用前需要安装并启动 Docker Desktop，然后拉取默认镜像：

```powershell
docker pull python:3.12-slim
```

`.env` 中可配置代码运行后端：

```env
CODE_RUNNER_BACKEND=docker
CODE_RUNNER_DOCKER_IMAGE=python:3.12-slim
CODE_RUNNER_DOCKER_NETWORK=none
CODE_RUNNER_DOCKER_MEMORY=256m
CODE_RUNNER_DOCKER_CPUS=1
CODE_RUNNER_DOCKER_PIDS_LIMIT=64
```

如果本机暂时没有 Docker，开发调试时可以切回本地 Python：

```env
CODE_RUNNER_BACKEND=local
```

启动后端前务必先确认 Docker 正常：

```powershell
docker desktop status
docker run hello-world
```

## API

### 状态接口

```http
GET /api/health
GET /api/model/status
```

### 会话聊天

必须配置 `DATABASE_URL`。

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

流式接口结束时，如果后端成功生成 patch，会在 `done` 事件中返回：

```json
{
  "type": "done",
  "patch": {
    "file_path": "solution.py",
    "summary": "Proposed replacement based on: ...",
    "old": "def second_largest(nums):\n    return sorted(set(nums))[-2]\n",
    "new": "def second_largest(nums: list[int]) -> int | None:\n    ...\n"
  }
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

- 必须配置 MySQL `DATABASE_URL`，不支持无数据库模式。
- 本地项目读取依赖浏览器 File System Access API，建议使用 Chrome 或 Edge。
- 后端当前只搜索前端选中的当前活动文件，不做全项目索引或跨文件自动搜索。
- Patch 主要支持 Python 函数级新增和替换；类内部方法、多文件联动、跨语言 patch 还不是完整能力。
- 内置代码运行目前只支持 Python。
- `serve_remote.py` 当前可以接受 `stream: true`，但具体是否逐 token 输出取决于远程服务实现。

## 后续计划

- 增加多文件项目级代码搜索与修改能力
- 增加测试生成与自动运行
- 增加 patch diff 展示和冲突解释
- 增加更多语言的运行支持