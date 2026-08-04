# DeepSeek Agent

一个使用 DeepSeek API 构建的命令行 Agent 项目。目前支持多轮对话、流式输出、基础命令、安全的工具调用循环、本地会话持久化、上下文裁剪、工具权限确认、自动重试、运行日志和受控文件操作。

## 安装

```bat
conda activate agent
cd /d D:\wyf\Python\agent
python -m pip install -e .
```

复制 `.env.example` 为 `.env`，然后填写自己的 DeepSeek API Key。

`DEEPSEEK_MAX_CONTEXT_TOKENS` 用于设置发送给模型的消息 Token 预算，默认值为 `8000`。这里使用本地近似估算，实际计费 Token 以 DeepSeek 返回的数据为准。

模型请求默认超时为 60 秒，临时网络错误最多重试 3 次，等待时间依次为 1、2、4 秒。可以通过 `.env` 中的 `DEEPSEEK_REQUEST_TIMEOUT`、`DEEPSEEK_MAX_RETRIES` 和 `DEEPSEEK_RETRY_BASE_DELAY` 调整。

`AGENT_WORKSPACE` 是文件工具唯一允许访问的根目录，默认是项目根目录；`AGENT_MAX_FILE_SIZE` 控制单次读取或写入的最大字节数，默认 `100000`。

## 运行

```bat
python main.py
```

会话以 JSON 文件保存在 `data/sessions/`。程序会自动恢复最近使用的会话，并在模型成功回复后自动保存。

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
- `/model`：查看当前模型
- `/tools`：查看当前可用工具
- `/exit`：退出程序
- `exit`、`quit`、`q`、`退出`：退出程序

## 内置工具

- `calculator`：安全计算基础数学表达式
- `get_current_time`：获取指定时区的当前日期和时间
- `list_directory`：列出工作目录内的直接子项
- `read_text_file`：读取工作目录内的 UTF-8 文本文件
- `write_text_file`：创建或完整覆盖 UTF-8 文本文件，执行前必须确认

计算器和时间工具属于安全工具，会自动执行。工具将 `requires_confirmation` 设置为 `True` 后，Agent 会在命令行展示工具名称、说明和参数，并且只在用户输入 `y` 或 `yes` 后执行；直接回车或输入 `n` 会拒绝执行。

文件工具禁止越过工作目录，并阻止访问 `.env`、`.git`、`.venv`、`__pycache__`、`data/sessions` 和 `logs`。文件写入通过临时文件替换目标文件；第一版不提供删除、移动或 Shell 工具。

运行日志保存在 `logs/agent.log`，使用大小轮转，最多保留 3 个备份。日志不会记录 API Key、用户消息或模型回答。

## 测试

```bat
python -m unittest discover -s tests -v
```
