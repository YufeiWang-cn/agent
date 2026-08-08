from src.deepseek_agent import Settings
from src.deepseek_agent.config import PROJECT_ROOT
from src.deepseek_agent.observability import build_file_logger
from src.deepseek_agent.ui import run_gui


def main() -> None:
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
