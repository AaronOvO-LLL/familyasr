# -*- coding: utf-8 -*-
"""访问鉴权与来源校验（需求 FR-06）。

约定：
  * 家庭访问码 ACCESS_TOKEN 通过 ?token= 首次进入后写入 HttpOnly Cookie，
    页面与 WebSocket 均校验；浏览器 WebSocket 握手自动携带 Cookie；
  * WebSocket 还校验 Origin 与 Host 一致（同源），阻断跨站 WebSocket 劫持；
  * 比较使用 hmac.compare_digest，避免时序侧信道；
  * 任何失败响应不回显访问码。
"""
from __future__ import annotations

import hmac
from urllib.parse import parse_qs

COOKIE_NAME = "familyasr_token"


def token_ok(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def extract_token_from_query(query_string: str) -> str | None:
    qs = parse_qs(query_string or "")
    vals = qs.get("token")
    return vals[0] if vals else None


def extract_token_from_cookie(header_value: str | None) -> str | None:
    if not header_value:
        return None
    for part in header_value.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return v.strip()
    return None


def extract_token(scope_or_headers, query_string: str = "") -> str | None:
    """ASGI scope（含 headers）或 (cookie_header, query) 组合里取访问码。"""
    cookie_header = None
    if isinstance(scope_or_headers, dict):
        for k, v in scope_or_headers.get("headers", []):
            if k.lower() == b"cookie":
                cookie_header = v.decode("latin-1")
        query_string = scope_or_headers.get("query_string", query_string)
        if isinstance(query_string, bytes):
            query_string = query_string.decode("latin-1")
    return (extract_token_from_cookie(cookie_header)
            or extract_token_from_query(query_string))


def origin_ok(origin: str | None, host: str | None) -> bool:
    """同源校验：无 Origin（非浏览器客户端）放行，有则必须与 Host 一致。"""
    if not origin:
        return True
    if not host:
        return False
    # origin 形如 https://192.168.1.5:7020；取 netloc 部分比较
    netloc = origin.split("://", 1)[-1].rstrip("/")
    return netloc.lower() == host.strip().lower()
