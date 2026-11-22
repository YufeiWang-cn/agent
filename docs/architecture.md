# 本地求职助手的核心结构

核心结构重构保留 Tk、CLI 和工具名称。后续证据依赖改进已将 SQLite 升级至 v2：结构化报告正文与派生初稿均记录事实版本依赖。没有新增 Web、账号系统或持久任务调度。迁移及接口说明见 [求职助手文档](career.md)。

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
| `infrastructure/sqlite_career.py` | SQLite v2 表结构、v1 备份迁移、SQL、事务提交与回滚 |
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
- 数据库路径保持兼容；v1→v2 迁移前备份、失败回滚，保留旧内容但不自动认证旧依赖。
- 初稿和报告在保存事务中采集事实版本；报告依赖合并初稿、正文判断与逐条修改的引用。
- 新报告以 findings/general_advice/changes 为输入，由纯渲染层生成 Markdown；旧 markdown 入口只保留待核验存档能力。
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

报告正文与初稿依赖已补全。求职联网增加 `career/privacy.py` 参数检查与 `career/network_tools.py` 工具适配器，在业务注册表接入逐次确认并复用原有网络客户端；不修改通用模式或通用执行器。持久业务任务、操作幂等、硬预算和求职真实模型评测仍是后续计划。单用户、单进程的本地使用仍是当前适用范围。

### 核心结构重构验证记录（2026-11-21，证据依赖改进之前）

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

### 证据依赖改进验证记录（2026-11-21）

先添加复现测试，确认旧实现会放行无引用自由正文报告，以及允许使用事实已纠正的旧初稿生成报告；两条测试在修复前均失败。随后实施结构化正文、初稿依赖与 v1→v2 迁移。

- 新增 `test_career_evidence.py` 共 14 项，覆盖正文/修改/初稿依赖并集、事实修订/删除、一般建议分区、跨工具与应用入口、初稿去重与重建，以及迁移保留、备份失败、事务回滚、WAL 数据备份和未来版本拒绝。
- 相关回归 56 项通过；全量发现 392 项，其中 391 项通过，1 项因 Docker 服务不可用跳过。
- 开启分支覆盖的总覆盖率为 79.61%，通过 75% 门槛；Ruff 与 Mypy（83 个源文件，沿用项目配置）通过。
- 原有求职用例和入口测试更新为结构化报告输入，保留原有业务断言；旧 Markdown 入口另有禁止接受/导出的回归。
- 仅使用临时数据库与合成材料，没有主动升级真实档案、调用外部模型或进行手工 GUI 全流程验收。

### 求职联网隐私验证记录（2026-11-21）

先用假网络 transport 复现搜索手机号外发、网页 question 邮箱外发，以及自动额度内绕过用户拒绝的三项问题；修复前均失败。新增 `test_career_privacy.py` 共 12 项，验证敏感内容阻止、编码检查、完整预览、拒绝/取消零请求、中英文双路确认、普通岗位查询与通用模式兼容。

- 相关回归 90 项通过；Ruff、Mypy（85 个源文件）和差异检查通过。
- 初次全量运行在已有双路搜索取消测试处触发 `Tcl_AsyncDelete: async handler deleted by the wrong thread`；后台线程触发垃圾回收时销毁了前面测试遗留的 Tk 对象。
- 使用临时测试入口关闭自动循环垃圾回收，并在每项测试结束后于主线程主动收集，仍发现并执行全部 404 项测试：403 项通过，1 项因 Docker 服务不可用跳过。分支覆盖开启的总覆盖率为 79.71%。没有跳过 GUI 测试或修改测试断言；此测试入口规避回收时机问题，并不表示已修复产品中的 Tk 对象生命周期。
- 没有调用真实搜索服务或付费模型，没有发送真实个人材料。本次不包括全局历史日志脱敏或模型请求的隐私治理。

必要时可将下列内容保存为临时脚本，从仓库根目录以 `python -m coverage run <脚本路径>` 复现主线程回收的测试入口：

```python
import gc
import unittest

class MainThreadCollectionResult(unittest.TextTestResult):
    def stopTest(self, test):
        super().stopTest(test)
        gc.collect()

gc.disable()
suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(
    verbosity=2, resultclass=MainThreadCollectionResult
).run(suite)
gc.collect()
raise SystemExit(not result.wasSuccessful())
```


### 求职离线评测（2026-11-22）

新增独立的求职多轮评测入口，不改变原有通用单轮 EvalRunner。`evaluation/career_cases.py` 严格加载案例，`career_script.py` 管理离线动作与按操作确认，`career_runner.py` 创建隔离 Agent/SQLite/假网络并收集逐轮结果，`career_graders.py` 检查业务状态、轨迹、产物和外发，`career_cli.py` 生成报告。共用现有 CheckResult/EvalResult、工作区快照与运行指标格式。

14 个合成案例、30 轮交互各重复两次，28/28 通过。对两个正常闭环案例注入缺失业务快照，两个负对照均在业务状态检查中失败；工具轨迹和成功回答不变。这是评分器敏感性验证，不是模型性能对照。

相关回归 82 项通过；全量 410 项，其中 409 项通过，1 项因 Docker 不可用跳过。覆盖率 80.06%；Ruff、Mypy（90 个源文件）和差异检查通过。沿用主线程回收临时测试入口，未跳过 GUI 用例。全量日志中“离线脚本 0/1”来自验证失败报告与非零退出码的负向单测，并非正式案例运行失败。

正式离线报告为 `data/evals/career-offline.json`，属于被 Git 忽略的生成数据。真实 API 调用为 0。真人评分、真实模型提示词基线及成本/质量比较尚未执行；试点的调用上限、token 预算公式和人工评分量表见 [求职评测说明](../evals/career/README.md)。
