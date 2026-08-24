# DeepSeek Agent

一个使用 DeepSeek API 构建的命令行 Agent 项目。目前支持多轮对话、流式输出、基础命令、安全的工具调用循环、本地会话持久化、上下文裁剪、工具权限确认、自动重试、运行日志和受控文件操作。

## 安装

### 方式一：下载压缩包安装

1. 下载并解压项目。
2. 打开解压后的项目文件夹，确认其中能看到 `pyproject.toml`。
3. 在文件夹空白处右键，选择“在终端中打开”。
4. 首次使用时根据项目环境文件创建 Conda 环境（已经创建过可跳过）：

```bat
conda env create -f environment.yml
```

5. 激活环境：

```bat
conda activate agent
```

`environment.yml` 会安装项目及其运行依赖。项目当前推荐使用 Python 3.10，最低要求也为 Python 3.10；推荐版本统一记录在 `.python-version` 中。

如果已经存在 `agent` 环境，需要按项目配置同步环境时执行：

```bat
conda env update -n agent -f environment.yml
```

为避免 Conda 在解析单个软件包依赖时改变 Python 版本，更新核心环境组件后应重新使用上述命令同步项目环境。

### 方式二：使用 Git 安装

熟悉 Git 的用户可以执行：

```bat
git clone https://github.com/YufeiWang-cn/agent.git
cd agent
conda env create -f environment.yml
conda activate agent
```

### 安装完成后的必要配置

无论使用上面的哪一种安装方式，都需要在项目根目录中复制 `.env.example`，将副本重命名为 `.env`，然后在 `.env` 中填写自己的 DeepSeek API Key。

`DEEPSEEK_MAX_CONTEXT_TOKENS` 用于设置发送给模型的消息 Token 预算，默认值为 `8000`。这里使用本地近似估算，实际计费 Token 以 DeepSeek 返回的数据为准。

模型请求默认超时为 60 秒，临时网络错误最多重试 3 次，等待时间依次为 1、2、4 秒。可以通过 `.env` 中的 `DEEPSEEK_REQUEST_TIMEOUT`、`DEEPSEEK_MAX_RETRIES` 和 `DEEPSEEK_RETRY_BASE_DELAY` 调整。

`AGENT_WORKSPACE` 是文件工具唯一允许访问的根目录，默认是项目根目录；`AGENT_MAX_FILE_SIZE` 控制单次读取或写入的最大字节数，默认 `100000`。

受控命令默认最多运行 120 秒，stdout 和 stderr 分别最多返回 50000 字节。可以通过 `AGENT_COMMAND_TIMEOUT` 和 `AGENT_MAX_COMMAND_OUTPUT` 调整。

复杂任务可以通过 `update_plan` 维护 2 到 7 个结构化步骤。`execution` 计划用于 Agent 实际执行任务，会跨用户消息和程序重启保留。它的 `scope=single_step` 只推进本轮目标步骤，完成、失败、跳过或等待用户后立即结束本轮，其余步骤保持待处理；`scope=entire_plan` 则在计划未结束时要求模型继续执行。需要用户补充信息时，当前步骤会进入 `waiting_user` 并暂停到下一条回复。`proposal` 计划用于把路线图或步骤清单本身作为交付物，发布后即可正常结束，不会把未来步骤误报为已完成。单轮模型执行步数默认上限为 12，可以通过 `AGENT_MAX_STEPS` 调整。

## 运行

桌面可视化界面：

```bat
deepseek-agent-gui
```

也可以在项目根目录使用兼容启动脚本：

```bat
python gui.py
```

界面支持流式显示、多轮对话、停止生成、清空对话以及完整展示高风险工具参数的确认窗口。补丁确认会默认展示带新旧行号和增删配色的差异视图，并可通过按钮或 `F11` 全屏审阅。工具调用会显示为默认折叠的状态卡片，点击后可以在带滚轮的固定高度区域中查看完整参数和结果，避免长结果占满聊天窗口。JSON 会自动缩进，常见代码和数据格式会使用深色编辑器主题进行语法着色；点击工具卡片中的“全屏”可以打开最大化查看器，`F11` 切换真正全屏，`Esc` 退出全屏或关闭查看器。左侧栏可以拖动调整宽度或通过顶部按钮折叠；右键项目可以新建、重命名或删除，右键会话可以打开、重命名、移动到项目或删除。删除项目只会把其中的会话移到“未分类”。项目及会话归属会自动保存在 `data/projects.json` 和会话文件中。

输入框中按 `Enter` 发送消息，按 `Shift+Enter` 换行。

命令行界面：

```bat
deepseek-agent
```

也可以在项目根目录使用兼容启动脚本：

```bat
python main.py
```

会话以 JSON 文件保存在 `data/sessions/`，项目索引保存在 `data/projects.json`。程序会自动恢复最近使用的会话，并在模型成功回复后自动保存。

多步骤任务会在对话区顶部显示可折叠计划卡片，并区分“执行计划 · 单步”、“执行计划 · 连续”和“规划方案”。执行计划实时展示待处理、进行中、等待输入、已完成、失败和已跳过状态。最近一次计划会随会话保存并在重新打开后恢复；简单问答不会强制生成计划，也不会自动清除最近的计划。

完整历史始终保存在会话文件中；调用模型时只发送预算内的最近完整对话，工具请求和工具结果不会被拆开。

## 命令

- `/help`：显示帮助
- `/new`：创建新会话
- `/save`：立即保存当前会话
- `/sessions`：查看已保存的会话
- `/load <ID>`：加载指定会话（可使用列表显示的短 ID）
- `/delete <ID>`：删除指定会话
- `/clear`：清空当前对话上下文
- `/history`：查看当前对话历史
- `/context`：查看上下文预算和裁剪情况
- `/stats`：查看本次运行的模型请求、重试和耗时统计
- `/plan`：查看当前任务计划
- `/model`：查看当前模型
- `/tools`：查看当前可用工具
- `/exit`：退出程序
- `exit`、`quit`、`q`、`退出`：退出程序

## 内置工具

- `calculator`：安全计算基础数学表达式
- `update_plan`：创建、更新或替换复杂任务的结构化执行计划和规划方案
- `get_current_time`：获取指定时区的当前日期和时间
- `list_directory`：列出工作目录内的直接子项
- `read_text_file`：读取工作目录内的 UTF-8 文本文件
- `search_text`：递归搜索工作目录内的 UTF-8 文本并返回文件和行号
- `run_command`：在工作目录内无 Shell 地运行受控 Git 或 Python 命令
- `apply_patch`：统一验证并批量创建或更新工作目录内的 UTF-8 文本文件
- `replace_text`：精确替换文件中唯一一处文本，执行前必须确认
- `write_text_file`：创建或完整覆盖 UTF-8 文本文件，执行前必须确认

计算器、时间工具和只读 Git 查询属于安全工具，会自动执行。运行测试、Python 脚本、依赖安装以及可能改变仓库或远端状态的 Git 命令必须确认。Agent 会展示工具名称、说明和完整参数，并且只在用户输入 `y` 或 `yes` 后执行；直接回车或输入 `n` 会拒绝执行。

文件工具禁止越过工作目录，并阻止访问 `.env`、`.git`、`.venv`、`__pycache__`，以及 Agent 内部状态路径 `data/sessions`、`data/runs`、`data/projects.json` 和 `logs`。普通项目文件（例如 `data/dataset.json`）仍然可以访问。文本搜索最多返回 100 条结果；局部替换仅在原文本恰好匹配一次时执行。补丁工具支持一次创建或更新多个文件，并在全部修改通过验证后写入；写入中途失败时会尝试回滚已经修改的文件。文件写入通过临时文件替换目标文件。

命令工具不使用 Shell，只接受参数数组。第一版仅允许明确支持的 Git 和 Python 命令，禁止 `cmd`、PowerShell、Bash、管道、重定向、命令拼接、内联 Python、`git reset` 和 `git clean`。执行目录和 Git 仓库根目录都必须位于工作区内；子进程不会继承名称中包含 Key、Token、Secret、Password 或 Credential 的环境变量。暂不提供删除和移动工具。

诊断日志保存在 `logs/agent.log`，使用大小轮转，最多保留 3 个备份。工具执行、计划状态与崩溃恢复日志以 JSONL 格式保存在 `data/runs/`。计划日志包含计划标识、用途、版本和各状态数量，不包含步骤正文。两类日志都不会记录 API Key、用户消息正文或模型回答正文；恢复日志也不会保存完整工具参数、工具结果或异常正文，只保存参数键名、大小、摘要和执行状态等恢复所需信息。

## 测试

```bat
python -m unittest discover -s tests -v
```

GitHub Actions 会读取 `.python-version` 中的推荐版本，并在代码推送和拉取请求中自动执行同一条测试命令。
