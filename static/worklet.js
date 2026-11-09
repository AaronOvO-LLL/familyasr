/* AudioWorklet：采音 → 16kHz / 16bit LE / 单声道 PCM（需求 FR-04）。
 *
 * - 输入为 AudioContext 原生采样率（44.1k/48k 等）的 Float32；
 * - 多声道下混单声道后线性插值重采样到 16k（跨块相位连续）；
 * - 每 200ms 凑满 6400 字节（3200 采样）postMessage 到主线程，
 *   由主线程按 1:1 实时速率经 WebSocket 发送二进制帧。
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.srcRate = sampleRate;           // AudioWorklet 全局：上下文实际采样率
    this.dstRate = 16000;
    this.step = this.srcRate / this.dstRate;
    this.pos = 0.0;
    this.prev = 0.0;
    this.pending = [];                    // int16 采样缓冲
    this.CHUNK_SAMPLES = 3200;            // 200ms @16k
  }

  pushSample(v) {
    var s = Math.round(Math.max(-1, Math.min(1, v)) * 32767);
    this.pending.push(s);
  }

  process(inputs) {
    var input = inputs[0];
    if (!input || input.length === 0 || !input[0]) return true;
    var frames = input[0].length;
    var nCh = input.length;
    // 下混单声道
    var mono = new Float32Array(frames);
    for (var i = 0; i < frames; i++) {
      var sum = 0;
      for (var c = 0; c < nCh; c++) sum += input[c][i];
      mono[i] = sum / nCh;
    }
    if (this.srcRate === this.dstRate) {
      for (var j = 0; j < frames; j++) this.pushSample(mono[j]);
    } else {
      // 线性插值（与 M0 验证工具同一算法，保持帧间相位连续）
      var buf = new Float32Array(frames + 1);
      buf[0] = this.prev;
      buf.set(mono, 1);
      while (this.pos < buf.length - 1) {
        var idx = Math.floor(this.pos);
        var frac = this.pos - idx;
        var a = buf[idx], b = buf[idx + 1];
        this.pushSample(a + (b - a) * frac);
        this.pos += this.step;
      }
      this.pos -= frames;
      this.prev = mono[frames - 1];
    }
    // 每 200ms 发一包
    while (this.pending.length >= this.CHUNK_SAMPLES) {
      var chunk = new Int16Array(this.pending.splice(0, this.CHUNK_SAMPLES));
      this.port.postMessage({ type: "pcm", buffer: chunk.buffer }, [chunk.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
