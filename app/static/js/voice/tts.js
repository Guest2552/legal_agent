/**
 * Queue-based wrapper around the Web Speech synthesis API.
 * Sentences are spoken as they stream in; `onIdle` fires when the queue drains.
 */
export class Speaker {
  constructor() {
    this.lang = 'en-IN';
    this.rate = 1;
    this.voice = null;
    this.pending = 0;
    this.generation = 0; // invalidates callbacks of cancelled utterances
    this.onStart = null;
    this.onIdle = null;
    if (Speaker.supported) {
      speechSynthesis.addEventListener('voiceschanged', () => this.setLanguage(this.lang));
    }
  }

  static get supported() {
    return typeof window !== 'undefined' && 'speechSynthesis' in window;
  }

  get busy() {
    return this.pending > 0;
  }

  /** Best local voice for `lang`: exact locale first, then same language; prefer natural voices. */
  static pickVoice(lang) {
    if (!Speaker.supported) return null;
    const voices = speechSynthesis.getVoices();
    const base = lang.split('-')[0].toLowerCase();
    const score = (voice) => (/(natural|neural|online|google)/i.test(voice.name) ? 1 : 0);
    const exact = voices.filter((v) => v.lang.toLowerCase() === lang.toLowerCase());
    const sameLanguage = voices.filter((v) => v.lang.toLowerCase().split(/[-_]/)[0] === base);
    const pool = exact.length ? exact : sameLanguage;
    return pool.sort((a, b) => score(b) - score(a))[0] ?? null;
  }

  hasVoiceFor(lang) {
    return Boolean(Speaker.pickVoice(lang));
  }

  setLanguage(lang) {
    this.lang = lang;
    this.voice = Speaker.pickVoice(lang);
  }

  speak(text) {
    if (!Speaker.supported || !text) return;
    const generation = this.generation;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = this.voice?.lang ?? this.lang;
    if (this.voice) utterance.voice = this.voice;
    utterance.rate = this.rate;
    const finished = () => {
      if (generation !== this.generation) return;
      this.pending = Math.max(0, this.pending - 1);
      if (this.pending === 0) this.onIdle?.();
    };
    utterance.onend = finished;
    utterance.onerror = finished;
    if (this.pending === 0) this.onStart?.();
    this.pending += 1;
    speechSynthesis.speak(utterance);
  }

  cancel() {
    this.generation += 1;
    this.pending = 0;
    if (Speaker.supported) speechSynthesis.cancel();
  }
}
