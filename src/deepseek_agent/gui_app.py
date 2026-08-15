from .config import PROJECT_ROOT, Settings
from .observability import build_file_logger
from .ui import run_gui


def main() -> None:
    """启动 DeepSeek Agent 桌面界面。"""
    try:
        settings = Settings.from_env()
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


if __name__ == "__main__":
    main()
