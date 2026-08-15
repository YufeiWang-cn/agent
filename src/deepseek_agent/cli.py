from .agent import Agent
from .config import PROJECT_ROOT, Settings
from .observability import build_file_logger


def main() -> None:
    """启动 DeepSeek Agent 命令行界面。"""
    settings = Settings.from_env()
    logger = build_file_logger(
        PROJECT_ROOT / "logs" / "agent.log",
        settings.log_level,
    )
    Agent(settings, logger=logger).run()


if __name__ == "__main__":
    main()
