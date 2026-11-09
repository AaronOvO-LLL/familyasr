# -*- coding: utf-8 -*-
"""服务端硬配额（需求 FR-06）。

约束：
  * 以累计上游音频时长计（秒），静默期间持续上传也计入；
  * 每日以 Asia/Shanghai 自然日重置；
  * 持久化为无内容的用量元数据（JSON 文件），重启不清零、不绕过限额；
  * 文件里只有日期与秒数，绝不写入音频或识别文字。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")


class QuotaStore:
    """线程/协程安全按单线程事件循环假设（uvicorn 单进程）。"""

    def __init__(self, path: Path, daily_limit_seconds: float,
                 now_fn=None, persist_interval: float = 5.0) -> None:
        self.path = Path(path)
        self.daily_limit_seconds = float(daily_limit_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(_TZ))
        self._persist_interval = persist_interval
        self._last_persist = 0.0
        self._dirty = False
        self._date, self._used = self._load()

    # ---------- 时间口径 ----------
    def today(self) -> str:
        return self._now_fn().strftime("%Y-%m-%d")

    def _roll_if_new_day(self) -> None:
        today = self.today()
        if today != self._date:
            self._date = today
            self._used = 0.0
            self._dirty = True

    # ---------- 持久化 ----------
    def _load(self) -> tuple[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            date = str(data.get("date", ""))
            used = float(data.get("used_seconds", 0.0))
            if date and used >= 0:
                return date, used
        except (OSError, ValueError, TypeError):
            pass
        return self.today(), 0.0

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and not self._dirty:
            return
        if not force and now - self._last_persist < self._persist_interval:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"date": self._date, "used_seconds": round(self._used, 2)},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self._last_persist = now
        self._dirty = False

    # ---------- 对外接口 ----------
    def used_today(self) -> float:
        self._roll_if_new_day()
        return self._used

    def remaining_daily(self) -> float:
        return max(0.0, self.daily_limit_seconds - self.used_today())

    def add_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._roll_if_new_day()
        self._used += seconds
        self._dirty = True
        self._save(force=False)

    def flush(self) -> None:
        self._roll_if_new_day()
        self._save(force=True)
