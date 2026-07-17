# DeepSeek Agent

一个使用 DeepSeek API 构建的命令行 Agent 项目。目前支持多轮对话、流式输出、基础命令，以及安全的工具调用循环。

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

## 命令

- `/help`：显示帮助
- `/clear`：清空当前对话上下文
- `/history`：查看当前对话历史
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
