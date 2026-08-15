import sys
from pathlib import Path


def add_src_to_path() -> None:
    """Make the installed package layout importable in source-tree tests."""
    project_root = Path(__file__).resolve().parents[1]
    src_root_text = str(project_root / "src")
    if src_root_text not in sys.path:
        sys.path.insert(0, src_root_text)
