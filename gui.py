"""兼容直接运行脚本的桌面应用入口。"""

from _bootstrap import setup_source_path

setup_source_path()

from deepseek_agent.gui_app import main


if __name__ == "__main__":
    main()
