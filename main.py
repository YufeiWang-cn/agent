from src.deepseek_agent import Agent, Settings
from src.deepseek_agent.config import PROJECT_ROOT
from src.deepseek_agent.observability import build_file_logger


def main() -> None:
    settings = Settings.from_env()
    logger = build_file_logger(
        PROJECT_ROOT / "logs" / "agent.log",
        settings.log_level,
    )
    Agent(settings, logger=logger).run()


if __name__ == "__main__":
    main()
