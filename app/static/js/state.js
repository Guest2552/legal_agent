/**
 * Shared client state: user preferences (persisted in localStorage) and the
 * document library (mirrored from the server). Modules subscribe via `bus`.
 */

const STORAGE_KEY = 'lexiguide.settings.v2';

const DEFAULTS = Object.freeze({
  language: 'English',
  jurisdiction: 'Auto-detect',
  userRole: '',
  webSearch: true,
  textScale: 1,
  voiceEngine: 'auto',
  bargeIn: true,
  speechRate: 1,
});

function loadSettings() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') };
  } catch {
    return { ...DEFAULTS };
  }
}

export const bus = new EventTarget();

export const state = {
  config: null,
  settings: loadSettings(),
  documents: [],
};

export function updateSettings(patch) {
  Object.assign(state.settings, patch);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.settings));
  } catch {
    /* private mode or storage disabled: keep settings for this page only */
  }
  bus.dispatchEvent(new CustomEvent('settings', { detail: patch }));
}

export function setDocuments(documents) {
  state.documents = documents;
  bus.dispatchEvent(new CustomEvent('documents'));
}

/** Options sent with every AI request. */
export function assistantOptions() {
  const { language, jurisdiction, userRole } = state.settings;
  return { language, jurisdiction, user_role: userRole };
}

/** BCP-47 tag for the chosen answer language (speech recognition & synthesis). */
export function languageTag() {
  return state.config?.languages?.[state.settings.language] ?? 'en-IN';
}
