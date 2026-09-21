/**
 * Minimal, XSS-safe Markdown renderer for model answers.
 *
 * Everything is HTML-escaped first; only a fixed set of tags we generate ourselves is
 * ever emitted, links are restricted to http(s), and `[S1]` citations become buttons.
 * Pure (no DOM access) so it is unit-tested in Node.
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

const CITATION = /\[(S\d+(?:\s*[,;]\s*S\d+)*)\]/g;

function citationButtons(group) {
  return group
    .split(/\s*[,;]\s*/)
    .map((id) => `<button type="button" class="cite" data-source="${id}" aria-label="Show source ${id}">${id}</button>`)
    .join('');
}

/** Inline formatting on an *already escaped* string. */
export function renderInline(escaped) {
  const codes = [];
  let html = escaped.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(`<code>${code}</code>`);
    return `\u0000${codes.length - 1}\u0000`;
  });
  html = html
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*(?!\s)(.+?)\*(?!\w)/g, '$1<em>$2</em>')
    .replace(/(^|\W)_(?!\s)(.+?)_(?!\w)/g, '$1<em>$2</em>')
    .replace(CITATION, (_, group) => citationButtons(group))
    .replace(/<strong>(General law:?)<\/strong>/g, '<strong class="law-label">$1</strong>');
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function renderTable(lines) {
  const rows = lines.filter((line) => !/^\|?\s*:?-{3,}/.test(line.trim())).map(splitRow);
  if (!rows.length) return '';
  const [head, ...body] = rows;
  const th = head.map((c) => `<th scope="col">${renderInline(escapeHtml(c))}</th>`).join('');
  const tr = body
    .map((row) => `<tr>${row.map((c) => `<td>${renderInline(escapeHtml(c))}</td>`).join('')}</tr>`)
    .join('');
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
}

/** Render Markdown to a safe HTML string. */
export function renderMarkdown(markdown) {
  const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let paragraph = [];
  let list = null; // { tag: 'ul' | 'ol', items: [] }

  const flushParagraph = () => {
    if (paragraph.length) out.push(`<p>${paragraph.map((l) => renderInline(escapeHtml(l))).join('<br>')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join('')}</${list.tag}>`);
    list = null;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }
    if (trimmed.startsWith('|')) {
      flushParagraph();
      flushList();
      const tableLines = [trimmed];
      while (i + 1 < lines.length && lines[i + 1].trim().startsWith('|')) tableLines.push(lines[++i].trim());
      out.push(renderTable(tableLines));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${renderInline(escapeHtml(heading[2]))}</h${level}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      flushParagraph();
      flushList();
      out.push('<hr>');
      continue;
    }
    if (trimmed.startsWith('>')) {
      flushParagraph();
      flushList();
      out.push(`<blockquote>${renderInline(escapeHtml(trimmed.replace(/^>\s?/, '')))}</blockquote>`);
      continue;
    }
    const bullet = /^[-*+]\s+(.*)$/.exec(trimmed);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (bullet || numbered) {
      flushParagraph();
      const tag = bullet ? 'ul' : 'ol';
      if (list && list.tag !== tag) flushList();
      list ??= { tag, items: [] };
      list.items.push(renderInline(escapeHtml((bullet ?? numbered)[1])));
      continue;
    }
    if (list && /^\s{2,}/.test(line)) {
      // continuation of the previous list item
      list.items[list.items.length - 1] += ` ${renderInline(escapeHtml(trimmed))}`;
      continue;
    }
    flushList();
    paragraph.push(trimmed);
  }
  flushParagraph();
  flushList();
  return out.join('');
}
