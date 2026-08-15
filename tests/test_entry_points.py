import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent import cli, gui_app


class EntryPointTests(unittest.TestCase):
    def test_cli_entry_point_builds_agent_and_runs_it(self) -> None:
        settings = SimpleNamespace(log_level="INFO")
        logger = Mock()
        agent = Mock()
        application = Mock()

        with (
            patch.object(cli.Settings, "from_env", return_value=settings),
            patch.object(cli, "build_file_logger", return_value=logger),
            patch.object(cli, "Agent", return_value=agent) as agent_class,
            patch.object(
                cli,
                "CliApplication",
                return_value=application,
            ) as application_class,
        ):
            cli.main()

        agent_class.assert_called_once_with(settings, logger=logger)
        application_class.assert_called_once_with(agent)
        application.run.assert_called_once_with()

    def test_gui_entry_point_builds_logger_and_starts_gui(self) -> None:
        settings = SimpleNamespace(log_level="DEBUG")
        logger = Mock()

        with (
            patch.object(gui_app.Settings, "from_env", return_value=settings),
            patch.object(gui_app, "build_file_logger", return_value=logger),
            patch.object(gui_app, "run_gui") as run_gui,
        ):
            gui_app.main()

        run_gui.assert_called_once_with(settings, logger=logger)


if __name__ == "__main__":
    unittest.main()
