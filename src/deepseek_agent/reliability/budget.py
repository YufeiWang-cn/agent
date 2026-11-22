"""进程内调用额度；失败不退款，多级额度在同一临界区内扣减。"""

from contextlib import ExitStack
from threading import Lock

from .errors import ModelCallError


class ModelBudgetExceeded(ModelCallError):
    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        label = "本轮" if scope == "turn" else "整批"
        super().__init__(
            f"{label}模型调用额度已耗尽（{used}/{limit}），任务未完成；"
            "已完成的工具操作会保留，未再发起模型请求。",
            category="budget_exhausted",
            retryable=False,
            partial=False,
        )


class ModelBudget:
    """由批次创建者持有并传给所有 Agent；没有隐式重置或持久化。"""

    def __init__(self, limit: int, *, scope: str = "batch") -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("模型调用额度必须为正整数。")
        if scope not in {"turn", "batch"}:
            raise ValueError("额度范围必须为 turn 或 batch。")
        self._limit = limit
        self._scope = scope
        self._used = 0
        self._lock = Lock()

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return dict(
                scope=self._scope,
                limit=self._limit,
                used=self._used,
                remaining=self._limit - self._used,
            )

    @staticmethod
    def consume(*budgets: "ModelBudget") -> None:
        # 固定锁顺序防止共享批次额度时死锁；任一级拒绝均不扣其他级额度。
        unique = sorted(set(budgets), key=id)
        with ExitStack() as stack:
            for budget in unique:
                stack.enter_context(budget._lock)
            for budget in budgets:
                if budget._used >= budget._limit:
                    raise ModelBudgetExceeded(budget._scope, budget._limit, budget._used)
            for budget in unique:
                budget._used += 1
