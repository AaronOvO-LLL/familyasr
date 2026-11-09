/* 家语字幕屏 M1 前端逻辑（原生 JS，无框架、无构建）。
 *
 * 状态机（需求 FR-01）：
 *   idle → requesting → connecting → listening ⇄ recovering → stopping → stopped
 *   任何阶段出错 → error（清理采音与连接后可重新开始）
 *
 * 关键约束：
 *   - 获得麦克风权限后才建立 WebSocket 会话；无权限不创建云端识别会话；
 *   - 仅“正在聆听”状态向后端发送 PCM；
 *   - 停止立即释放麦克风，字幕与回看保留；再次开始才清空；
 *   - 同句修订覆盖原项（SubtitleStore），不串会话；
 *   - 音频与字幕不写入任何持久化存储；仅字号存 localStorage（非敏感）。
 */
(function () {
  "use strict";

  // ---------- DOM ----------
  var $ = function (id) { return document.getElementById(id); };
  var btnToggle = $("btn-toggle");
  var stateText = $("state-text");
  var stateIcon = $("state-icon");
  var micBlock = $("mic-block");
  var usageText = $("usage-text");
  var banner = $("banner");
  var area = $("subtitle-area");
  var listEl = $("subtitle-list");
  var btnNew = $("btn-new");
  var fontSelect = $("font-size");

  // ---------- 状态 ----------
  var STATE_LABEL = {
    idle: ["未开始", "○"],
    requesting: ["正在请求麦克风权限", "…"],
    connecting: ["连接中", "…"],
    listening: ["正在聆听", "●"],
    recovering: ["正在恢复", "◐"],
    stopping: ["正在停止", "…"],
    stopped: ["已停止", "○"],
    error: ["出错", "✕"]
  };
  var state = "idle";
  var backendState = "idle";     // 后端 status 里的状态（决定是否发送音频）
  var store = new SubtitleStore();
  var ws = null;
  var mediaStream = null;
  var audioCtx = null;
  var workletNode = null;
  var sourceNode = null;
  var pingTimer = null;
  var usageTimer = null;
  var clientSessionId = null;
  var wakeLock = null;
  var stopWaitTimer = null;

  function setState(s) {
    state = s;
    var label = STATE_LABEL[s] || [s, "○"];
    stateText.textContent = label[0];
    stateIcon.textContent = label[1];
    document.body.classList.toggle("listening", s === "listening");
    document.body.classList.toggle("error-state", s === "error");
    document.body.classList.toggle("stopped", s === "stopped");
    micBlock.hidden = !(s === "listening" || s === "recovering");
    if (s === "listening" || s === "recovering") {
      btnToggle.textContent = "停止字幕";
      btnToggle.classList.add("stop");
    } else {
      btnToggle.textContent = s === "stopping" ? "正在停止…" : "开始字幕";
      btnToggle.classList.remove("stop");
    }
    btnToggle.disabled = (s === "stopping" || s === "requesting" || s === "connecting");
  }

  function showBanner(text, kind) {
    if (!text) { banner.hidden = true; return; }
    banner.textContent = text;
    banner.className = "banner" + (kind === "info" ? " info" : "");
    banner.hidden = false;
  }

  // ---------- 滚动与回看 ----------
  var atBottom = true;
  area.addEventListener("scroll", function () {
    atBottom = (area.scrollHeight - area.scrollTop - area.clientHeight) < 48;
    if (atBottom) btnNew.hidden = true;
  });
  btnNew.addEventListener("click", function () {
    area.scrollTop = area.scrollHeight;
    atBottom = true;
    btnNew.hidden = true;
  });
  $("btn-latest").addEventListener("click", function () {
    area.scrollTop = area.scrollHeight;
    atBottom = true;
    btnNew.hidden = true;
  });
  $("btn-prev").addEventListener("click", function () {
    // 查看上一句：向上滚动一个句子行（不抢位置）
    var lines = listEl.querySelectorAll(".subtitle-line");
    for (var i = lines.length - 1; i >= 0; i--) {
      if (lines[i].offsetTop < area.scrollTop - 8) {
        area.scrollTop = Math.max(0, lines[i].offsetTop - 12);
        return;
      }
    }
    if (lines.length > 0) area.scrollTop = Math.max(0, lines[0].offsetTop - 12);
  });

  // ---------- 字幕渲染（按 key 增量更新） ----------
  var elByKey = {};
  function renderEntry(rec) {
    var key = rec.session_id + ":" + rec.sentence_id;
    var el = elByKey[key];
    if (!el) {
      el = document.createElement("div");
      elByKey[key] = el;
      listEl.appendChild(el);
    }
    el.className = "subtitle-line " + (rec.is_final ? "final" : "interim");
    el.textContent = rec.text;
  }
  function removeEntry(key) {
    var el = elByKey[key];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    delete elByKey[key];
  }
  function clearAll() {
    listEl.innerHTML = "";
    elByKey = {};
    store.reset();
  }

  function afterTranscriptChange() {
    if (state !== "stopped") {
      // 回看缓存淘汰（停止后冻结，不再淘汰）
      var before = store.map.size;
      store.trim();
      if (store.map.size !== before) {
        // 同步删除被淘汰的 DOM
        var alive = {};
        store.list().forEach(function (r) { alive[r.session_id + ":" + r.sentence_id] = 1; });
        Object.keys(elByKey).forEach(function (k) {
          if (!alive[k]) removeEntry(k);
        });
        showBanner("更早字幕已清除（回看仅保留最近 5 分钟、最多 300 条）", "info");
      }
    }
    if (atBottom) {
      area.scrollTop = area.scrollHeight;
    } else {
      btnNew.hidden = false;
    }
  }

  // ---------- 用量 ----------
  function renderUsage(u) {
    if (!u) return;
    var used = Math.round((u.daily_seconds || 0) / 60);
    var limit = Math.round((u.daily_limit_seconds || 0) / 60);
    usageText.textContent = "今日已用 " + used + " / " + limit + " 分钟";
  }

  // ---------- WebSocket 消息 ----------
  function handleMessage(msg) {
    switch (msg.type) {
      case "status":
        backendState = msg.state || backendState;
        if (msg.state === "connecting") setState("connecting");
        else if (msg.state === "listening") {
          setState("listening");
          requestWakeLock();
          if (msg.detail) showBanner("", null);
        } else if (msg.state === "recovering") {
          setState("recovering");
          showBanner("连接中断，部分内容可能遗漏，正在自动恢复…");
        } else if (msg.state === "stopping") setState("stopping");
        else if (msg.state === "stopped") finishStop(false);
        else if (msg.state === "error") {
          setState("error");
          stopAudio();
        }
        break;
      case "transcript": {
        var res = store.upsert(msg);
        if (res.changed) {
          renderEntry(res.rec);
          afterTranscriptChange();
        }
        break;
      }
      case "error":
        showBanner(msg.message || "出现问题");
        if (msg.code === "quota_daily" || msg.code === "quota_session"
            || msg.code === "idle_timeout" || msg.code === "internal") {
          finishStop(true);
        }
        break;
      case "usage":
        renderUsage(msg);
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  // ---------- 采音与发送 ----------
  function startAudio() {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    sourceNode = audioCtx.createMediaStreamSource(mediaStream);
    return audioCtx.audioWorklet.addModule("/static/worklet.js").then(function () {
      workletNode = new AudioWorkletNode(audioCtx, "pcm-capture");
      workletNode.port.onmessage = function (ev) {
        // 仅“正在聆听”向后端发送音频（FR-01）
        if (ws && ws.readyState === 1 && backendState === "listening") {
          ws.send(ev.data.buffer);
        }
      };
      sourceNode.connect(workletNode);
      // worklet 不需要出声：不连接 destination
    });
  }

  function stopAudio() {
    try { if (workletNode) workletNode.port.onmessage = null; } catch (e) {}
    try { if (workletNode) workletNode.disconnect(); } catch (e) {}
    try { if (sourceNode) sourceNode.disconnect(); } catch (e) {}
    try { if (audioCtx) audioCtx.close(); } catch (e) {}
    workletNode = sourceNode = audioCtx = null;
    if (mediaStream) {
      mediaStream.getTracks().forEach(function (t) { t.stop(); });
      mediaStream = null;
    }
  }

  function requestWakeLock() {
    if (!("wakeLock" in navigator)) {
      showBanner("此浏览器不支持屏幕常亮，请在系统设置里延长息屏时间", "info");
      return;
    }
    navigator.wakeLock.request("screen").then(function (wl) {
      wakeLock = wl;
    }).catch(function () {
      showBanner("无法保持屏幕常亮，请在系统设置里延长息屏时间", "info");
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible"
        && (state === "listening" || state === "recovering") && !wakeLock) {
      requestWakeLock();
    }
  });

  // ---------- 会话启停 ----------
  function startSession() {
    clientSessionId = (crypto.randomUUID && crypto.randomUUID())
      || String(Date.now()) + "-" + Math.random().toString(16).slice(2);
    setState("requesting");
    showBanner("", null);
    clearAll();                       // 再次主动开始：清空旧字幕
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    }).then(function (stream) {
      mediaStream = stream;
      setState("connecting");
      var proto = location.protocol === "https:" ? "wss://" : "ws://";
      ws = new WebSocket(proto + location.host + "/ws/live");
      ws.binaryType = "arraybuffer";
      ws.onopen = function () {
        ws.send(JSON.stringify({ type: "start", client_session_id: clientSessionId }));
        startAudio().catch(function () {
          showBanner("音频初始化失败，请刷新页面重试");
          finishStop(true);
        });
        pingTimer = setInterval(function () {
          if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
        }, 5000);
        usageTimer = setInterval(function () {
          if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "usage" }));
        }, 60000);
      };
      ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        handleMessage(msg);
      };
      ws.onclose = function () {
        if (state === "listening" || state === "recovering" || state === "connecting") {
          setState("error");
          stopAudio();
          showBanner("连接中断，部分内容可能遗漏。可点击“开始字幕”重新开始");
        }
      };
    }).catch(function (err) {
      setState("error");
      stopAudio();
      var hint = "无法使用麦克风：请在浏览器设置中允许麦克风权限后重试";
      if (err && err.name === "NotFoundError") hint = "未检测到麦克风设备";
      else if (err && err.name === "NotReadableError") hint = "麦克风被其他程序占用，请关闭占用后重试";
      showBanner(hint);
    });
  }

  function stopSession() {
    setState("stopping");
    stopAudio();                       // 立即停采音、释放麦克风
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "stop" }));
      // 后端最多 3 秒收尾句后发 stopped；兜底超时自行收尾
      stopWaitTimer = setTimeout(function () { finishStop(true); }, 4500);
    } else {
      finishStop(true);
    }
  }

  function finishStop(force) {
    if (state === "stopped" || state === "idle") return;
    if (stopWaitTimer) { clearTimeout(stopWaitTimer); stopWaitTimer = null; }
    stopAudio();
    setState("stopped");
    backendState = "stopped";
    if (wakeLock) { try { wakeLock.release(); } catch (e) {} wakeLock = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (usageTimer) { clearInterval(usageTimer); usageTimer = null; }
    // 尾句阶段未定稿的句子保留显示并标记（不伪称定稿）
    if (ws && ws.readyState === 1 && force) {
      try { ws.close(); } catch (e) {}
    }
  }

  btnToggle.addEventListener("click", function () {
    if (state === "listening" || state === "recovering"
        || state === "connecting" || state === "requesting") {
      stopSession();
    } else {
      startSession();
    }
  });

  // 页面退出尽力释放资源（后端另有心跳/空闲清理兜底）
  window.addEventListener("pagehide", function () {
    stopAudio();
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ type: "stop" })); ws.close(); } catch (e) {}
    }
  });

  // ---------- 字号与全屏 ----------
  var savedSize = localStorage.getItem("font-size");
  if (savedSize) {
    fontSelect.value = savedSize;
    document.documentElement.style.setProperty("--font-size", savedSize + "px");
  }
  fontSelect.addEventListener("change", function () {
    document.documentElement.style.setProperty("--font-size", fontSelect.value + "px");
    localStorage.setItem("font-size", fontSelect.value);   // 非敏感设置
  });
  $("btn-fullscreen").addEventListener("click", function () {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen();
    }
  });

  setState("idle");
})();
