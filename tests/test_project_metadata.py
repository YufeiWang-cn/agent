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


if __name__ == "__main__":
    unittest.main()
