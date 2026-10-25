"""启动学生求职助手命令行。"""

from _bootstrap import setup_source_path

setup_source_path()

from deepseek_agent.cli import career_main

if __name__ == "__main__":
    career_main()
