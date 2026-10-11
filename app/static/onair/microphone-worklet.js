/* Native-rate PCM capture stays on the audio thread when the UI is busy. */
class RadioMicrophoneCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(4800);
    this.offset = 0;
    this.enabled = false;
    this.port.onmessage = (event) => {
      this.enabled = Boolean(event.data?.enabled);
      this.offset = 0;
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!this.enabled || !input) return true;
    for (const value of input) {
      const sample = Math.max(-1, Math.min(1, value));
      this.samples[this.offset++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.offset === this.samples.length) {
        const bytes = this.samples.buffer;
        this.port.postMessage(bytes, [bytes]);
        this.samples = new Int16Array(4800);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('radiotedu-microphone', RadioMicrophoneCapture);
