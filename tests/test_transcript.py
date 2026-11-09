# -*- coding: utf-8 -*-
"""TranscriptTracker 归并语义测试（FR-02：修订覆盖、定稿冻结、容错）。"""
from app.transcript import TranscriptTracker, extract_sentence_items


def _item(sid, text, final=False, **kw):
    d = {"sentence_id": sid, "text": text, "sentence_type": 1 if final else 0,
         "speaker_id": -1, "start_time": 0, "end_time": 100}
    d.update(kw)
    return d


def test_interim_revision_final():
    t = TranscriptTracker()
    assert t.update(_item(0, "今天")) == "interim"
    assert t.update(_item(0, "今天天气")) == "interim"       # 修订覆盖
    assert t.update(_item(0, "今天天气不错。", final=True)) == "final"
    snap = t.snapshot()
    assert len(snap) == 1
    assert snap[0]["text"] == "今天天气不错。"
    assert snap[0]["is_final"] is True


def test_final_frozen():
    t = TranscriptTracker()
    t.update(_item(0, "好的", final=True))
    assert t.update(_item(0, "好的呀")) == "duplicate"        # 定稿后不再改动
    assert t.snapshot()[0]["text"] == "好的"


def test_out_of_order_shorter_ignored():
    t = TranscriptTracker()
    t.update(_item(1, "明天早上八点"))
    assert t.update(_item(1, "明")) == "ignored"              # 乱序旧文本丢弃
    assert t.snapshot()[0]["text"] == "明天早上八点"


def test_empty_and_invalid_ignored():
    t = TranscriptTracker()
    assert t.update(_item(0, "")) == "ignored"
    assert t.update(_item(-1, "x")) == "ignored"
    assert t.update({"sentence_id": None, "text": "x"}) == "ignored"
    assert t.snapshot() == []


def test_final_shorter_text_still_wins():
    t = TranscriptTracker()
    t.update(_item(2, "一个比较长的临时句"))
    assert t.update(_item(2, "定稿。", final=True)) == "final"   # 定稿覆盖不看长度
    assert t.snapshot()[0]["text"] == "定稿。"


def test_extract_sentence_items_shapes():
    msg = {"sentences": {"sentence_list": [
        {"sentence": "你好", "sentence_id": 0, "sentence_type": 1,
         "speaker_id": 2, "start_time": 1, "end_time": 2},
        "not-a-dict",
    ]}}
    items = extract_sentence_items(msg)
    assert len(items) == 1
    assert items[0]["text"] == "你好"
    assert items[0]["speaker_id"] == 2
    # 兼容顶层 list 与缺省
    assert extract_sentence_items({"sentences": [{"text": "a", "sentence_id": 3}]})[0][
        "text"] == "a"
    assert extract_sentence_items({}) == []
