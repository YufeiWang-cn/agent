"""提取工作区内的简历文本；原始文件保持不变。"""

from io import BytesIO
import sys
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from ..workspace import WorkspaceGuard
from .store import CareerDataError


class DocumentReader:
    def __init__(self, workspace: Path) -> None:
        self.guard = WorkspaceGuard(workspace, 5_000_000)

    def read(self, path: str) -> str:
        resolved, data = self.guard.read_bytes(path)
        suffix = resolved.suffix.lower()
        try:
            if suffix in {".txt", ".md"}:
                content = data.decode("utf-8-sig")
            elif suffix == ".docx":
                content = self._docx(data)
            elif suffix == ".pdf":
                content = self._pdf(data)
            else:
                raise CareerDataError("支持 TXT、Markdown、PDF 和 DOCX；旧版 DOC 请先另存为 DOCX。")
        except (UnicodeError, BadZipFile, ElementTree.ParseError, KeyError) as error:
            raise CareerDataError("文档格式无效，请检查文件，或粘贴简历文本。") from error
        if not content.strip():
            raise CareerDataError("未提取到文本；扫描件或图片简历请先进行 OCR，或直接粘贴文字。")
        if len(content) > 50_000:
            raise CareerDataError("提取文本超过 50000 字符，请缩短文档；没有截断或保存部分内容。")
        return content.strip()

    @staticmethod
    def _docx(data: bytes) -> str:
        with ZipFile(BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 30_000_000:
                raise CareerDataError("DOCX 解压体积过大。")
            xml = archive.read("word/document.xml")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise CareerDataError("不支持包含 DTD 或实体定义的 DOCX。")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(namespace + "p"):
            parts = []
            for element in paragraph.iter():
                if element.tag == namespace + "t":
                    parts.append(element.text or "")
                elif element.tag in {namespace + "br", namespace + "tab"}:
                    parts.append(" ")
            paragraphs.append("".join(parts))
        return "\n".join(paragraphs)

    @staticmethod
    def _pdf(data: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            interpreter = Path(sys.executable)
            if interpreter.name.lower() == "pythonw.exe":
                interpreter = interpreter.with_name("python.exe")
            project = Path(__file__).resolve().parents[3]
            raise CareerDataError(
                "当前解释器未安装 PDF 解析依赖 pypdf。"
                f"请在 PowerShell 执行：& \"{interpreter}\" -m pip install -e \"{project}\"，"
                "完成后重启应用。注意应安装到运行此应用的解释器，不能使用其他环境的 pip。"
            ) from error
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise CareerDataError("请先解除 PDF 密码保护。")
            if len(reader.pages) > 20:
                raise CareerDataError("最多读取 20 页，请提供简历或精简后的岗位描述。")
            parts = []
            for page in reader.pages:
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > 5_000_000:
                    raise CareerDataError("PDF 页面内容过大，请转换为文本。")
                parts.append(page.extract_text() or "")
                if sum(len(part) for part in parts) > 50_000:
                    raise CareerDataError("PDF 文本超过 50000 字符。")
            return "\n".join(parts)
        except CareerDataError:
            raise
        except Exception as error:
            raise CareerDataError("无法解析 PDF，请检查格式，或粘贴简历文本。") from error
