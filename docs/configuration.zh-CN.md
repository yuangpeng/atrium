# Atrium 配置指南

[English](configuration.md)

Atrium 只有一个属于自己的配置文件：`~/.atrium/settings.json`。其余都是各个 harness 自己的配置，也就是
`claude`、`codex`、`grok` 命令行和 Antigravity CLI 读取的那些文件。所以在这些命令行里能用的配置，
在 Atrium 里同样能用。

**⚙ Settings**（侧边栏底部）在编辑器里打开所有这些文件，当前项目的也在其中。保存前会先检查（JSON / TOML
格式是否正确、打开之后文件有没有在磁盘上被改过），旧版本备份到 `~/.atrium/backups/`。

一个 agent 最终用什么配置，由三处决定：

1. **这个 agent 自己的选择器**（新建 agent 页面、聊天框）：权限模式、模型、effort（推理强度）。
2. **Atrium 的 `settings.json`**：这些选择器的初始值（`harnesses.<harness>`），以及 Atrium 自身的一切：
   Tracker、Git、Code Review 这几个 agent，通知，web 工具，额外的 MCP 服务器，语音输入。
3. **harness 自己的文件**：登录、端点和中转站（relay）、MCP 服务器、hooks、指令文件；1 和 2 都是
   *Default*（`""`）时，模型和 effort 也由它决定。

权限模式永远来自 1 或 2，harness 自己配置的默认模式不起作用。

- [哪个文件管什么](#哪个文件管什么)
- [Atrium 的 settings.json](#atrium-的-settingsjson)
- [Claude Code](#claude-code)
- [Codex](#codex)
- [Antigravity](#antigravity)
- [Grok Build](#grok-build)
- [MCP 服务器](#mcp-服务器)
- [给 agent 的指令文件](#给-agent-的指令文件)
- [项目里的文件](#项目里的文件)
- [环境变量](#环境变量)
- [检查配置](#检查配置)

## 哪个文件管什么

| 要设置什么 | Claude Code | Codex | Antigravity | Grok Build |
|---|---|---|---|---|
| 登录或 API key | `claude auth login`，或 `ANTHROPIC_API_KEY` | `codex login` | `~/.gemini/antigravity-cli/settings.json` 里的 `env.GEMINI_API_KEY` | `grok login`，或 `XAI_API_KEY`；自定义模型的 `api_key` / `env_key` |
| 中转站或其他端点 | `~/.claude/settings.json` 里的 `env.ANTHROPIC_BASE_URL` | `~/.codex/config.toml` 里的 `openai_base_url` 或 `[model_providers]` | `~/.gemini/antigravity-cli/settings.json` 里的自定义模型 | `~/.grok/config.toml` 里的自定义模型（`[model.<id>]`） |
| 新 agent 的模型和 effort | Atrium 的 `harnesses.claude-code`；`""` = Claude Code 配置里的 `model` / `effortLevel` | Atrium 的 `harnesses.codex`；`""` = `config.toml` 里的 `model` / `model_reasoning_effort` | Atrium 的 `harnesses.antigravity`；`""` = 它配置文件里的 `model` | Atrium 的 `harnesses.grok`；`""` = `config.toml` 里的 `[models] default`，以及模型自己的 effort |
| 新 agent 的权限模式 | Atrium 的 `harnesses.claude-code.permission_mode` | Atrium 的 `harnesses.codex.permission_mode` | Atrium 的 `harnesses.antigravity.permission_mode` | Atrium 的 `harnesses.grok.permission_mode` |
| 哪些操作不用询问 | 权限模式，加上 Claude Code 配置里的 `permissions` 规则 | 权限模式（审批策略 + 沙箱） | 权限模式 | 权限模式，加上 `config.toml` 里的 `[permission]` 规则 |
| MCP 服务器 | Atrium 的 `mcp_servers`，加上 Claude Code 自己的 | `config.toml` 里的 `[mcp_servers]` | Atrium 的 `mcp_servers` | Atrium 的 `mcp_servers`，加上 `config.toml` 里的 `[mcp_servers]` |
| 指令文件 | `CLAUDE.md`（或 `AGENTS.md`） | `AGENTS.md` | `AGENTS.md`、`GEMINI.md`、`.agents/rules/` | `AGENTS.md`、`CLAUDE.md`、`.grok/rules/`（以及 Claude 和 Cursor 的规则） |
| 运行哪个 CLI | `claude-agent-sdk` 内置的 | 内置的，或 `harnesses.codex.binary` 指定的 | `google-antigravity` 内置的运行时 | 你自己装的（`~/.grok/bin/grok`、`PATH`），没有就用内置的；或 `harnesses.grok.binary` 指定的 |

## Atrium 的 settings.json

路径是 `~/.atrium/settings.json`（`$ATRIUM_HOME/settings.json`）：在 ⚙ Settings → Atrium 里编辑，或者用任何编辑器。

- **改了不用重启。** 文件一变，Atrium 就会重新读取。什么时候看到效果，取决于这项设置管什么：`harnesses`
  的默认值给之后新建的 agent 用，Tracker 在下次运行时读取自己的设置，依此类推。已打开的页面只在加载时和
  ⚙ Settings 保存之后读取设置：在别的编辑器里改了文件，要刷新页面，否则新建 agent 页面给的还是旧的默认值。
- **你的文件按 key 合并到内置默认值之上**：文件里有的 key 以文件为准，没有的 key 用默认值。
- **首次启动时会把全部默认值写进文件。** 这些副本固定了当时的值：之后的 Atrium 版本改了某个默认值，你的
  文件里还是旧值。删掉一个 key，就会用当前版本的默认值。首次启动之后才新增的配置段（比如 `review_agent`）
  不在你的文件里，在你手动加上之前按默认值运行。
- **不是合法 JSON 的文件会被忽略**：继续用上一次读到的正确配置，`/api/meta` 在 `settings._error` 里给出
  错误。⚙ Settings 不会保存不合法的 JSON。
- 文件里有密钥（web 工具的 key、语音输入的 key），所以创建时权限是 600。Atrium 环境变量里的
  `SERPER_API_KEY` 和 `JINA_API_KEY` 优先于文件。

默认值：

```json
{
  "defaults": {"harness": "claude-code"},
  "harnesses": {
    "claude-code": {"permission_mode": "bypassPermissions", "model": "", "effort": ""},
    "codex": {"enabled": true, "binary": "", "permission_mode": "full-access", "model": "", "effort": ""},
    "antigravity": {"enabled": true, "permission_mode": "always-proceed", "model": "", "effort": "high"},
    "grok": {"enabled": true, "binary": "", "permission_mode": "always-approve", "model": "", "effort": ""}
  },
  "sessions": {"idle_close_minutes": 30},
  "notifications": {"enabled": true, "permission": true, "turn_end": true, "sound": true},
  "tracker": {"enabled": true, "harness": "claude-code", "model": "", "effort": "low", "interval_seconds": 300, "after_turn_seconds": 45},
  "git_agent": {"harness": "claude-code", "model": ""},
  "review_agent": {"auto": true, "harness": "claude-code", "model": "", "effort": "", "language": "", "delay_seconds": 15, "max_concurrent": 2},
  "web": {"serper_api_key": "", "jina_api_key": "", "replace_builtin_web_tools": true},
  "mcp_servers": {},
  "voice": {"base_url": "", "api_key": "", "asr_model": "", "refine_model": "", "refine_effort": "low"},
  "lsp": {"enabled": true, "commands": {}, "python": {"typeCheckingMode": "standard"}},
  "terminal": {"enabled": true, "shell": ""},
  "capture": {"enabled": true, "max_mb": 1024},
  "ui": {"theme": "system"}
}
```

### 新 agent 的初始值

新建 agent 页面的初始选项。那里和聊天框里的选择器可以逐个 agent 修改。

| Key | 默认值 | 说明 |
|---|---|---|
| `defaults.harness` | `"claude-code"` | 新建 agent 页面默认选中的 harness：`claude-code`、`codex`、`antigravity` 或 `grok`。 |
| `harnesses.<harness>.permission_mode` | 见[下文](#权限模式) | 新 agent 的初始权限模式。 |
| `harnesses.<harness>.model` | `""` | `""` = harness 自己的默认值（来自它的配置文件）。Claude Code：`opus`、`sonnet`、`haiku` 或模型 id；Codex：它模型列表里的模型；Antigravity：模型 id，或自定义模型的 key 或 label；Grok Build：`grok models` 列出的模型，自定义模型的 key 也在其中。 |
| `harnesses.<harness>.effort` | `""`；Antigravity 为 `"high"` | `""` = harness 自己的默认值。Claude Code：`low`、`medium`、`high`、`xhigh`、`max`；Codex：模型支持的档位（`low` … `xhigh`）；Antigravity：Gemini 的思考等级，`minimal` … `high`。Antigravity 写 `""` 时不发送思考配置，Gemini 也就不返回思考内容：聊天里看不到，Tracker 也读不到（见[下文](#antigravity)）；Grok Build：模型自己的档位，只对支持 effort 的模型有效（见[下文](#grok-build)）。 |
| `harnesses.codex.enabled`、`harnesses.antigravity.enabled`、`harnesses.grok.enabled` | `true` | `false` 关闭这个 harness。 |
| `harnesses.codex.binary` | `""` | 运行哪个 Codex CLI。`""`（或 `"codex"`）= Atrium 内置的，没有的话用 `PATH` 里的 `codex`；写一个路径（比如 `/opt/homebrew/bin/codex`）就用那一个。Codex 的 app-server 重新启动后才生效：重启 Atrium。 |
| `harnesses.grok.binary` | `""` | 运行哪个 Grok CLI。`""`（或 `"grok"`）= 你自己装的那个（xAI 安装脚本放的 `~/.grok/bin/grok`，没有的话 `PATH` 里的 `grok`），都没有就用 Atrium 内置的；写一个路径就用那一个。对之后启动的 agent 生效。 |

### 权限模式

| Harness | `permission_mode` | 选择器里显示 | 行为 |
|---|---|---|---|
| Claude Code | `default` | Ask permissions | 编辑文件和执行命令前询问（`permissions` 规则允许的除外）。 |
| | `acceptEdits` | Accept edits | 编辑文件不问，执行命令前询问。 |
| | `plan` | Plan mode | 只读和做计划，不做任何修改。 |
| | `auto` | Auto (classifier) | 由分类器逐个批准或拦下操作。 |
| | `bypassPermissions` | Bypass permissions | 从不询问。默认值。 |
| Codex | `auto` | Ask for approval | 在只能写工作区的沙箱里运行，需要更多权限时询问。 |
| | `auto-review` | Approve for me | 同上，但由 Codex 的自动审核代替你回答。 |
| | `full-access` | Full access | 没有沙箱，从不询问。默认值。 |
| Antigravity | `request-review` | Request review | 随意读取；编辑、执行命令和调用 MCP 工具前询问。 |
| | `accept-edits` | Accept edits | 在 agent 自己的目录里编辑不问，其余询问。 |
| | `always-proceed` | Always proceed | 从不询问。默认值。 |
| Grok Build | `ask` | Ask | 读取和 Grok 认为安全的命令直接运行；编辑和其他操作前询问。 |
| | `auto` | Auto | 由 Grok 的分类器批准安全的操作，有风险的询问。 |
| | `always-approve` | Always approve | 从不询问；`config.toml` 里的 deny 规则仍然有效。默认值。 |

Git Agent 和 Code Review Agent 有自己的模式，不受这些设置影响：它们只读，Git Agent 做其他任何事之前都会询问。

### 会话与通知

| Key | 默认值 | 说明 |
|---|---|---|
| `sessions.idle_close_minutes` | `30` | agent 空闲这么久后停止它的进程。对话还在；下一条消息会重新启动进程，用那时的配置。 |
| `notifications.enabled` | `true` | 桌面通知总开关；聊天正开在可见且聚焦的标签页里的 agent 不发通知。 |
| `notifications.permission` | `true` | agent 请求批准或向你提问时通知。 |
| `notifications.turn_end` | `true` | agent 一轮结束时通知。 |
| `notifications.sound` | `true` | 通知带声音。 |

通知怎么送到桌面（osascript、terminal-notifier、notify-send）见 [README](../README.md#where-things-live)。

### Tracker、Git Agent 与 Code Review Agent

**Tracker Agent**（每个项目一个）负责更新看板（board）上的状态行、标题和时间线，并给新 agent 起名字。

| Key | 默认值 | 说明 |
|---|---|---|
| `tracker.enabled` | `true` | `false`：只在你要求时运行。 |
| `tracker.harness` | `"claude-code"` | 用哪个 harness 调用模型；这个 harness 不可用时改用 Claude Code。 |
| `tracker.model` | `""` | `""` = 该 harness 的默认模型。小而快的模型就够了（`haiku`）。 |
| `tracker.effort` | `"low"` | |
| `tracker.interval_seconds` | `300` | 多久检查一次新动态（至少 30 秒）。没有变化时不调用模型。 |
| `tracker.after_turn_seconds` | `45` | 一轮结束后多久运行。 |

**Git Agent** 是 Git 面板里的聊天。这些设置在项目的 Git Agent 创建时读取；之后它一直是同一个对话，每次打开都接着之前的继续。

| Key | 默认值 | 说明 |
|---|---|---|
| `git_agent.harness` | `"claude-code"` | `""` = `defaults.harness`。 |
| `git_agent.model` | `""` | `""` = `harnesses.<harness>.model`。 |

**Code Review Agent** 在单独的只读会话里审查每个 agent 的工作。它的 harness、模型和 effort 在某个 agent
第一次被审查、分配到审查者时读取；之后各轮都交给同一个审查者。

| Key | 默认值 | 说明 |
|---|---|---|
| `review_agent.auto` | `true` | agent 报告任务 `done` 或 `waiting`（等你处理）时，自动审查它的新改动。抽屉里的 *Auto* 勾选框可以对单个 agent 覆盖这项设置。 |
| `review_agent.harness` | `"claude-code"` | `""` = `defaults.harness`。 |
| `review_agent.model`、`review_agent.effort` | `""` | `""` = `harnesses.<harness>.model` / `effort`。 |
| `review_agent.language` | `""` | 审查者用什么语言写；`""` = 跟你用的语言一样。 |
| `review_agent.delay_seconds` | `15` | 一轮结束后多久开始审查；到时 agent 又忙起来就跳过。 |
| `review_agent.max_concurrent` | `2` | 同时运行的自动审查数量上限。手动点 *Review* 不用排队。 |

### Web 工具与 MCP 服务器

这些设置对之后启动的 agent 生效（包括 `sessions.idle_close_minutes` 之后被重新唤醒的 agent）。

| Key | 默认值 | 说明 |
|---|---|---|
| `web.serper_api_key` | `""` | `web.search` 用的 [Serper](https://serper.dev) key。`SERPER_API_KEY` 优先。 |
| `web.jina_api_key` | `""` | 可选的 [Jina Reader](https://jina.ai/reader) key，提高 `web.fetch` 的频率限制。`JINA_API_KEY` 优先。 |
| `web.replace_builtin_web_tools` | `true` | 让 agent 用 Atrium 的 web 工具代替自带的：Claude Code 去掉 WebFetch，有 Serper key 时再去掉 WebSearch；Antigravity 同样去掉 `read_url_content` 和 `search_web`。Codex 自带的网页搜索在它的 `config.toml` 里设置；Grok Build 保留它的 `web_search`。 |
| `mcp_servers` | `{}` | 给 Claude Code、Antigravity 和 Grok Build agent 用的 MCP 服务器：见 [MCP 服务器](#mcp-服务器)。 |

### 语音输入

发送按钮旁边的麦克风（agent 的聊天框、Git 和 Code Review 抽屉、新建 agent 页面都有）把你说的话变成你想打的
那条消息。它向同一个 OpenAI 兼容的服务器发两次模型请求：

1. **语音转文字。** 录音（16 kHz 单声道 WAV）发到 `<base_url>/v1/audio/transcriptions`。服务器在那里不提供
   的模型（返回 400、404、405、415 或 422）——比如能听音频的聊天模型 Doubao Seed——改走
   `/v1/chat/completions`，录音作为 `input_audio` 发送，并带上 `reasoning_effort: "minimal"`：推理模型否则会
   先想上一分钟（不接受这个参数的模型会去掉它再请求一次）。一个模型走哪条路，在它第一次录音时试出来，之后
   一直记着，直到 Atrium 重启。
2. **Refine。** 转写结果连同 agent 看到的上下文发到 `<base_url>/v1/responses`：上次压缩（compaction）以来的
   对话，以压缩摘要开头，最多送最近约 60,000 字符（约 15k token）：超出时从最早的部分开始去掉，压缩摘要最先
   被去掉。其中去掉 thinking，工具调用和结果都缩短，agent 在你上一条消息之后的回复保留得更长。还有它在看板
   上的卡片（转成文字）、看板上显示的其他 agent（不含已归档的）的名字和正在做的事、它工作目录里的文件
   （`git ls-files`），以及输入框里已有的草稿。
   模型写出你想表达的意思，而不是逐字整理：去掉口头禅和说错的话，改口（“不对，……”）以改正后的意思为准，
   “这个”“第二个”这类指代在上下文能确定时写明指的是哪个（文件路径、函数名、列表项的序号和要点）。同时符合
   两样东西时，提示词要求保留原话，但模型常常会自己选一个：发送前请核对，不对就切回 *As heard*。
   路径、标识符、命令按项目和对话里的写法写出，纠正听错的词，把英文改通顺，保留你这次说的语言。
   你没说的要求和细节不会从上下文里补进来，“好的，就这么办”这样的短回复保持原样。新建 agent 页面还没有对话：
   上下文就是项目的文件、看板和草稿。

文字插入到草稿里光标所在的位置，发送前你可以先看、再改。在那之前，输入框底栏上方的提示条可以在改写后的
文字和听到的原文之间切换（*As heard* / *Refined*）；refine 失败时，直接放入听到的原文。录音 5 分钟后自动停止。

| Key | 默认值 | 说明 |
|---|---|---|
| `voice.base_url` | `""` | 服务器地址，带不带 `/v1` 都行：`https://api.openai.com` 和 `https://api.openai.com/v1` 是一样的。 |
| `voice.api_key` | `""` | 以 `Authorization: Bearer …` 发送；为空时不发。`/api/meta` 只告诉浏览器它是否已设置；⚙ Settings 显示的是文件原文（和 web 工具的 key 一样）。 |
| `voice.asr_model` | `""` | 语音转文字的模型，例如 `gpt-4o-transcribe`，或能听音频的聊天模型。`""` = 没有麦克风。 |
| `voice.refine_model` | `""` | 改写转写结果的模型，走 Responses API。`""` = 直接放入听到的原文。 |
| `voice.refine_effort` | `"low"` | 它的推理强度，取模型支持的值（`minimal`、`low`、`medium`、`high`……）；low 让听写保持快速。不带推理的模型拒绝这个参数时，会去掉它再请求一次，之后直到 Atrium 重启都不再发送；`""` = 一开始就不发送。 |

推荐配置：用 Doubao Seed 2.1 Lite 听写（它走 chat 那条路），用 gpt-6-luna 以 `low` 改写：

```json
"voice": {
  "base_url": "https://<你的服务器>",
  "api_key": "…",
  "asr_model": "doubao-seed-2-1-lite-260915",
  "refine_model": "gpt-6-luna",
  "refine_effort": "low"
}
```

在一台这样的服务器上实测：13 秒的录音，转写 3–5 秒，改写 2–3 秒。按后缀路由模型的服务器（`<模型>:<路由>`）
这里要写它自己的模型名。

浏览器只把麦克风给安全的页面：通过 HTTPS 打开的 Atrium，或 `localhost`。在手机上以 `http://<地址>` 打开时，
麦克风会提示这一点，而不会录音。第一次录音时浏览器会请求麦克风权限；如果被拒绝了，到浏览器里这个网站的
设置中允许它。

### 编辑器与界面

| Key | 默认值 | 说明 |
|---|---|---|
| `lsp.enabled` | `true` | 编辑器背后的语言服务器。 |
| `lsp.commands` | `{}` | 按语言指定服务器，代替自动找到的那个：写一个程序（名字或路径，不带参数），或参数列表，例如 `{"c": "clangd", "python": ["pyright-langserver", "--stdio"]}`。支持的语言：`python`、`c`、`cpp`、`go`、`rust`。 |
| `lsp.python.typeCheckingMode` | `"standard"` | `off`、`basic`、`standard`、`strict`、`recommended` 或 `all`。 |
| `ui.theme` | `"system"` | 不会被读取：侧边栏主题开关的选择由浏览器记住。 |

`lsp` 的修改对之后启动的语言服务器生效。

### 终端

| Key | 默认值 | 说明 |
|---|---|---|
| `terminal.enabled` | `true` | 工作区的 Terminal 标签页：在运行 Atrium 的机器上、以你的身份、在项目目录或某个 worktree 里开的 shell。关掉后标签页消失，不能再新开或连接终端；已经开着的会一直运行到 Atrium 重启。 |
| `terminal.shell` | `""` | `""` 是你的 shell（`$SHELL`），以登录 shell 方式启动：会读你的 profile，所以 `PATH` 和你在终端 app 里的一样。写一个 shell 的名字或路径（如 `"/opt/homebrew/bin/fish"`）也按同样方式启动；写参数列表（如 `["/bin/bash", "--noprofile"]`）则原样运行。 |

终端运行在服务端：刷新页面、手机断线重连、或换一台设备，连上的都是同一个 shell，并先收到它最近的输出
（最后 1 MiB，连同其中程序设置的终端模式）。最后用过的那个页面（刚连上、打过字、改过大小或被点进去的）
决定终端的大小，也由它回答程序向终端提出的查询，和 tmux 一样。关掉它的标签页、shell 退出（用 `exit`；
以其他方式结束的会留着标签页供查看）、或 Atrium 停止时，终端才结束。shell 像新开的终端窗口一样从干净的
环境启动，由你的 profile 设置：从 Atrium 自己的环境变量里只带上你是谁（`HOME`、`USER`…）、locale、
`TMPDIR`、`SSH_AUTH_SOCK`、代理变量和 `ATRIUM_HOME`，不带启动 Atrium 的那个环境里的东西（某个 agent
的会话、tmux、virtualenv）。另外设置 `TERM=xterm-256color`、`TERM_PROGRAM=Atrium`，没有设置 locale 时
补上 UTF-8 的 `LANG`。

### 请求抓取

工作区的 **Requests** 标签页逐次显示每个 agent 发给模型 API 的请求和收到的响应。每次调用都会结构化展开：
参数、系统提示词、工具和消息（比上一次调用新增的会标出来），以及模型的回答。另外还能看响应流里的每个事件，
以及原样的请求体和响应体。

| Key | 默认值 | 说明 |
|---|---|---|
| `capture.enabled` | `true` | 让 agent 的模型调用经过 Atrium 的抓包代理并记录下来。对之后启动进程的 agent 生效：新建的，以及在 `sessions.idle_close_minutes` 之后或 Atrium 重启之后被唤醒的。关掉后不再记录，新的 agent 直接调用自己的 API。 |
| `capture.max_mb` | `1024` | 记录下来的调用在 `~/.atrium/captures/captures.db` 里最多占多少（压缩后）；超出时先删最早的，删到 90%。压缩调用，以及每次压缩前 agent 的最后一次调用，单独保留：不计入这个上限，也不会被删（见[下文](#请求抓取)）。`0` = 不限。 |

Atrium 在一个自己的回环端口上运行这个代理（只绑 `127.0.0.1`，不管 Atrium 自己在什么地址上服务），并把每个
agent 的 base URL 指向它。代理把每个请求原样转发到 harness 本来要发往的地址（同样经过 `HTTPS_PROXY`），
响应边收边转：流式的仍然是流式。

- 它信任的 CA 比 harness 自己的更多：系统的（macOS 钥匙串、Windows 证书库、OpenSSL 的路径）、certifi 的，以及
  给 harness 配置的（`NODE_EXTRA_CA_CERTS`、`CODEX_CA_CERTIFICATE`、`SSL_CERT_FILE`、`SSL_CERT_DIR`）。它还会出示
  Claude Code 的客户端证书（`CLAUDE_CODE_CLIENT_CERT`、`CLAUDE_CODE_CLIENT_KEY`）。复现不了的配置（比如读不到的 CA
  文件）就不抓包：那个 agent 直接调用它的 API。
- 写盘之前先抹掉凭证：`Authorization`、`x-api-key`、`x-goog-api-key`、cookie、名字表明是凭证的请求头（`key`、
  `token`、`secret`、`auth` 等）、`ANTHROPIC_CUSTOM_HEADERS` 或 Codex provider 的 `http_headers` /
  `env_http_headers` 添加的头，以及 URL 里的 `key=`。请求体和响应体完整保留，里面有整段对话，包括 agent 读过的
  文件。数据库只有你自己能读。
- 转发时只改一处：如果 `Accept-Encoding` 里提供了 br 或 zstd，会改成只提供 gzip 和 deflate，这样响应才能被解开
  查看（Request 视图里能看到 harness 原本提供的值）。
- 删除 agent 时会一起删除它的调用记录；标签页上的垃圾桶按钮也能删除。
- **压缩相关的调用会保留。**每次调用都带着到当时为止的整段对话，所以旧调用的内容大多也在后面的调用里；直到一次
  压缩：从那以后，调用里带的是总结。所以压缩调用（harness 让模型写这份总结的那次调用，里面是整段对话；标签页里
  标为 *Compact*）和压缩前 agent 的最后一次调用都会保留（📌）：不计入 `capture.max_mb`，清理时也跳过它们，只有
  删除 agent 或它的调用记录时才会删掉。如果压缩改写了对话的开头（Claude Code 在提示过长之后的压缩：去掉图片、
  丢掉最早的消息），就改为保留同一线程里 agent 的最后一次调用。从 Atrium 移除项目后，这个项目的保留调用变回
  普通调用；Atrium 已经找不到的会话（transcript 在别处被删了），在最后一次调用一周之后也一样。
  Atrium 看各家 harness 加进对话的那段指令是怎么开头的来认出压缩（引用它、前面还有别的话的消息不算）：Claude Code
  的 "CRITICAL: Respond with TEXT ONLY… Your task is to create a detailed summary of the conversation so far"（或它
  专门用来总结的系统提示词）、Codex 的 "You are performing a CONTEXT CHECKPOINT COMPACTION"（或调用
  `/responses/compact`）、Antigravity 的 "You have been working on the task described above… Write a continuation
  summary"。harness 换了说法的版本，要等 Atrium 跟着更新后才认得出来。
- **agent 启动的进程。** Claude Code agent 的环境里，`ANTHROPIC_BASE_URL` 是这个代理，所以它启动的进程（比如跑
  `claude -p` 的脚本）会继承下来，它们的调用记在这个 agent 名下。它们还会拿到 `ATRIUM_CAPTURE_URL`（代理的地址）和
  `ATRIUM_CAPTURE_UPSTREAM`（它代替的地址，`""` = Anthropic 的 API）。这样的进程可能比 Atrium 活得久：代理在端口
  空闲时会再用同一个端口，路由保留 90 天，所以 Atrium 重启后它们的调用照样能用。从 agent 里启动的 Atrium（比如测试
  实例）会先把自己的环境改回 `ATRIUM_CAPTURE_UPSTREAM`：它自己的 agent 经它自己的代理调用 API，而不是经过两层代理。

哪些调用会经过代理：

- **Claude Code**：发往 `ANTHROPIC_BASE_URL`（或 Anthropic API）的全部调用，包括 agent 的主循环、它的子 agent，
  以及会话标题之类的辅助调用。用 Bedrock、Vertex 或 Foundry（`CLAUDE_CODE_USE_*`）时不经过。
- **Codex**：thread 发给自己模型 provider 的调用，前提是这个 provider 是你自己定义的（`[model_providers]`）。
  内置的 OpenAI provider（ChatGPT 登录、`openai_base_url`），以及设置了 `supports_websockets` 的 provider，
  都直接调用。
- **Antigravity**：发往 Gemini API 端点的调用，不管是自定义模型的中转站还是 Google 自己的。Vertex AI 不经过。
- **Grok Build**：不记录。模型的调用发到哪里只由它的 `config.toml` 决定，没法给单个 agent 的进程换一个地址
  （CLI 的覆盖层 `GROK_CONFIG` 不允许设置模型的 `base_url`）。

harness 发出的其他流量不会被记录：遥测、更新检查、走 HTTP 的 MCP 服务器、Codex 拉取的插件、网页。

## Claude Code

Atrium 通过 `claude-agent-sdk` 运行 Claude Code，SDK 内置了 CLI：不需要另外安装。

Atrium 里的 Claude Code agent 会加载 Claude Code 的全部配置文件，和 `claude` 命令行一样（越具体的文件优先级越高）；
如果你的组织下发了托管配置（managed settings），也会加载：

| 文件 | 作用范围 |
|---|---|
| `~/.claude/settings.json`（`$CLAUDE_CONFIG_DIR/settings.json`） | 你自己，所有项目 |
| `<project>/.claude/settings.json` | 项目里的所有人（提交到仓库） |
| `<project>/.claude/settings.local.json` | 你自己，仅这个项目（不要提交） |

**登录。** 内置的 CLI 用你现有的 Claude Code 登录。还没登录的话，在 Atrium 的代码目录里执行：

```bash
CLAUDE=$(.venv/bin/python -c 'import claude_agent_sdk, os; print(os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude"))')
$CLAUDE auth login
$CLAUDE auth status
```

也可以用 API key：设成 Atrium 的环境变量 `ANTHROPIC_API_KEY`，或写进 `~/.claude/settings.json` 的 `env`。

**中转站或网关**写在 `~/.claude/settings.json` 的 `env` 里：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://relay.example.com",
    "ANTHROPIC_AUTH_TOKEN": "...",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "relay-opus"
  },
  "model": "opus"
}
```

`ANTHROPIC_AUTH_TOKEN` 以 `Authorization: Bearer …` 发送，`ANTHROPIC_API_KEY` 以 `x-api-key` 发送。
`ANTHROPIC_DEFAULT_OPUS_MODEL`（以及 `…_SONNET_…`、`…_HAIKU_…`）把别名映射成中转站的模型名。模型选择器里的
*Default* 显示 `ANTHROPIC_MODEL`（没有的话是 `model`）实际解析成的模型。

**Atrium 为每个 agent 设置的内容**（覆盖你的配置）：

- **权限模式**，总是由 Atrium 设置：`permissions.defaultMode` 不起作用。你的 `allow` / `ask` / `deny` 规则仍然有效。
- **模型**和 **effort**，仅当这个 agent 选了具体值时。*Default* 用你配置里的值：上面说的模型，以及
  `env.CLAUDE_CODE_EFFORT_LEVEL`，没有的话是 `effortLevel`。选定的 effort 优先于这两者。
- **增加**：追加到 Claude Code 系统提示词末尾的看板协议；Atrium 的工具（`mcp__board__*`、`mcp__web__*`）；
  Atrium `mcp_servers` 里的服务器；`~/.atrium/uploads`（附件）的读取权限。
- **去掉**：开启 `web.replace_builtin_web_tools` 时去掉 WebFetch；有 Serper key 时再去掉 WebSearch。
- **API 地址**，在 `capture.enabled` 开启时：`ANTHROPIC_BASE_URL` 指向 Atrium 的抓包代理，再由代理把每次调用
  转发到你配置里的地址（见[请求抓取](#请求抓取)）。`NO_PROXY` 会加上回环地址，好让 CLI 直接连到代理；
  `ATRIUM_CAPTURE_URL` / `ATRIUM_CAPTURE_UPSTREAM` 记下替换了什么。调用发往 Anthropic 自己的 API 时，还会设
  `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1`：否则 CLI 会把任何别的 base URL 当成网关，不再发送只给 Anthropic
  API 的内容（`cch=` 之类的 billing 字段、`diagnostics` 字段）。

其他一切（环境变量、hooks、插件、skills、Claude Code 自己的 MCP 服务器）都和在命令行里一样。Tracker 用
Claude Code 调用模型时只读 `~/.claude/settings.json`（那里配置的中转站同样生效），也不带任何工具。

修改对之后启动的 agent 生效。参考：[Claude Code 设置](https://code.claude.com/docs/zh-CN/settings)。

## Codex

Atrium 运行自带的 Codex CLI（`openai-codex-cli-bin`，版本固定在 `pyproject.toml` 里），形式是
`codex app-server`：所有 Codex agent 共用一个进程。

| 文件 | 作用范围 |
|---|---|
| `~/.codex/config.toml`（`$CODEX_HOME/config.toml`） | 你自己，所有项目 |
| `<project>/.codex/config.toml` | 这个项目，被信任后才生效 |

Codex 只有在你信任项目之后才读项目里的文件。在项目目录里运行一次 `codex` 会询问，并把你的选择记到
`config.toml`；也可以自己加：

```toml
[projects."/Users/me/code/app"]
trust_level = "trusted"
```

项目里的文件不能修改服务商或凭据（`model_provider`、`model_providers`、`openai_base_url`、profiles 等）。

**登录**，在 Atrium 的代码目录里用内置 CLI：

```bash
CODEX=$(.venv/bin/python -c 'import codex_cli_bin as c; print(c.bundled_codex_path())')
$CODEX login                                          # 用 ChatGPT 账号在浏览器里登录（--device-auth：用设备码登录）
printenv OPENAI_API_KEY | $CODEX login --with-api-key
$CODEX login status
```

**中转站**（兼容 OpenAI 的 Responses API）：把内置服务商指向它，

```toml
openai_base_url = "https://relay.example.com/v1"
```

或者单独定义一个服务商：

```toml
model = "relay-model"
model_provider = "relay"

[model_providers.relay]
name = "我的中转站"
base_url = "https://relay.example.com/v1"
env_key = "RELAY_API_KEY"
wire_api = "responses"
```

`env_key` 是存放 key 的环境变量名。Codex 从自己的环境里读取它，也就是 Atrium 的环境：启动 Atrium 之前先 export。

**Atrium 为每个 agent 设置的内容：**

- **权限模式**，转换成 Codex 的审批策略和沙箱：`auto` = `on-request` + `workspace-write`；`auto-review` =
  同上，由 Codex 的自动审核回答；`full-access` = `never` + `danger-full-access`。`config.toml` 里的
  `approval_policy` 和 `sandbox_mode` 不起作用。
- **模型**和 **effort**，仅当这个 agent 选了具体值时。*Default* 用 `config.toml` 里的 `model` /
  `model_reasoning_effort`。
- **增加**：Atrium 的说明（作为 developer instructions），以及它的工具（作为 Codex 的 dynamic tools）：
  `board.*`，以及 `internet.search` / `internet.fetch`（Codex 保留了 `web` 这个名字）。
- Atrium 的 `mcp_servers` **不会**传给 Codex：Codex 的 MCP 服务器来自 `config.toml` 里的 `[mcp_servers]`。
- **provider 的地址**，在 `capture.enabled` 开启时：thread 的 `model_providers.<id>.base_url` 指向 Atrium 的
  抓包代理，再由代理转发到你的地址（见[请求抓取](#请求抓取)）。app-server 运行时，`NO_PROXY` 里会加上回环地址。

修改对之后启动的 agent 生效。

**用你自己的 Codex CLI**：`"harnesses": {"codex": {"binary": "/opt/homebrew/bin/codex"}}`，然后重启 Atrium。
Atrium 用的是固定版本的 app-server 协议，而这个协议变化频繁：版本别差太远。`/api/meta` 会显示实际运行的
CLI（`cli.path`、`cli.version`、`cli.bundled`）。

参考：[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、
[服务商与项目配置文件](https://learn.chatgpt.com/docs/config-file/config-advanced)（英文）。

## Antigravity

Atrium 通过 Google Antigravity 的 SDK `google-antigravity` 运行它，各平台的 wheel 里都带着 agent 运行时：
不需要另外安装。模型请求发到哪里（模型、中转站、key）由 Antigravity CLI 自己的配置文件
`~/.gemini/antigravity-cli/settings.json` 决定（⚙ Settings → Antigravity），Atrium 读取这个文件。

**Gemini API key** 写在 `env` 里，可以从 [Google AI Studio](https://aistudio.google.com/app/api-keys) 获取，
也可以用中转站的：

```json
{
  "modelProvider": "gemini",
  "model": "gemini-3.8-flash",
  "env": {"GEMINI_API_KEY": "..."}
}
```

用 Google 官方 API 时，Atrium 环境变量里的 `GEMINI_API_KEY` 也可以。CLI 本身只从环境变量读 key；`env`
这一段是给 Atrium 用的，Atrium 只从中读取本节说到的 key、端点和 Vertex AI 变量。其他变量（比如代理）要放在
Atrium 的环境变量里。Antigravity 的条款规定 Google 账号登录只能用于 Google 自己的应用，所以 Atrium 不使用它。

**中转站**（兼容 Gemini API）就是一个自定义模型，写法和 CLI 写入的一样：

```json
{
  "modelProvider": "gemini",
  "customModelsConfig": {"customModels": {"my-model": {
    "modelName": "gemini-api://relay.example.com/gemini/models/<model>", "label": "我的模型"}}},
  "model": "我的模型",
  "env": {"GEMINI_API_KEY": "..."}
}
```

请求发到 `<base_url>/v1beta/models/<model>:streamGenerateContent`，key 放在 `x-goog-api-key` 里。
`gemini-api://` 后面的主机不写协议时表示 `https://`；本地服务写成 `gemini-api://http://127.0.0.1:<port>/models/<model>`。
如果漏了 `gemini-api://` 前缀，`modelName` 会被当成 Google API 上的模型 id，这个文件里的 key 就会发给 Google。
自定义模型还可以设置：

- `maxTokens`：上下文窗口大小（token 数），用于上下文用量指示（默认 1,048,576）。
- `modelFeatures.supportsThinking: false`：不发送思考等级；effort 选择器不起作用。
- `modelFeatures.supportsImages: false`：附加的图片只传路径，不作为图片发送。

**用哪个模型**：这个 agent 选的模型（新 agent 用 `harnesses.antigravity.model`），否则用文件里的 `model`，
再否则用 `gemini-3.8-flash`。每一处都可以写自定义模型（key 或 label）、模型 id，或 CLI 的显示名
（`"Gemini 3.8 Flash (High)"`）；括号里的思考等级不起作用：effort 用 Atrium 的选择器或
`harnesses.antigravity.effort` 设置。模型选择器里列出 *Default*（文件里的 `model`）和各个自定义模型。

**key 发给谁：**

- 在这个文件里设置的中转站（自定义模型，或 `env` 里的 `GOOGLE_GEMINI_BASE_URL`）只会拿到这个文件里的
  `env.GEMINI_API_KEY`，绝不会用 Atrium 环境变量里的 `GEMINI_API_KEY`。没有的话，Atrium 会直接提示，而不是
  启动。不需要 key 的中转站也要填一个占位值：没有 key，运行时不会启动。
- Google 的 Gemini API（比如 `gemini-3.8-flash` 这样的模型 id），或者在 Atrium 环境变量里设置的
  `GOOGLE_GEMINI_BASE_URL`，用这个文件里的 `env.GEMINI_API_KEY`，没有的话用环境变量里的。所以如果这个文件里
  放的是中转站的 key，选 Google 的模型时这个 key 会被发给 Google。

**Gemini Enterprise（Vertex AI）**：在 `env` 里写 `"GOOGLE_GENAI_USE_VERTEXAI": "true"`，然后二选一：指定
项目（`env` 里的 `GOOGLE_CLOUD_PROJECT` 和 `GOOGLE_CLOUD_LOCATION`，后者默认 `global`；或者顶层的
`"gcp": {"project": …, "location": …}`），配合 Application Default Credentials
（`gcloud auth application-default login`）；或者只写 `GOOGLE_API_KEY`，用 express 模式。
`GOOGLE_VERTEX_BASE_URL` 可以修改端点。这些变量也可以来自 Atrium 的环境变量。自定义模型仍然发往它自己的端点。

**Atrium 为每个 agent 设置的内容：**

- **权限模式**，由 Atrium 自己的拦截检查实现：CLI 的 `permissions`、`toolPermission` 和 `agentMode` 不会被读取。
- **模型**和**思考等级**：agent 的 effort 作为 Gemini 的 `thinkingLevel` 发送（同时带 `includeThoughts`），
  中途修改从下一轮生效。effort 为 `""`（哪里都没选）时不发送任何思考配置：模型按自己的默认等级思考；没有
  `includeThoughts`，Gemini API 也不会返回思考内容：聊天里看不到，Tracker 也读不到，而 Gemini 在工具调用之间
  很少写别的文字。所以 `harnesses.antigravity.effort` 默认是 `high`。
- **增加**：Antigravity 自家应用会加载的指令文件（见[下文](#给-agent-的指令文件)），因为 SDK 的运行时不加载
  它们；Atrium 的工具，命名为 `<namespace>_<tool>`（`board_status`、`web_search` 等）；Atrium `mcp_servers`
  里的服务器（Antigravity 的 `mcp_config.json` 文件不会被读取）。
- **去掉**：开启 `web.replace_builtin_web_tools` 时去掉 `read_url_content`；有 Serper key 时再去掉 `search_web`。
- **端点地址**，在 `capture.enabled` 开启时：Gemini 端点的 base URL 指向 Atrium 的抓包代理，再由代理转发到你的
  地址（见[请求抓取](#请求抓取)）。

修改对之后启动的 agent 生效。Antigravity 是否可用、它的模型列表会立即更新（已打开的页面要在 ⚙ Settings
保存之后或刷新后才看到）。它的对话保存在 `~/.atrium/antigravity/`，没有 CLI 能列出它们。fork 会在那里以新 id
复制一份对话（运行时的状态数据库和 transcript，截到 fork 点）；修改已发送的消息则就地截断原对话。

## Grok Build

Atrium 通过 xAI Grok Build 命令行的 agent 模式 `grok agent stdio`（stdio 上的
[Agent Client Protocol](https://agentclientprotocol.com)）运行它，每个运行中的 agent 一个进程。`make install`
会装好这个 CLI：xAI 的 `grok-cli-bin` wheel 内置了它，版本固定在 `pyproject.toml` 里（0.2.111）。如果你自己
装了一个——xAI 的安装脚本（`curl -fsSL https://x.ai/cli/install.sh | bash`）放在 `~/.grok/bin/grok`，或者
`PATH` 里有 `grok`——Atrium 改用那一个：它更新，也是你在终端里写会话用的那一个。`harnesses.grok.binary`
可以指定别的；`/api/meta` 显示实际运行的是哪个（`cli`）。其余配置都在 Grok 自己的 `~/.grok/config.toml`
（`$GROK_HOME/config.toml`；⚙ Settings → Grok Build），项目里的 `.grok/config.toml` 可以设置该项目的 MCP 服务器、
插件和权限规则。

**登录**跟 CLI 一样：`grok login`（你的 Grok 订阅），或者在 Atrium 的环境变量里设置
[console.x.ai](https://console.x.ai) 的 API key `XAI_API_KEY`。自带 key 的模型两者都不需要。

**中转站**，或者任何兼容 OpenAI Chat Completions / Responses API 或 Anthropic Messages API 的端点，写成
自定义模型：

```toml
[models]
default = "my-grok"

[model.my-grok]
model = "grok-4.7"                    # 端点认的模型 id
base_url = "https://relay.example.com/v1"
name = "Grok 4.7 (relay)"
api_key = "..."                       # 或 env_key = "RELAY_API_KEY"：Atrium 环境里的一个变量
api_backend = "chat_completions"      # 或 "responses"、"messages"
supports_reasoning_effort = true      # 端点接受 reasoning_effort 时：Atrium 的 effort 选择器才起作用
context_window = 500000               # 用于上下文用量显示和自动压缩（默认 200000）
```

如果端点报 `Invalid schema for function 'use_tool': schema must have type 'object' and not have 'oneOf'…`
（Azure 的 Responses API 会这样），对这个模型改用 `api_backend = "chat_completions"` 即可。

**用哪个模型**：这个 agent 选的模型（新 agent 用 `harnesses.grok.model`），否则用 `[models] default`，否则用
Grok 自己的默认值。模型选择器里列出 *Default* 和 `grok models` 列出的模型。**effort** 是模型自己的档位
（设了 `supports_reasoning_effort` 的自定义模型用 Grok 的档位，`minimal` … `xhigh`；端点接受哪几档由端点决定），
对运行中的 agent 立即生效；没有 `supports_reasoning_effort` 的模型会忽略它。

**Atrium 为每个 agent 设置的内容：**

- **权限模式**（`ask`、`auto`、`always-approve`），只作用于这个 agent，可以在运行中切换：不使用 `config.toml`
  里的 `[ui] permission_mode`，但它的 `[permission]` 规则有效。拒绝一个操作会结束 Grok 的这一轮；拒绝时
  写下的说明会作为你的下一条消息发给 agent，仍算同一轮。
- **系统提示词里 Atrium 的那一段**，在 agent 新建时加入：Grok 会保留会话的提示词，所以改动只影响之后新建的
  agent，不影响恢复的。
- **Atrium 的工具**：以命名空间命名的进程内 MCP 服务器（`board`、`web`、`graph`、`review`），agent 通过它和
  Atrium 之间的连接调用。Grok 会延迟加载 MCP 工具：模型先用 `search_tool` 找到它们，再通过 `use_tool` 调用
  （`board__status` 等）。这些工具不经询问直接运行。
- Atrium `mcp_servers` 里的服务器，和 Grok 自己文件里的 `[mcp_servers]` 并存。
- 不检查更新（`GROK_DISABLE_AUTOUPDATER`）。

它的会话就是 CLI 自己的，保存在 `~/.grok/sessions/<目录>/<id>/`：`grok --resume` 能列出 Atrium 启动的 agent，
Atrium 也能列出你在终端里开的会话。删除 agent 会同时删除它的会话。输入框提供 CLI 的命令（`/compact`、
`/session-info`、`/deep-research`、`/goal` 等）。Grok 默认还会读取 Claude Code 和 Cursor 的规则与 skills，并运行
Claude Code 的 hooks；`config.toml` 里的 `[compat.claude]` 和 `[compat.cursor]` 可以关掉。暂不支持：fork agent、
修改已发送的消息、在侧边栏列出它的子 agent、在 Requests 里看到它的调用。内置的 0.2.111 不接受图片（附加的图片
以路径的形式交给它）。

## MCP 服务器

每个 agent 都自带 Atrium 的工具：看板（`board.status`、`board.publish`、`board.note`、`board.read`、`board.todo_add`、`board.todo_done`）和网页
（`web.search`、`web.fetch`）。除了 [web 工具的 key](#web-工具与-mcp-服务器)，不需要任何配置。

**Claude Code、Antigravity 和 Grok Build** 从 Atrium 配置的 `mcp_servers` 读取你的服务器，格式和 Claude Code 的相同：

```json
"mcp_servers": {
  "notes": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/notes"]},
  "docs": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
}
```

- Claude Code agent 还会加载为 Claude Code 本身配置的服务器（`claude mcp add` 添加的、项目里的 `.mcp.json`）。
- Antigravity 支持 `command` 服务器（stdio）和 `url` 服务器（streamable HTTP），名称只能用字母、数字、`_` 和 `-`。
- Grok Build 支持 `command` 服务器和 `url` 服务器（`"type": "sse"` 为 SSE，否则为 streamable HTTP），另外还有它
  自己 `config.toml` 里的 `[mcp_servers]`（`grok mcp add`）。

**Codex** 从 `~/.codex/config.toml` 的 `[mcp_servers]` 读取；在 Codex agent 的聊天里输入 `/mcp` 可以列出它们：

```toml
[mcp_servers.notes]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/notes"]

[mcp_servers.docs]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "DOCS_TOKEN"
```

**在别处用 Atrium 的 web 工具**：`atrium-web-mcp` 以 stdio 方式提供这些工具，key 来自 Atrium 的配置或
环境变量，例如 `claude mcp add atrium-web -- /path/to/atrium/.venv/bin/atrium-web-mcp`。

## 给 agent 的指令文件

每个 harness 读取自己的指令文件。Atrium 会在每个 agent 的系统提示词里加一段自己的内容（看板、agent 所在的
worktree），这部分不需要配置。

| Harness | 读取 |
|---|---|
| Claude Code | `CLAUDE.md`：`~/.claude/CLAUDE.md`，以及项目和它各级父目录里的（`CLAUDE.md`、`CLAUDE.local.md`、`.claude/CLAUDE.md`），还有 `.claude/rules/`。如果 agent 所在目录及以上都没有这三种文件，就改读那里的 `AGENTS.md`。 |
| Codex | `AGENTS.md`：`~/.codex/AGENTS.md`，然后是从仓库根目录到 agent 所在目录的每一级目录里的。 |
| Grok Build | 它自己读取：从仓库根目录到 agent 所在目录每一级的 `AGENTS.md`、`AGENT.md`、`CLAUDE.md`、`CLAUDE.local.md`，`.grok/rules/*.md` 和 `~/.grok/rules/`；如果没有在 `config.toml` 的 `[compat]` 里关掉，还有 Claude Code 的（`~/.claude/`、`.claude/rules/`）和 Cursor 的。 |
| Antigravity | 由 Atrium 加入，因为 SDK 的运行时不加载它们：`~/.gemini/AGENTS.md` 和 `~/.gemini/GEMINI.md`，然后从仓库根目录到 agent 所在目录的每一级：`AGENTS.md`、`GEMINI.md`、`.agents/AGENTS.md`、`.agents/GEMINI.md`，以及 `.agents/rules/*.md` 里的规则（`trigger: always_on` 的全文加入；`trigger: model_decision` 且带 `description` 的只列出提示，适用时由 agent 自己去读）。每个文件最多 24 KB。 |

一个 `AGENTS.md` 就能同时服务四个 harness。如果项目里还有 `CLAUDE.md`，可以在里面加一行 `@AGENTS.md`
把它引进来，本仓库就是这么做的。也可以让 Claude Code 两个都读，写在 `~/.claude/settings.json` 里：

```json
{"pluginConfigs": {"cc-plugin-agents-md@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}}}
```

（见 [Claude Code 的 AGENTS.md 支持](https://code.claude.com/docs/zh-CN/memory#agents-md)；项目自己的配置文件不能设置这一项。）

## 项目里的文件

| 文件 | 说明 |
|---|---|
| `.worktreeinclude` | 新建 worktree 时要复制进去的、被 git 忽略的文件（gitignore 语法），比如 `.env`。Claude Code 和 Codex 应用也读这个文件。 |
| `.claude/settings.json`、`.claude/settings.local.json` | Claude Code 的项目配置（见[上文](#claude-code)）。 |
| `.codex/config.toml` | Codex 的项目配置，项目被信任后生效（见[上文](#codex)）。 |
| `.grok/config.toml` | Grok Build 的项目配置：MCP 服务器、插件、权限规则（见[上文](#grok-build)）。 |
| `.mcp.json` | Claude Code 的项目级 MCP 服务器。 |
| `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、`.agents/rules/` | [指令文件](#给-agent-的指令文件)。 |
| `.atrium/` | Atrium 在这个项目里的状态：看板、agent、tracker、审查记录、worktree、回收站。它不是配置，由 Atrium 管理；里面的 `.gitignore`（`*`）让它不进 git。 |

## 环境变量

每个 agent 的进程都继承 Atrium 的环境变量：要在带有 agent 所需 key 和代理设置的 shell 里启动 Atrium。
Claude Code 的配置可以在 `env` 里另加变量；Antigravity 配置的 `env` 只认它的 key、端点和 Vertex AI 变量
（见 [Antigravity](#antigravity)）。

| 变量 | 说明 |
|---|---|
| `ATRIUM_HOME` | Atrium 存放配置和状态的目录（默认 `~/.atrium`）。 |
| `ATRIUM_HOST`、`ATRIUM_PORT` | `--host`（`127.0.0.1`）和 `--port`（`8787`）的默认值；`make run PORT=8790` 可以设置端口。所有页面、API 调用和 WebSocket 都要先登录（见 [README → Login](../README.md#login)）；跨网络访问时，请在前面加一层 HTTPS。 |
| `ATRIUM_URL` | 点击通知时打开的地址（默认是 Atrium 当前服务的地址）。 |
| `SERPER_API_KEY`、`JINA_API_KEY` | web 工具的 key；优先于 `settings.json`。 |
| `CLAUDE_CONFIG_DIR` | Claude Code 的目录（默认 `~/.claude`）：它的配置和对话记录。 |
| `CODEX_HOME` | Codex 的目录（默认 `~/.codex`）：`config.toml`、登录信息、对话线程。 |
| `GEMINI_API_KEY`、`GOOGLE_GEMINI_BASE_URL`、Vertex AI 的变量 | Antigravity 用的，在它配置文件的 `env` 没有设置时生效（见 [Antigravity](#antigravity)）。 |
| `ANTIGRAVITY_HARNESS_PATH` | 用另一个 Antigravity 运行时二进制，代替 SDK wheel 里的那个。 |
| `GROK_HOME` | Grok Build 的目录（默认 `~/.grok`）：`config.toml`、登录信息、会话、它安装的 CLI（`bin/`）。 |
| `XAI_API_KEY` | Grok Build 访问 xAI API 用的 key，在没有 `grok login` 登录时使用（见 [Grok Build](#grok-build)）。 |
| `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` | 原样传给每个 agent。开启 `capture.enabled` 时，Claude Code 和 Codex 的 `NO_PROXY` 会加上回环地址，抓包代理再按它们本来会用的代理把调用发出去。 |
| `ATRIUM_CAPTURE_URL`、`ATRIUM_CAPTURE_UPSTREAM` | Atrium 给被抓包的 Claude Code agent 设置的：代理的地址，以及它代替的地址。带着它们启动的 Atrium 会让自己的 agent 改用 `ATRIUM_CAPTURE_UPSTREAM`（见[请求抓取](#请求抓取)）。 |

## 检查配置

- `curl -s --noproxy '*' -H "Authorization: Bearer $(.venv/bin/atrium --token)" http://127.0.0.1:8787/api/meta` 列出每个 harness：`available`（是否可用），不可用时的
  `unavailable_reason`；`config_model` / `config_effort`，即 *Default* 实际解析成什么；Codex 和 Grok Build 还有 `cli`
  （`path`、`version`、`bundled`）。`settings._error` 说明 `settings.json` 读取失败的原因。`capture` 说明模型调用
  是否被记录（`enabled`）、抓包代理是否已经启动（`running`），以及记录占了多少 MB（`mb`），其中不计入
  `capture.max_mb` 的保留部分有多少（`kept_mb`）。
- ⚙ Settings 在 `~/.atrium/backups/` 里为每个文件保留最近 20 个版本（`<file>-<date>-<time>`）。要回退，
  把其中一个复制回原文件即可。

| 现象 | 原因 |
|---|---|
| 代码或本指南里的默认值没有生效 | 你的 `settings.json` 里还是首次启动时写入的值：修改或删掉那个 key。 |
| harness 配置文件里的 `model` 或 effort 被忽略 | 这个 agent 选了具体值，或者 `harnesses.<harness>.model` / `effort` 设了值；那里写 `""` 才交给 harness 的配置文件决定。在 Claude Code 的配置里，`env.CLAUDE_CODE_EFFORT_LEVEL` 优先于 `effortLevel`。 |
| harness 自己的默认权限模式被忽略 | 权限模式总是由 Atrium 设置：来自 agent 的选择器，或 `harnesses.<harness>.permission_mode`。 |
| Codex 里缺少某个 MCP 服务器 | Atrium 的 `mcp_servers` 只给 Claude Code、Antigravity 和 Grok Build；Codex 读 `config.toml` 里的 `[mcp_servers]`。 |
| Grok Build agent 报 `Invalid schema for function 'use_tool'` | 它的端点的 Responses API 不接受 Grok `use_tool` 的 schema：对这个模型改用 `api_backend = "chat_completions"`（见 [Grok Build](#grok-build)）。 |
| Antigravity 不可用，提示 "Needs the API key of …" | 在它配置文件里设置的中转站只用该文件 `env` 里的 key。 |
| 正在运行的 agent 看不到修改 | harness 配置在 agent 进程启动时才生效：新建的 agent，以及 `sessions.idle_close_minutes` 之后被重新唤醒的 agent。 |
| agent 的模型调用失败，报 "Atrium's capture proxy" 的 502 | 代理连不上 API，消息里写了原因（不信任的证书、中间的代理）。`"capture": {"enabled": false}` 让之后启动的 agent 直接调用它们的 API。 |
| Requests 里看不到某个 agent 的调用 | 它的进程是在开启 `capture.enabled` 之前启动的，或者它的 harness 端点不经过代理（见[请求抓取](#请求抓取)）。 |
| 发送按钮旁边没有麦克风 | `voice.base_url` 或 `voice.asr_model` 为空，或者页面是在设置它们之前打开的：在 ⚙ Settings 里保存，或刷新页面。 |
| 麦克风提示需要安全的页面 | 浏览器只把麦克风给 HTTPS 页面和 `localhost`。 |
