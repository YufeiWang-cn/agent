# 本地求职助手的核心结构

本轮为结构重构：保留 Tk、CLI、工具名称、交互流程和 SQLite v1 数据格式。没有新增 Web、账号系统、持久任务调度或数据库迁移。

## 调用与依赖

```text
CLI / Tk ── Agent（兼容门面）── bootstrap.assemble_runtime
                  │                      │
                  ▼                      └─ 选择模型、日志、会话、SQLite 和求职扩展
           runtime.AgentRuntime
                  │
                  ├─ BusinessContext：绑定 RunContext，取得求职提示上下文
                  └─ ToolExecutor ── 求职工具适配器
                                         │
CLI / Tk 明确业务操作 ─────────────────────┤
                                         ▼
                                CareerApplication
                                         │
                                   CareerUseCases
                                    │          │
                                领域规则    CareerRepository 协议
                                               │
                                     SqliteCareerRepository
```

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `agent.py` | 保留 `Agent(settings, ...)`、历史方法和异常导入路径；委托装配与运行 |
| `bootstrap.py` | 应用级依赖装配；选择具体模型、存储和业务扩展 |
| `runtime/engine.py` | 模型/工具循环、计划边界、中断记录和会话保存；不直接导入求职或界面模块 |
| `runtime/contracts.py` | 不可变 `RunContext` 与业务上下文扩展协议 |
| `career/commands.py` | UI 和工具共用的类型化命令与确认策略 |
| `career/application.py` | 业务命令分发、确认前置条件、用户输入来源检查、导出协调 |
| `career/use_cases.py` | 事实修订、报告校验、审阅和召回；通过事务协议操作数据 |
| `career/domain.py` | 纯校验与依赖规则；不包含 SQL、模型或界面对象 |
| `career/ports.py` | 事务、文档读取与报告导出协议 |
| `career/context.py` | 事实选择和模型上下文构建 |
| `career/rendering.py` | 从已取得的数据快照生成 Markdown，不访问数据库 |
| `career/tool_adapters.py`、`career/tools.py` | 模型工具参数到业务命令的转换、schema 和白名单 |
| `career/service.py` | 本地求职扩展的兼容装配桥接、每轮上下文及缓存 |
| `infrastructure/sqlite_career.py` | SQLite v1 表结构、SQL、事务提交与回滚 |
| `infrastructure/local_reports.py` | 工作区内的本地报告文件导出 |
| `career/store.py` | 兼容 `CareerStore(path)`；新的业务代码依赖用例和存储协议 |

`CareerService.current_user` 保留为兼容属性，含义是“本轮用户原话”，并非身份。运行时现在显式绑定包含 profile/session/run/turn 标识与用户输入的 `RunContext`。每个 Agent 拥有自己的服务实例，不能作为多用户共享实例使用。

## 确认边界

保存/删除事实、接受/拒绝报告和导出需要确认。模型路径先经过 `ToolExecutor`；它完成确认后，工具适配器再调用 `CareerApplication.execute(..., confirmed=True)`。Tk 和 CLI 的明确删除操作在现有确认窗口或输入确认后调用同一命令。

应用入口默认 `confirmed=False`，敏感写入会被拒绝。`confirmed=True` 是受信任本地入口的内部声明，不是远程认证凭据。后续 Web 不能接受浏览器传入这个值作为授权，必须增加服务端审批记录、身份/归属检查和版本核对。

保留原有界面行为：导入按钮仍准备提示词，用户发送后由模型调用导入工具；没有增加自动导入、自动接受或自动导出。`CareerStore` 与 `CareerUseCases` 属于内部业务层，不负责弹出确认窗口。

## 事务与数据兼容

- 事实当前值更新、修订历史追加及历史裁剪在同一个 SQLite 事务中进行。
- 事务提交成功后才更新进程内 generation；异常回滚不会使缓存错误失效。
- 数据库路径、表结构、PRAGMA user_version=1、文档去重和报告内容格式保持兼容。
- `CareerStore.connection()` 保留给既有本地诊断代码；新业务代码不得借它绕过存储协议。
- 数据库事务与文件导出仍是两个边界，没有新增跨系统原子事务承诺。

## 事件与入口兼容

Tk 的 `AgentChatRunner` 和 CLI 使用统一 `AgentEvent` 流展示文本、工具调用、结果和计划。每个运行时事件附带 `run_id`、`turn_id` 和轮次内递增的 `sequence`。

`Agent.chat` 原有 `on_text`、`on_tool_call`、`on_tool_result` 回调仍保留供既有调用者使用；入口只订阅统一流，避免重复渲染。运行终态仍通过 `TurnOutcome` 或携带该结果的异常返回。

这些事件目前是进程内对象，工具记录可能包含敏感参数。未来 HTTP/SSE 层必须转换为经过筛选的对外 DTO，并单独实现事件保存和重放；事件序号本身不等于断线恢复。

## 如何添加能力

1. 在 `commands.py` 定义命令及确认策略。
2. 在应用入口接入命令；业务规则写入领域函数/用例。
3. 仅在需要新的外部访问时扩展具体端口和基础设施实现。
4. 若交给模型调用，增加 schema 及参数映射；不要复制业务规则。
5. 为业务不变量、事务失败和跨入口一致性添加测试。

## 验证与后续范围

新增 `test_architecture.py` 验证运行时/业务依赖边界、独立上下文、无 SQLite 的用例执行和故障回滚；`test_application_contracts.py` 验证确认、来源/过期检查的一致性以及事件身份和无重复展示。

本轮未实施前次审查提出的报告正文依赖补全、联网隐私控制、持久业务任务、操作幂等、硬预算和求职真实模型评测。这些改进现在有明确的模块落点，后续按优先级实施。单用户、单进程的本地使用仍是当前适用范围。

### 本轮验证记录（2026-11-21）

使用本地 `agent` Conda 环境，三个步骤的相关回归依次通过 139、150、157 项测试；每一步通过后才继续下一步。

最终从仓库根目录运行：

```powershell
python -B -m coverage run -m unittest discover -s tests -v
python -B -m coverage report
python -B -m ruff check src tests --no-cache
python -B -m mypy --no-incremental
git diff --check
```

- 全量发现 378 项：377 项通过，1 项因 Docker Desktop 服务不可用而跳过（容器写入边界测试）。
- 分支覆盖开启的总覆盖率为 79%，通过现有 75% 门槛。
- Ruff 通过；Mypy 检查 83 个源文件通过（沿用项目配置的范围）；差异空白检查通过。
- 新增 10 项测试覆盖架构边界、事务回滚、跨入口规则及事件展示；原有 Tk Markdown 未提交修改保持原样。
- Windows 本次测试进程设定 `GIT_CONFIG_COUNT=0`，避免测试的环境变量 mock 恢复后遗失注入的空配置值、导致 `git init` 失败；没有改全局 Git 配置。

以上是离线自动化验证，未调用付费模型或真实联网服务，未完成真实模型质量评测或逐项手工 GUI 验收。
