# -*- coding: utf-8 -*-
"""配额持久化与北京时间自然日重置测试（FR-06）。"""
import json
from datetime import datetime, timedelta

from app.quota import QuotaStore


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self):
        return self.dt


def test_add_and_remaining(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 10, 0))
    q = QuotaStore(tmp_path / "usage.json", 1800, now_fn=clock)
    q.add_seconds(100)
    q.add_seconds(50.5)
    assert abs(q.used_today() - 150.5) < 1e-6
    assert abs(q.remaining_daily() - 1649.5) < 1e-6


def test_persisted_across_restart(tmp_path):
    path = tmp_path / "usage.json"
    clock = Clock(datetime(2026, 11, 9, 10, 0))
    q1 = QuotaStore(path, 1800, now_fn=clock)
    q1.add_seconds(123)
    q1.flush()
    # 重启（新实例读取文件）不清零
    q2 = QuotaStore(path, 1800, now_fn=clock)
    assert abs(q2.used_today() - 123) < 1e-6
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"date", "used_seconds"}   # 无内容元数据


def test_day_rollover_beijing_midnight(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 23, 59))
    q = QuotaStore(tmp_path / "usage.json", 1800, now_fn=clock)
    q.add_seconds(600)
    clock.dt = datetime(2026, 11, 10, 0, 1)     # 跨过 Asia/Shanghai 自然日
    assert q.used_today() == 0
    assert q.remaining_daily() == 1800


def test_corrupt_file_starts_clean(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{broken", encoding="utf-8")
    clock = Clock(datetime(2026, 11, 9, 8, 0))
    q = QuotaStore(path, 1800, now_fn=clock)
    assert q.used_today() == 0
    q.add_seconds(10)
    q.flush()
    assert json.loads(path.read_text(encoding="utf-8"))["used_seconds"] == 10


def test_negative_add_ignored(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 8, 0))
    q = QuotaStore(tmp_path / "usage.json", 1800, now_fn=clock)
    q.add_seconds(-5)
    assert q.used_today() == 0
