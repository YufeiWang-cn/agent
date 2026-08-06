from pathlib import Path
from uuid import uuid4


DEFAULT_BLOCKED_NAMES = frozenset({".env", ".git", ".venv", "__pycache__"})  # 匹配任意一层的名称
DEFAULT_BLOCKED_PREFIXES = (Path("data/sessions"), Path("logs"))  # 匹配从工作区根目录开始的路径前缀


class WorkspaceAccessError(RuntimeError):
    pass


class WorkspaceGuard:
    def __init__(self, root: Path, max_file_size: int) -> None:
        if max_file_size <= 0:
            raise ValueError("max_file_size 必须大于 0")
        try:
            resolved_root = root.expanduser().resolve(strict=True)
        except OSError as error:
            raise WorkspaceAccessError(f"工作目录不可用：{root}") from error
        if not resolved_root.is_dir():
            raise WorkspaceAccessError(f"工作目录不是文件夹：{root}")
        self._root = resolved_root
        self._max_file_size = max_file_size

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_file_size(self) -> int:
        return self._max_file_size

    def resolve_directory(self, user_path: str) -> Path:
        path = self._resolve(user_path)
        if not path.exists():
            raise WorkspaceAccessError(f"目录不存在：{user_path}")
        if not path.is_dir():
            raise WorkspaceAccessError(f"路径不是目录：{user_path}")
        return path

    def read_text(self, user_path: str) -> tuple[Path, str]:
        path = self._resolve(user_path)
        if not path.exists():
            raise WorkspaceAccessError(f"文件不存在：{user_path}")
        if not path.is_file():
            raise WorkspaceAccessError(f"路径不是文件：{user_path}")
        try:
            content = path.read_bytes()
            if len(content) > self._max_file_size:
                raise WorkspaceAccessError(
                    f"文件超过大小限制 {self._max_file_size} 字节：{user_path}"
                )
            return path, content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceAccessError(f"文件不是有效的 UTF-8 文本：{user_path}") from error
        except OSError as error:
            raise WorkspaceAccessError(f"无法读取文件：{user_path}") from error

    def write_text(self, user_path: str, content: str) -> tuple[Path, bool, int]:
        path = self._resolve(user_path)
        try:
            encoded_content = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WorkspaceAccessError("写入内容不能编码为 UTF-8") from error
        if len(encoded_content) > self._max_file_size:
            raise WorkspaceAccessError(
                f"写入内容超过大小限制 {self._max_file_size} 字节：{user_path}"
            )
        if path.exists() and not path.is_file():
            raise WorkspaceAccessError(f"写入目标不是文件：{user_path}")
        if not path.parent.exists() or not path.parent.is_dir():
            raise WorkspaceAccessError(f"目标文件的父目录不存在：{user_path}")

        created = not path.exists()
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(encoded_content)
            temporary_path.replace(path)
        except OSError as error:
            raise WorkspaceAccessError(f"无法写入文件：{user_path}") from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path, created, len(encoded_content)

    def relative_path(self, path: Path) -> str:
        relative = path.relative_to(self._root)
        return "." if not relative.parts else relative.as_posix()  # as_posix() 可以避免 Windows 的反斜杠在 JSON 中出现大量转义

    def is_accessible(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self._root)
            self._resolve(relative.as_posix())
            return True
        except (ValueError, WorkspaceAccessError):
            return False

    def _resolve(self, user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise WorkspaceAccessError("路径必须是非空字符串")
        requested_path = Path(user_path.strip())
        if requested_path.is_absolute():
            raise WorkspaceAccessError(f"必须使用相对于工作目录的路径：{user_path}")
        try:
            candidate = (self._root / requested_path).resolve(strict=False)
        except OSError as error:
            raise WorkspaceAccessError(f"无法解析路径：{user_path}") from error
        try:
            relative = candidate.relative_to(self._root)
        except ValueError as error:
            raise WorkspaceAccessError(f"禁止访问工作目录之外的路径：{user_path}") from error
        self._ensure_not_blocked(relative, user_path)
        return candidate

    @staticmethod
    def _ensure_not_blocked(relative: Path, user_path: str) -> None:
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if any(part in DEFAULT_BLOCKED_NAMES for part in lowered_parts):
            raise WorkspaceAccessError(f"禁止访问敏感路径：{user_path}")

        lowered_relative = Path(*lowered_parts)
        for blocked_prefix in DEFAULT_BLOCKED_PREFIXES:
            if (
                lowered_relative == blocked_prefix
                or blocked_prefix in lowered_relative.parents
            ):
                raise WorkspaceAccessError(f"禁止访问敏感路径：{user_path}")
