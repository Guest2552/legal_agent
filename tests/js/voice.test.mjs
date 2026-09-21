import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SSEParser } from '../../app/static/js/sse.js';
import { SentenceChunker, toSpeakableText } from '../../app/static/js/voice/speech-text.js';
import { VoiceActivityDetector, rms } from '../../app/static/js/voice/vad.js';
import { downsample, encodeWav, mergeChunks } from '../../app/static/js/voice/wav.js';

test('VAD: detects speech start after sustained loudness and end after silence', () => {
  const vad = new VoiceActivityDetector({ startMs: 150, silenceMs: 600 });
  const events = [];
  let t = 0;
  const feed = (level, ms) => {
    for (const end = t + ms; t < end; t += 50) {
      const event = vad.process(level, t);
      if (event) events.push([event, t]);
    }
  };
  feed(0.002, 500); // background noise
  feed(0.2, 1000); // speech
  feed(0.002, 1000); // silence
  assert.deepEqual(events.map(([e]) => e), ['start', 'end']);
  assert.ok(events[0][1] >= 650 && events[0][1] <= 700, 'start after ~150 ms of speech');
  assert.ok(events[1][1] >= 2050, 'end after ~600 ms of silence');
});

test('VAD: short clicks do not count as speech', () => {
  const vad = new VoiceActivityDetector({ startMs: 200 });
  assert.equal(vad.process(0.5, 0), null);
  assert.equal(vad.process(0.001, 50), null);
  assert.equal(vad.process(0.5, 100), null);
  assert.equal(vad.speaking, false);
});

test('VAD: noise floor adapts so a noisy room raises the threshold', () => {
  const vad = new VoiceActivityDetector();
  const quiet = vad.threshold;
  for (let t = 0; t < 5000; t += 50) vad.process(0.009, t);
  assert.ok(vad.threshold > quiet);
});

test('rms of a constant signal', () => {
  assert.ok(Math.abs(rms(new Float32Array([0.5, -0.5, 0.5, -0.5])) - 0.5) < 1e-9);
  assert.equal(rms(new Float32Array()), 0);
});

test('WAV: 16-bit mono header and payload length', () => {
  const samples = new Float32Array([0, 1, -1, 0.5]);
  const view = new DataView(encodeWav(samples, 16000));
  const text = (offset) => String.fromCharCode(...new Uint8Array(view.buffer, offset, 4));
  assert.equal(text(0), 'RIFF');
  assert.equal(text(8), 'WAVE');
  assert.equal(view.getUint16(22, true), 1); // mono
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(40, true), samples.length * 2);
  assert.equal(view.getInt16(46, true), 32767); // +1.0 clipped to max
  assert.equal(view.getInt16(48, true), -32768);
});

test('WAV: merge and downsample 48 kHz to 16 kHz', () => {
  const merged = mergeChunks([new Float32Array(3).fill(1), new Float32Array(3).fill(1)]);
  assert.equal(merged.length, 6);
  assert.equal(downsample(merged, 48000, 16000).length, 2);
  assert.equal(downsample(merged, 16000, 16000), merged);
});

test('speech text: citations and markdown are not read aloud', () => {
  const spoken = toSpeakableText('## Answer\n- You get **Rs. 50,000** back [S1][S2].\n1. See [the Act](https://x.in).');
  assert.equal(spoken, 'Answer You get Rs. 50,000 back. See the Act.');
});

test('sentence chunker emits complete sentences as the stream arrives', () => {
  const chunker = new SentenceChunker();
  assert.deepEqual(chunker.push('Your deposit must be returned within 30 da'), []);
  assert.deepEqual(chunker.push('ys [S1]. The landlord can deduct only unpaid rent. And'), [
    'Your deposit must be returned within 30 days [S1].',
    'The landlord can deduct only unpaid rent.',
  ]);
  assert.deepEqual(chunker.flush(), ['And']);
});

test('sentence chunker merges very short fragments and understands the Hindi danda', () => {
  const chunker = new SentenceChunker();
  const out = chunker.push('Yes. आपकी जमा राशि 30 दिनों में वापस मिलनी चाहिए। ठीक है');
  assert.deepEqual(out, ['Yes. आपकी जमा राशि 30 दिनों में वापस मिलनी चाहिए।']);
});

test('SSE parser handles events split across network chunks', () => {
  const events = [];
  const parser = new SSEParser((event, data) => events.push([event, data]));
  parser.push('event: sources\ndata: {"a":1}\n\nevent: del');
  parser.push('ta\ndata: {"text":"Hi"}\r\n\r\n: keep-alive\n\n');
  assert.deepEqual(events, [
    ['sources', '{"a":1}'],
    ['delta', '{"text":"Hi"}'],
  ]);
});
