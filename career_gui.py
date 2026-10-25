"""启动学生求职助手桌面界面。"""

from _bootstrap import setup_source_path

setup_source_path()

from deepseek_agent.gui_app import career_main

if __name__ == "__main__":
    career_main()
