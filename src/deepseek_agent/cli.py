"""实现 Agent 的交互式命令行界面和会话管理命令。"""

from .agent import Agent
from .config import PROJECT_ROOT, Settings
from .conversation import Message
from .memory import ProjectStoreError, SessionStoreError
from .models import ToolCallRequest
from .observability import build_file_logger
from .planning import (
    PlanExecutionScope,
    PlanKind,
    PlanStepStatus,
    TaskPlan,
)
from .runtime import AgentEvent, AgentEventType, RunStatus


EXIT_COMMANDS = {"/exit", "exit", "quit", "q", "退出"}


class CliApplication:
    """负责命令行输入、输出和命令分发。"""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._running = True

    def run(self) -> None:
        self._print_welcome()
        self._print_recovery_issues()

        while self._running:
            try:
                prompt = input("\n您：").strip()
            except (EOFError, KeyboardInterrupt):
                self._save_session_safely()
                print("\n对话已结束。")
                break

            if not prompt:
                continue
            if self.handle_command(prompt):
                continue
            self._chat(prompt)

    def handle_command(self, prompt: str) -> bool:
        """处理命令行命令；普通对话文本返回 ``False``。"""
        command = prompt.lower()

        if command in EXIT_COMMANDS:
            self._save_session_safely()
            print("对话已结束。")
            self._running = False
            return True
        if command == "/help":
            self._print_help()
            return True
        if command == "/clear":
            self._clear_conversation()
            return True
        if command == "/new":
            self._start_new_session()
            return True
        if command == "/save":
            self._save_session_safely(success_message=True)
            return True
        if command == "/sessions":
            self._print_sessions()
            return True
        if command.startswith("/load "):
            self._load_session(prompt.split(maxsplit=1)[1])
            return True
        if command.startswith("/delete "):
            self._delete_session(prompt.split(maxsplit=1)[1])
            return True
        if command == "/history":
            self._print_history()
            return True
        if command == "/context":
            self._print_context()
            return True
        if command == "/stats":
            self._print_stats()
            return True
        if command == "/plan":
            self._print_plan(self._agent.current_plan)
            return True
        if command == "/model":
            print(f"当前模型：{self._agent.model_name}")
            return True
        if command == "/tools":
            print("可用工具：" + "、".join(self._agent.tool_names))
            return True
        if command.startswith("/"):
            print("未知命令。输入 /help 查看可用命令。")
            return True
        return False

    def _chat(self, prompt: str) -> None:
        started_output = False

        def print_text(content: str) -> None:
            nonlocal started_output
            if not started_output:
                print("\n助手：", end="", flush=True)
                started_output = True
            print(content, end="", flush=True)

        def print_tool_call(request: ToolCallRequest) -> None:
            nonlocal started_output
            if started_output:
                print()
                started_output = False
            print(f"\n[工具调用] {request.name} 参数：{request.arguments}")

        def print_tool_result(_request: ToolCallRequest, result: str) -> None:
            print(f"[工具结果] {result}")

        def print_runtime_event(event: AgentEvent) -> None:
            if event.type is AgentEventType.PLAN_UPDATED and event.plan is not None:
                self._print_plan(event.plan)

        try:
            outcome = self._agent.chat(
                prompt,
                on_text=print_text,
                on_tool_call=print_tool_call,
                on_tool_result=print_tool_result,
                on_event=print_runtime_event,
            )
            if started_output:
                print()
            if outcome.status is RunStatus.WAITING_USER:
                print("[等待输入] 当前计划已暂停，请根据上方问题继续回复。")
        except Exception as error:
            if started_output:
                print()
            print(f"\n调用失败：{error}")

    def _clear_conversation(self) -> None:
        try:
            self._agent.clear_conversation()
        except (OSError, SessionStoreError) as error:
            print(f"清空失败：{error}")
        else:
            print("当前对话上下文已清空。")

    def _start_new_session(self) -> None:
        try:
            session = self._agent.start_new_session()
        except (OSError, SessionStoreError, ProjectStoreError) as error:
            print(f"创建新会话失败：{error}")
        else:
            print(f"已创建新会话：{session.id[:8]}")

    def _save_session_safely(self, success_message: bool = False) -> None:
        try:
            self._agent.save_session()
        except (OSError, SessionStoreError) as error:
            print(f"会话保存失败：{error}")
        else:
            if success_message:
                print(f"会话已保存：{self._agent.session_id[:8]}")

    def _load_session(self, session_id: str) -> None:
        try:
            session = self._agent.load_session(session_id)
        except (OSError, ValueError, SessionStoreError) as error:
            print(f"加载失败：{error}")
            return
        print(f"已加载会话：{session.id[:8]}  {session.title}")

    def _delete_session(self, session_id: str) -> None:
        previous_session_id = self._agent.session_id
        try:
            deleted_id = self._agent.delete_session(session_id)
        except (OSError, SessionStoreError, ProjectStoreError) as error:
            print(f"删除失败：{error}")
            return
        if deleted_id == previous_session_id:
            print(
                "当前会话已删除，并已创建新会话："
                f"{self._agent.session_id[:8]}"
            )
        else:
            print(f"会话已删除：{deleted_id[:8]}")

    def _print_sessions(self) -> None:
        sessions = self._agent.list_sessions()
        if not sessions:
            print("还没有已保存的会话。")
            return
        print("已保存的会话：")
        for session in sessions:
            marker = "*" if session.id == self._agent.session_id else " "
            print(
                f"{marker} {session.id[:8]}  {session.title}  "
                f"{session.updated_at}"
            )

    def _print_welcome(self) -> None:
        print(f"DeepSeek Agent 已启动，当前模型：{self._agent.model_name}")
        print(
            f"当前会话：{self._agent.session_id[:8]}  "
            f"{self._agent.session_title}\n"
            "输入 /help 查看命令，输入 /exit 结束对话。"
        )

    def _print_recovery_issues(self) -> None:
        if not self._agent.recovery_issues:
            return
        print("\n检测到上次运行存在结果未知的工具调用：")
        for issue in self._agent.recovery_issues:
            print(f"- {issue.message}")

    @staticmethod
    def _print_help() -> None:
        print(
            "可用命令：\n"
            "  /help         显示帮助\n"
            "  /new          创建新会话\n"
            "  /save         立即保存当前会话\n"
            "  /sessions     查看已保存的会话\n"
            "  /load <ID>    加载指定会话\n"
            "  /delete <ID>  删除指定会话\n"
            "  /clear        清空当前会话上下文\n"
            "  /history      查看当前对话历史\n"
            "  /context      查看上下文预算和裁剪情况\n"
            "  /stats        查看本次运行的模型调用统计\n"
            "  /plan         查看当前任务计划\n"
            "  /model        查看当前模型\n"
            "  /tools        查看可用工具\n"
            "  /exit         退出程序"
        )

    @staticmethod
    def _print_plan(plan: TaskPlan | None) -> None:
        """以适合终端阅读的形式打印当前任务计划。"""
        if plan is None:
            print("当前没有任务计划。")
            return
        symbols = {
            PlanStepStatus.PENDING: "[ ]",
            PlanStepStatus.IN_PROGRESS: "[>]",
            PlanStepStatus.WAITING_USER: "[?]",
            PlanStepStatus.COMPLETED: "[x]",
            PlanStepStatus.FAILED: "[!]",
            PlanStepStatus.SKIPPED: "[-]",
        }
        if plan.kind is PlanKind.PROPOSAL:
            plan_title = "规划方案"
        else:
            scope_text = (
                "单步"
                if plan.scope is PlanExecutionScope.SINGLE_STEP
                else "连续"
            )
            plan_title = f"执行计划 · {scope_text}"
        progress = (
            f"共 {len(plan.steps)} 步"
            if plan.kind is PlanKind.PROPOSAL
            else f"{plan.completed_count}/{len(plan.steps)} 已完成"
        )
        print(f"\n[{plan_title} v{plan.revision}] {progress}")
        if plan.explanation:
            print(f"说明：{plan.explanation}")
        for index, step in enumerate(plan.steps, start=1):
            print(f"  {symbols[step.status]} {index}. {step.step}")

    def _print_history(self) -> None:
        history = self._agent.history()
        if not history:
            print("当前还没有对话记录。")
            return
        for message in history:
            print(self._format_history_message(message))

    def _print_context(self) -> None:
        window = self._agent.context_window()
        print(
            "上下文状态（近似值）：\n"
            f"  消息 Token 预算：{self._agent.max_context_tokens}\n"
            f"  完整历史 Token：{window.total_estimated_tokens}\n"
            f"  预计发送 Token：{window.estimated_tokens}\n"
            f"  省略历史消息数：{window.omitted_messages}"
        )

    def _print_stats(self) -> None:
        metrics = self._agent.runtime_metrics
        print(
            "本次运行统计：\n"
            f"  模型请求数：{metrics.model_requests}\n"
            f"  API 尝试次数：{metrics.api_attempts}\n"
            f"  成功请求数：{metrics.successful_requests}\n"
            f"  失败请求数：{metrics.failed_requests}\n"
            f"  自动重试次数：{metrics.retries}\n"
            f"  平均请求耗时：{metrics.average_duration_seconds:.3f} 秒"
        )

    @staticmethod
    def _format_history_message(message: Message) -> str:
        role = message["role"]
        if role == "user":
            return f"您：{message['content']}"
        if role == "tool":
            return f"工具 {message.get('name', '')}：{message['content']}"
        if message.get("tool_calls"):
            names = [call["function"]["name"] for call in message["tool_calls"]]
            return "助手：[请求调用工具：" + "、".join(names) + "]"
        return f"助手：{message.get('content') or ''}"


def main() -> None:
    """启动 DeepSeek Agent 命令行界面。"""
    settings = Settings.from_env()
    logger = build_file_logger(
        PROJECT_ROOT / "logs" / "agent.log",
        settings.log_level,
    )
    CliApplication(Agent(settings, logger=logger)).run()


if __name__ == "__main__":
    main()
