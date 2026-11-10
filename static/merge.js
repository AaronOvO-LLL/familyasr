/* 字幕归并与回看缓存（浏览器 / Node 共用，纯逻辑、无 DOM）。
 *
 * 语义（需求 FR-02；V0.2 任务书 §4.B）：
 *   - 以 session_id + sentence_id 为键；同句修订覆盖原项，定稿后冻结；
 *   - 空文本、重复、乱序短文本容错，不产生重复定稿；
 *   - 不串会话：不同 session_id 的同号句子互不影响；
 *   - 说话人 speaker_id：有效取值 0–9（0 是有效值），缺失/非法规范为 -1（未知）；
 *     未定稿句允许 -1 → 有效 ID 标签补全，-1 不撤回已知有效 ID；
 *   - 匿名编号 SpeakerNamer 按 session_id（=上游连接）隔离、仅内存保存：
 *     同一有效上游 ID 显示同一“说话人 N”；重连后新连接重新编号；
 *     未知显示“说话人待确认”，不沿用上一位标签。
 *   - 回看缓存：最近 5 分钟、最多 300 条，先达到的上限生效，淘汰即不可回看。
 */
(function (global) {
  "use strict";

  var DEFAULT_MAX_SENTENCES = 300;
  var DEFAULT_MAX_AGE_MS = 5 * 60 * 1000;

  /** 规范说话人 ID：有效 0–9（0 有效），其余一律 -1（未知）。 */
  function normalizeSpeakerId(v) {
    if (typeof v === "number" && isFinite(v) && Math.floor(v) === v && v >= 0 && v <= 9) {
      return v;
    }
    return -1;
  }

  /** 匿名说话人编号器：按上游连接（session_id）隔离，仅内存保存。 */
  function SpeakerNamer() {
    this._maps = {};   // session_id -> { 上游 ID -> 匿名编号 }
  }
  /** 返回匿名编号（1 起）；未知/非法返回 null（调用方显示“说话人待确认”）。 */
  SpeakerNamer.prototype.labelFor = function (sessionId, speakerId) {
    var id = normalizeSpeakerId(speakerId);
    if (id < 0) return null;
    var map = this._maps[sessionId] || (this._maps[sessionId] = {});
    if (!(id in map)) map[id] = Object.keys(map).length + 1;
    return map[id];
  };

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
    var speaker = normalizeSpeakerId(t.speaker_id);
    if (cur && speaker < 0) {
      speaker = normalizeSpeakerId(cur.speaker_id);   // -1 不撤回已知有效 ID
    }
    var rec = {
      session_id: t.session_id,
      sentence_id: t.sentence_id,
      text: text,
      is_final: !!t.is_final,
      speaker_id: speaker,
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

  var api = { SubtitleStore: SubtitleStore, SpeakerNamer: SpeakerNamer,
              normalizeSpeakerId: normalizeSpeakerId };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  global.SubtitleStore = SubtitleStore;
  global.SpeakerNamer = SpeakerNamer;
})(typeof window !== "undefined" ? window : globalThis);
