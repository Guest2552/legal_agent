import { api } from './api.js';
import { $, $$, announce, downloadBlob, el, icon, toast } from './dom.js';
import { recordVoiceNote } from './documents.js';
import { RENDERERS } from './renderers.js';
import { assistantOptions, bus, state } from './state.js';

/** Maps each tool form to its API endpoint and request payload. */
const TOOLS = {
  simplify: {
    endpoint: 'simplify',
    payload: (data) => ({ document_id: data.get('document_id'), level: data.get('level') }),
    needsDocument: true,
  },
  risks: { endpoint: 'risks', payload: (data) => ({ document_id: data.get('document_id') }), needsDocument: true },
  compare: {
    endpoint: 'compare',
    payload: (data) => ({ document_a: data.get('document_a'), document_b: data.get('document_b') }),
    needsDocument: true,
  },
  plan: {
    endpoint: 'action-plan',
    payload: (data) => ({ situation: data.get('situation') ?? '', document_ids: data.getAll('document_ids') }),
  },
  brief: {
    endpoint: 'lawyer-brief',
    payload: (data) => ({ situation: data.get('situation') ?? '', document_ids: data.getAll('document_ids') }),
  },
};

export function initTools() {
  for (const form of $$('form[data-tool]')) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      run(form.dataset.tool, form);
    });
  }
  for (const button of $$('[data-dictate]')) {
    button.addEventListener('click', () => dictate($(`#${button.dataset.dictate}`), button));
  }
  bus.addEventListener('documents', populateDocumentControls);
  populateDocumentControls();
}

function populateDocumentControls() {
  const docs = state.documents;
  for (const select of $$('select[data-doc-select]')) {
    const previous = select.value;
    if (!docs.length) {
      select.replaceChildren(el('option', { value: '' }, 'Upload a document first'));
      continue;
    }
    select.replaceChildren(...docs.map((doc) => el('option', { value: doc.id }, doc.name)));
    select.value = docs.some((d) => d.id === previous) ? previous : docs[0].id;
    if (select.dataset.docSelect === 'second' && docs.length > 1) {
      // Default the second picker to a different document than the first one in the same form.
      const first = $('select[data-doc-select=""]', select.form);
      if (first && select.value === first.value) select.value = docs.find((d) => d.id !== first.value).id;
    }
  }
  for (const fieldset of $$('[data-doc-checks]')) {
    const checked = new Set($$('input:checked', fieldset).map((input) => input.value));
    const legend = $('legend', fieldset);
    const hadInputs = $$('input', fieldset).length > 0;
    fieldset.replaceChildren(
      legend,
      docs.length
        ? el(
            'div',
            { class: 'check-list' },
            docs.map((doc) =>
              el(
                'label',
                { class: 'check-chip', title: doc.name },
                el('input', { type: 'checkbox', name: 'document_ids', value: doc.id, checked: !hadInputs || checked.has(doc.id) }),
                el('span', {}, doc.name),
              ),
            ),
          )
        : el('p', { class: 'empty-hint' }, 'No documents yet. Your description alone will be used.'),
    );
  }
}

function loadingCard(onCancel) {
  const started = performance.now();
  const elapsed = el('span', { class: 'muted' }, '0 s');
  const card = el(
    'div',
    { class: 'loading-card', role: 'status' },
    el('div', { class: 'spinner', 'aria-hidden': 'true' }),
    el('div', {}, el('strong', {}, 'Reading the whole document and checking every quote...'), el('br'), elapsed),
    el('button', { type: 'button', class: 'btn btn-ghost btn-sm', onClick: onCancel }, 'Cancel'),
  );
  const timer = setInterval(() => {
    elapsed.textContent = `${Math.round((performance.now() - started) / 1000)} s`;
  }, 1000);
  return { card, stop: () => clearInterval(timer) };
}

function quoteCheckBadge(check) {
  const total = check.verified + check.partial + check.unverified;
  if (!total) return null;
  const tone = check.unverified ? 'badge-bad' : check.partial ? 'badge-warn' : 'badge-ok';
  return el(
    'span',
    { class: `badge ${tone}`, title: 'Every quote was matched against the original document text' },
    icon(check.unverified ? 'alert' : 'shield'),
    `${check.verified}/${total} quotes verified word-for-word`,
  );
}

function resultView(tool, payload) {
  const renderer = RENDERERS[tool];
  const title = `${renderer.title}: ${payload.documents.map((d) => d.name).join(' vs ') || 'your situation'}`;
  const markdown = () => renderer.markdown(payload.result, payload);

  const copyButton = el('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, icon('copy'), 'Copy');
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(`# ${title}\n\n${markdown()}`);
      toast('Copied as Markdown.');
    } catch {
      toast('Copy failed: your browser blocked clipboard access.', 'error');
    }
  });
  const wordButton = el('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, icon('download'), 'Word (.docx)');
  wordButton.addEventListener('click', async () => {
    try {
      downloadBlob(await api.exportDocx(title, markdown()), `${renderer.title.replace(/\W+/g, '_')}.docx`);
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  const printButton = el('button', { type: 'button', class: 'btn btn-ghost btn-sm', onClick: () => window.print() }, icon('print'), 'Print / PDF');

  return el(
    'div',
    {},
    el('div', { class: 'result-toolbar' }, quoteCheckBadge(payload.quote_check), el('span', { class: 'spacer' }), copyButton, wordButton, printButton),
    payload.truncated ? el('p', { class: 'notice' }, 'This document is very long; only the first part was analysed.') : null,
    renderer.render(payload.result, payload),
  );
}

async function run(tool, form) {
  const spec = TOOLS[tool];
  const container = $(`#result-${tool}`);
  const submit = $('button[type="submit"]', form);
  if (spec.needsDocument && !state.documents.length) {
    toast('Upload a document first (or try the sample documents).', 'error');
    return;
  }
  const payload = { ...assistantOptions(), ...spec.payload(new FormData(form)) };
  if (tool === 'compare' && payload.document_a === payload.document_b) {
    toast('Choose two different documents to compare.', 'error');
    return;
  }
  if ((tool === 'plan' || tool === 'brief') && !payload.situation.trim() && !payload.document_ids.length) {
    toast('Describe your situation or select at least one document.', 'error');
    return;
  }

  const controller = new AbortController();
  const loading = loadingCard(() => controller.abort());
  container.replaceChildren(loading.card);
  submit.disabled = true;
  try {
    const result = await api.analysis(spec.endpoint, payload, controller.signal);
    container.replaceChildren(resultView(tool, result));
    announce(`${RENDERERS[tool].title} ready.`);
    container.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    container.replaceChildren(
      error.name === 'AbortError' ? '' : el('p', { class: 'notice', role: 'alert' }, error.message),
    );
  } finally {
    loading.stop();
    submit.disabled = false;
  }
}

async function dictate(textarea, button) {
  button.setAttribute('aria-pressed', 'true');
  try {
    const wav = await recordVoiceNote({
      title: 'Dictate your situation',
      help: 'Speak in any language. The transcript is added to the text box so you can edit it.',
    });
    if (!wav) return;
    const text = await api.transcribe(wav);
    if (text) {
      textarea.value = `${textarea.value.trim()} ${text}`.trim();
      textarea.focus();
      announce('Transcript added.');
    } else {
      toast('No speech was detected.', 'error');
    }
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.setAttribute('aria-pressed', 'false');
  }
}
