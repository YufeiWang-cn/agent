# DeepSeek Agent

一个使用 DeepSeek API 构建的命令行 Agent 项目。目前支持多轮对话、流式输出和基础命令。

## 安装

```bat
conda activate agent
cd /d D:\wyf\Python\agent
python -m pip install -e .
```

复制 `.env.example` 为 `.env`，然后填写自己的 DeepSeek API Key。

## 运行

```bat
python main.py
```

旧入口仍然可用：

```bat
python llm_call.py
```

## 命令

- `/help`：显示帮助
- `/clear`：清空当前对话上下文
- `/history`：查看当前对话历史
- `/model`：查看当前模型
- `/exit`：退出程序
- `exit`、`quit`、`q`、`退出`：退出程序
