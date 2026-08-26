"""在单个 JSON 文件中持久化项目列表。"""

import json
from pathlib import Path
from uuid import uuid4

from .project import Project


class ProjectStoreError(RuntimeError):
    """表示项目数据的读取、校验或保存操作失败。"""

    pass


class JsonProjectStore:
    """管理项目的增删改查，并通过临时文件原子提交完整列表。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def list_projects(self) -> list[Project]:
        return sorted(self._load_all(), key=lambda project: project.created_at)

    def create(self, name: str) -> Project:
        try:
            project = Project.create(name)
        except ValueError as error:
            raise ProjectStoreError(str(error)) from error

        projects = self._load_all()
        if any(item.name.casefold() == project.name.casefold() for item in projects):
            raise ProjectStoreError(f"项目名称已存在：{project.name}")
        projects.append(project)
        self._save_all(projects)
        return project

    def get(self, project_id: str) -> Project:
        value = project_id.strip().lower()
        if not value:
            raise ProjectStoreError("项目 ID 不能为空")
        matches = [
            project
            for project in self._load_all()
            if project.id == value or project.id.startswith(value)
        ]
        if not matches:
            raise ProjectStoreError(f"未找到项目：{project_id}")
        if len(matches) > 1:
            raise ProjectStoreError(f"项目 ID 前缀不唯一：{project_id}")
        return matches[0]

    def rename(self, project_id: str, name: str) -> Project:
        projects = self._load_all()
        current = self._find(projects, project_id)
        try:
            renamed = current.renamed(name)
        except ValueError as error:
            raise ProjectStoreError(str(error)) from error
        if any(
            item.id != current.id and item.name.casefold() == renamed.name.casefold()
            for item in projects
        ):
            raise ProjectStoreError(f"项目名称已存在：{renamed.name}")
        updated = [renamed if item.id == current.id else item for item in projects]
        self._save_all(updated)
        return renamed

    def delete(self, project_id: str) -> Project:
        projects = self._load_all()
        project = self._find(projects, project_id)
        self._save_all([item for item in projects if item.id != project.id])
        return project

    @staticmethod
    def _find(projects: list[Project], project_id: str) -> Project:
        value = project_id.strip().lower()
        if not value:
            raise ProjectStoreError("项目 ID 不能为空")
        matches = [
            project
            for project in projects
            if project.id == value or project.id.startswith(value)
        ]
        if not matches:
            raise ProjectStoreError(f"未找到项目：{project_id}")
        if len(matches) > 1:
            raise ProjectStoreError(f"项目 ID 前缀不唯一：{project_id}")
        return matches[0]

    def _load_all(self) -> list[Project]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("项目数据必须是 JSON 对象")
            raw_projects = data.get("projects")
            if not isinstance(raw_projects, list):
                raise ValueError("缺少 projects 列表")
            return [Project.from_dict(item) for item in raw_projects]
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectStoreError(f"无法读取项目数据：{error}") from error

    def _save_all(self, projects: list[Project]) -> None:
        # 每次使用唯一临时文件，避免并发或崩溃遗留文件相互覆盖。
        temporary_path = self._path.with_name(
            f".{self._path.name}.{uuid4().hex}.tmp"
        )
        content = json.dumps(
            {"version": 1, "projects": [item.to_dict() for item in projects]},
            ensure_ascii=False,
            indent=2,
        )
        try:
            temporary_path.write_text(content, encoding="utf-8")
            temporary_path.replace(self._path)
        except OSError as error:
            raise ProjectStoreError(f"无法保存项目数据：{error}") from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
