/* SubtitleStore（字幕归并/回看缓存）Node 单元测试。
 * 运行：node tests/js/test_merge.js
 */
"use strict";
var assert = require("assert");
var path = require("path");
var SubtitleStore = require(path.join(__dirname, "..", "..", "static", "merge.js"))
  .SubtitleStore;

var now = 1000000;
var store = new SubtitleStore({ clock: function () { return now; } });

// 1) 临时句 → 修订 → 定稿：覆盖同 ID
assert.deepStrictEqual(store.upsert({ session_id: "s1", sentence_id: 0, text: "今天" }),
  { changed: true, rec: store.map.get("s1:0") });
store.upsert({ session_id: "s1", sentence_id: 0, text: "今天天气" });
assert.strictEqual(store.map.get("s1:0").text, "今天天气");
store.upsert({ session_id: "s1", sentence_id: 0, text: "今天天气不错。", is_final: true });
assert.strictEqual(store.map.get("s1:0").text, "今天天气不错。");
assert.strictEqual(store.map.get("s1:0").is_final, true);

// 2) 定稿冻结：迟到修订忽略
var r = store.upsert({ session_id: "s1", sentence_id: 0, text: "改了", is_final: false });
assert.strictEqual(r.changed, false);
assert.strictEqual(r.reason, "duplicate");

// 3) 乱序短文本忽略
store.upsert({ session_id: "s1", sentence_id: 1, text: "明天早上八点" });
r = store.upsert({ session_id: "s1", sentence_id: 1, text: "明" });
assert.strictEqual(r.changed, false);
assert.strictEqual(r.reason, "out-of-order");

// 4) 空文本 / 非法 ID 忽略
assert.strictEqual(store.upsert({ session_id: "s1", sentence_id: 2, text: "" }).changed, false);
assert.strictEqual(store.upsert({ session_id: "s1", sentence_id: -1, text: "x" }).changed, false);

// 5) 不串会话：不同 session_id 同号句子互不影响
store.upsert({ session_id: "s2", sentence_id: 0, text: "另一会话", is_final: true });
assert.strictEqual(store.map.get("s2:0").text, "另一会话");
assert.strictEqual(store.map.get("s1:0").text, "今天天气不错。");

// 6) 回看缓存：条数上限淘汰最早
var s2 = new SubtitleStore({ maxSentences: 3, clock: function () { return now; } });
for (var i = 0; i < 5; i++) s2.upsert({ session_id: "s", sentence_id: i, text: "句" + i, is_final: true });
assert.strictEqual(s2.trim(), 2);
assert.strictEqual(s2.map.size, 3);
assert.strictEqual(s2.trimmed, true);
assert.strictEqual(s2.map.has("s:0"), false);   // 最早被淘汰
assert.strictEqual(s2.map.has("s:4"), true);

// 7) 回看缓存：5 分钟时间上限
var s3 = new SubtitleStore({ clock: function () { return now; } });
s3.upsert({ session_id: "s", sentence_id: 0, text: "旧句", is_final: true });
now += 5 * 60 * 1000 + 1;
s3.upsert({ session_id: "s", sentence_id: 1, text: "新句", is_final: true });
assert.strictEqual(s3.trim(), 1);
assert.strictEqual(s3.map.has("s:0"), false);
assert.strictEqual(s3.map.has("s:1"), true);

// 8) reset 清空
s3.reset();
assert.strictEqual(s3.map.size, 0);
assert.strictEqual(s3.trimmed, false);

console.log("merge.js: 全部断言通过");

// ---------------------------------------------------------------------------
// V0.2 说话人标签
// ---------------------------------------------------------------------------
var merge = require(path.join(__dirname, "..", "..", "static", "merge.js"));
var SpeakerNamer = merge.SpeakerNamer;
var normalizeSpeakerId = merge.normalizeSpeakerId;

// 6) 规范化：0 有效，非法/缺失 → -1
assert.strictEqual(normalizeSpeakerId(0), 0);
assert.strictEqual(normalizeSpeakerId(9), 9);
assert.strictEqual(normalizeSpeakerId(10), -1);
assert.strictEqual(normalizeSpeakerId("2"), -1);
assert.strictEqual(normalizeSpeakerId(null), -1);
assert.strictEqual(normalizeSpeakerId(true), -1);

// 7) 归并标签规则：-1 → 有效补全；-1 不撤回已知有效 ID；定稿冻结
var st = new SubtitleStore({ clock: function () { return now; } });
st.upsert({ session_id: "s1", sentence_id: 0, text: "今天", speaker_id: -1 });
assert.strictEqual(st.map.get("s1:0").speaker_id, -1);
st.upsert({ session_id: "s1", sentence_id: 0, text: "今天天气", speaker_id: 0 });
assert.strictEqual(st.map.get("s1:0").speaker_id, 0);        // 0 是有效 ID，保留
st.upsert({ session_id: "s1", sentence_id: 0, text: "今天天气好", speaker_id: -1 });
assert.strictEqual(st.map.get("s1:0").speaker_id, 0);        // -1 不撤回
st.upsert({ session_id: "s1", sentence_id: 0, text: "今天天气好。", is_final: true });
st.upsert({ session_id: "s1", sentence_id: 0, text: "改", speaker_id: 5 });
assert.strictEqual(st.map.get("s1:0").speaker_id, 0);        // 定稿冻结
st.upsert({ session_id: "s1", sentence_id: 1, text: "嗯", speaker_id: 12 });
assert.strictEqual(st.map.get("s1:1").speaker_id, -1);       // 非法 → 未知
st.upsert({ session_id: "s1", sentence_id: 1, text: "嗯好", speaker_id: 3 });
assert.strictEqual(st.map.get("s1:1").speaker_id, 3);        // 未知 → 有效补全

// 8) 匿名编号：会话内稳定、0 有效、未知 null、跨连接重新编号
var namer = new SpeakerNamer();
assert.strictEqual(namer.labelFor("s1", 0), 1);              // 0 是有效 ID → 说话人 1
assert.strictEqual(namer.labelFor("s1", 2), 2);              // 新 ID → 说话人 2
assert.strictEqual(namer.labelFor("s1", 0), 1);              // 同 ID 稳定
assert.strictEqual(namer.labelFor("s1", -1), null);          // 未知 → null（“说话人待确认”）
assert.strictEqual(namer.labelFor("s1", 10), null);          // 非法 → null
assert.strictEqual(namer.labelFor("s2", 0), 1);              // 新连接重新从 1 编号

console.log("test_merge.js 全部通过");
