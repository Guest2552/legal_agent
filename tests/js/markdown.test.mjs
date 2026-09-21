import assert from 'node:assert/strict';
import { test } from 'node:test';

import { escapeHtml, renderMarkdown } from '../../app/static/js/markdown.js';

test('escapes raw HTML so model output cannot inject markup (XSS)', () => {
  const html = renderMarkdown('<img src=x onerror=alert(1)> **bold** <script>alert(2)</script>');
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(html.includes('<strong>bold</strong>'));
});

test('only http(s) links are rendered, always with noopener', () => {
  const html = renderMarkdown('[safe](https://indiacode.nic.in) [evil](javascript:alert(1))');
  assert.ok(html.includes('<a href="https://indiacode.nic.in" target="_blank" rel="noopener noreferrer">safe</a>'));
  assert.ok(!html.includes('href="javascript'));
});

test('citations become accessible source buttons', () => {
  const html = renderMarkdown('The deposit is refundable [S2][S3] and rent is due [S1, S4].');
  for (const id of ['S1', 'S2', 'S3', 'S4']) {
    assert.ok(html.includes(`data-source="${id}" aria-label="Show source ${id}"`), id);
  }
});

test('renders headings, lists, quotes, tables and the general-law label', () => {
  const html = renderMarkdown(
    '## Why\n\n1. First\n2. Second\n\n- a\n- b\n\n> quoted clause\n\n| A | B |\n|---|---|\n| x | y |\n\n**General law:** text',
  );
  assert.ok(html.includes('<h4>Why</h4>'));
  assert.ok(html.includes('<ol><li>First</li><li>Second</li></ol>'));
  assert.ok(html.includes('<ul><li>a</li><li>b</li></ul>'));
  assert.ok(html.includes('<blockquote>quoted clause</blockquote>'));
  assert.ok(html.includes('<th scope="col">A</th>'));
  assert.ok(html.includes('<td>y</td>'));
  assert.ok(html.includes('<strong class="law-label">General law:</strong>'));
});

test('inline code is escaped and not formatted', () => {
  const html = renderMarkdown('Use `<b>**x**</b>` here');
  assert.ok(html.includes('<code>&lt;b&gt;**x**&lt;/b&gt;</code>'));
});

test('escapeHtml covers quotes and ampersands', () => {
  assert.equal(escapeHtml(`"a" & 'b'`), '&quot;a&quot; &amp; &#39;b&#39;');
});
