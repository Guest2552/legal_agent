import { api } from './api.js';
import { $, confirmAction, el, icon, promptRename, relativeTime, toast } from './dom.js';

/**
 * Sidebar list of saved conversations (stored server-side in SQLite).
 * @param {{onOpen: (id: string) => void, onDeleted: (id: string) => void}} handlers
 */
export function initHistory({ onOpen, onDeleted }) {
  const list = $('#chat-list');
  const empty = $('#chat-list-empty');
  let conversations = [];
  let activeId = null;

  function item(conversation) {
    return el(
      'li',
      { class: `side-item${conversation.id === activeId ? ' active' : ''}` },
      el(
        'button',
        {
          type: 'button',
          class: 'side-main',
          'aria-current': conversation.id === activeId ? 'page' : null,
          title: conversation.title,
          onClick: () => onOpen(conversation.id),
        },
        el(
          'span',
          { class: 'side-text' },
          el('span', { class: 'side-title' }, conversation.title),
          el('span', { class: 'side-meta' }, relativeTime(conversation.updated_at)),
        ),
      ),
      el(
        'span',
        { class: 'side-actions' },
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Rename "${conversation.title}"`, onClick: () => rename(conversation) }, icon('pencil')),
        el('button', { type: 'button', class: 'icon-btn', 'aria-label': `Delete "${conversation.title}"`, onClick: () => remove(conversation) }, icon('trash')),
      ),
    );
  }

  function render() {
    list.replaceChildren(...conversations.map(item));
    empty.hidden = conversations.length > 0;
  }

  async function refresh() {
    try {
      conversations = await api.listConversations();
      render();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function rename(conversation) {
    const title = await promptRename(conversation.title);
    if (!title || title === conversation.title) return;
    try {
      await api.renameConversation(conversation.id, title);
      conversation.title = title;
      render();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function remove(conversation) {
    const confirmed = await confirmAction({
      title: 'Delete chat?',
      text: `"${conversation.title}" will be permanently deleted.`,
    });
    if (!confirmed) return;
    try {
      await api.deleteConversation(conversation.id);
      conversations = conversations.filter((c) => c.id !== conversation.id);
      render();
      onDeleted(conversation.id);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  return {
    refresh,
    setActive(id) {
      activeId = id;
      render();
    },
    /** Insert or move a conversation to the top (after a new message). */
    touch(summary) {
      conversations = [summary, ...conversations.filter((c) => c.id !== summary.id)];
      render();
    },
    clear() {
      conversations = [];
      render();
    },
  };
}
