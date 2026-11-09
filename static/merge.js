/* 字幕归并与回看缓存（浏览器 / Node 共用，纯逻辑、无 DOM）。
 *
 * 语义（需求 FR-02）：
 *   - 以 session_id + sentence_id 为键；同句修订覆盖原项，定稿后冻结；
 *   - 空文本、重复、乱序短文本容错，不产生重复定稿；
 *   - 不串会话：不同 session_id 的同号句子互不影响；
 *   - 回看缓存：最近 5 分钟、最多 300 条，先达到的上限生效，淘汰即不可回看。
 */
(function (global) {
  "use strict";

  var DEFAULT_MAX_SENTENCES = 300;
  var DEFAULT_MAX_AGE_MS = 5 * 60 * 1000;

  function SubtitleStore(opts) {
    opts = opts || {};
    this.maxSentences = opts.maxSentences || DEFAULT_MAX_SENTENCES;
    this.maxAgeMs = opts.maxAgeMs || DEFAULT_MAX_AGE_MS;
    this._clock = opts.clock || function () { return Date.now(); };
    this.map = new Map();       // 保持插入顺序
    this.trimmed = false;       // 是否发生过淘汰（“更早字幕已清除”提示）
  }

  SubtitleStore.prototype.key = function (t) {
    return String(t.session_id) + ":" + String(t.sentence_id);
  };

  /** 归并一条 transcript；返回 {changed, reason?}。 */
  SubtitleStore.prototype.upsert = function (t) {
    if (t.sentence_id === undefined || t.sentence_id === null || t.sentence_id < 0) {
      return { changed: false, reason: "ignored" };
    }
    var text = (t.text || "").trim();
    if (!text) return { changed: false, reason: "ignored" };
    var k = this.key(t);
    var cur = this.map.get(k);
    if (cur && cur.is_final) return { changed: false, reason: "duplicate" };
    if (cur && !t.is_final && text.length < cur.text.length) {
      return { changed: false, reason: "out-of-order" };
    }
    var now = this._clock();
    var rec = {
      session_id: t.session_id,
      sentence_id: t.sentence_id,
      text: text,
      is_final: !!t.is_final,
      speaker_id: (t.speaker_id === undefined ? -1 : t.speaker_id),
      start_time_ms: t.start_time_ms,
      end_time_ms: t.end_time_ms,
      createdAt: cur ? cur.createdAt : now,
      updatedAt: now
    };
    this.map.set(k, rec);       // 已存在则保持原插入序
    return { changed: true, rec: rec };
  };

  /** 按时间与条数淘汰；返回淘汰条数。停止后应不再调用（冻结）。 */
  SubtitleStore.prototype.trim = function () {
    var now = this._clock();
    var removed = 0;
    var self = this;
    this.map.forEach(function (rec, k) {
      if (now - rec.createdAt > self.maxAgeMs) {
        self.map.delete(k);
        removed++;
      }
    });
    while (this.map.size > this.maxSentences) {
      var first = this.map.keys().next().value;
      this.map.delete(first);
      removed++;
    }
    if (removed > 0) this.trimmed = true;
    return removed;
  };

  SubtitleStore.prototype.list = function () {
    return Array.from(this.map.values());
  };

  SubtitleStore.prototype.reset = function () {
    this.map.clear();
    this.trimmed = false;
  };

  var api = { SubtitleStore: SubtitleStore };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.SubtitleStore = SubtitleStore;
})(typeof window !== "undefined" ? window : globalThis);
