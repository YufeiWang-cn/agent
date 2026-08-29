"""验证项目版本、CI 和示例配置元数据的一致性。"""

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProjectMetadataTests(unittest.TestCase):
    def test_python_version_declarations_are_consistent(self) -> None:
        recommended = (
            PROJECT_ROOT / ".python-version"
        ).read_text(encoding="utf-8").strip()

        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        minimum_match = re.search(
            r'^requires-python\s*=\s*">=([0-9]+\.[0-9]+)"$',
            pyproject,
            re.MULTILINE,
        )
        self.assertIsNotNone(minimum_match)
        self.assertEqual(minimum_match.group(1), recommended)

        environment = (PROJECT_ROOT / "environment.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"  - python={recommended}\n", environment)

        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("python-version-file: .python-version", workflow)

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"推荐使用 Python {recommended}", readme)
        self.assertIn(f"最低要求也为 Python {recommended}", readme)

    def test_windows_ci_enables_python_utf8_mode(self) -> None:
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('PYTHONUTF8: "1"', workflow)

    def test_env_example_does_not_force_a_machine_specific_workspace(self) -> None:
        env_example = (PROJECT_ROOT / ".env.example").read_text(
            encoding="utf-8"
        )

        self.assertNotRegex(env_example, r"(?m)^AGENT_WORKSPACE\s*=")
        self.assertIn(
            "# AGENT_WORKSPACE=C:/path/to/your/workspace",
            env_example,
        )
        self.assertIn("必填", env_example)
        self.assertIn("替换为自己的真实 DeepSeek API Key", env_example)
        self.assertIn("TAVILY_API_KEY=replace_with_your_tavily_api_key", env_example)
        self.assertIn("搜索词会发送给 Tavily", env_example)
        self.assertIn("AGENT_WEB_SEARCH_AUTO_CALLS_PER_TURN=2", env_example)
        self.assertIn("AGENT_WEB_SEARCH_DEFAULT_SCOPE=balanced", env_example)
        self.assertIn("AGENT_WEB_SEARCH_DOMESTIC_RESULTS=3", env_example)
        self.assertIn("AGENT_WEB_SEARCH_INTERNATIONAL_RESULTS=3", env_example)
        self.assertIn("国内检索", env_example)
        self.assertIn("国际检索", env_example)

    def test_user_facing_setup_files_do_not_contain_machine_specific_paths(self) -> None:
        machine_specific_path = re.compile(
            r"(?i)(?:[A-Z]:[\\/](?:Users|Anaconda3|Miniconda3)[\\/]"
            r"|/(?:home|Users)/[^/\s]+/)"
        )
        for relative_path in ("README.md", ".env.example"):
            with self.subTest(path=relative_path):
                content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(content, machine_specific_path)

    def test_system_prompt_keeps_tests_independent_from_implementation(self) -> None:
        prompt = (
            PROJECT_ROOT / "src" / "deepseek_agent" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("用户明确表达的行为和验收条件是首要依据", prompt)
        self.assertIn("不得为了让当前实现通过而删除、放宽或反向调整有效断言", prompt)
        self.assertIn("实现与需求冲突时应修复实现", prompt)


if __name__ == "__main__":
    unittest.main()
