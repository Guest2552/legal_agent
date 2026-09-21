/* AudioWorklet: forwards ~43 ms blocks of raw microphone samples to the main thread. */
const BLOCK_SIZE = 2048;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(BLOCK_SIZE);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      let read = 0;
      while (read < channel.length) {
        const count = Math.min(channel.length - read, BLOCK_SIZE - this.offset);
        this.block.set(channel.subarray(read, read + count), this.offset);
        this.offset += count;
        read += count;
        if (this.offset === BLOCK_SIZE) {
          this.port.postMessage(this.block.slice(0));
          this.offset = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
