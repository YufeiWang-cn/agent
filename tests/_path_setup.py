"""为源码树测试提供统一的包导入路径设置。"""

import sys
from pathlib import Path


def add_src_to_path() -> None:
    """让测试在未安装项目时也能从 ``src`` 目录导入包。"""
    project_root = Path(__file__).resolve().parents[1]
    src_root_text = str(project_root / "src")
    if src_root_text not in sys.path:
        sys.path.insert(0, src_root_text)
