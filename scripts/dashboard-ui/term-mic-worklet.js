// Terminal 0's microphone tap (owner decisions 12 Nov 2026), loaded by term-mic.js into the frame's AudioContext from the
// terminal origin (script-src 'self'). It turns whatever the microphone gives (any rate, any channels) into 16 kHz mono
// signed 16-bit samples: channels averaged, then each output sample the mean of the input samples it covers (a box filter,
// enough against aliasing for speech). Samples go back to the frame in chunks of 20 ms (the segmenter's frame) with the chunk's peak level; "stop"
// sends what is left, marked end, and ends the node. It sees nothing but audio and sends it nowhere but its own frame.
"use strict";
const OUT_RATE = 16000, CHUNK = OUT_RATE / 50;

class TermMic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / OUT_RATE; this.pos = 0; this.acc = 0; this.cnt = 0;
    this.out = new Int16Array(CHUNK); this.k = 0; this.peak = 0; this.stopped = false;
    this.port.onmessage = e => { if (e.data === "stop" && !this.stopped) { this.stopped = true; this.flush(true); } };
  }
  flush(end) {
    const pcm = this.out.slice(0, this.k);
    this.port.postMessage({ pcm, level: this.peak, end }, [pcm.buffer]);
    this.k = 0; this.peak = 0;
  }
  process(inputs) {
    if (this.stopped) return false;
    const ch = inputs[0];
    if (!ch || !ch.length) return true;
    for (let i = 0, n = ch[0].length; i < n; i++) {
      let v = 0;
      for (let c = 0; c < ch.length; c++) v += ch[c][i];
      this.acc += v / ch.length; this.cnt++; this.pos += 1;
      if (this.pos >= this.step) {
        this.pos -= this.step;
        const s = Math.max(-1, Math.min(1, this.acc / this.cnt));
        this.acc = 0; this.cnt = 0;
        this.peak = Math.max(this.peak, Math.abs(s));
        this.out[this.k++] = Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
        if (this.k === CHUNK) this.flush(false);
      }
    }
    return true;
  }
}

registerProcessor("term-mic", TermMic);
