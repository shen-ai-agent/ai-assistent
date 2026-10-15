// AudioWorklet: lấy mono từ mic, resample về 16kHz, gom thành frame Int16 100ms.
// Gửi về main thread: { pcm: ArrayBuffer (Int16 LE), level: RMS 0..1 }.

const TARGET_RATE = 16000;
const FRAME = 1600; // 100ms

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // sampleRate: biến toàn cục của AudioWorkletGlobalScope
    this.pos = 0;          // vị trí đọc (theo mẫu đầu vào) của mẫu 16kHz kế tiếp
    this.prev = 0;         // mẫu cuối của block trước, để nội suy qua ranh giới block
    this.frame = new Int16Array(FRAME);
    this.n = 0;
    this.sumSq = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (this.ratio === 1) {
      for (let i = 0; i < ch.length; i++) this.push(ch[i]);
      return true;
    }
    // Nội suy tuyến tính; x[-1] = this.prev
    while (this.pos < ch.length - 1) {
      const i = Math.floor(this.pos);
      const t = this.pos - i;
      const a = i < 0 ? this.prev : ch[i];
      const b = ch[i + 1];
      this.push(a + (b - a) * t);
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    this.prev = ch[ch.length - 1];
    return true;
  }

  push(x) {
    const s = Math.max(-1, Math.min(1, x));
    this.frame[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    this.sumSq += s * s;
    if (this.n === FRAME) {
      const level = Math.sqrt(this.sumSq / FRAME);
      this.port.postMessage({ pcm: this.frame.buffer, level }, [this.frame.buffer]);
      this.frame = new Int16Array(FRAME);
      this.n = 0;
      this.sumSq = 0;
    }
  }
}

registerProcessor("capture", CaptureProcessor);
