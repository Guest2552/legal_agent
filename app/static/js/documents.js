import { api } from './api.js';
import { $, $$, announce, confirmAction, el, formatBytes, formatDuration, icon, toast } from './dom.js';
import { bus, setDocuments, state } from './state.js';
import { VoiceNoteRecorder } from './voice/recorder.js';

const KIND_ICONS = {
  pdf: 'file', docx: 'file', text: 'file', html: 'file', xlsx: 'sheet', csv: 'sheet',
  pptx: 'slides', email: 'mail', image: 'image', audio: 'audio',
};
const KIND_LABELS = {
  pdf: 'PDF', docx: 'Word', xlsx: 'Excel', csv: 'CSV', pptx: 'PowerPoint', text: 'Text',
  html: 'Web page', email: 'E-mail', image: 'Image', audio: 'Audio',
};
const SAMPLES = [
  '/static/samples/Rental_Agreement_Original.txt',
  '/static/samples/Rental_Agreement_Revised.txt',
];
const MAX_PARALLEL_UPLOADS = 3;

const pending = new Map(); // upload id -> { name, error }
let uploadCounter = 0;

/** Sidebar document library, the "Add documents" dialog and drag-and-drop anywhere. */
export function initDocuments() {
  const input = $('#file-input');
  const dropzone = $('#dropzone');
  input.accept = state.config.accepted_extensions.join(',');
  $('#accepted-hint').textContent =
    `PDF, Word, Excel, PowerPoint, text, e-mail, images and audio. Up to ${state.config.max_upload_mb} MB` +
    ` and ${state.config.max_pdf_pages.toLocaleString()} pages per file.`;
  $('#ttl-days').textContent = state.config.session_ttl_days;

  input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    $('#add-doc-dialog').close();
    uploadFiles(files);
  });
  setupDropTarget(dropzone);
  setupWindowDrop();

  for (const button of $$('[data-open-add]')) button.addEventListener('click', openAddDocuments);
  $('#add-doc-btn').addEventListener('click', openAddDocuments);
  $('#paste-text-btn').addEventListener('click', () => {
    $('#add-doc-dialog').close();
    openPasteDialog();
  });
  $('#voice-note-btn').addEventListener('click', async () => {
    $('#add-doc-dialog').close();
    const blob = await recordVoiceNote();
    if (blob) uploadFiles([new File([blob], `Voice note ${new Date().toLocaleString()}.wav`, { type: 'audio/wav' })]);
  });
  $('#sample-btn').addEventListener('click', () => {
    $('#add-doc-dialog').close();
    loadSamples();
  });

  bus.addEventListener('documents', render);
  return refreshDocuments();
}

export function openAddDocuments() {
  $('#add-doc-dialog').showModal();
}

export async function refreshDocuments() {
  try {
    setDocuments(await api.listDocuments());
  } catch (error) {
    toast(error.message, 'error');
  }
}

function setupDropTarget(target) {
  for (const type of ['dragenter', 'dragover']) {
    target.addEventListener(type, (event) => {
      event.preventDefault();
      target.classList.add('dragging');
    });
  }
  for (const type of ['dragleave', 'drop']) {
    target.addEventListener(type, (event) => {
      event.preventDefault();
      target.classList.remove('dragging');
    });
  }
  target.addEventListener('drop', (event) => {
    $('#add-doc-dialog').close();
    uploadFiles([...event.dataTransfer.files]);
  });
}

/** Dropping files anywhere on the page uploads them too. */
function setupWindowDrop() {
  window.addEventListener('dragover', (event) => {
    if (event.dataTransfer?.types.includes('Files')) event.preventDefault();
  });
  window.addEventListener('drop', (event) => {
    if (!event.dataTransfer?.files.length || event.defaultPrevented) return;
    event.preventDefault();
    uploadFiles([...event.dataTransfer.files]);
  });
}

function validate(file) {
  const extension = `.${file.name.split('.').pop().toLowerCase()}`;
  if (!state.config.accepted_extensions.includes(extension)) return `Unsupported file type (${extension}).`;
  if (file.size > state.config.max_upload_mb * 1024 * 1024) return `Larger than ${state.config.max_upload_mb} MB.`;
  return null;
}

export async function uploadFiles(files) {
  const queue = [...files];
  const worker = async () => {
    while (queue.length) await uploadOne(queue.shift());
  };
  await Promise.all(Array.from({ length: Math.min(MAX_PARALLEL_UPLOADS, queue.length) }, worker));
}

async function uploadOne(file) {
  uploadCounter += 1;
  const id = uploadCounter;
  const problem = validate(file);
  pending.set(id, { name: file.name, error: problem });
  render();
  if (problem) return;
  try {
    const document = await api.uploadDocument(file);
    pending.delete(id);
    setDocuments([...state.documents.filter((d) => d.id !== document.id), document]);
    announce(`${document.name} is ready.`);
  } catch (error) {
    pending.set(id, { name: file.name, error: error.message });
    render();
  }
}

function describe(doc) {
  const unit = { pdf: 'page', pptx: 'slide', audio: 'recording' }[doc.kind] ?? 'section';
  const count = `${doc.locations.toLocaleString()} ${unit}${doc.locations === 1 ? '' : 's'}`;
  return `${KIND_LABELS[doc.kind] ?? doc.kind} · ${formatBytes(doc.size_bytes)} · ${count}`;
}

function documentItem(doc) {
  const notes = doc.notes.length ? ` · ${doc.notes.join(' ')}` : '';
  return el(
    'li',
    { class: 'side-item' },
    el(
      'button',
      { type: 'button', class: 'side-main', title: `${doc.name}\n${describe(doc)}${notes}`, onClick: () => viewDocument(doc) },
      icon(KIND_ICONS[doc.kind] ?? 'file'),
      el('span', { class: 'side-text' }, el('span', { class: 'side-title' }, doc.name), el('span', { class: 'side-meta' }, describe(doc))),
    ),
    el(
      'span',
      { class: 'side-actions' },
      el('button', { type: 'button', class: 'icon-btn', 'aria-label': `View ${doc.name}`, onClick: () => viewDocument(doc) }, icon('eye')),
      el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Remove ${doc.name}`, onClick: () => removeDocument(doc) }, icon('trash')),
    ),
  );
}

function pendingItem(id, { name, error }) {
  const dismiss = () => {
    pending.delete(id);
    render();
  };
  return el(
    'li',
    { class: 'side-item pending', 'aria-busy': error ? null : 'true' },
    el(
      'div',
      { class: 'side-main' },
      icon(error ? 'alert' : 'upload'),
      el(
        'span',
        { class: 'side-text' },
        el('span', { class: 'side-title' }, name),
        el('span', { class: `side-meta${error ? ' error' : ''}`, role: error ? 'alert' : null }, error ?? 'Reading and indexing...'),
        error ? null : el('span', { class: 'progress' }),
      ),
    ),
    error
      ? el('span', { class: 'side-actions' }, el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Dismiss ${name}`, onClick: dismiss }, icon('x')))
      : null,
  );
}

function render() {
  const items = [...state.documents.map(documentItem), ...[...pending].map(([id, item]) => pendingItem(id, item))];
  $('#doc-list').replaceChildren(...items);
  $('#doc-list-empty').hidden = items.length > 0;
  $('#doc-count').textContent = state.documents.length ? `(${state.documents.length})` : '';
}

async function removeDocument(doc) {
  const confirmed = await confirmAction({
    title: 'Remove document?',
    text: `"${doc.name}" and its saved analyses will be deleted. Chats that cited it keep their text.`,
    confirmLabel: 'Remove',
  });
  if (!confirmed) return;
  try {
    await api.deleteDocument(doc.id);
    setDocuments(state.documents.filter((d) => d.id !== doc.id));
    toast(`Removed ${doc.name}`);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function viewDocument(doc) {
  const dialog = $('#viewer-dialog');
  $('#viewer-title').textContent = doc.name;
  const body = $('#viewer-body');
  body.replaceChildren(el('p', { class: 'muted' }, 'Loading...'));
  dialog.showModal();
  try {
    const data = await api.documentText(doc.id);
    body.replaceChildren(
      ...data.sections.flatMap((section) => [el('h3', {}, section.location), el('div', {}, section.text)]),
    );
    if (data.truncated) body.append(el('p', { class: 'notice' }, 'Preview shortened. The full text is still used for answers.'));
  } catch (error) {
    body.replaceChildren(el('p', { class: 'error-text' }, error.message));
  }
}

function openPasteDialog() {
  const dialog = $('#paste-dialog');
  $('#paste-form').reset();
  dialog.returnValue = '';
  dialog.showModal();
  dialog.addEventListener(
    'close',
    () => {
      if (dialog.returnValue !== 'save') return;
      const name = $('#paste-name').value.trim() || 'Pasted text';
      const text = $('#paste-body').value.trim();
      const filename = name.toLowerCase().endsWith('.txt') ? name : `${name}.txt`;
      uploadFiles([new File([text], filename, { type: 'text/plain' })]);
    },
    { once: true },
  );
}

async function loadSamples() {
  try {
    const files = await Promise.all(
      SAMPLES.map(async (path) => {
        const response = await fetch(path);
        if (!response.ok) throw new Error('Sample documents are unavailable.');
        return new File([await response.blob()], path.split('/').pop(), { type: 'text/plain' });
      }),
    );
    await uploadFiles(files);
  } catch (error) {
    toast(error.message, 'error');
  }
}

/**
 * Open the recorder dialog. Resolves with a WAV Blob, or null if cancelled.
 * Used for voice-note documents and for dictating situations.
 */
export function recordVoiceNote({ title = 'Record a voice note', help } = {}) {
  if (!VoiceNoteRecorder.supported) {
    toast('Voice recording is not supported in this browser.', 'error');
    return Promise.resolve(null);
  }
  const dialog = $('#recorder-dialog');
  const toggle = $('#recorder-toggle');
  const level = $('#recorder-level');
  const time = $('#recorder-time');
  $('#recorder-title').textContent = title;
  $('#recorder-help').textContent = help ?? 'Describe your situation in any language. Gemini transcribes it.';
  time.textContent = '0:00';
  level.style.width = '0%';
  toggle.textContent = 'Start recording';

  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      toggle.onclick = null;
      $('#recorder-cancel').onclick = null;
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const recorder = new VoiceNoteRecorder({
      onLevel: (value) => {
        level.style.width = `${Math.min(100, value * 400)}%`;
      },
      onTick: (seconds) => {
        time.textContent = formatDuration(seconds);
      },
      onLimit: () => toggle.click(),
    });

    toggle.onclick = async () => {
      if (!recorder.startedAt) {
        try {
          await recorder.start();
          toggle.textContent = 'Stop and use';
          announce('Recording started.');
        } catch {
          toast('Microphone access was blocked. Allow it in your browser settings.', 'error');
          finish(null);
        }
        return;
      }
      finish(await recorder.stop());
    };
    $('#recorder-cancel').onclick = () => {
      recorder.cancel();
      finish(null);
    };
    dialog.addEventListener(
      'close',
      () => {
        if (!settled) {
          recorder.cancel();
          finish(null);
        }
      },
      { once: true },
    );
    dialog.showModal();
  });
}
