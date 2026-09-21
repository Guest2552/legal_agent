import { SSEParser } from './sse.js';

/** Error carrying the server's user-safe message. */
export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

async function request(path, { method = 'GET', json, body, signal } = {}) {
  const headers = {};
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body, signal, credentials: 'same-origin' });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('Network error: check your connection and try again.', 0, 'network');
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    let code = 'http_error';
    try {
      const data = await response.json();
      message = data?.error?.message ?? message;
      code = data?.error?.code ?? code;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message, response.status, code);
  }
  return response;
}

const toJson = (response) => response.json();

export const api = {
  config: () => request('/api/config').then(toJson),

  listDocuments: () => request('/api/documents').then(toJson),

  uploadDocument(file, signal) {
    const form = new FormData();
    form.append('file', file, file.name);
    return request('/api/documents', { method: 'POST', body: form, signal }).then(toJson);
  },

  pasteText: (name, text) => request('/api/documents/text', { method: 'POST', json: { name, text } }).then(toJson),

  documentText: (id) => request(`/api/documents/${encodeURIComponent(id)}/text`).then(toJson),

  deleteDocument: (id) => request(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  clearDocuments: () => request('/api/documents', { method: 'DELETE' }),

  listConversations: () => request('/api/conversations').then(toJson),

  getConversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`).then(toJson),

  renameConversation: (id, title) =>
    request(`/api/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', json: { title } }),

  deleteConversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  clearConversations: () => request('/api/conversations', { method: 'DELETE' }),

  analysis: (kind, payload, signal) =>
    request(`/api/analysis/${kind}`, { method: 'POST', json: payload, signal }).then(toJson),

  async transcribe(blob, signal) {
    const form = new FormData();
    form.append('audio', blob, 'speech.wav');
    const data = await request('/api/voice/transcribe', { method: 'POST', body: form, signal }).then(toJson);
    return data.text ?? '';
  },

  exportDocx: (title, markdown) =>
    request('/api/export/docx', { method: 'POST', json: { title, markdown } }).then((r) => r.blob()),

  /**
   * POST a chat question and consume the SSE answer stream.
   * @param {object} payload
   * @param {{signal?: AbortSignal, onEvent: (event: string, data: any) => void}} options
   */
  async chatStream(payload, { signal, onEvent }) {
    const response = await request('/api/chat', { method: 'POST', json: payload, signal });
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SSEParser((event, data) => onEvent(event, JSON.parse(data)));
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.push(value);
    }
  },
};
