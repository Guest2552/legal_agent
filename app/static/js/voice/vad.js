/**
 * Energy-based voice activity detector with an adaptive noise floor.
 *
 * Feed it one RMS value per audio frame; it returns 'start' when speech begins,
 * 'end' when the speaker has been quiet for `silenceMs`, otherwise null.
 * Pure logic (no Web Audio) so it is unit-tested in Node.
 */
export class VoiceActivityDetector {
  constructor({
    ratio = 2.5, // speech must be this many times louder than the noise floor
    minLevel = 0.012, // absolute minimum RMS treated as speech
    startMs = 180, // sustained loudness needed to start
    silenceMs = 900, // quiet time that ends an utterance
    maxUtteranceMs = 30000,
    initialFloor = 0.005,
  } = {}) {
    Object.assign(this, { ratio, minLevel, startMs, silenceMs, maxUtteranceMs, initialFloor });
    this.reset();
  }

  reset() {
    this.noiseFloor = this.initialFloor;
    this.speaking = false;
    this.loudSince = null;
    this.lastLoud = 0;
    this.startedAt = 0;
  }

  get threshold() {
    return Math.max(this.minLevel, this.noiseFloor * this.ratio);
  }

  /** @returns {'start' | 'end' | null} */
  process(rms, timeMs) {
    const loud = rms > this.threshold;
    if (!this.speaking) {
      if (!loud) {
        this.loudSince = null;
        // Track background noise slowly; clamp so a noisy room cannot silence detection.
        this.noiseFloor = Math.min(0.2, this.noiseFloor * 0.95 + rms * 0.05);
        return null;
      }
      this.loudSince ??= timeMs;
      if (timeMs - this.loudSince < this.startMs) return null;
      this.speaking = true;
      this.startedAt = this.loudSince;
      this.lastLoud = timeMs;
      return 'start';
    }
    if (loud) this.lastLoud = timeMs;
    const silentTooLong = timeMs - this.lastLoud >= this.silenceMs;
    const tooLong = timeMs - this.startedAt >= this.maxUtteranceMs;
    if (silentTooLong || tooLong) {
      this.speaking = false;
      this.loudSince = null;
      return 'end';
    }
    return null;
  }
}

/** Root-mean-square level of a block of samples. */
export function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return samples.length ? Math.sqrt(sum / samples.length) : 0;
}
