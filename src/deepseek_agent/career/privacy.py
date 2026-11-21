"""求职联网参数策略；不修改原文，不把正则匹配等同于完整隐私识别。"""

import json
import re
import unicodedata
from urllib.parse import unquote

from ..tools import JsonObject, ToolExecutionError


SENSITIVE = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}"
    r"|(?<!\d)(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}(?!\d)"
    r"|(?<!\d)\d{17}[\dXx](?!\d)"
    r"|(?i:bearer\s+\S+|-----BEGIN[^\n]*PRIVATE KEY|(?:sk|tvly)-[\w-]{8,})"
    r"|(?i:(?:api[_-]?key|access[_-]?token|token|secret|password|密码|身份证|证件号)"
    r"\s*[:=：]\s*[^\s,;&\"}]+)"
)


def check_outbound(arguments: JsonObject) -> None:
    # 仅规范化用于检查，发送的参数不做静默脱敏/改写。
    encoded = json.dumps(arguments, ensure_ascii=False)
    value = unicodedata.normalize("NFKC", encoded)
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    else:
        raise ToolExecutionError("联网参数编码层级过多，请改用简短公开岗位条件。")
    value = unicodedata.normalize("NFKC", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    if SENSITIVE.search(value):
        raise ToolExecutionError(
            "求职联网参数疑似包含联系方式、证件号或凭据，已阻止外发。"
            "请仅使用岗位、城市、技术栈等公开条件重新查询；确认不能绕过此限制。"
        )


def outbound_preview(arguments: JsonObject) -> str:
    payload = dict(
        recipient="第三方 Tavily 搜索/网页提取服务",
        notice="以下是将发送的查询或网页参数；不是仅在本地分析。"
        "网页服务可能访问所列站点。balanced 搜索会发送中英文两个查询。"
        "请核对姓名、私人经历和其他个人信息；正则检查不能识别所有隐私。",
        arguments=arguments,
    )
    preview = json.dumps(payload, ensure_ascii=False, indent=2)
    # 低于现有 CLI 的 2000 字符预览上限，避免先截断再批准隐藏参数。
    if len(preview) > 1800:
        raise ToolExecutionError("联网参数过长，无法完整展示确认；请缩短查询或分批读取网页。")
    return preview
