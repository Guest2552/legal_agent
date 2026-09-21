import { MicCapture } from './mic.js';
import { BrowserRecognizer, GeminiRecognizer } from './recognizers.js';
import { SentenceChunker, toSpeakableText } from './speech-text.js';
import { Speaker } from './tts.js';
import { VoiceActivityDetector } from './vad.js';

export const VoiceState = Object.freeze({
  OFF: 'off',
  STARTING: 'starting',
  LISTENING: 'listening',
  THINKING: 'thinking',
  SPEAKING: 'speaking',
});

const BARGE_IN_WARMUP_MS = 700;

/**
 * Hands-free conversational loop - no push-to-talk:
 *
 *   listening --(pause in speech)--> thinking --(first sentence)--> speaking --(done)--> listening
 *
 * Answers stream in and are spoken sentence by sentence. While the assistant is thinking
 * or speaking, a second voice-activity detector watches the microphone; if the user starts
 * talking (louder than the assistant's own echo) the answer is cut off and it listens again.
 */
export class VoiceAgent {
  /**
   * @param {object} options
   * @param {(text: string, ctx: {signal: AbortSignal, onDelta: (delta: string) => void}) => Promise<void>} options.ask
   * @param {(wav: Blob) => Promise<string>} options.transcribe
   * @param {(state: string) => void} [options.onState]
   * @param {(text: string, final: boolean) => void} [options.onCaption]
   * @param {(level: number) => void} [options.onLevel]
   * @param {(message: string, kind: string) => void} [options.onNotice]
   */
  constructor({ ask, transcribe, onState, onCaption, onLevel, onNotice }) {
    Object.assign(this, { ask, transcribe, onState, onCaption, onLevel, onNotice });
    this.state = VoiceState.OFF;
    this.lang = 'en-IN';
    this.engine = 'auto';
    this.bargeIn = true;
    this.speaker = new Speaker();
    this.speaker.onIdle = () => this.#onSpeechFinished();
    this.bargeDetector = new VoiceActivityDetector({ ratio: 3.2, minLevel: 0.03, startMs: 320 });
    this.warmupUntil = 0;
    this.controller = null;
    this.streaming = false;
    this.mic = null;
    this.recognizer = null;
  }

  static get supported() {
    return MicCapture.supported || BrowserRecognizer.supported;
  }

  get active() {
    return this.state !== VoiceState.OFF;
  }

  get engineName() {
    return this.recognizer instanceof GeminiRecognizer ? 'Gemini transcription' : 'Browser speech recognition';
  }

  configure({ lang, engine, bargeIn, rate }) {
    if (lang && lang !== this.lang) {
      this.lang = lang;
      this.speaker.setLanguage(lang);
      this.recognizer?.setLang(lang);
    }
    if (engine) this.engine = engine;
    if (typeof bargeIn === 'boolean') this.bargeIn = bargeIn;
    if (rate) this.speaker.rate = rate;
  }

  async start() {
    if (this.active) return;
    this.#setState(VoiceState.STARTING);
    try {
      if (MicCapture.supported) {
        this.mic = new MicCapture({ onFrame: (samples, level, rate) => this.#onFrame(samples, level, rate) });
        await this.mic.start();
      }
    } catch (error) {
      this.mic = null;
      this.#setState(VoiceState.OFF);
      throw error;
    }
    this.speaker.setLanguage(this.lang);
    if (!this.speaker.hasVoiceFor(this.lang)) {
      this.onNotice?.('No speech voice is installed for this language; answers will be shown as text.', 'info');
    }
    this.recognizer = this.#createRecognizer(this.#preferBrowserEngine());
    this.#listen();
  }

  async stop() {
    this.controller?.abort();
    this.controller = null;
    this.streaming = false;
    this.speaker.cancel();
    this.recognizer?.destroy();
    this.recognizer = null;
    await this.mic?.stop();
    this.mic = null;
    this.#setState(VoiceState.OFF);
  }

  /** Cut off the current answer and listen again (barge-in, Escape key or button). */
  interrupt() {
    if (this.state !== VoiceState.THINKING && this.state !== VoiceState.SPEAKING) return;
    this.controller?.abort();
    this.controller = null;
    this.streaming = false;
    this.speaker.cancel();
    this.#listen();
  }

  #preferBrowserEngine() {
    if (this.engine === 'gemini' && this.mic) return false;
    return BrowserRecognizer.supported;
  }

  #createRecognizer(useBrowser) {
    const handlers = {
      onPartial: (text) => this.onCaption?.(text, false),
      onFinal: (text) => this.#handleUtterance(text),
      onError: (kind, message) => this.#onRecognizerError(kind, message),
    };
    return useBrowser
      ? new BrowserRecognizer({ lang: this.lang, ...handlers })
      : new GeminiRecognizer({ transcribe: this.transcribe, ...handlers });
  }

  #onRecognizerError(kind, message) {
    if (kind === 'permission') {
      this.onNotice?.('Microphone access was blocked. Allow it in the browser to talk.', 'error');
      this.stop();
      return;
    }
    if (this.recognizer instanceof BrowserRecognizer && this.mic) {
      // Network / language problems in the browser engine: switch to Gemini transcription.
      this.recognizer.destroy();
      this.recognizer = this.#createRecognizer(false);
      this.onNotice?.('Switched to Gemini speech recognition.', 'info');
      if (this.state === VoiceState.LISTENING) this.recognizer.start();
      return;
    }
    this.onNotice?.(message, 'error');
  }

  #setState(state) {
    this.state = state;
    this.onState?.(state);
  }

  #listen() {
    this.#setState(VoiceState.LISTENING);
    this.onCaption?.('', false);
    this.recognizer?.start();
  }

  async #handleUtterance(text) {
    if (this.state !== VoiceState.LISTENING) return;
    this.recognizer.pause();
    this.onCaption?.(text, true);
    this.#setState(VoiceState.THINKING);
    this.bargeDetector.reset();
    this.warmupUntil = performance.now() + BARGE_IN_WARMUP_MS;

    const controller = new AbortController();
    this.controller = controller;
    this.streaming = true;
    const chunker = new SentenceChunker();
    try {
      await this.ask(text, {
        signal: controller.signal,
        onDelta: (delta) => chunker.push(delta).forEach((sentence) => this.#say(sentence)),
      });
      if (!controller.signal.aborted) chunker.flush().forEach((sentence) => this.#say(sentence));
    } catch (error) {
      if (!controller.signal.aborted) this.onNotice?.(error.message || 'Could not get an answer.', 'error');
    } finally {
      if (this.controller === controller) {
        this.controller = null;
        this.streaming = false;
      }
    }
    if (this.active && this.state !== VoiceState.LISTENING && !this.speaker.busy && !this.controller) {
      this.#listen();
    }
  }

  #say(sentence) {
    const text = toSpeakableText(sentence);
    if (!text || !this.active || !this.controller) return;
    if (this.state !== VoiceState.SPEAKING) {
      this.#setState(VoiceState.SPEAKING);
      this.bargeDetector.reset();
      this.warmupUntil = performance.now() + BARGE_IN_WARMUP_MS;
    }
    this.speaker.speak(text);
  }

  #onSpeechFinished() {
    if (this.state === VoiceState.SPEAKING && !this.streaming) this.#listen();
  }

  #onFrame(samples, level, sampleRate) {
    const now = performance.now();
    this.onLevel?.(level);
    if (this.recognizer instanceof GeminiRecognizer) {
      this.recognizer.handleFrame(samples, level, sampleRate, now);
    }
    const busy = this.state === VoiceState.THINKING || this.state === VoiceState.SPEAKING;
    if (!this.bargeIn || !busy) return;
    if (now < this.warmupUntil) {
      // Learn how loud the assistant's own echo is; the user must be clearly louder.
      this.bargeDetector.noiseFloor = Math.max(this.bargeDetector.noiseFloor, level);
      return;
    }
    if (this.bargeDetector.process(level, now) === 'start') this.interrupt();
  }
}
