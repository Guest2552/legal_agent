import { api } from './api.js';
import { $, announce, toast } from './dom.js';
import { bus, languageTag, state } from './state.js';
import { VoiceAgent, VoiceState } from './voice/voice-agent.js';

const STATE_LABELS = {
  [VoiceState.STARTING]: 'Starting microphone...',
  [VoiceState.LISTENING]: 'Listening',
  [VoiceState.THINKING]: 'Thinking',
  [VoiceState.SPEAKING]: 'Speaking',
};

/** Wires the hands-free VoiceAgent to the voice bar, the Talk button and shortcuts. */
export function initVoiceUI({ ask }) {
  const bar = $('#voice-bar');
  const toggle = $('#voice-toggle');
  const stateLabel = $('#voice-state');
  const caption = $('#voice-caption');
  const levelDot = $('#voice-level');
  let levelFrame = 0;
  let lastLevel = 0;

  if (!VoiceAgent.supported) {
    toggle.disabled = true;
    toggle.title = 'Voice conversation is not supported in this browser';
    return;
  }

  const agent = new VoiceAgent({
    ask: (text, { signal, onDelta }) => ask(text, { voice: true, signal, onDelta }),
    transcribe: (wav) => api.transcribe(wav),
    onState: (voiceState) => {
      const active = voiceState !== VoiceState.OFF;
      bar.hidden = !active;
      bar.dataset.state = voiceState;
      toggle.setAttribute('aria-pressed', String(active));
      toggle.querySelector('.btn-voice-label').textContent = active ? 'End' : 'Talk';
      if (!active) return;
      let label = STATE_LABELS[voiceState] ?? '';
      if (voiceState === VoiceState.SPEAKING && agent.bargeIn) label += ' (talk to interrupt)';
      stateLabel.textContent = label;
      announce(label);
    },
    onCaption: (text, final) => {
      caption.textContent = text || (agent.state === VoiceState.LISTENING ? 'Just start talking. No button needed.' : '');
      caption.classList.toggle('final', final);
    },
    onLevel: (level) => {
      lastLevel = level;
      levelFrame ||= requestAnimationFrame(() => {
        levelFrame = 0;
        levelDot.style.setProperty('--level', String(0.3 + Math.min(1, lastLevel * 12) * 0.7));
      });
    },
    onNotice: (message, kind) => toast(message, kind),
  });

  const configure = () =>
    agent.configure({
      lang: languageTag(),
      engine: state.settings.voiceEngine,
      bargeIn: state.settings.bargeIn,
      rate: state.settings.speechRate,
    });
  configure();
  bus.addEventListener('settings', configure);

  const start = async () => {
    configure();
    try {
      await agent.start();
      toast(`Voice conversation on (${agent.engineName}). Speak naturally; pause when you are done.`);
    } catch {
      toast('Microphone access was blocked. Allow it in your browser to talk with LexiGuide.', 'error');
    }
  };
  const toggleConversation = () => (agent.active ? agent.stop() : start());

  toggle.addEventListener('click', toggleConversation);
  $('#voice-end').addEventListener('click', () => agent.stop());
  $('#voice-interrupt').addEventListener('click', () => agent.interrupt());
  document.addEventListener('keydown', (event) => {
    if (event.altKey && event.key.toLowerCase() === 'v') {
      event.preventDefault();
      toggleConversation();
    } else if (event.key === 'Escape' && agent.active && !document.querySelector('dialog[open]')) {
      agent.interrupt();
    }
  });
  window.addEventListener('pagehide', () => agent.stop());
}
