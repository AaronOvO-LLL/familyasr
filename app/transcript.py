# -*- coding: utf-8 -*-
"""识别结果解析与归并（纯函数，来自 M0 验证工具并入应用）。

语义（需求 FR-02 / FR-05）：
  * sentence_type=1 表示句子定稿；final=1 表示整流结束，两者不混用；
  * 同一 sentence_id 的修订覆盖原项，定稿后不再改动；
  * 空文本、重复、乱序、非法 ID 容错，不产生重复定稿；
  * 每个上游连接（session_id）独立归并，不串会话。
"""
from __future__ import annotations


def extract_sentence_items(message: dict) -> list[dict]:
    """从一条服务端消息里取出句子列表。

    真实腾讯云实时 V2 返回（2026-11-09 实测抓包，与官方文档示例不一致）：
      {"code":0, ..., "result": {"slice_type": 0|1|2, "index": N,
       "start_time": ms, "end_time": ms, "voice_text_str": "...", ...}}
      slice_type：0=句首块，1=修订中，2=定稿；index=句子序号（从 0 自增）。
    同时兼容文档旧形状：sentences 为 {"sentence_list": [...]} 或 [...]。
    """
    out: list[dict] = []

    def _item(sid, text, final, speaker, start, end) -> dict:
        return {"sentence_id": sid, "sentence_type": 1 if final else 0,
                "speaker_id": speaker, "text": text,
                "start_time": start, "end_time": end}

    # —— 实测形状：result 对象 ——
    res = message.get("result")
    if isinstance(res, dict) and ("voice_text_str" in res or "slice_type" in res):
        speaker = -1
        si = res.get("speaker_info")
        if isinstance(si, dict):
            cand = si.get("speaker_id", si.get("id", -1))
            speaker = cand if isinstance(cand, int) else -1
        out.append(_item(
            sid=res.get("index", -1),
            text=res.get("voice_text_str", "") or "",
            final=res.get("slice_type") == 2,
            speaker=speaker,
            start=res.get("start_time"),
            end=res.get("end_time")))
        return out

    # —— 文档旧形状：sentences ——
    sentences = message.get("sentences")
    if isinstance(sentences, dict):
        items = sentences.get("sentence_list", [])
    elif isinstance(sentences, list):
        items = sentences
    else:
        items = []
    for s in items:
        if not isinstance(s, dict):
            continue
        out.append(_item(
            sid=s.get("sentence_id", -1),
            text=(s.get("sentence") if s.get("sentence") is not None
                  else s.get("text", "")) or "",
            final=s.get("sentence_type", 0) == 1,
            speaker=s.get("speaker_id", -1),
            start=s.get("start_time"),
            end=s.get("end_time")))
    return out


class TranscriptTracker:
    """按 sentence_id 归并：临时句覆盖同 ID，定稿后冻结；重复/乱序/空文本容错。

    只属于一个上游连接；跨连接必须新建实例，旧连接迟到消息由调用方
    按 session_id 隔离后才送达本类。
    """

    def __init__(self) -> None:
        self._items: dict[int, dict] = {}   # sentence_id -> 记录
        self._order: list[int] = []

    def update(self, item: dict) -> str:
        """返回事件类型：'interim' | 'final' | 'duplicate' | 'ignored'。"""
        sid = item.get("sentence_id")
        text = (item.get("text") or "").strip()
        if sid is None or sid < 0:
            return "ignored"
        if not text:
            return "ignored"
        cur = self._items.get(sid)
        is_final = item.get("sentence_type") == 1
        if cur is None:
            self._items[sid] = {**item, "text": text, "is_final": is_final}
            self._order.append(sid)
            return "final" if is_final else "interim"
        if cur["is_final"]:
            # 已定稿：迟到的重复/修订消息一律不再改动
            return "duplicate"
        if is_final or len(text) >= len(cur["text"]):
            cur.update(item)
            cur["text"] = text
            cur["is_final"] = is_final
            return "final" if is_final else "interim"
        return "ignored"   # 更短的乱序旧文本，丢弃

    def snapshot(self) -> list[dict]:
        return [self._items[s] for s in self._order]
