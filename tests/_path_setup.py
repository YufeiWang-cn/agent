import sys
from pathlib import Path


def add_project_root_to_path() -> None:
    """Make ``src`` importable when a test file is executed directly."""
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)
