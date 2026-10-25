"""加载配置、日志记录器和 Tk 主界面，作为桌面应用入口。"""

from dataclasses import replace

from .config import PROJECT_ROOT, Settings
from .observability import build_file_logger
from .ui import run_gui


def main(mode: str | None = None) -> None:
    """启动 DeepSeek Agent 桌面界面。"""
    try:
        settings = Settings.from_env()
        if mode is not None:
            settings = replace(settings, agent_mode=mode)
    except Exception as error:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", str(error), parent=root)
        root.destroy()
        return

    logger = build_file_logger(
        PROJECT_ROOT / "logs" / "agent.log",
        settings.log_level,
    )
    run_gui(settings, logger=logger)


def career_main() -> None:
    main(mode="career")


if __name__ == "__main__":
    main()
