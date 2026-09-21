import { rms } from './vad.js';

/**
 * Microphone capture via AudioWorklet. Calls `onFrame(samples, level, sampleRate)`
 * for every block. Echo cancellation keeps the assistant's own voice out of the mic.
 */
export class MicCapture {
  constructor({ onFrame }) {
    this.onFrame = onFrame;
    this.stream = null;
    this.context = null;
  }

  static get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode);
  }

  get sampleRate() {
    return this.context?.sampleRate ?? 48000;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.context = new AudioContext();
    await this.context.audioWorklet.addModule('/static/js/voice/pcm-worklet.js');
    const source = this.context.createMediaStreamSource(this.stream);
    const node = new AudioWorkletNode(this.context, 'pcm-capture');
    const mute = this.context.createGain();
    mute.gain.value = 0; // keep the graph pulled without playing the mic back
    node.port.onmessage = ({ data }) => this.onFrame(data, rms(data), this.context.sampleRate);
    source.connect(node).connect(mute).connect(this.context.destination);
    if (this.context.state === 'suspended') await this.context.resume();
  }

  async stop() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.context = null;
  }
}
