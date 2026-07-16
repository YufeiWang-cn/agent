from src.deepseek_agent import Agent, Settings


def main() -> None:
    settings = Settings.from_env()
    Agent(settings).run()


if __name__ == "__main__":
    main()
