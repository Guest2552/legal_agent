import { chunksToWavBlob } from './wav.js';
import { VoiceActivityDetector } from './vad.js';

const NativeRecognition =
  typeof window === 'undefined' ? undefined : window.SpeechRecognition || window.webkitSpeechRecognition;

const PREROLL_FRAMES = 10; // ~0.4 s kept before speech is detected, so first words are not clipped
const MIN_UTTERANCE_SECONDS = 0.5;

/**
 * Engine 1 - the browser's streaming speech recognition (Chrome/Edge/Safari).
 * Continuous and hands-free: an utterance is committed after a short pause.
 */
export class BrowserRecognizer {
  static get supported() {
    return Boolean(NativeRecognition);
  }

  constructor({ lang, onPartial, onFinal, onError, commitDelayMs = 1100 }) {
    Object.assign(this, { lang, onPartial, onFinal, onError, commitDelayMs });
    this.active = false;
    this.finalText = '';
    this.interim = '';
    this.timer = null;
    this.recentRestarts = [];
    this.recognition = null;
  }

  #create() {
    const recognition = new NativeRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.lang = this.lang;
    recognition.onresult = (event) => {
      this.interim = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) this.finalText += `${result[0].transcript} `;
        else this.interim += result[0].transcript;
      }
      this.onPartial(`${this.finalText}${this.interim}`.trim());
      clearTimeout(this.timer);
      // Wait longer while the engine is still unsure (interim text pending).
      this.timer = setTimeout(() => this.#commit(), this.commitDelayMs + (this.interim ? 700 : 0));
    };
    recognition.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return;
      if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(event.error)) {
        this.active = false;
        this.onError('permission', 'Microphone access was blocked.');
      } else {
        this.onError(event.error, `Speech recognition error: ${event.error}`);
      }
    };
    recognition.onend = () => {
      if (!this.active) return;
      // Browsers stop after long silences; restart unless it is failing in a tight loop.
      const now = Date.now();
      this.recentRestarts = this.recentRestarts.filter((t) => now - t < 5000).concat(now);
      if (this.recentRestarts.length > 6) {
        this.active = false;
        this.onError('unstable', 'Speech recognition keeps stopping.');
        return;
      }
      try {
        recognition.start();
      } catch {
        /* already started */
      }
    };
    return recognition;
  }

  #commit() {
    const text = `${this.finalText}${this.interim}`.trim();
    this.finalText = '';
    this.interim = '';
    if (text && this.active) this.onFinal(text);
  }

  start() {
    if (this.active) return;
    this.active = true;
    this.finalText = '';
    this.interim = '';
    this.recognition ??= this.#create();
    try {
      this.recognition.start();
    } catch {
      /* already started */
    }
  }

  pause() {
    this.active = false;
    clearTimeout(this.timer);
    this.finalText = '';
    this.interim = '';
    this.recognition?.abort();
  }

  setLang(lang) {
    this.lang = lang;
    if (this.recognition) this.recognition.lang = lang;
  }

  destroy() {
    this.pause();
    this.recognition = null;
  }
}

/**
 * Engine 2 - works in every browser with a microphone: our own voice-activity
 * detection cuts utterances, which are sent as 16 kHz WAV to Gemini for transcription.
 */
export class GeminiRecognizer {
  constructor({ transcribe, onPartial, onFinal, onError }) {
    Object.assign(this, { transcribe, onPartial, onFinal, onError });
    this.vad = new VoiceActivityDetector({ silenceMs: 1000 });
    this.preroll = [];
    this.chunks = [];
    this.active = false;
    this.recording = false;
  }

  start() {
    this.active = true;
    this.recording = false;
    this.chunks = [];
    this.vad.reset();
  }

  pause() {
    this.active = false;
    this.recording = false;
    this.chunks = [];
  }

  setLang() {
    /* Gemini detects the spoken language automatically. */
  }

  destroy() {
    this.pause();
  }

  /** Called for every microphone block (also while paused, to keep the pre-roll warm). */
  handleFrame(samples, level, sampleRate, now) {
    this.preroll.push(samples);
    if (this.preroll.length > PREROLL_FRAMES) this.preroll.shift();
    if (!this.active) return;
    const event = this.vad.process(level, now);
    if (event === 'start') {
      this.recording = true;
      this.chunks = [...this.preroll];
      this.onPartial('...');
      return;
    }
    if (this.recording) this.chunks.push(samples);
    if (event === 'end') {
      this.recording = false;
      const chunks = this.chunks;
      this.chunks = [];
      this.#send(chunks, sampleRate);
    }
  }

  async #send(chunks, sampleRate) {
    const seconds = chunks.reduce((total, chunk) => total + chunk.length, 0) / sampleRate;
    if (seconds < MIN_UTTERANCE_SECONDS) return;
    this.active = false; // one utterance at a time
    this.onPartial('Transcribing...');
    try {
      const text = (await this.transcribe(chunksToWavBlob(chunks, sampleRate))).trim();
      if (text) {
        this.onFinal(text);
        return;
      }
      this.onPartial('');
    } catch (error) {
      this.onError('transcribe', error.message || 'Transcription failed.');
    }
    this.start();
  }
}
