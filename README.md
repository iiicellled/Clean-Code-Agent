# Clean Code Agent

`Clean Code Agent` 是一个面向本地代码修改场景的双模型代码智能体应用：主模型负责意图识别、任务规划、代码检索和审阅，远程 Clean-Code-Qwen coder 模型专注生成目标代码。后端基于 FastAPI 和 LangGraph 编排实现追问补槽、任务分流、planner-coder-review-patch 链路，前端提供会话、本地项目读取、Monaco Editor 编辑、函数级 patch 一键应用和 Python 运行验证能力。

项目基于 [iiicellled/Clean-Code-Qwen](https://github.com/iiicellled/Clean-Code-Qwen) 构建应用层能力。`Clean Code Qwen` 是一个基于 `Qwen/Qwen2.5-Coder-7B-Instruct` 进行 SFT + DPO LoRA 微调的代码模型；本项目将 merge 后的模型通过 OpenAI-compatible/vLLM 服务接入为 coder 后端，并围绕真实代码工作流补齐上下文检索、会话记忆、代码审阅、patch 提议和运行验证。

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
| 主模型 | `primary_chat_model` | 普通聊天、意图识别、槽位抽取、工作区代码检索 tool 调用、planner 计划生成、代码审阅与整理 | `PRIMARY_MODEL_URL`、`PRIMARY_MODEL_NAME`、`PRIMARY_API_KEY` |
| coder 模型 | `coder_chat_model` | 只根据 planner 结果生成目标代码；不直接调用检索 tool，不直接读取完整工作区上下文 | `CODER_MODEL_URL`、`CODER_MODEL_NAME`、`CODER_API_KEY` |

coder 模型实际部署的是 Clean-Code-Qwen merge 后的完整模型：

```text
cleancode_qwen/output_models/qwen-coder-simplifier-dpo-merged
```

开启 `MODEL_ROUTING_ENABLED=true` 且配置 `AGENT_ORCHESTRATION=langgraph` 后，后端会先调用主模型做意图识别，再根据识别结果决定进入普通聊天、追问、写代码、写函数或改函数链路。该版本建议启用模型路由；本地文件修改、函数级 patch 和追问补槽都依赖主模型的结构化决策。

### 2. 后端 Agent 流程

一次会话请求的大致流程如下：

```mermaid
flowchart TD
    A[User message] --> B[conversation_service 保存用户消息]
    B --> C[加载最近对话和当前前端工作区]
    C --> D[intent_service 调用主模型识别意图]
    D --> E{intent}
    E -->|general_chat| F[chatbot_service 调用主模型]
    F --> F1{需要检索代码?}
    F1 -->|是| F2[LangChain search_workspace tool 检索工作区]
    F2 --> F
    F1 -->|否| N1[conversation_service 保存普通助手消息]
    N1 --> N2[返回普通聊天回复]
    E -->|code intent 且缺少槽位| G[返回追问]
    E -->|code intent 且槽位完整| H[conversation_service 生成当前文件基础检索上下文]
    H --> I[planner_service 调用主模型]
    I --> I1{需要更多代码上下文?}
    I1 -->|是| I2[LangChain search_workspace tool 检索工作区]
    I2 --> I
    I1 -->|否| J[coder_service 只读取 planner 结果]
    J --> K[coder_chat_model 调用 Clean-Code-Qwen]
    K --> L[code_review_service 调用主模型审阅和整理]
    L --> M[patch_service 生成函数级 patch 提议]
    M --> N3[conversation_service 保存助手消息并返回 patch]
    N3 --> O[Web 代码区一键应用 / 保存本地文件]
```
#### LangGraph 标准编排

当前项目保留 legacy 和 LangGraph 双轨编排：

- `AGENT_ORCHESTRATION=legacy`：继续使用 `app/services/model_router_service.py` 的手写流程。
- `AGENT_ORCHESTRATION=langgraph`：使用 `app/graph/graph_service.py` 的 `StateGraph`。

LangGraph 版本不改动原业务 service，而是在 `app/graph/nodes/` 中把现有 service 包装成状态节点：

```mermaid
flowchart TD
    A[intent_node] --> B{route_after_intent}
    B -->|general_chat| C[chatbot_node]
    B -->|missing_slots| D[follow_up_node]
    B -->|code_ready| E[planner_node]
    E --> F[coder_node]
    F --> G[review_node]
    G --> H[patch_node]
    C --> I[END]
    D --> I
    H --> I
```

图状态定义在 `app/graph/state.py`，主要保存 `messages`、`workspace`、`active_task`、`decision`、`planner_messages`、`raw_code`、`content`、`executed` 和 `patch`。`conversation_service` 仍负责数据库、历史消息、任务状态同步和 API 返回，LangGraph 只负责单轮 Agent 编排。


对应的核心代码位置：

- `app/services/model_router_service.py`：决定走普通聊天、追问、代码生成还是代码审阅链路；只把 `workspace` 传给主模型聊天/tool 路径，不传给 coder 模型
- `app/services/intent_service.py`：构造意图识别 prompt，要求主模型返回严格 JSON
- `app/context/current_file_search.py`：内部代码检索组件，提供当前文件/工作区搜索、Python AST 定义块提取和片段格式化能力
- `app/tools/agent_search_tool.py`：智能体工具，使用 LangChain `StructuredTool` 封装 `search_workspace`
- `app/services/chatbot_service.py`：普通聊天服务；当用户询问代码解释、项目结构或函数行为时，主模型可通过 `search_workspace` tool 按需检索工作区
- `app/services/planner_service.py`：代码任务的主模型规划器；可通过 `search_workspace` tool 检索代码，并生成给 coder 的实现计划
- `app/services/coder_service.py`：有 planner 结果时只把 planner 结果传给 coder 模型；planner 失败时才退回旧的结构化任务/基础上下文 prompt
- `app/services/code_review_service.py`：将 coder 生成的 raw code 交给主模型复核和整理
- `app/services/patch_service.py`：把审阅后的代码整理成可一键应用的函数级 patch
- `app/services/conversation_service.py`：负责消息、上下文、任务状态、工作区传递和 patch 返回

### 3. 主模型的意图识别

主模型把当前最新用户输入压缩为一个结构化决策。意图识别阶段不会再读取旧的 20 条历史消息来猜关键词；如果当前文件下存在未完成任务，则只合并该任务的已填槽位和用户最新输入。当前意图集合包括：

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

### 4. 工作区代码搜索、上下文与工具调用

右侧代码区通过浏览器 File System Access API 读取本地项目目录。前端会在每次会话请求中提交当前打开的文件列表、`active_file`，以及当前选择的项目目录名；后端会结合 `.env` 中的 `WORKSPACE_ROOT` 定位到实际项目子目录，并在该目录内执行工作区搜索。

当前版本提供两类搜索能力：

- `app/context/current_file_search.py`：内部当前文件上下文组件，用于生成基础工作区事实。它支持 Python AST 定义块提取、缩进兜底、调用片段、关键词窗口和文件头部上下文。
- `app/context/file_search.py` + `app/tools/agent_search_tool.py`：面向主模型的 `search_workspace` 工具，用于在当前项目目录内检索文件、定位符号、查看定义和调用点。

`search_workspace` 支持三种模式：

| 模式 | 行为 |
|---|---|
| `auto` | 根据参数自动选择搜索粒度；没有指定 `file_path` 时偏向工作区概览，指定 `file_path` 时偏向定点查看。 |
| `survey` | 返回压缩后的代码地图，包括候选定义、调用点、文件位置、匹配行和 Suggested next searches，不返回大段代码。 |
| `inspect` | 在指定文件或候选范围内返回更具体的代码片段；Python 定义会优先用 AST 返回完整函数/类定义，AST 失败时使用缩进法兜底。 |

工具参数包括：

- `query`：自然语言或关键词查询。
- `file_path`：可选的相对文件路径；指定后搜索会收窄到该文件。
- `symbol` / `symbols`：优先检索的函数名、类名或方法名。
- `qualified_symbol`：限定符号，例如 `A_class.b_function`。
- `owner`：成员符号所属类或模块，例如 `A_class`。
- `max_chars` / `max_files`：控制返回结果预算。

对于 `A_class.b_function` 这类限定符号，工具会把 `A_class` 作为 owner scope，把 `b_function` 作为主要目标符号；survey 结果会优先展示最可能的定义位置，并给出下一步 inspect 建议。搜索结果会在后端日志中以醒目颜色打印，包含 mode、search root、query、file_path、qualified symbol、owner、symbols 和最终返回给模型的内容。

主模型可以在两个位置调用 `search_workspace`：

- `chatbot_service`：普通聊天、代码讲解、函数行为分析和项目问答。
- `planner_service`：代码生成/修改任务中，为 planner 生成结构化实现计划补充工作区事实。

`coder_model` 不直接调用搜索工具，也不直接读取完整工作区。正常链路下，coder 只消费 planner 生成的结构化实现计划。

### 5. Planner 如何把代码上下文转成结构化实现计划

早期版本会把当前文件检索结果直接拼进 coder prompt。当前版本改为主模型先规划、coder 后执行：`planner_service` 会阅读用户请求、意图槽位、基础当前文件上下文，并可通过 LangChain `search_workspace` tool 继续检索工作区，然后输出给 coder 使用的结构化 JSON 计划。

Planner 不负责写完整代码，而是输出稳定的 JSON 对象，通常包括：

- `target`：目标动作、文件路径、函数/类名、签名和检索符号
- `current_code_facts`：coder 可见的原始代码片段或可靠代码事实，优先包含目标函数体
- `required_changes`：必须实现的行为变化
- `constraints`：不能破坏的接口、边界和修改范围
- `implementation_notes`：可选实现提示
- `uncertainties`：上下文不足时的假设或风险
- `insufficient_context`：是否缺少足够代码上下文

Planner 用符号名、函数签名、可见代码片段和行为要求来指导 coder。若 planner 调用失败，后端仍会继续执行；这时 `planner_service` 会根据已知槽位和基础上下文生成 fallback JSON 计划，`coder_service` 继续按 planner-only 流程生成代码。

### 6. 主模型如何告知 coder 模型

主模型和 coder 模型之间通过后端的结构化中间层衔接：

1. `intent_service` 调用主模型，得到 `IntentDecision`。
2. `conversation_service` 加载当前工作区，并生成基础当前文件上下文。
3. `planner_service` 调用主模型；主模型可通过 LangChain `search_workspace` tool 进一步检索代码，并生成给 coder 的结构化 JSON 实现计划。
4. `conversation_service` 把 planner 计划追加为 system message，前缀为 `PLANNER_CONTEXT_PREFIX`。
5. `model_router_service` 在 code intent 分支调用 `coder_service.generate_code()`；此处不会把 `workspace` 传给 coder。
6. `coder_service` 如果发现 planner message，只构造 planner-only prompt：不再拼接用户完整输入、结构化槽位、`search_current_file` 结果或补丁规则。
7. `coder_chat_model` 通过 OpenAI-compatible HTTP 请求调用远程 Clean-Code-Qwen，只返回目标代码。

对于 `create_function`，planner 会在 JSON 计划中说明应新增的目标函数和签名；对于 `modify_function`，planner 会说明应替换的目标函数、当前可见代码事实、兼容性要求和边界条件。coder 模型只负责按结构化计划产出代码。

### 7. 主模型审阅与 patch 提议

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

### 8. 会话记忆与数据库

本版本必须配置 `DATABASE_URL`。没有数据库时，后端启动会失败，前端也不会退回无历史模式。

数据库保存：

- 会话标题、创建时间和更新时间
- 用户 / 助手 / system 消息，以及消息产生时的 `active_file`
- 未完成代码任务的意图、`active_file`、slots 和 missing_slots

数据库不再保存代码快照，也不保存本地文件内容。当前工作区状态来自前端当前打开的文件，并随每次会话请求提交给后端。

当前上下文策略：

- 每条用户/助手消息都会记录当轮请求对应的 `active_file`
- 加载模型上下文时，只取当前 `active_file` 相同的最近 `20` 条消息
- 当前活动文件会经过 `app/context/current_file_search.py` 的基础搜索后写入 system message，主要供 planner 使用
- 主模型在 planner/general chat 中可通过 LangChain `search_workspace` tool 继续检索工作区
- coder 模型不直接调用 tool；有 planner 计划时只读取 planner-only prompt
- 如果当前文件下存在未补全的代码任务，会从 `conversation_tasks` 中恢复已填槽位和缺失槽位
- 查找最近未完成任务时会按当前 `active_file` 过滤，避免 A 文件的缺槽任务污染 B 文件
- 意图识别只看用户最新输入和当前文件下的未完成任务，不从旧历史消息里推断关键词
- 用户在右侧代码区修改后的文件内容，会随下一轮请求一起提交给后端

这使得系统可以支持连续交互，例如：

```text
选择项目文件夹 -> 打开文件 -> 让模型修改函数 -> 应用 patch -> 保存文件 -> 运行验证 -> 继续优化
```

### 9. MySQL 数据表

配置 `DATABASE_URL` 后，SQLAlchemy 会自动创建 3 张表：

| 表名 | 作用 |
|---|---|
| `conversations` | 保存会话标题、创建时间和更新时间 |
| `messages` | 保存用户、助手和 system 消息，并记录消息对应的 `active_file` |
| `conversation_tasks` | 保存按 `active_file` 隔离的任务状态、slots 和 missing_slots |

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
        string active_file
        datetime created_at
    }
    conversation_tasks {
        int id
        int conversation_id
        string intent
        string active_file
        string status
        json slots_json
        json missing_slots_json
    }
```

### 10. Python 代码运行

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

### 11. Web 页面展示

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
    - context search helpers
    - LangChain search_workspace tool
    - implementation planning
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
      chatbot_service.py            # 普通聊天服务；主模型可按需调用 search_workspace tool
      planner_service.py            # 主模型规划代码任务，可按需调用 search_workspace tool
      coder_service.py              # 有 planner 结果时只把 planner 计划转给 coder 模型
      code_review_service.py        # 主模型审阅和整理 coder 输出
      code_runner_service.py        # Python 代码运行
      conversation_service.py       # 会话、上下文、任务状态、当前工作区和 patch 编排
      intent_service.py             # 主模型意图识别、槽位抽取、追问
      model_router_service.py       # 模型路由与任务编排
      patch_service.py              # 函数级 patch 提议
      service_configs.py            # 各服务模型参数和提示词
    context/
      current_file_search.py        # 内部当前文件上下文组件，非 agent tool
      file_search.py                # 工作区文件系统搜索、survey/inspect 和定义/窗口提取
    tools/
      agent_search_tool.py           # LangChain StructuredTool：search_workspace
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
langgraph
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
AGENT_ORCHESTRATION=langgraph
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


运行 LangGraph 路由测试：

```powershell
python -m unittest discover -s tests
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

当前 Web 前端默认调用非流式 `/chat`。`/chat/stream` 仍保留兼容旧客户端；在 `AGENT_ORCHESTRATION=langgraph` 模式下会执行非流式图并一次性返回结果。


会话请求可以携带当前代码区状态。`active_file` 会用于当前文件搜索、消息上下文过滤和未完成任务隔离：

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

会话详情和聊天响应中的消息对象会包含 `active_file`，用于标识该条消息属于哪个当前文件。流式接口结束时，如果后端成功生成 patch，会在 `done` 事件中返回：

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

- 本地项目读取依赖浏览器 File System Access API，建议使用 Chrome 或 Edge。
- 工作区搜索基于当前前端选择的项目目录和文本文件类型过滤；未打开或不在 `WORKSPACE_ROOT` 子项目中的文件不会作为当前项目上下文。
- Patch 主要支持 Python 函数级新增和替换；类内部方法、多文件联动、跨语言 patch 还不是完整能力。
- 内置代码运行目前只支持 Python。
- `serve_remote.py` 当前可以接受 `stream: true`，但具体是否逐 token 输出取决于远程服务实现。

## 后续计划

- 增加多文件项目级代码搜索与修改能力
- 增加测试生成与自动运行
- 增加 patch diff 展示和冲突解释
- 增加更多语言的运行支持
- 增加 Git diff 展示和补丁应用
