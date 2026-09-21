import { api } from './api.js';
import { initChat } from './chat.js';
import { initDocuments, openAddDocuments } from './documents.js';
import { $, $$, confirmAction, el, setupDialogs, toast } from './dom.js';
import { initHistory } from './history.js';
import { bus, setDocuments, state, updateSettings } from './state.js';
import { setupTabs } from './tabs.js';
import { initTools } from './tools.js';
import { initVoiceUI } from './voice-ui.js';

function applyTextScale() {
  document.documentElement.style.setProperty('--text-scale', String(state.settings.textScale));
}

/** "English · India · Tenant" in the top bar; opens Settings. */
function renderContextChip() {
  const { language, jurisdiction, userRole } = state.settings;
  const parts = [language, jurisdiction === 'Auto-detect' ? 'Any jurisdiction' : jurisdiction, userRole].filter(Boolean);
  $('#context-label').textContent = parts.join(' · ');
}

function setupSettings({ onChatsCleared }) {
  const dialog = $('#settings-dialog');
  const language = $('#language');
  const jurisdiction = $('#jurisdiction');
  const role = $('#user-role');
  const engine = $('#pref-engine');
  const rate = $('#pref-rate');
  const rateValue = $('#pref-rate-value');
  const barge = $('#pref-barge');

  language.replaceChildren(...Object.keys(state.config.languages).map((name) => el('option', { value: name }, name)));
  jurisdiction.replaceChildren(...state.config.jurisdictions.map((name) => el('option', { value: name }, name)));
  if (!(state.settings.language in state.config.languages)) updateSettings({ language: 'English' });
  if (!state.config.jurisdictions.includes(state.settings.jurisdiction)) updateSettings({ jurisdiction: 'Auto-detect' });

  const showRate = () => {
    rateValue.textContent = `${Number(rate.value).toFixed(1)}x`;
  };
  const open = () => {
    const { settings } = state;
    language.value = settings.language;
    jurisdiction.value = settings.jurisdiction;
    role.value = settings.userRole;
    engine.value = settings.voiceEngine;
    rate.value = String(settings.speechRate);
    barge.checked = settings.bargeIn;
    for (const radio of $$('input[name="text-size"]')) radio.checked = Number(radio.value) === settings.textScale;
    showRate();
    dialog.showModal();
  };

  $('#open-settings').addEventListener('click', open);
  $('#context-chip').addEventListener('click', open);
  language.addEventListener('change', () => updateSettings({ language: language.value }));
  jurisdiction.addEventListener('change', () => updateSettings({ jurisdiction: jurisdiction.value }));
  role.addEventListener('input', () => updateSettings({ userRole: role.value.trim() }));
  engine.addEventListener('change', () => updateSettings({ voiceEngine: engine.value }));
  rate.addEventListener('input', () => {
    showRate();
    updateSettings({ speechRate: Number(rate.value) });
  });
  barge.addEventListener('change', () => updateSettings({ bargeIn: barge.checked }));
  for (const radio of $$('input[name="text-size"]')) {
    radio.addEventListener('change', () => {
      updateSettings({ textScale: Number(radio.value) });
      applyTextScale();
    });
  }

  $('#clear-chats').addEventListener('click', async () => {
    const confirmed = await confirmAction({ title: 'Delete all chats?', text: 'Every saved conversation will be permanently deleted.' });
    if (!confirmed) return;
    try {
      await api.clearConversations();
      onChatsCleared();
      toast('All chats deleted.');
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  $('#clear-docs').addEventListener('click', async () => {
    const confirmed = await confirmAction({
      title: 'Delete all documents?',
      text: 'Every uploaded document and its saved analyses will be permanently deleted.',
    });
    if (!confirmed) return;
    try {
      await api.clearDocuments();
      setDocuments([]);
      toast('All documents deleted.');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  bus.addEventListener('settings', renderContextChip);
  renderContextChip();
}

/** Off-canvas sidebar on small screens. */
function setupDrawer() {
  const app = $('#app');
  const toggle = $('#menu-toggle');
  const scrim = $('#scrim');
  const setOpen = (open) => {
    app.classList.toggle('nav-open', open);
    scrim.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) $('#new-chat').focus();
  };
  toggle.addEventListener('click', () => setOpen(true));
  $('#sidebar-close').addEventListener('click', () => setOpen(false));
  scrim.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && app.classList.contains('nav-open')) setOpen(false);
  });
  return () => setOpen(false);
}

async function boot() {
  applyTextScale();
  setupDialogs();
  try {
    state.config = await api.config();
  } catch {
    toast('Could not reach the LexiGuide server. Please refresh the page.', 'error');
    return;
  }

  const closeDrawer = setupDrawer();
  const tabs = setupTabs($('.tool-nav'));
  let chat = null;
  const history = initHistory({
    onOpen: (id) => {
      tabs.select('tab-ask');
      closeDrawer();
      chat.openConversation(id);
    },
    onDeleted: (id) => chat.onConversationDeleted(id),
  });
  chat = initChat({ history, openAddDocuments });

  setupSettings({
    onChatsCleared: () => {
      history.clear();
      chat.onAllConversationsDeleted();
    },
  });
  $('#new-chat').addEventListener('click', () => {
    tabs.select('tab-ask');
    closeDrawer();
    chat.newChat();
  });

  initTools();
  initVoiceUI({
    ask: (text, options) => {
      tabs.select('tab-ask'); // voice answers always appear in the chat
      return chat.ask(text, options);
    },
  });
  await Promise.all([initDocuments(), history.refresh()]);
}

boot();
