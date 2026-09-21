import { MicCapture } from './mic.js';
import { chunksToWavBlob } from './wav.js';

/** Records a voice note (up to `maxSeconds`) and returns it as a 16 kHz WAV Blob. */
export class VoiceNoteRecorder {
  constructor({ onLevel, onTick, onLimit, maxSeconds = 300 } = {}) {
    Object.assign(this, { onLevel, onTick, onLimit, maxSeconds });
    this.chunks = [];
    this.mic = null;
    this.timer = null;
    this.startedAt = 0;
  }

  static get supported() {
    return MicCapture.supported;
  }

  get elapsedSeconds() {
    return this.startedAt ? (performance.now() - this.startedAt) / 1000 : 0;
  }

  async start() {
    this.chunks = [];
    this.mic = new MicCapture({
      onFrame: (samples, level) => {
        this.chunks.push(samples);
        this.onLevel?.(level);
      },
    });
    await this.mic.start();
    this.startedAt = performance.now();
    this.timer = setInterval(() => {
      this.onTick?.(this.elapsedSeconds);
      if (this.elapsedSeconds >= this.maxSeconds) this.onLimit?.();
    }, 250);
  }

  /** Stop and return the recording (or null if nothing was captured). */
  async stop() {
    clearInterval(this.timer);
    const rate = this.mic?.sampleRate ?? 48000;
    await this.mic?.stop();
    this.mic = null;
    const chunks = this.chunks;
    this.chunks = [];
    this.startedAt = 0;
    return chunks.length ? chunksToWavBlob(chunks, rate) : null;
  }

  async cancel() {
    clearInterval(this.timer);
    await this.mic?.stop();
    this.mic = null;
    this.chunks = [];
    this.startedAt = 0;
  }
}
