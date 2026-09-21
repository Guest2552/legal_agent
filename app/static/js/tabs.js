import { $$ } from './dom.js';

/** WAI-ARIA tabs: click or arrow keys / Home / End to switch panels. */
export function setupTabs(tablist, { onChange } = {}) {
  const tabs = $$('[role="tab"]', tablist);

  const select = (tab, focus = true) => {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute('aria-selected', String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute('aria-controls')).hidden = !selected;
    }
    if (focus) tab.focus();
    onChange?.(tab.id);
  };

  tablist.addEventListener('click', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) select(tab);
  });

  tablist.addEventListener('keydown', (event) => {
    const index = tabs.indexOf(document.activeElement);
    if (index === -1) return;
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    select(tabs[(moves[event.key] + tabs.length) % tabs.length]);
  });

  return { select: (id) => select(document.getElementById(id), false) };
}
