# 学生求职模式：Agent 应用开发与后端

首版面向计算机相关专业学生的实习与校招，以已有简历优化为主，支持从零经历访谈。沿用 CLI 和 Tk 桌面界面，模型与联网配置继续使用现有 `.env`。

## 启动

```powershell
conda activate agent
python -m pip install -e .
python career_gui.py
```

CLI 使用 `python career.py`。安装后的命令分别是 `career-agent-gui` 和 `career-agent`。原来的 `main.py`、`gui.py` 默认为通用模式；也可通过 `.env` 中 `AGENT_MODE=career` 切换。没有配置真实模型 API Key 时不能进行模型对话。

Windows 在项目目录也可执行 `start_career_gui.cmd` 或 `start_career.cmd`，启动器会明确使用 Conda 的 `agent` 环境，避免误用系统 Python。源码入口会自动添加本项目的 `src`，无需手改为 `src.deepseek_agent` 导入。

诊断环境可执行 `python career_gui.py --check`，或 `start_career_gui.cmd --check`。它只显示解释器、源码位置和依赖状态，不打开窗口或调用模型。PDF 依赖必须安装到实际运行应用的解释器；错误消息会给出对应的完整安装命令。

## 已有简历

1. 将文件放入 `AGENT_WORKSPACE`（默认项目目录），通过桌面“导入简历”选择文件，检查填入的提示后发送；也可直接粘贴文本。支持 UTF-8 TXT、Markdown、DOCX 和可提取文字的 PDF。
2. 使用“简历诊断”，先查看最值得处理的问题；回答职责、技术方法、验证和成果方面的追问。
3. 提供目标 JD，再使用“岗位分析”。系统依据具体要求分析 Agent、后端或复合岗位，并说明证据、缺口及准备建议。
4. 保存经历事实时会出现确认窗口。核对 statement、稳定 key、来源 ID 和 source_quote 后再同意；拒绝不会写入事实。
5. 查看模型展示的逐条原文、建议、理由和事实依据。请求保存为修改报告；新报告始终是草稿。
6. 接受或拒绝报告需要工具确认。请求“把这份报告导出到 review.md”；导出也需要确认，只创建新的 Markdown 文件。
7. 使用“项目面试”围绕项目和目标岗位练习；一次一个问题，反馈中的事实需要确认后才加入长期档案。

没有简历时选择“从零整理”，介绍课程设计、项目、竞赛、科研或实践。经访谈确认后，助手使用 `build_resume_draft` 将事实组织为初稿，再进入相同的优化流程。该初稿保留生成依据，不能被工具反向当作新的个人事实来源。

CLI 命令：

| 命令 | 用途 |
| --- | --- |
| `/resume resume.pdf` | 导入工作区相对路径的简历；路径有空格时可加双引号 |
| `/resume` | 多行粘贴简历；单独输入 `/end` 提交，`/cancel` 取消 |
| `/jd` 或 `/jd job.md` | 粘贴或导入 JD |
| `/diagnose` | 简历诊断 |
| `/match` | 对照目标岗位分析 |
| `/interview` | 开始项目面试 |
| `/memory` | 查看确认事实、来源文档与报告索引 |
| `/forget <完整事实 ID>` | 用户确认后删除事实及纠正历史 |

随附虚构样例 `examples/career/student_resume.md` 和 `examples/career/agent_backend_internship.md`。在 CLI 依次执行 `/resume examples/career/student_resume.md`、`/jd examples/career/agent_backend_internship.md`，然后 `/match`。样例不代表真实学生或招聘信息。

## 长期记忆与版本

- 每个档案保存到 `data/career/<档案 ID>/`，包含 `profile.sqlite3`、默认会话、项目分组和运行日志。设置 `AGENT_CAREER_PROFILE_ID=student_01` 可以使用另一个档案。
- 导入材料只是来源；个人事实需要实际工具确认。来源引文必须逐字存在于导入简历或当前用户输入。JD 不能作为个人经历来源。
- 同一事项使用相同 key，纠正后保留同一事实 ID 并增加 revision；每次模型调用注入当前事实，旧对话不应覆盖纠正。完整来源和其他事实通过业务工具查询。
- 模型修改草稿独立保存，接受报告不会自动改写个人经历。修改项必须引用当前确认事实，原文必须存在于指定简历。
- 事实纠正或删除后，引用旧 revision 的报告会失效，无法继续接受或导出，需要重新生成。
- `/new` 和清空对话不会删除长期档案。删除事实不会自动删除原始简历、导出文件或聊天历史；原始材料仍可能出现旧描述。完整清理时关闭应用，另行管理该档案目录及已导出的文件。
- 数据目录已被 Git 忽略，并禁止通用文件工具直接访问。求职模式提供业务工具及时间、计划、可选联网工具，不开放命令执行和任意文件修改。

## 首版边界

PDF 依赖 pypdf；DOCX 提取正文段落与表格中的文字。读取上限为 5 MB、20 页 PDF 和 50000 字符，超限时拒绝导入，不悄悄截断。扫描 PDF 和图片没有 OCR，旧版 DOC 应转换为 DOCX。复杂排版提取后需要核对文字顺序；详见 [pypdf 文本提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

导出的是包含诊断、逐条修改、来源和审阅状态的 Markdown 报告，本版不提供排版后的 PDF／Word 简历，也不自动投递。

本地档案 ID 用于隔离数据，不是账号认证。本版适用于本地学生助手；多用户服务还需要独立认证与授权设计。确认后的记忆、简历和对话会发送给已配置的模型服务；仅在用户要求实时岗位搜索时使用联网工具，系统提示禁止向搜索参数传递私人简历内容。

来源校验、用户确认和 revision 检查由代码执行；建议的专业判断与语义准确性仍依赖模型，需要用真实简历与 JD 进行人工验收。不能将事实 ID 存在等同于模型建议已经被语义证明。
