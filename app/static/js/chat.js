import { api } from './api.js';
import { $, announce, autoGrow, el, icon, toast } from './dom.js';
import { renderMarkdown } from './markdown.js';
import { assistantOptions, bus, languageTag, state, updateSettings } from './state.js';
import { toSpeakableText } from './voice/speech-text.js';
import { Speaker } from './voice/tts.js';

const STARTERS = [
  ['Summarise my document', 'What am I agreeing to, in plain words?', 'Summarise this document in plain language. What am I agreeing to?'],
  ['Obligations and deadlines', 'What must I do, and by when?', 'What are my obligations and deadlines under this document?'],
  ['Risky clauses', 'What is unfair, and what should I change?', 'Which clauses are risky or unfair for me, and what should I ask to change?'],
  ['Ending the agreement', 'Can they end it early? What about my money?', 'Can the other party end this agreement early? What happens to my money if they do?'],
];

const GROUNDING_LABELS = {
  grounded: ['badge-ok', 'check', 'Grounded in your documents'],
  partially_grounded: ['badge-warn', 'alert', 'Partly verified'],
  unverified: ['badge-bad', 'alert', 'Some quotes not found in your documents'],
  general_knowledge: ['badge-info', 'globe', 'General legal information'],
};

/**
 * Chat panel: grounded, streaming answers with clickable citations, saved to history.
 * @param {{history: object, openAddDocuments: () => void}} deps
 */
export function initChat({ history, openAddDocuments }) {
  const log = $('#chat-log');
  const thread = $('#chat-thread');
  const form = $('#chat-form');
  const input = $('#chat-input');
  const sendButton = $('#send-btn');
  const stopButton = $('#stop-btn');
  const scope = $('#chat-scope');
  const webSearch = $('#web-search');
  const sourcesByMessage = new WeakMap();
  const reader = new Speaker();
  const resize = autoGrow(input);
  let conversationId = null;
  let controller = null;

  webSearch.checked = state.settings.webSearch;
  webSearch.addEventListener('change', () => updateSettings({ webSearch: webSearch.checked }));

  // ---------------------------------------------------------------- rendering

  function renderWelcome() {
    const hasDocuments = state.documents.length > 0;
    thread.replaceChildren(
      el(
        'div',
        { class: 'welcome' },
        el('div', { class: 'welcome-mark', 'aria-hidden': 'true' }, icon('scale')),
        el('h1', {}, 'How can I help with your legal documents?'),
        el(
          'p',
          { class: 'welcome-sub' },
          hasDocuments
            ? 'Ask anything about your documents. Every answer points to the exact clause, and every quote is checked.'
            : 'Add a contract, notice or policy, or ask a general legal question. Answers cite the exact clause.',
        ),
        el(
          'div',
          { class: 'starter-grid' },
          STARTERS.map(([title, subtitle, prompt]) =>
            el('button', { type: 'button', class: 'starter', onClick: () => ask(prompt) }, el('strong', {}, title), el('span', {}, subtitle)),
          ),
        ),
        hasDocuments
          ? null
          : el(
              'div',
              { class: 'welcome-actions' },
              el('button', { type: 'button', class: 'btn btn-primary', onClick: openAddDocuments }, icon('plus'), 'Add a document'),
            ),
      ),
    );
  }

  const isWelcome = () => Boolean(thread.querySelector('.welcome'));

  function userMessage(text, voice) {
    return el(
      'div',
      { class: 'msg msg-user' },
      el('div', { class: 'bubble' }, voice ? el('span', { class: 'voice-tag' }, 'Spoken') : null, text),
    );
  }

  function assistantMessage() {
    const answer = el('div', { class: 'answer', lang: languageTag() });
    answer.append(el('div', { class: 'typing', 'aria-label': 'Thinking' }, el('span'), el('span'), el('span')));
    const footer = el('div', { class: 'answer-footer' });
    const message = el(
      'div',
      { class: 'msg msg-ai' },
      el('span', { class: 'avatar', 'aria-hidden': 'true' }, icon('scale')),
      el('div', { class: 'bubble' }, answer, footer),
    );
    return { message, answer, footer };
  }

  function paintAnswer(answer, text) {
    // renderMarkdown escapes all model text and only emits a fixed set of safe tags.
    answer.innerHTML = renderMarkdown(text);
  }

  function renderFooter(parts, text, meta) {
    const { message, footer } = parts;
    sourcesByMessage.set(message, new Map((meta.sources ?? []).map((s) => [s.id, s])));
    message.dataset.quotes = JSON.stringify(meta.grounding?.quotes ?? []);
    const grounding = meta.grounding ?? { verdict: 'general_knowledge', quotes: [], cited_sources: [] };
    const [tone, iconName, label] = GROUNDING_LABELS[grounding.verdict] ?? GROUNDING_LABELS.general_knowledge;
    const verified = grounding.quotes.filter((q) => q.status === 'verified').length;
    const detail = grounding.quotes.length ? ` · ${verified}/${grounding.quotes.length} quotes verified` : '';
    const unmatched = grounding.quotes.filter((q) => q.status !== 'verified');
    const webSources = meta.web_sources ?? [];

    const children = [
      el('span', { class: `badge ${tone}` }, icon(iconName), `${label}${detail}`),
      el(
        'span',
        { class: 'answer-actions' },
        el('button', { type: 'button', class: 'icon-btn', title: 'Copy', 'aria-label': 'Copy answer', onClick: () => copy(text) }, icon('copy')),
        el('button', { type: 'button', class: 'icon-btn', title: 'Read aloud', 'aria-label': 'Read answer aloud', onClick: () => readAloud(text) }, icon('speaker')),
      ),
      unmatched.length
        ? el(
            'details',
            { class: 'answer-details' },
            el('summary', {}, 'Quotes that did not match exactly'),
            el('ul', {}, unmatched.map((q) => el('li', {}, `"${q.text}" (${Math.round(q.coverage * 100)}% match)`))),
          )
        : null,
      webSources.length
        ? el(
            'details',
            { class: 'answer-details' },
            el('summary', {}, `Checked on the web (${webSources.length} sources)`),
            el('ul', {}, webSources.map((s) => el('li', {}, el('a', { href: s.uri, target: '_blank', rel: 'noopener noreferrer' }, s.title)))),
          )
        : null,
    ];
    footer.replaceChildren(...children.filter(Boolean));
  }

  const scrollToEnd = () => {
    log.scrollTop = log.scrollHeight;
  };

  const setBusy = (busy) => {
    sendButton.hidden = busy;
    stopButton.hidden = !busy;
    log.setAttribute('aria-busy', String(busy));
  };

  // ---------------------------------------------------------------- actions

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Answer copied.');
    } catch {
      toast('Copy failed: your browser blocked clipboard access.', 'error');
    }
  }

  function readAloud(text) {
    if (!Speaker.supported) {
      toast('Speech is not supported in this browser.', 'error');
      return;
    }
    reader.cancel();
    reader.setLanguage(languageTag());
    reader.rate = state.settings.speechRate;
    reader.speak(toSpeakableText(text));
  }

  function newChat() {
    controller?.abort();
    conversationId = null;
    history.setActive(null);
    renderWelcome();
    input.focus();
  }

  async function openConversation(id) {
    controller?.abort();
    let detail;
    try {
      detail = await api.getConversation(id);
    } catch (error) {
      toast(error.message, 'error');
      return;
    }
    conversationId = id;
    history.setActive(id);
    thread.replaceChildren();
    for (const message of detail.messages) {
      if (message.role === 'user') {
        thread.append(userMessage(message.content, message.meta.voice));
        continue;
      }
      const parts = assistantMessage();
      paintAnswer(parts.answer, message.content);
      renderFooter(parts, message.content, message.meta);
      thread.append(parts.message);
    }
    requestAnimationFrame(scrollToEnd);
  }

  /**
   * Ask a question and stream the answer. Shared by typed questions and the voice agent.
   * @returns {Promise<string>} the full answer text
   */
  async function ask(text, { voice = false, signal, onDelta } = {}) {
    const question = text.trim();
    if (!question) return '';
    if (isWelcome()) thread.replaceChildren();
    thread.append(userMessage(question, voice));
    const parts = assistantMessage();
    thread.append(parts.message);
    scrollToEnd();

    const local = new AbortController();
    controller = local;
    signal?.addEventListener('abort', () => local.abort(), { once: true });
    setBusy(true);

    let full = '';
    let frame = 0;
    const paint = () => {
      frame = 0;
      paintAnswer(parts.answer, full);
      scrollToEnd();
    };

    const payload = {
      ...assistantOptions(),
      message: question,
      conversation_id: conversationId,
      document_ids: scope.value ? [scope.value] : null,
      voice,
      web_search: webSearch.checked,
    };

    let sources = [];
    try {
      await api.chatStream(payload, {
        signal: local.signal,
        onEvent: (event, data) => {
          if (event === 'conversation') {
            conversationId = data.id;
            history.touch({ ...data, updated_at: Date.now() / 1000 });
            history.setActive(data.id);
          } else if (event === 'sources') {
            sources = data.sources;
          } else if (event === 'delta') {
            full += data.text;
            onDelta?.(data.text);
            frame ||= requestAnimationFrame(paint);
          } else if (event === 'done') {
            cancelAnimationFrame(frame);
            paint();
            renderFooter(parts, full, { ...data, sources });
          } else if (event === 'error') {
            throw new Error(data.message);
          }
        },
      });
      announce('Answer ready.');
    } catch (error) {
      cancelAnimationFrame(frame);
      if (full) paint();
      if (error.name === 'AbortError') {
        if (!full) parts.answer.replaceChildren(el('p', { class: 'muted' }, 'Stopped.'));
      } else {
        parts.answer.append(el('p', { class: 'error-text', role: 'alert' }, error.message));
        if (voice) throw error;
      }
    } finally {
      if (controller === local) controller = null;
      setBusy(false);
    }
    return full;
  }

  function showSource(message, sourceId) {
    const source = sourcesByMessage.get(message)?.get(sourceId);
    if (!source) {
      toast(`Source ${sourceId} is not available.`, 'error');
      return;
    }
    const quotes = JSON.parse(message.dataset.quotes ?? '[]')
      .filter((q) => q.source_id === sourceId)
      .map((q) => q.text);
    $('#source-title').textContent = `${source.id} · ${source.doc_name}`;
    $('#source-meta').textContent = [source.location, source.section].filter(Boolean).join(' · ');
    $('#source-text').replaceChildren(...highlight(source.text, quotes));
    $('#source-dialog').showModal();
  }

  // ---------------------------------------------------------------- wiring

  const populateScope = () => {
    const current = scope.value;
    scope.replaceChildren(
      el('option', { value: '' }, 'All documents'),
      ...state.documents.map((doc) => el('option', { value: doc.id }, doc.name)),
    );
    scope.value = state.documents.some((d) => d.id === current) ? current : '';
  };
  bus.addEventListener('documents', () => {
    populateScope();
    if (isWelcome()) renderWelcome();
  });
  populateScope();

  thread.addEventListener('click', (event) => {
    const cite = event.target.closest('.cite');
    if (cite) showSource(cite.closest('.msg'), cite.dataset.source);
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (controller) return;
    const text = input.value;
    input.value = '';
    resize();
    ask(text);
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  stopButton.addEventListener('click', () => controller?.abort());

  renderWelcome();

  return {
    ask,
    newChat,
    openConversation,
    onConversationDeleted(id) {
      if (id === conversationId) newChat();
    },
    onAllConversationsDeleted() {
      newChat();
    },
  };
}

/** Split `text` into nodes, wrapping exact occurrences of `quotes` in <mark>. */
function highlight(text, quotes) {
  const lower = text.toLowerCase();
  const ranges = [];
  for (const quote of quotes) {
    const index = lower.indexOf(quote.toLowerCase());
    if (index !== -1) ranges.push([index, index + quote.length]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const nodes = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start < cursor) continue;
    nodes.push(text.slice(cursor, start), el('mark', {}, text.slice(start, end)));
    cursor = end;
  }
  nodes.push(text.slice(cursor));
  return nodes;
}
