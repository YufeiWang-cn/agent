"""为隔离评测工作区创建可重复比较的文件快照。"""

import hashlib
import os
from pathlib import Path


IGNORED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
IGNORED_FILE_NAMES = frozenset({".coverage"})


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(root: Path) -> dict[str, str]:
    """返回工作区所有稳定文件的相对路径及 SHA-256。"""
    resolved_root = root.resolve(strict=True)
    snapshot: dict[str, str] = {}
    for directory, directory_names, file_names in os.walk(
        resolved_root,
        followlinks=False,
    ):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRECTORY_NAMES
            and not (Path(directory) / name).is_symlink()
        )
        for file_name in sorted(file_names):
            if file_name in IGNORED_FILE_NAMES:
                continue
            path = Path(directory) / file_name
            relative_path = path.relative_to(resolved_root).as_posix()
            if path.is_symlink():
                snapshot[relative_path] = "symlink:" + os.readlink(path)
            elif path.is_file():
                snapshot[relative_path] = _hash_file(path)
    return snapshot


def changed_paths(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """返回创建、删除或内容发生变化的路径。"""
    return tuple(
        sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
    )


__all__ = ["changed_paths", "snapshot_workspace"]
