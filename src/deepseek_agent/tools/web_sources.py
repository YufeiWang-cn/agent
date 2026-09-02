"""共享的公开网页 URL 校验与来源标识工具。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit


MAX_PUBLIC_URL_LENGTH = 2_048
BLOCKED_HOST_SUFFIXES = (
    ".internal",
    ".lan",
    ".local",
    ".localhost",
    ".home",
)
SENSITIVE_QUERY_NAME_PATTERN = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"token|auth|authorization|credential|password|passwd|secret|session|"
    r"signature|sig)(?:$|[_-])",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?:\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}\b|"
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----)",
    re.IGNORECASE,
)
HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)


class PublicWebUrlError(ValueError):
    """表示 URL 不适合发送给第三方网页服务或返回给模型。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_public_web_url(
    value: object,
    *,
    excluded_domains: Sequence[str] = (),
) -> str:
    """校验公开 HTTP(S) URL，并移除片段、规范主机和默认端口。"""
    if not isinstance(value, str) or not value.strip():
        raise PublicWebUrlError("empty_url")
    url = value.strip()
    if len(url) > MAX_PUBLIC_URL_LENGTH:
        raise PublicWebUrlError("url_too_long")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise PublicWebUrlError("invalid_url")
    if SECRET_VALUE_PATTERN.search(url):
        raise PublicWebUrlError("credential_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PublicWebUrlError("invalid_url") from error
    hostname = parsed.hostname
    if parsed.scheme.casefold() not in {"http", "https"} or hostname is None:
        raise PublicWebUrlError("invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise PublicWebUrlError("credential_url")
    if port is not None and port not in {80, 443}:
        raise PublicWebUrlError("nonstandard_port")

    normalized_host = hostname.casefold().rstrip(".")
    ascii_hostname = _ascii_hostname(normalized_host)
    if _is_non_public_host(normalized_host, ascii_hostname):
        raise PublicWebUrlError("non_public_host")
    comparable_host = ascii_hostname.removeprefix("www.")
    if _hostname_matches_any(comparable_host, excluded_domains):
        raise PublicWebUrlError("excluded_domain")
    if _is_search_redirect(comparable_host, parsed.path):
        raise PublicWebUrlError("search_redirect")
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        if item and SENSITIVE_QUERY_NAME_PATTERN.search(name):
            raise PublicWebUrlError("credential_url")

    display_host = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    default_port = 80 if parsed.scheme.casefold() == "http" else 443
    netloc = display_host if port in {None, default_port} else f"{display_host}:{port}"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def source_id_for_url(url: str) -> str:
    """根据规范 URL 生成不暴露内容、可跨工具复用的稳定来源标识。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"src_{digest}"


def _ascii_hostname(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise PublicWebUrlError("invalid_url") from error


def _is_non_public_host(hostname: str, ascii_hostname: str) -> bool:
    if hostname == "localhost" or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return HOST_PATTERN.fullmatch(ascii_hostname) is None
    return not address.is_global


def _hostname_matches_any(hostname: str, domains: Sequence[str]) -> bool:
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in domains
    )


def _is_search_redirect(hostname: str, path: str) -> bool:
    normalized_path = path.rstrip("/").casefold()
    is_google = hostname.startswith("google.") or ".google." in hostname
    is_bing = hostname == "bing.com" or hostname.endswith(".bing.com")
    is_duckduckgo = hostname == "duckduckgo.com" or hostname.endswith(
        ".duckduckgo.com"
    )
    return (
        (is_google and normalized_path in {"/url", "/goto"})
        or (is_bing and normalized_path == "/ck/a")
        or (is_duckduckgo and normalized_path == "/l")
    )


__all__ = [
    "MAX_PUBLIC_URL_LENGTH",
    "PublicWebUrlError",
    "normalize_public_web_url",
    "source_id_for_url",
]
