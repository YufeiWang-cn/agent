# DeepSeek Agent

一个使用 DeepSeek API 构建的命令行 Agent 项目。目前支持多轮对话、流式输出、基础命令、安全的工具调用循环、本地会话持久化和上下文裁剪。

## 安装

```bat
conda activate agent
cd /d D:\wyf\Python\agent
python -m pip install -e .
```

复制 `.env.example` 为 `.env`，然后填写自己的 DeepSeek API Key。

`DEEPSEEK_MAX_CONTEXT_TOKENS` 用于设置发送给模型的消息 Token 预算，默认值为 `8000`。这里使用本地近似估算，实际计费 Token 以 DeepSeek 返回的数据为准。

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
- `/model`：查看当前模型
- `/tools`：查看当前可用工具
- `/exit`：退出程序
- `exit`、`quit`、`q`、`退出`：退出程序

## 内置工具

- `calculator`：安全计算基础数学表达式
- `get_current_time`：获取指定时区的当前日期和时间

## 测试

```bat
python -m unittest discover -s tests -v
```
