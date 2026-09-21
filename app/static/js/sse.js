/**
 * Incremental parser for `text/event-stream` bodies read through fetch().
 * (EventSource cannot POST, so we parse the stream ourselves.)
 */
export class SSEParser {
  /** @param {(event: string, data: string) => void} onEvent */
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.buffer = '';
  }

  /** Feed a decoded chunk; complete events are dispatched synchronously. */
  push(chunk) {
    this.buffer += chunk.replace(/\r\n?/g, '\n');
    let boundary = this.buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      this.#dispatch(block);
      boundary = this.buffer.indexOf('\n\n');
    }
  }

  #dispatch(block) {
    let event = 'message';
    const data = [];
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (data.length) this.onEvent(event, data.join('\n'));
  }
}
