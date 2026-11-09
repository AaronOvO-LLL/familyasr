# -*- coding: utf-8 -*-
"""ASR 提供方工厂。"""
from __future__ import annotations

from ..config import Settings
from .base import AsrProvider
from .mock import MockAsrProvider
from .tencent import TencentAsrProvider


def create_provider(settings: Settings, **kwargs) -> AsrProvider:
    if settings.provider == "mock":
        return MockAsrProvider(settings, **kwargs)
    if settings.provider == "tencent":
        return TencentAsrProvider(settings, **kwargs)
    raise ValueError(f"未知 ASR_PROVIDER: {settings.provider!r}")
