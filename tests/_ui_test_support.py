"""提供界面测试共用的轻量 Agent 替身。"""

from deepseek_agent.memory import Session


class FakeUiAgent:
    """模拟界面测试所需的最小 Agent 接口。"""

    model_name = "fake-model"
    available_models = ("fake-model",)

    def __init__(self) -> None:
        self._session = Session.create(
            [{"role": "system", "content": "system"}]
        )
        self.current_plan = None
        self.saved = 0

    @property
    def session_id(self) -> str:
        return self._session.id

    @property
    def session_title(self) -> str:
        return self._session.title

    def history(self):
        return []

    def list_sessions(self):
        return [self._session]

    def list_projects(self):
        return []

    def save_session(self) -> None:
        self.saved += 1
