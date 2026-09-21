/**
 * Text helpers for text-to-speech: strip Markdown/citations and cut a streaming answer
 * into speakable sentences as soon as each one is complete (low perceived latency).
 */

/** Turn Markdown into plain speakable text (citations are shown, not spoken). */
export function toSpeakableText(markdown) {
  return String(markdown ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/\[(S\d+(?:\s*[,;]\s*S\d+)*)\]/g, '')
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+[.)]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\|/g, ' ')
    .replace(/[*_`~#]/g, '')
    .replace(/\s+/g, ' ')
    .replace(/\s+([.,;:!?])/g, '$1')
    .trim();
}

// Sentence end: Latin . ! ?, the Devanagari danda, CJK full stops, or a line break.
const SENTENCE_END = /([.!?\u0964\u3002\uff01\uff1f]+["')\]]*(?:\s*\[S\d+(?:\s*[,;]\s*S\d+)*\])*)(\s+)|\n+/g;
const MIN_SENTENCE_CHARS = 24;

export class SentenceChunker {
  constructor() {
    this.pending = '';
  }

  /** Add streamed text; returns the sentences that are now complete. */
  push(text) {
    this.pending += text;
    const sentences = [];
    let lastIndex = 0;
    let carry = '';
    SENTENCE_END.lastIndex = 0;
    let match = SENTENCE_END.exec(this.pending);
    while (match) {
      const end = match.index + (match[1] ? match[1].length : 0);
      const sentence = (carry + this.pending.slice(lastIndex, end)).trim();
      lastIndex = match.index + match[0].length;
      if (sentence.length < MIN_SENTENCE_CHARS) {
        carry = `${sentence} `; // too short to speak alone: merge with the next one
      } else {
        sentences.push(sentence);
        carry = '';
      }
      match = SENTENCE_END.exec(this.pending);
    }
    this.pending = carry + this.pending.slice(lastIndex);
    return sentences;
  }

  /** Return whatever is left at the end of the stream. */
  flush() {
    const rest = this.pending.trim();
    this.pending = '';
    return rest ? [rest] : [];
  }
}
