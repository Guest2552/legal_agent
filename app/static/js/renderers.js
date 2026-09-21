import { el, icon } from './dom.js';

/*
 * Renderers for the structured analysis results. Each tool has:
 *   render(result, payload) -> DOM node      (for the page)
 *   markdown(result, payload) -> string       (for copy / .docx export)
 * All text goes through textContent, so model output can never inject HTML.
 */

const RISK_ORDER = { high: 0, medium: 1, low: 2 };
const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : '');

const QUOTE_BADGES = {
  verified: ['badge-ok', 'check', 'Verified in document'],
  partial: ['badge-warn', 'alert', 'Close match'],
  unverified: ['badge-bad', 'alert', 'Not found in document'],
};

function quote(text, status) {
  if (!text?.trim()) return null;
  const badge = QUOTE_BADGES[status];
  return el(
    'blockquote',
    { class: 'quote', dataset: { status: status ?? 'none' } },
    `"${text}"`,
    badge ? el('span', { class: `badge ${badge[0]}` }, icon(badge[1]), badge[2]) : null,
  );
}

const riskBadge = (level) => el('span', { class: `badge risk-${level}` }, `${cap(level)} risk`);

function section(title, ...children) {
  const content = children.flat().filter(Boolean);
  if (!content.length) return null;
  return el('section', { class: 'result-section' }, el('h3', {}, title), ...content);
}

const list = (items, render = (x) => x) =>
  items?.length ? el('ul', { class: 'plain-list' }, items.map((item) => el('li', {}, render(item)))) : null;

const paragraphs = (text) =>
  String(text ?? '')
    .split(/\n{2,}/)
    .filter((p) => p.trim())
    .map((p) => el('p', {}, p.trim()));

function headline(eyebrow, text, ...extra) {
  return el('div', { class: 'headline' }, el('span', { class: 'eyebrow' }, eyebrow), el('p', {}, text), ...extra);
}

const mdQuote = (text) => (text?.trim() ? `> "${text.trim()}"\n` : '');
const mdList = (items, render = (x) => x) => (items?.length ? `${items.map((i) => `- ${render(i)}`).join('\n')}\n` : '');

// ------------------------------------------------------------------------- simplify

function renderSimplify(r) {
  return el(
    'div',
    {},
    headline(
      r.document_type || 'Document',
      r.one_line_summary,
      r.parties.length
        ? el('div', { class: 'pill-row' }, r.parties.map((p) => el('span', { class: 'pill' }, `${p.name}: ${p.role}`)))
        : null,
    ),
    section('In plain words', paragraphs(r.plain_summary)),
    section(
      'Key points',
      el(
        'div',
        { class: 'item-list' },
        r.key_points.map((k) =>
          el('div', { class: 'item' }, el('p', {}, k.point), k.location ? el('span', { class: 'item-meta' }, k.location) : null, quote(k.quote, k.quote_status)),
        ),
      ),
    ),
    section(
      'Section by section',
      r.sections.map((s) =>
        el(
          'details',
          { class: 'section-detail' },
          el('summary', {}, s.heading, s.location ? el('span', { class: 'item-meta' }, `  ${s.location}`) : null),
          el('p', {}, s.explanation),
        ),
      ),
    ),
    section(
      'Terms explained',
      r.terms_explained.length
        ? el('dl', { class: 'glossary' }, r.terms_explained.flatMap((t) => [el('dt', {}, t.term), el('dd', {}, t.meaning)]))
        : null,
    ),
  );
}

function markdownSimplify(r) {
  return [
    `## ${r.document_type}\n\n**${r.one_line_summary}**\n`,
    r.parties.length ? `**Parties:** ${r.parties.map((p) => `${p.name} (${p.role})`).join('; ')}\n` : '',
    `## In plain words\n\n${r.plain_summary}\n`,
    `## Key points\n\n${r.key_points.map((k) => `- ${k.point}${k.location ? ` (${k.location})` : ''}\n${mdQuote(k.quote)}`).join('')}`,
    `## Section by section\n\n${r.sections.map((s) => `### ${s.heading}\n\n${s.explanation}\n`).join('\n')}`,
    r.terms_explained.length ? `## Terms explained\n\n${mdList(r.terms_explained, (t) => `**${t.term}**: ${t.meaning}`)}` : '',
  ].join('\n');
}

// ------------------------------------------------------------------------- risks

function renderRisks(r) {
  const clauses = [...r.clauses].sort((a, b) => RISK_ORDER[a.risk] - RISK_ORDER[b.risk]);
  const counts = { high: 0, medium: 0, low: 0 };
  clauses.forEach((c) => {
    counts[c.risk] += 1;
  });
  const tone = { high: 'var(--bad)', medium: 'var(--warn)', low: 'var(--ok)' }[r.overall_risk];
  const dial = el('div', { class: 'score-dial', role: 'img', 'aria-label': `Risk score ${r.risk_score} out of 100` }, el('span', {}, String(r.risk_score)));
  dial.style.setProperty('--value', String(r.risk_score));
  dial.style.setProperty('--tone', tone);

  const clauseList = el(
    'div',
    { class: 'item-list' },
    clauses.map((c) =>
      el(
        'article',
        { class: 'item', dataset: { risk: c.risk } },
        el('div', { class: 'item-head' }, riskBadge(c.risk), el('h4', { class: 'item-title' }, c.title), el('span', { class: 'item-meta' }, [c.category, c.location].filter(Boolean).join(' · '))),
        el('p', {}, c.explanation),
        quote(c.quote, c.quote_status),
        c.suggestion ? el('p', { class: 'suggestion' }, el('strong', {}, 'Ask for: '), c.suggestion) : null,
      ),
    ),
  );
  const filters = el(
    'div',
    { class: 'filter-row', role: 'group', 'aria-label': 'Filter clauses by risk' },
    ['all', 'high', 'medium', 'low'].map((level) =>
      el(
        'button',
        {
          type: 'button',
          class: 'btn btn-ghost btn-sm',
          'aria-pressed': String(level === 'all'),
          onClick: (event) => {
            for (const b of filters.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b === event.currentTarget));
            for (const item of clauseList.children) item.hidden = level !== 'all' && item.dataset.risk !== level;
          },
        },
        level === 'all' ? `All (${clauses.length})` : `${cap(level)} (${counts[level]})`,
      ),
    ),
  );

  return el(
    'div',
    {},
    el(
      'div',
      { class: 'score' },
      dial,
      el('div', {}, el('h3', {}, `${cap(r.overall_risk)} risk for you`), el('p', {}, r.verdict), el('p', { class: 'muted' }, el('strong', {}, 'Favours: '), r.favours)),
    ),
    section('Clauses', filters, clauseList),
    section(
      'Red flags',
      r.red_flags.length
        ? el('div', { class: 'item-list' }, r.red_flags.map((f) => el('div', { class: 'item', dataset: { risk: 'high' } }, el('h4', { class: 'item-title' }, f.issue), el('p', {}, f.why_it_matters), quote(f.quote, f.quote_status))))
        : null,
    ),
    section(
      'Contradictions',
      r.inconsistencies.length
        ? el('div', { class: 'item-list' }, r.inconsistencies.map((i) => el('div', { class: 'item', dataset: { risk: 'medium' } }, el('h4', { class: 'item-title' }, i.issue), quote(i.quote, i.quote_status), quote(i.conflicting_quote, i.conflicting_quote_status))))
        : null,
    ),
    section(
      'Obligations',
      r.obligations.length
        ? el(
            'div',
            { class: 'table-wrap' },
            el(
              'table',
              { class: 'data-table' },
              el('thead', {}, el('tr', {}, ['Who', 'Must do', 'By when'].map((h) => el('th', { scope: 'col' }, h)))),
              el('tbody', {}, r.obligations.map((o) => el('tr', {}, el('td', {}, o.party), el('td', {}, o.obligation, quote(o.quote, o.quote_status)), el('td', {}, o.deadline)))),
            ),
          )
        : null,
    ),
    section(
      'Key dates and deadlines',
      r.key_dates.length
        ? el('ol', { class: 'timeline' }, r.key_dates.map((d) => el('li', {}, el('time', {}, d.date), el('span', {}, d.event), quote(d.quote, d.quote_status))))
        : null,
    ),
    section('Missing protections', list(r.missing_clauses, (m) => [el('strong', {}, m.clause), `: ${m.why_it_matters}`])),
  );
}

function markdownRisks(r) {
  const clauses = [...r.clauses].sort((a, b) => RISK_ORDER[a.risk] - RISK_ORDER[b.risk]);
  return [
    `## Risk score: ${r.risk_score}/100 (${r.overall_risk})\n\n${r.verdict}\n\n**Favours:** ${r.favours}\n`,
    `## Clauses\n\n${clauses.map((c) => `### [${c.risk.toUpperCase()}] ${c.title}\n\n${c.explanation}\n\n${mdQuote(c.quote)}\n${c.suggestion ? `**Ask for:** ${c.suggestion}\n` : ''}`).join('\n')}`,
    r.red_flags.length ? `## Red flags\n\n${r.red_flags.map((f) => `- **${f.issue}**: ${f.why_it_matters}\n${mdQuote(f.quote)}`).join('')}` : '',
    r.inconsistencies.length ? `## Contradictions\n\n${r.inconsistencies.map((i) => `- **${i.issue}**\n${mdQuote(i.quote)}${mdQuote(i.conflicting_quote)}`).join('')}` : '',
    r.obligations.length ? `## Obligations\n\n| Who | Must do | By when |\n|---|---|---|\n${r.obligations.map((o) => `| ${o.party} | ${o.obligation} | ${o.deadline} |`).join('\n')}\n` : '',
    r.key_dates.length ? `## Key dates\n\n${mdList(r.key_dates, (d) => `**${d.date}**: ${d.event}`)}` : '',
    r.missing_clauses.length ? `## Missing protections\n\n${mdList(r.missing_clauses, (m) => `**${m.clause}**: ${m.why_it_matters}`)}` : '',
  ].join('\n');
}

// ------------------------------------------------------------------------- compare

function renderCompare(r, payload) {
  const [docA, docB] = payload.documents;
  const names = { A: docA?.name ?? 'Document A', B: docB?.name ?? 'Document B' };
  const verdict = r.better_for_user === 'equal' ? 'Neither document is clearly better for you' : `${names[r.better_for_user]} is better for you`;
  const favourBadge = (f) => (f === 'neutral' ? el('span', { class: 'badge badge-info' }, 'No clear winner') : el('span', { class: 'badge badge-ok' }, `Better in ${f}`));
  const uniqueList = (items, statusKey = 'quote_status') =>
    items.length ? el('div', { class: 'item-list' }, items.map((u) => el('div', { class: 'item' }, el('h4', { class: 'item-title' }, u.clause), el('p', {}, u.impact), quote(u.quote, u[statusKey])))) : null;

  return el(
    'div',
    {},
    headline('Verdict', verdict, el('p', {}, r.reason), el('p', { class: 'muted' }, r.summary)),
    section(
      'Differences',
      el(
        'div',
        { class: 'item-list' },
        r.differences.map((d) =>
          el(
            'article',
            { class: 'item', dataset: { risk: d.severity } },
            el('div', { class: 'item-head' }, riskBadge(d.severity), el('h4', { class: 'item-title' }, d.topic), favourBadge(d.favours)),
            el(
              'div',
              { class: 'compare-grid' },
              el('div', { class: `compare-col${d.favours === 'A' ? ' better' : ''}` }, el('h4', {}, `A: ${names.A}`), el('p', {}, d.doc_a), quote(d.quote_a, d.quote_a_status)),
              el('div', { class: `compare-col${d.favours === 'B' ? ' better' : ''}` }, el('h4', {}, `B: ${names.B}`), el('p', {}, d.doc_b), quote(d.quote_b, d.quote_b_status)),
            ),
            el('p', {}, el('strong', {}, 'What it means for you: '), d.impact),
          ),
        ),
      ),
    ),
    section(`Only in A (${names.A})`, uniqueList(r.only_in_a)),
    section(`Only in B (${names.B})`, uniqueList(r.only_in_b)),
    section('What to negotiate', list(r.negotiation_points)),
  );
}

function markdownCompare(r, payload) {
  const [docA, docB] = payload.documents;
  return [
    `## Verdict\n\n**Better for you:** ${r.better_for_user === 'A' ? docA?.name : r.better_for_user === 'B' ? docB?.name : 'Neither'}\n\n${r.reason}\n\n${r.summary}\n`,
    `## Differences\n\n| Topic | A: ${docA?.name} | B: ${docB?.name} | Better |\n|---|---|---|---|\n${r.differences.map((d) => `| ${d.topic} | ${d.doc_a} | ${d.doc_b} | ${d.favours} |`).join('\n')}\n`,
    r.only_in_a.length ? `## Only in A\n\n${mdList(r.only_in_a, (u) => `**${u.clause}**: ${u.impact}`)}` : '',
    r.only_in_b.length ? `## Only in B\n\n${mdList(r.only_in_b, (u) => `**${u.clause}**: ${u.impact}`)}` : '',
    r.negotiation_points.length ? `## What to negotiate\n\n${mdList(r.negotiation_points)}` : '',
  ].join('\n');
}

// ------------------------------------------------------------------------- action plan

function renderPlan(r) {
  return el(
    'div',
    {},
    headline('Your position', r.legal_position, el('p', { class: 'muted' }, r.situation_summary)),
    section('Legal basis', list(r.legal_basis, (b) => [el('strong', {}, b.law), `: ${b.relevance}`])),
    section(
      'Your options',
      el(
        'div',
        { class: 'options-grid' },
        r.options.map((o) =>
          el(
            'article',
            { class: 'item' },
            el('h4', { class: 'item-title' }, o.option),
            el('p', { class: 'item-meta' }, `Effort: ${o.effort}`),
            el('p', {}, el('strong', {}, 'Best when: '), o.best_when),
            el('div', { class: 'pros-cons' }, el('div', { class: 'pros' }, el('h4', {}, 'Pros'), list(o.pros)), el('div', { class: 'cons' }, el('h4', {}, 'Cons'), list(o.cons))),
          ),
        ),
      ),
    ),
    section(
      'Checklist',
      el(
        'ol',
        { class: 'checklist' },
        r.checklist.map((c, index) => {
          const id = `task-${index}-${Math.random().toString(36).slice(2, 7)}`;
          return el(
            'li',
            {},
            el('input', { type: 'checkbox', id }),
            el(
              'div',
              {},
              el('label', { for: id, class: 'task-text' }, el('strong', {}, c.task)),
              el('div', { class: 'item-head' }, riskBadge(c.priority), el('span', { class: 'item-meta' }, `Deadline: ${c.deadline}`)),
              el('p', {}, c.why),
              quote(c.quote, c.quote_status),
            ),
          );
        }),
      ),
    ),
    section(
      'Deadlines',
      r.deadlines.length
        ? el(
            'div',
            { class: 'table-wrap' },
            el(
              'table',
              { class: 'data-table' },
              el('thead', {}, el('tr', {}, ['When', 'What', 'If you miss it'].map((h) => el('th', { scope: 'col' }, h)))),
              el('tbody', {}, r.deadlines.map((d) => el('tr', {}, el('td', {}, d.date), el('td', {}, d.what, quote(d.quote, d.quote_status)), el('td', {}, d.consequence)))),
            ),
          )
        : null,
    ),
    section('Documents and evidence to gather', list(r.documents_to_gather)),
  );
}

function markdownPlan(r) {
  return [
    `## Your position\n\n${r.legal_position}\n\n_${r.situation_summary}_\n`,
    r.legal_basis.length ? `## Legal basis\n\n${mdList(r.legal_basis, (b) => `**${b.law}**: ${b.relevance}`)}` : '',
    `## Options\n\n${r.options.map((o) => `### ${o.option}\n\n- Effort: ${o.effort}\n- Best when: ${o.best_when}\n- Pros: ${o.pros.join('; ')}\n- Cons: ${o.cons.join('; ')}\n`).join('\n')}`,
    `## Checklist\n\n${r.checklist.map((c, i) => `${i + 1}. **${c.task}** (${c.priority} priority, deadline: ${c.deadline}): ${c.why}`).join('\n')}\n`,
    r.deadlines.length ? `## Deadlines\n\n| When | What | If you miss it |\n|---|---|---|\n${r.deadlines.map((d) => `| ${d.date} | ${d.what} | ${d.consequence} |`).join('\n')}\n` : '',
    r.documents_to_gather.length ? `## Documents to gather\n\n${mdList(r.documents_to_gather)}` : '',
  ].join('\n');
}

// ------------------------------------------------------------------------- lawyer brief

function renderBrief(r) {
  return el(
    'div',
    {},
    headline('Case summary', r.case_summary, el('p', {}, el('strong', {}, 'Goal: '), r.goal)),
    section(
      'Timeline',
      r.timeline.length ? el('ol', { class: 'timeline' }, r.timeline.map((t) => el('li', {}, el('time', {}, t.date), el('span', {}, t.event), quote(t.quote, t.quote_status)))) : null,
    ),
    section('Key facts', list(r.key_facts)),
    section('Legal issues to resolve', list(r.legal_issues)),
    section(
      'Questions to ask',
      r.questions_to_ask.length
        ? el('ol', { class: 'plain-list' }, r.questions_to_ask.map((q) => el('li', {}, el('strong', {}, q.question), el('br'), el('span', { class: 'muted' }, q.why))))
        : null,
    ),
    section('Documents to bring', list(r.documents_to_bring)),
    section('Still to confirm', list(r.open_points)),
  );
}

function markdownBrief(r) {
  return [
    `## Case summary\n\n${r.case_summary}\n\n**Goal:** ${r.goal}\n`,
    r.timeline.length ? `## Timeline\n\n${mdList(r.timeline, (t) => `**${t.date}**: ${t.event}`)}` : '',
    r.key_facts.length ? `## Key facts\n\n${mdList(r.key_facts)}` : '',
    r.legal_issues.length ? `## Legal issues\n\n${mdList(r.legal_issues)}` : '',
    r.questions_to_ask.length ? `## Questions to ask\n\n${r.questions_to_ask.map((q, i) => `${i + 1}. **${q.question}** (${q.why})`).join('\n')}\n` : '',
    r.documents_to_bring.length ? `## Documents to bring\n\n${mdList(r.documents_to_bring)}` : '',
    r.open_points.length ? `## Still to confirm\n\n${mdList(r.open_points)}` : '',
  ].join('\n');
}

export const RENDERERS = {
  simplify: { title: 'Plain-language summary', render: renderSimplify, markdown: markdownSimplify },
  risks: { title: 'Risk report', render: renderRisks, markdown: markdownRisks },
  compare: { title: 'Document comparison', render: renderCompare, markdown: markdownCompare },
  plan: { title: 'Next-steps plan', render: renderPlan, markdown: markdownPlan },
  brief: { title: 'Lawyer brief', render: renderBrief, markdown: markdownBrief },
};
