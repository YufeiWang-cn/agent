"""兼容直接运行脚本的 Agent 评测入口。"""

from _bootstrap import setup_source_path

setup_source_path()

from deepseek_agent.evaluation.cli import main


if __name__ == "__main__":
    main()
