(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const snapshot = window.BENCH_SNAPSHOT != null;
  const source = snapshot ? window.BENCH_SNAPSHOT : null;
  const filterKeys = ['q', 'tool', 'model', 'task_id', 'purpose', 'prompt_version', 'status', 'date_from', 'date_to'];
  const scoreLabels = { compliance: '指令遵循', recognizability: '辨识度', motion: '运动表现', visual_detail: '视觉细节', interaction: '交互表现', completeness: '完整性' };
  const statusNames = { queued: '排队中', running: '运行中', generated: '待评估', completed: '已完成', failed: '失败', interrupted: '已中断', unavailable: '不可用', blocked: '被阻止' };
  const metricDefs = { cost_usd: ['已知费用', 'USD'], duration_ms: ['端到端耗时', '秒'], total_tokens: ['总 Token', 'token'], api_duration_ms: ['API 耗时', '秒'], input_tokens: ['输入 Token', 'token'], output_tokens: ['输出 Token', 'token'], source_bytes: ['源码体积（辅助）', 'byte'], source_file_count: ['源码文件数（辅助）', 'file'] };
  const state = { runs: [], tasks: [], options: {}, view: 'batch', filters: {}, selected: new Set(), authenticated: false, loaded: false, request: 0, controller: null, trendMetric: 'duration_ms', trendPage: 0, blindQueue: [], blindIndex: 0, blindSignature: '', texture: false, loading: false };
  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const valueOf = (run, key) => key === 'duration_ms' ? run.duration_ms : run.metrics?.[key];
  const text = value => value == null || value === '' ? 'unknown' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const number = value => finite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : 'unknown';
  const metricFormat = (value, key) => !finite(value) ? 'unknown' : key.includes('duration') ? `${(value / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 秒` : key === 'cost_usd' ? `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}` : number(value);
  const dateOf = run => String(run.date || run.started_at || '').slice(0, 10);
  const coverage = (n, total) => total ? `${Math.round(n / total * 100)}%` : '—';
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (key === 'hidden' || key === 'disabled' || key === 'checked') node[key] = !!value;
      else node.setAttribute(key, value);
    }
    for (const child of Array.isArray(children) ? children : [children]) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return node;
  }
  function svgEl(tag, attrs = {}, content) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (content != null) node.textContent = content;
    return node;
  }
  function notify(message, error = false) {
    const target = $('#notice'); target.hidden = !message; target.textContent = message || ''; target.classList.toggle('error', error);
  }
  function safePath(path) { return typeof path === 'string' && path.length > 0 && !path.startsWith('/') && !path.includes('\\') && !path.split('/').some(part => part === '..' || part === '.') && !/[\x00-\x1f]/.test(path); }
  function fileURL(run, path) { return `/api/runs/${encodeURIComponent(run.id)}/file?path=${encodeURIComponent(path)}`; }
  async function api(path, options = {}) {
    if (snapshot) throw new Error('只读 snapshot 不访问网络。');
    const headers = { ...options.headers }; if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
    if (response.status === 401) { clearSession(); openAuth(); throw new Error('会话未建立或已过期，请重新登录。'); }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      const raw = await response.text();
      try { const data = JSON.parse(raw); message = data.error?.message || data.error || data.detail || message; } catch (error) { message = `${message} ${raw.slice(0, 200)}`; }
      throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
    }
    return response;
  }
  async function json(path, options) { const response = await api(path, options); return response.json(); }
  function query() { const params = new URLSearchParams(); for (const key of filterKeys) if (state.filters[key]) params.set(key, state.filters[key]); return params.toString(); }
  function syncURL() {
    const url = new URL(window.location.href);
    for (const key of filterKeys) { if (state.filters[key]) url.searchParams.set(key, state.filters[key]); else url.searchParams.delete(key); }
    // Explicit empty purpose is meaningful: distinguish it from default benchmark on reload.
    if (!state.filters.purpose) url.searchParams.set('purpose', '');
    url.hash = state.view;
    try { history.replaceState(null, '', url); } catch (error) { notify('当前查看器无法保存筛选 URL；筛选仍然有效。'); }
  }
  function localFilter(runs) {
    return runs.filter(run => {
      for (const key of ['tool', 'model', 'task_id', 'purpose', 'prompt_version', 'status']) if (state.filters[key] && String(run[key] ?? '') !== state.filters[key]) return false;
      const date = dateOf(run);
      if (state.filters.date_from && (!date || date < state.filters.date_from)) return false;
      if (state.filters.date_to && (!date || date > state.filters.date_to)) return false;
      const q = (state.filters.q || '').trim().toLocaleLowerCase();
      if (q && !JSON.stringify({ ...run, reviews: undefined }).toLocaleLowerCase().includes(q)) return false;
      return true;
    });
  }
  function updateAuth() {
    $('#auth-button').textContent = snapshot ? '只读快照' : state.authenticated ? '退出登录' : '登录';
    $('#auth-button').disabled = snapshot;
    $('#refresh-button').disabled = snapshot;
    $$('[data-export]').forEach(button => { button.disabled = snapshot || !state.authenticated || !state.loaded; });
  }
  function clearSession() {
    ++state.request; if (state.controller) state.controller.abort(); setLoading(false);
    state.authenticated = false; state.loaded = false; state.runs = []; state.options = {}; state.tasks = []; state.selected.clear(); state.blindQueue = []; state.blindSignature = '';
    $('#detail-dialog').close(); $('#compare-dialog').close(); $('#detail-content').replaceChildren(); $('#compare-content').replaceChildren();
    fillOptions(); updateAuth(); render();
  }
  function openAuth() { if (!snapshot && !$('#auth-dialog').open) $('#auth-dialog').showModal(); }
  function setLoading(loading) { state.loading = loading; document.body.classList.toggle('loading', loading); $('#workspace').setAttribute('aria-busy', String(loading)); $('#refresh-button').disabled = snapshot || loading; }
  function fillOptions() {
    const options = state.options;
    const mappings = { tool: options.tools, model: options.models, task_id: options.tasks || state.tasks, prompt_version: options.prompt_versions, purpose: options.purposes, status: options.statuses || Object.keys(statusNames) };
    const names = { tool: '全部工具', model: '全部模型', task_id: '全部任务', prompt_version: '全部版本', purpose: '全部用途', status: '全部状态' };
    for (const [key, values] of Object.entries(mappings)) {
      const select = $(`[name="${key}"]`, $('#filters'));
      const entries = new Map();
      for (const value of values || []) {
        const id = typeof value === 'object' ? value.id : value;
        if (id != null) entries.set(String(id), typeof value === 'object' ? `${value.name || value.id}` : statusNames[value] || String(value));
      }
      if (key === 'purpose') entries.set('benchmark', 'benchmark');
      if (state.filters[key] && !entries.has(state.filters[key])) entries.set(state.filters[key], state.filters[key]);
      select.replaceChildren(el('option', { value: '', text: names[key] }), ...[...entries].map(([value, name]) => el('option', { value, text: name })));
      select.value = state.filters[key] || '';
    }
    for (const key of ['q', 'date_from', 'date_to']) $(`[name="${key}"]`, $('#filters')).value = state.filters[key] || '';
  }
  function inferOptions(runs) {
    const unique = key => [...new Set(runs.map(run => run[key]).filter(value => value != null))].sort();
    return { tools: unique('tool'), models: unique('model'), tasks: state.tasks.length ? state.tasks : [...new Map(runs.map(run => [run.task_id, { id: run.task_id, name: run.task_name || run.task_id }])).values()], prompt_versions: unique('prompt_version'), purposes: unique('purpose'), statuses: unique('status'), dates: unique('date') };
  }
  async function loadData(withOptions = false) {
    const request = ++state.request;
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    setLoading(true);
    try {
      if (snapshot) {
        state.tasks = Array.isArray(source.tasks) ? source.tasks : [];
        state.options = { ...inferOptions(source.runs || []), ...(source.options || {}) };
        state.runs = localFilter(Array.isArray(source.runs) ? source.runs : []);
        fillOptions();
      } else {
        if (withOptions) {
          const [options, tasks] = await Promise.all([json('/api/options', { signal: state.controller.signal }), json('/api/tasks', { signal: state.controller.signal })]);
          if (request !== state.request) return;
          state.options = options; state.tasks = tasks.tasks || []; fillOptions();
        }
        const data = await json(`/api/runs?${query()}`, { signal: state.controller.signal });
        if (request !== state.request) return;
        state.runs = Array.isArray(data.runs) ? data.runs : [];
        state.authenticated = true;
      }
      state.loaded = true;
      state.selected = new Set([...state.selected].filter(id => state.runs.some(run => run.id === id)));
      state.trendPage = 0;
      $('#updated-at').textContent = snapshot ? '只读快照 · 无持久状态' : `更新于 ${new Date().toLocaleTimeString('zh-CN')}`;
      notify(''); render(); updateAuth();
    } catch (error) {
      if (error.name !== 'AbortError' && request === state.request) { notify(error.message, true); if (!state.loaded) render(); }
    } finally { if (request === state.request) setLoading(false); }
  }
  function stat(label, value, detail, ratio) {
    const node = el('article', { class: 'stat' }, [el('p', { class: 'label', text: label }), el('p', { class: 'value', text: value }), el('small', { text: detail })]);
    if (ratio != null) { const fill = el('span'); fill.style.width = `${ratio * 100}%`; node.append(el('div', { class: 'coverage-track', 'aria-hidden': 'true' }, fill)); }
    return node;
  }
  function renderSummary() {
    const runs = state.runs, total = runs.length;
    const done = runs.filter(run => run.status === 'completed').length;
    const cost = runs.map(run => valueOf(run, 'cost_usd')).filter(finite);
    const tokens = runs.map(run => valueOf(run, 'total_tokens')).filter(finite);
    const evidence = runs.filter(run => evidencePaths(run).length).length;
    $('#summary').replaceChildren(
      stat('筛选内运行 / 完成覆盖', String(total).padStart(2, '0'), `${done} 已完成 / ${total} 运行 · ${coverage(done, total)}`, total ? done / total : 0),
      stat('已知费用小计 · USD', cost.length ? metricFormat(cost.reduce((a, b) => a + b, 0), 'cost_usd') : 'unknown', `${cost.length}/${total} 已知 · coverage ${coverage(cost.length, total)} · ${total - cost.length} unknown · ${runs.some(run => /unverified/i.test(run.metrics?.cost_source || '')) ? 'CLI 自报未核验 ≠ 账单' : '报告值 ≠ 账单'}`),
      stat('已知总 Token 小计', tokens.length ? number(tokens.reduce((a, b) => a + b, 0)) : 'unknown', `${tokens.length}/${total} 已知 · ${total - tokens.length} unknown；不以 0 代替`),
      stat('图像 / 视频证据登记覆盖', coverage(evidence, total), `${evidence}/${total} 已登记 · 非自动验收`, total ? evidence / total : 0)
    );
  }
  function empty(title = '当前条件下，没有运行。', detail = '调整上方筛选，或完成一次真实实验后刷新。') {
    return el('div', { class: 'empty' }, [el('div', { class: 'empty-symbol', text: '∅', 'aria-hidden': 'true' }), el('h3', { text: title }), el('p', { text: detail })]);
  }
  function render() {
    hideTooltip(); renderSummary(); updateSelection();
    const blind = state.view === 'blind';
    $('#summary').hidden = blind; $('#filters').hidden = blind;
    $$('[data-export]').forEach(button => { button.hidden = blind; });
    $('#texture-toggle').hidden = blind;
    $('#mode-label').textContent = snapshot ? '脱敏快照 · 只读' : blind ? '匿名审阅 · 信息遮蔽' : '真实运行 · 可追溯';
    $('#result-label').textContent = blind ? `匿名队列 · ${state.runs.length} 个候选运行 · 已隐藏模型 / 日期 / 费用及筛选内容` : `${snapshot ? '快照' : '当前筛选'} / ${state.runs.length} 运行 · ${new Set(state.runs.map(run => run.batch_id).filter(Boolean)).size} 批次`;
    const root = $('#view-content'); root.replaceChildren();
    if (!state.loaded) { root.append(empty('实验数据尚未连接。', snapshot ? '快照无可用数据。' : '登录后加载真实运行；此处不会填入演示数据。')); return; }
    if (blind) { renderBlind(root); return; }
    if (!state.runs.length) { root.append(empty()); return; }
    if (state.view === 'batch') renderBatch(root);
    else if (state.view === 'trend') renderTrend(root);
    else if (state.view === 'gallery') renderGallery(root);
    else renderEvidence(root);
  }
  function showTooltip(event, message) {
    const target = $('#chart-tooltip'); target.textContent = message; target.hidden = false;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX || rect.left + rect.width / 2, y = event.clientY || rect.top;
    target.style.left = `${Math.max(8, Math.min(x + 12, window.innerWidth - target.offsetWidth - 12))}px`;
    target.style.top = `${Math.max(8, Math.min(y + 14, window.innerHeight - target.offsetHeight - 12))}px`;
  }
  function hideTooltip() { $('#chart-tooltip').hidden = true; }
  function attachTooltip(mark, message, crosshair, x) {
    mark.setAttribute('tabindex', '0'); mark.setAttribute('role', 'img'); mark.setAttribute('aria-label', message);
    const show = event => { showTooltip(event, message); if (crosshair) { crosshair.hidden = false; crosshair.setAttribute('visibility', 'visible'); crosshair.setAttribute('x1', x); crosshair.setAttribute('x2', x); } };
    const hide = () => { hideTooltip(); if (crosshair) crosshair.setAttribute('visibility', 'hidden'); };
    mark.addEventListener('pointermove', show); mark.addEventListener('focus', show); mark.addEventListener('pointerleave', hide); mark.addEventListener('blur', hide);
  }
  function table(headers, rows, className = '') {
    return el('div', { class: 'table-scroll' }, el('table', { class: className }, [el('thead', {}, el('tr', {}, headers.map(name => el('th', { scope: 'col', text: name })))), el('tbody', {}, rows.map(row => el('tr', {}, row.map(cell => cell instanceof Node && cell.tagName === 'TD' ? cell : el('td', {}, cell)))))]));
  }
  function tableDisclosure(headers, rows) { return el('details', { class: 'chart-table' }, [el('summary', { text: '查看数据表 · 键盘可读' }), table(headers, rows)]); }
  let chartSequence = 0;
  function chartShell(width, height, title) {
    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'group', 'aria-label': title });
    const patternId = `bench-pattern-${++chartSequence}`;
    svg.style.setProperty('--bar-pattern', `url(#${patternId})`);
    const defs = svgEl('defs'), pattern = svgEl('pattern', { id: patternId, width: 8, height: 8, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    const base = svgEl('rect', { width: 8, height: 8 }); base.style.fill = 'var(--blue)';
    const stroke = svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 8, 'stroke-width': 2 }); stroke.style.stroke = 'var(--surface)';
    pattern.append(base, stroke); defs.append(pattern); svg.append(defs); return svg;
  }
  function barPanel(key) {
    const [title, unit] = metricDefs[key], all = state.runs.map(run => ({ run, value: valueOf(run, key) })).filter(item => finite(item.value)).sort((a, b) => b.value - a.value);
    const panel = el('article', { class: 'panel' }, el('div', { class: 'panel-head' }, el('div', {}, [el('h3', { text: title }), el('p', { text: `单次运行排序 · ${unit} · ${all.length}/${state.runs.length} 已知` })])));
    if (!all.length) { panel.append(empty('unknown', '当前筛选缺少此项计量，不绘制零值。')); return panel; }
    if (all.length === 1) {
      panel.append(el('p', { class: 'value-label', text: metricFormat(all[0].value, key) }), el('p', { class: 'footnote', text: text(all[0].run.model) }));
    } else {
      const items = all.slice(0, 12), width = 420, height = items.length * 38 + 32;
      const svg = chartShell(width, height, `${title}，按单次运行从高到低排序`);
      const max = Math.max(...items.map(item => item.value)) || 1;
      for (const ratio of [0, .5, 1]) {
        const x = 142 + ratio * 200;
        svg.append(svgEl('line', { x1: x, x2: x, y1: 8, y2: height - 25, class: 'gridline' }));
        svg.append(svgEl('text', { x, y: height - 7, 'text-anchor': 'middle' }, number(key.includes('duration') ? max * ratio / 1000 : max * ratio)));
      }
      items.forEach(({ run, value }, i) => {
        const y = 14 + i * 38, label = `${text(run.model)} · ${text(run.task_name || run.task_id)}`;
        const name = svgEl('text', { x: 0, y: y + 13 }, label.length > 21 ? `${label.slice(0, 20)}…` : label); name.append(svgEl('title', {}, label)); svg.append(name);
        const group = svgEl('g', { class: 'mark' });
        const w = value / max * 200;
        // Rounded data-end only: the baseline is square.
        const bar = svgEl('path', { d: w >= 4 ? `M142 ${y} H${142 + w - 4} Q${142 + w} ${y} ${142 + w} ${y + 4} V${y + 14} Q${142 + w} ${y + 18} ${142 + w - 4} ${y + 18} H142 Z` : `M142 ${y}h${w}v18h-${w}Z`, class: 'bar' });
        group.append(svgEl('rect', { x: 138, y: y - 4, width: 275, height: 30, class: 'hit' }), bar);
        attachTooltip(group, `${metricFormat(value, key)}\n${label}\n${text(run.id)} · ${dateOf(run) || 'unknown'}`); svg.append(group);
        svg.append(svgEl('text', { x: 414, y: y + 13, 'text-anchor': 'end', class: 'value-label' }, key.includes('duration') ? number(value / 1000) : key === 'cost_usd' ? `$${value.toFixed(4)}` : number(value)));
      });
      panel.append(el('div', { class: 'chart-scroll' }, svg));
    }
    panel.append(tableDisclosure(['运行', '模型', title], all.map(({ run, value }) => [text(run.id), text(run.model), metricFormat(value, key)])));
    if (all.length > 12) panel.append(el('p', { class: 'footnote', text: '图中显示前 12 个；数据表保留全部已知运行。' }));
    return panel;
  }
  function renderBatch(root) {
    root.append(el('div', { class: 'charts' }, ['cost_usd', 'duration_ms', 'total_tokens'].map(barPanel)), el('div', { class: 'section-heading' }, [el('div', {}, [el('h2', { text: '运行账本' }), el('p', { text: '勾选 2–4 次运行并排对照。混合条件仅描述原始计量，不构成模型优劣结论。' })]), el('span', { class: 'chip', text: '费用 / 时间 / Token 独立刻度' })]), runTable());
  }
  function statusChip(status) {
    const cls = status === 'completed' ? '' : ['failed', 'blocked'].includes(status) ? 'bad' : ['running', 'queued', 'interrupted', 'generated'].includes(status) ? 'warn' : '';
    const icon = status === 'completed' ? '○' : ['failed', 'blocked'].includes(status) ? '×' : '·';
    return el('span', { class: `chip ${cls}`, text: `${icon} ${statusNames[status] || text(status)}` });
  }
  function checksText(run) {
    const checks = Array.isArray(run.checks) ? run.checks : [];
    if (!checks.length) return '未验收';
    // A collapsed check set is not a passing ratio: an evaluator failure that
    // only left provider_route must never read as "all checks passed".
    const evaluation = run.evaluation?.status;
    if (evaluation && evaluation !== 'completed') return `未验收（${text(evaluation)}）`;
    const pass = checks.filter(check => ['pass', 'passed', 'ok'].includes(check.status)).length;
    return `${pass}/${checks.length} 通过`;
  }
  function detailButton(run, label = text(run.id)) { return el('button', { text: label, onclick: () => openDetail(run.id) }); }
  function runTable() {
    const rows = state.runs.map(run => {
      const checkbox = el('input', { type: 'checkbox', 'aria-label': `选择运行 ${run.id}`, checked: state.selected.has(run.id), onchange: event => {
        if (event.target.checked && state.selected.size >= 4) { event.target.checked = false; notify('最多选择 4 次运行。'); return; }
        if (event.target.checked) state.selected.add(run.id); else state.selected.delete(run.id); updateSelection();
      } });
      return [checkbox, el('td', { class: 'run-name' }, [detailButton(run), el('small', { text: `批次 ${text(run.batch_id)}` })]), dateOf(run) || 'unknown', text(run.tool), text(run.model), text(run.task_name || run.task_id), text(run.prompt_version), statusChip(run.status), checksText(run), ...['cost_usd', 'duration_ms', 'total_tokens', 'source_bytes'].map(key => el('td', { class: 'numeric', text: metricFormat(valueOf(run, key), key) }))];
    });
    return table(['选择', '运行 / 批次', '日期', '工具', '模型', '任务', 'Prompt', '状态', 'Checks', '费用 USD', '耗时', '总 Token', '源码 byte（辅助）'], rows, 'run-table');
  }
  function updateSelection() {
    const blind = state.view === 'blind'; $('#comparison-tray').hidden = blind || !state.selected.size;
    $('#selection-label').textContent = `已选择 ${state.selected.size} / 4 次运行`;
    $('#compare-button').disabled = state.selected.size < 2;
  }
  function conditionKey(run) {
    // An absent fingerprint is never evidence of identical experimental conditions.
    return [run.condition_fingerprint || `unknown-${run.id}`, run.tool, run.model, run.task_id, run.prompt_version, run.purpose].map(text).join(' | ');
  }

  // Effort is a benchmark condition, not a footnote: two runs of one model at
  // different effort levels are not comparable, and a run whose effort could
  // not be controlled must never be grouped with one whose effort was.
  const effortControlNames = {
    declared: '已设为该模型最高档',
    fixed_unspecified_effort: '模型无档位阶梯，强度由模型内部固定',
    no_effort_vocabulary: '目录未声明档位阶梯',
    not_in_catalogue: '目录中无此模型，强度不可控',
    ambiguous_in_catalogue: '目录中 ID 歧义，强度不可控',
    catalogue_unreadable: '目录不可读，强度不可控',
    disabled_by_operator: '本次批次未启用强度控制',
  };

  function effortText(run) {
    const applied = run.conditions?.reasoning_effort_applied;
    const control = run.conditions?.reasoning_effort_control;
    if (!control) return '未记录（旧归档无此条件）';
    if (!applied) return `不可控：${effortControlNames[control] || control}`;
    const downgraded = run.conditions?.reasoning_effort_requested
      && run.conditions.reasoning_effort_requested !== applied;
    return downgraded
      ? `${applied}（请求 ${run.conditions.reasoning_effort_requested}，该模型上限 ${applied}）`
      : `${applied}（${effortControlNames[control] || control}）`;
  }
  function trendGroups() {
    const groups = new Map();
    for (const run of state.runs) { const key = conditionKey(run); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(run); }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }
  function dailyDates(runs) {
    const observed = [...new Set(runs.map(dateOf).filter(date => /^\d{4}-\d{2}-\d{2}$/.test(date)))].sort();
    if (!observed.length) return [];
    const first = Date.parse(`${observed[0]}T00:00:00Z`), last = Date.parse(`${observed.at(-1)}T00:00:00Z`);
    if (!Number.isFinite(first) || !Number.isFinite(last)) return [];
    const days = Math.round((last - first) / 86400000);
    if (days > 3660) return observed; // Long ranges still break segments at any non-consecutive calendar day.
    return Array.from({ length: days + 1 }, (_, i) => new Date(first + i * 86400000).toISOString().slice(0, 10));
  }
  function renderTrend(root) {
    const groups = trendGroups(); state.trendPage = Math.min(state.trendPage, Math.max(0, Math.ceil(groups.length / 3) - 1));
    const selector = el('select', { 'aria-label': '趋势指标', onchange: event => { state.trendMetric = event.target.value; render(); } }, Object.entries(metricDefs).map(([key, [name]]) => el('option', { value: key, text: name })));
    selector.value = state.trendMetric;
    const controls = el('div', { class: 'trend-controls' }, [el('label', {}, ['指标', selector]), el('button', { text: '上一组', disabled: !state.trendPage, onclick: () => { state.trendPage--; render(); } }), el('button', { text: '下一组', disabled: (state.trendPage + 1) * 3 >= groups.length, onclick: () => { state.trendPage++; render(); } })]);
    root.append(el('div', { class: 'section-heading' }, [el('div', {}, [el('h2', { text: '同条件，才有可比趋势。' }), el('p', { text: `按指纹 + 工具 / 模型 / 任务 / 版本 / purpose 分面，每页最多 3 组（${state.trendPage + 1}/${Math.max(1, Math.ceil(groups.length / 3))}）。每日仅对已完成且计量已知的运行取均值。` })]), controls]));
    root.append(el('p', { class: 'blind-note', text: '缺失日期或计量会断线；失败不当作零值。指纹缺失的运行单独成组。耗时和费用变化不是质量退化；不生成综合质量分。' }));
    const dates = dailyDates(state.runs);
    root.append(el('div', { class: 'charts' }, groups.slice(state.trendPage * 3, state.trendPage * 3 + 3).map(([key, runs]) => trendPanel(key, runs, dates))));
    root.append(el('div', { class: 'section-heading' }, el('h2', { text: '趋势对应运行' })), runTable());
  }
  function trendPanel(key, runs, dates) {
    const metric = state.trendMetric, [name, unit] = metricDefs[metric], run = runs[0];
    const values = dates.map(date => {
      const atDate = runs.filter(item => dateOf(item) === date && item.status === 'completed');
      const valid = atDate.map(item => valueOf(item, metric)).filter(finite);
      return { date, value: valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null, count: valid.length, total: atDate.length };
    });
    const panel = el('article', { class: 'panel' }, [el('h3', { text: `${text(run.model)} · ${text(run.task_name || run.task_id)}` }), el('p', { class: 'condition-key', text: `${text(run.tool)} · ${text(run.prompt_version)} · ${text(run.purpose)}` }), el('p', { class: 'condition-key mono', text: `指纹 ${text(run.condition_fingerprint)}`, title: key }), el('p', { class: 'condition-key', text: `推理强度 ${effortText(run)}` }), el('p', { class: 'footnote', text: `${name} · ${unit} · 单条件每日均值` })]);
    if (!values.some(item => finite(item.value))) { panel.append(empty('暂无可绘制计量', '当前条件没有已完成且指标已知的运行。')); return panel; }
    const svg = chartShell(420, 240, `${text(run.model)}，${name}每日均值`), max = Math.max(...values.map(item => item.value || 0)) * 1.15 || 1;
    const x = i => 56 + (values.length > 1 ? i / (values.length - 1) : .5) * 332, y = value => 190 - value / max * 150;
    for (const ratio of [0, .5, 1]) {
      const yy = y(max * ratio); svg.append(svgEl('line', { x1: 56, x2: 388, y1: yy, y2: yy, class: 'gridline' }));
      svg.append(svgEl('text', { x: 50, y: yy + 4, 'text-anchor': 'end' }, number(metric.includes('duration') ? max * ratio / 1000 : max * ratio)));
    }
    const paths = []; let segment = [];
    values.forEach((item, i) => {
      const consecutive = i === 0 || Date.parse(item.date) - Date.parse(values[i - 1].date) === 86400000;
      if (!finite(item.value) || !consecutive) { if (segment.length) paths.push(segment); segment = []; }
      if (finite(item.value)) segment.push([x(i), y(item.value)]);
    });
    if (segment.length) paths.push(segment);
    for (const points of paths) svg.append(svgEl('path', { d: points.map(([xx, yy], i) => `${i ? 'L' : 'M'}${xx} ${yy}`).join(' '), class: 'trendline' }));
    values.forEach((item, i) => { if (finite(item.value)) svg.append(svgEl('circle', { cx: x(i), cy: y(item.value), r: 4, class: 'point' })); });
    const crosshair = svgEl('line', { x1: 56, x2: 56, y1: 30, y2: 195, class: 'crosshair', visibility: 'hidden' }); svg.append(crosshair);
    const hitWidth = Math.max(2, 332 / Math.max(1, values.length - 1));
    values.forEach((item, i) => {
      const hit = svgEl('rect', { x: x(i) - hitWidth / 2, y: 24, width: hitWidth, height: 174, class: 'hit' });
      attachTooltip(hit, `${metricFormat(item.value, metric)}\n${item.date} · ${text(run.model)}\n${item.count}/${item.total} 完成运行计量已知`, crosshair, x(i)); svg.append(hit);
    });
    for (const i of [...new Set([0, Math.floor((values.length - 1) / 2), values.length - 1])]) if (values[i]) svg.append(svgEl('text', { x: x(i), y: 219, 'text-anchor': 'middle' }, values[i].date.slice(5)));
    const last = values.findLastIndex(item => finite(item.value));
    if (last >= 0) svg.append(svgEl('text', { x: Math.min(382, x(last)), y: Math.max(18, y(values[last].value) - 12), 'text-anchor': 'end', class: 'value-label' }, metricFormat(values[last].value, metric)));
    panel.append(el('div', { class: 'chart-scroll' }, svg), tableDisclosure(['日期', `${name}均值`, '已知 / 完成'], values.map(item => [item.date, metricFormat(item.value, metric), `${item.count}/${item.total}`])));
    return panel;
  }
  function evidencePaths(run) {
    const list = Array.isArray(run.evaluation?.evidence) ? run.evaluation.evidence : [];
    return [...new Set(list.map(item => typeof item === 'string' ? item : item?.path).filter(path => safePath(path) && /\.(png|jpe?g|webp|gif|webm|mp4)$/i.test(path)))];
  }
  function media(run, path, anonymous = false) {
    const frame = el('div', { class: 'media-frame' });
    const video = /\.(webm|mp4)$/i.test(path), expected = video ? 'video/' : 'image/';
    const uri = snapshot ? source.evidence?.[`${run.id}/${path}`] : fileURL(run, path);
    // Snapshot only embeds allowlisted raster/video data URIs, never HTML or SVG.
    if (!uri || (snapshot && !(new RegExp(`^data:${expected}(?:${video ? 'webm|mp4' : 'png|jpeg|webp|gif'});base64,`, 'i')).test(uri))) { frame.append(el('p', { class: 'muted', text: '此快照未包含可安全预览的证据。' })); return frame; }
    const attrs = video ? { src: uri, controls: '', preload: 'metadata', playsinline: '', 'aria-label': anonymous ? '匿名视频证据' : path } : { src: uri, loading: 'lazy', alt: anonymous ? '匿名图像证据' : `运行 ${text(run.id)} 的证据 ${path}` };
    const element = el(video ? 'video' : 'img', attrs);
    element.addEventListener('error', () => { if (!frame.querySelector('.media-failure')) frame.append(el('div', { class: 'media-failure', text: '证据无法加载；请检查会话或文件完整性。' })); });
    frame.append(element); return frame;
  }
  function renderGallery(root) {
    const withEvidence = state.runs.filter(run => evidencePaths(run).length);
    root.append(el('div', { class: 'section-heading' }, el('div', {}, [el('h2', { text: '产物，只看证据。' }), el('p', { text: '仅预览 evaluation.evidence 中的栅格图像与视频。生成的 HTML / SVG 不执行、不内嵌。' })])));
    if (!withEvidence.length) { root.append(empty('还没有可预览的证据。', '运行完成不代表已采集证据；请检查 evaluation 与产物附件。')); return; }
    root.append(el('div', { class: 'gallery' }, withEvidence.map(run => el('article', { class: 'evidence-card' }, [media(run, evidencePaths(run)[0]), el('div', { class: 'caption' }, [el('h3', { text: text(run.model) }), el('p', { text: `${text(run.task_name || run.task_id)} · ${dateOf(run) || 'unknown'} · ${text(run.prompt_version)}` }), statusChip(run.status), detailButton(run, `查看 ${evidencePaths(run).length} 份证据与详情`)])]))));
  }
  function renderEvidence(root) {
    root.append(el('div', { class: 'section-heading' }, el('div', {}, [el('h2', { text: '每个结论，都有出处。' }), el('p', { text: '从运行详情检查 manifest、metrics 来源、Checks、日志、附件与追加评分。' })])), runTable());
  }
  async function getRun(id) {
    if (snapshot) { const run = (source.runs || []).find(item => item.id === id); if (!run) throw new Error('快照中不存在此运行。'); return run; }
    const result = await json(`/api/runs/${encodeURIComponent(id)}`); if (!result.run) throw new Error('响应缺少运行详情。'); return result.run;
  }
  function metadata(pairs) { return el('dl', { class: 'metadata' }, pairs.flatMap(([name, value]) => [el('dt', { text: name }), el('dd', { text: text(value) })])); }
  function detailBlock(title, children, full = false) { return el('section', { class: `detail-block${full ? ' full-width' : ''}` }, [el('h3', { text: title }), ...children]); }
  async function openDetail(id) {
    const dialog = $('#detail-dialog'), content = $('#detail-content');
    content.replaceChildren(el('p', { class: 'muted', text: '正在读取运行详情…' })); if (!dialog.open) dialog.showModal();
    try {
      const run = await getRun(id); if (!dialog.open) return;
      $('#detail-title').textContent = text(run.id);
      const blocks = [];
      blocks.push(detailBlock('实验条件 / Prompt', [metadata([
        ['运行状态', statusNames[run.status] || run.status], ['生成结论', run.generation_status || run.status], ['尝试编号（首次为 0）', run.attempt], ['重试来源', run.retry_of], ['归档完整性', run.archive_status], ['评估结束', run.evaluation_finished_at], ['批次', run.batch_id], ['日期', dateOf(run)], ['工具 / 模型', `${text(run.tool)} / ${text(run.model)}`], ['任务', `${text(run.task_name)} (${text(run.task_id)})`], ['Purpose', run.purpose], ['Prompt 版本', run.prompt_version], ['条件指纹', run.condition_fingerprint], ['推理强度', effortText(run)], ['Prompt hash', run.prompt_hash || run.prompt?.sha256 || run.prompt?.hash], ['归档路径', run.archive_dir], ['开始 / 结束', `${text(run.started_at)} / ${text(run.finished_at)}`], ['错误', run.error]
      ]), el('details', {}, [el('summary', { text: 'Prompt 原文 / 元信息（如 manifest 提供）' }), el('div', { class: 'pre-scroll' }, el('pre', { text: text(run.prompt || run.prompt_text) }))]) ]));
      const metrics = run.metrics || {};
      blocks.push(detailBlock('计量与来源', [metadata([['端到端耗时', metricFormat(run.duration_ms, 'duration_ms')], ...Object.entries(metricDefs).filter(([key]) => key !== 'duration_ms').map(([key, [label]]) => [label, metricFormat(metrics[key], key)]), ['缓存读取', number(metrics.cache_read_tokens)], ['缓存写入', number(metrics.cache_write_tokens)], ['推理 Token', number(metrics.reasoning_tokens)], ['轮数', number(metrics.num_turns)], ['费用来源', metrics.cost_source], ['Metrics 来源', run.metrics_source || metrics.source || 'manifest.metrics（来源未声明）']]), el('p', { class: 'footnote', text: '计量缺失是 unknown，零值仅在来源明确记录为 0 时展示。源码体积与文件数仅辅助观察变化，不是质量分，也不是越大越好。' })]));
      blocks.push(detailBlock('Checks / Evaluation', [el('p', { class: 'footnote', text: `Evaluation: ${text(run.evaluation?.status)} · 完成状态不自动转换为 pass` }), ...(run.checks?.length ? run.checks.map(check => el('p', {}, [el('span', { class: 'chip', text: `${text(check.status)} · ${text(check.name)}` }), el('span', { text: ` ${text(check.detail)}` })])) : [el('p', { class: 'muted', text: '未提供 Checks；未验收。' })]) ]));
      blocks.push(detailBlock('追加评分记录', [reviewList(run.reviews || [])]));
      const paths = evidencePaths(run);
      blocks.push(detailBlock('安全证据预览', paths.length ? [el('div', { class: 'gallery' }, paths.map(path => el('div', {}, [media(run, path), el('p', { class: 'footnote mono', text: path })])))] : [el('p', { class: 'muted', text: '没有可预览的 PNG / WebM 等证据。' })], true));
      blocks.push(detailBlock('产物附件 / 完整性', [attachments(run)], true));
      blocks.push(detailBlock('文本日志', [logViewer(run)], true));
      blocks.push(detailBlock('Manifest · 原始记录', [el('details', {}, [el('summary', { text: '展开 JSON（纯文本，不执行内容）' }), el('div', { class: 'pre-scroll' }, el('pre', { text: JSON.stringify(run, null, 2) }))])], true));
      content.replaceChildren(el('div', { class: 'detail-grid' }, blocks));
    } catch (error) { content.replaceChildren(el('p', { class: 'error', text: error.message })); }
  }
  function reviewList(reviews) {
    if (!reviews.length) return el('p', { class: 'muted', text: '暂无评分；不会推算或自动补齐。' });
    return el('ol', { class: 'review-list' }, reviews.map(review => el('li', {}, [el('strong', { text: `${text(review.reviewer)} · ${review.blind ? '盲评' : '非盲评'}` }), el('small', { text: ` ${text(review.created_at)}` }), el('p', { text: Object.entries(scoreLabels).map(([key, label]) => `${label} ${review.scores?.[key] == null ? '未评' : review.scores[key]}`).join(' / ') }), el('p', { text: review.note || '无附注' })])));
  }
  function attachments(run) {
    if (!run.artifacts?.length) return el('p', { class: 'muted', text: '无产物附件。' });
    return el('ul', { class: 'attachment-list' }, run.artifacts.map(artifact => el('li', {}, [el('div', { class: 'file-info' }, [el('span', { class: 'mono', text: text(artifact.path) }), el('small', { text: `${text(artifact.kind)} · ${number(artifact.size)} bytes` }), el('small', { class: 'mono', text: `SHA256 ${text(artifact.sha256)}` })]), el('button', { text: snapshot ? '快照不可下载' : '下载附件', disabled: snapshot || !safePath(artifact.path), onclick: async event => {
      event.target.disabled = true;
      try { const response = await api(fileURL(run, artifact.path)); await download(response, artifact.path.split('/').at(-1)); } catch (error) { notify(error.message, true); } finally { event.target.disabled = false; }
    } })])));
  }
  function logViewer(run) {
    if (snapshot) return el('p', { class: 'muted', text: '只读脱敏快照不包含、不请求文本日志。' });
    const paths = (run.artifacts || []).filter(artifact => safePath(artifact.path) && /\.(log|txt|jsonl)$/i.test(artifact.path));
    if (!paths.length) return el('p', { class: 'muted', text: '没有可读取的文本日志附件。' });
    const select = el('select', { 'aria-label': '文本日志附件' }, paths.map(artifact => el('option', { value: artifact.path, text: artifact.path })));
    const output = el('pre', { text: '选择日志后读取；始终以纯文本显示。' });
    const button = el('button', { text: '读取日志', onclick: async () => {
      button.disabled = true;
      try { const response = await api(fileURL(run, select.value)); const raw = await response.text(); output.textContent = raw.length > 2_000_000 ? `${raw.slice(0, 2_000_000)}\n[日志过长，显示前 2MB 字符；完整内容请下载附件]` : raw; } catch (error) { output.textContent = `日志读取失败：${error.message}`; } finally { button.disabled = false; }
    } });
    return el('div', {}, [el('div', { class: 'log-picker' }, [select, button]), el('div', { class: 'pre-scroll' }, output)]);
  }
  async function download(response, name) {
    if (snapshot) throw new Error('快照不允许下载。');
    // Force attachment semantics even for raw generated HTML/SVG.
    const blob = new Blob([await response.blob()], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob), anchor = el('a', { href: url, download: name });
    document.body.append(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
  async function openComparison() {
    if (state.selected.size < 2 || state.selected.size > 4) return;
    const dialog = $('#compare-dialog'), content = $('#compare-content'); content.replaceChildren(); dialog.showModal();
    try {
      const runs = await Promise.all([...state.selected].map(getRun)); content.style.setProperty('--columns', runs.length);
      content.replaceChildren(...runs.map(run => {
        const paths = evidencePaths(run);
        return el('article', { class: 'compare-column' }, [el('h3', { text: text(run.model) }), el('p', { class: 'condition-key', text: `${text(run.tool)} · ${text(run.task_name || run.task_id)} · ${dateOf(run)}` }), el('p', { class: 'condition-key mono', text: `${text(run.prompt_version)} / ${text(run.condition_fingerprint)}` }), statusChip(run.status), el('p', { class: 'footnote', text: `${metricFormat(valueOf(run, 'cost_usd'), 'cost_usd')} · ${metricFormat(run.duration_ms, 'duration_ms')} · ${checksText(run)}` }), ...(paths.length ? paths.map(path => media(run, path)) : [empty('无证据', '未采集可预览的图像或视频。')]), detailButton(run, '运行详情')]);
      }));
    } catch (error) { content.replaceChildren(el('p', { class: 'error', text: error.message })); }
  }
  function shuffle(items) {
    const array = [...items];
    for (let i = array.length - 1; i > 0; i--) {
      const limit = Math.floor(0x100000000 / (i + 1)) * (i + 1); let random;
      do { const values = new Uint32Array(1); crypto.getRandomValues(values); random = values[0]; } while (random >= limit);
      const j = random % (i + 1); [array[i], array[j]] = [array[j], array[i]];
    }
    return array;
  }
  function renderBlind(root) {
    if (snapshot) { root.append(empty('快照仅支持只读审阅。', '盲评分写入已禁用；可在产物图库查看已脱敏证据。')); return; }
    const candidates = state.runs.filter(run => evidencePaths(run).length), signature = candidates.map(run => run.id).sort().join('\u001f');
    if (signature !== state.blindSignature) { state.blindSignature = signature; state.blindQueue = shuffle(candidates); state.blindIndex = 0; }
    if (!candidates.length) { root.append(empty('当前筛选没有可盲评证据。', '返回其他视图调整筛选；只有安全图像 / 视频进入匿名队列。')); return; }
    const run = state.blindQueue[state.blindIndex % state.blindQueue.length];
    root.append(el('div', { class: 'section-heading' }, [el('div', {}, [el('h2', { text: '先看结果，再看名字。' }), el('p', { text: '随机匿名队列；已隐藏模型、日期、成本、运行标识与证据路径。' })]), el('button', { text: '重新打乱', onclick: () => { state.blindQueue = shuffle(candidates); state.blindIndex = 0; render(); } })]));
    const caseNumber = (state.blindIndex % state.blindQueue.length) + 1;
    const evidence = el('section', { class: 'panel blind-evidence' }, [el('p', { class: 'eyebrow', text: 'ANONYMOUS REVIEW' }), el('h3', { class: 'blind-anon', text: `样本 ${String(caseNumber).padStart(2, '0')}` }), ...evidencePaths(run).map(path => media(run, path, true))]);
    const form = el('form', { class: 'panel blind-form', id: 'review-form' });
    form.append(el('h3', { text: '六项独立评分 · 0–4' }), el('p', { class: 'blind-note', text: '0 未达成 · 1 较弱 · 2 部分达成 · 3 良好 · 4 充分达成。允许部分评分，不计算总分。筛选或证据画面本身可能暴露身份；有疑虑请留证。' }));
    form.append(el('label', {}, ['Reviewer（必填）', el('input', { name: 'reviewer', required: '', maxlength: 200, autocomplete: 'off', placeholder: '你的审阅标识' })]));
    form.append(el('div', { class: 'score-grid' }, Object.entries(scoreLabels).map(([key, label]) => el('label', {}, [label, el('select', { name: key }, [el('option', { value: '', text: '未评 / 不适用' }), ...[0, 1, 2, 3, 4].map(score => el('option', { value: score, text: String(score) }))])]))));
    const unblinded = el('input', { name: 'unblinded', type: 'checkbox' });
    form.append(el('label', { class: 'checkbox-label' }, [unblinded, '疑似解盲（保存为非盲评并留证）']), el('label', {}, ['Note / 评分依据', el('textarea', { name: 'note', maxlength: 10000, placeholder: '记录观察依据、缺失项或疑似解盲原因…' })]));
    const result = el('p', { class: 'error', role: 'status', id: 'review-result' });
    const save = el('button', { class: 'primary', type: 'submit', text: '追加保存评分' });
    form.append(result, el('div', { class: 'dialog-actions' }, [el('button', { type: 'button', text: '跳过 / 下一份', onclick: () => { state.blindIndex++; render(); } }), save]));
    form.addEventListener('submit', async event => {
      event.preventDefault(); const data = new FormData(form), reviewer = String(data.get('reviewer') || '').trim();
      if (!reviewer) { result.textContent = 'Reviewer 必填。'; return; }
      const scores = {}; for (const key of Object.keys(scoreLabels)) { const raw = data.get(key); if (raw !== '') scores[key] = Number(raw); }
      const note = String(data.get('note') || '').trim();
      const payload = { reviewer, scores, note: unblinded.checked ? `[疑似解盲] ${note}` : note, blind: !unblinded.checked };
      save.disabled = true; result.textContent = '正在追加保存…';
      try {
        await json(`/api/runs/${encodeURIComponent(run.id)}/reviews`, { method: 'POST', body: JSON.stringify(payload) });
        const updated = await getRun(run.id);
        const saved = (updated.reviews || []).some(review => review.reviewer === payload.reviewer && review.note === payload.note && review.blind === payload.blind && Object.entries(scores).every(([key, value]) => review.scores?.[key] === value));
        if (!saved) throw new Error('提交成功但读回未发现完整评分记录，请检查后端存储。');
        run.reviews = updated.reviews;
        const local = state.runs.find(item => item.id === run.id); if (local) local.reviews = updated.reviews;
        result.classList.remove('error'); result.classList.add('muted'); result.textContent = '评分已追加保存，并已读回验证。不会覆盖既有评分。';
        save.textContent = '已保存'; // One append per submission; next sample offers a new form.
      } catch (error) { result.classList.add('error'); result.textContent = error.message; save.disabled = false; }
    });
    root.append(el('div', { class: 'blind-layout' }, [evidence, form]));
  }
  function setView(view) {
    state.view = ['batch', 'trend', 'gallery', 'blind', 'evidence'].includes(view) ? view : 'batch';
    $$('[data-view]').forEach(button => { const active = button.dataset.view === state.view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    // Entering anonymous review must not leave an identifying dialog open.
    if (state.view === 'blind') { $('#detail-dialog').close(); $('#compare-dialog').close(); }
    syncURL(); render();
  }
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('#theme-toggle').setAttribute('aria-label', `切换至${theme === 'dark' ? '浅色' : '深色'}主题`);
    $('#theme-toggle').textContent = theme === 'dark' ? '浅色' : '深色';
    try { localStorage.setItem('bench-theme', theme); } catch (error) { notify('浏览器无法保存主题偏好；本次主题切换仍然有效。'); }
  }
  let filterTimer;
  function handleFilter(event) {
    for (const key of filterKeys) state.filters[key] = $(`[name="${key}"]`, $('#filters')).value;
    clearTimeout(filterTimer);
    if (state.filters.date_from && state.filters.date_to && state.filters.date_from > state.filters.date_to) { notify('开始日期不能晚于结束日期。', true); return; }
    syncURL(); state.selected.clear();
    filterTimer = window.setTimeout(() => loadData(), event.target.name === 'q' ? 250 : 0);
  }
  $('#filters').addEventListener('input', handleFilter);
  $('#filters').addEventListener('submit', event => { event.preventDefault(); clearTimeout(filterTimer); loadData(); });
  $('#reset-filters').addEventListener('click', () => { state.filters = { purpose: 'benchmark' }; state.selected.clear(); fillOptions(); syncURL(); loadData(); });
  $$('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
  $('#theme-toggle').addEventListener('click', () => { const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches); setTheme(dark ? 'light' : 'dark'); });
  $('#texture-toggle').addEventListener('click', event => { state.texture = !state.texture; document.body.classList.toggle('texture', state.texture); event.target.setAttribute('aria-pressed', String(state.texture)); });
  $('#refresh-button').addEventListener('click', () => loadData(true));
  $('#auth-button').addEventListener('click', async () => {
    if (!state.authenticated) { openAuth(); return; }
    try {
      await api('/api/logout', { method: 'POST', body: '{}' });
      clearSession(); notify('已退出登录，实验数据已从当前视图清除。');
    } catch (error) { notify(error.message, true); }
  });
  $('#cancel-login').addEventListener('click', () => { $('#auth-dialog').close(); $('#login-token').value = ''; });
  $('#auth-dialog').addEventListener('close', () => { $('#login-token').value = ''; });
  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('button[type="submit"]', event.target); button.disabled = true; $('#login-error').textContent = '';
    const token = $('#login-token').value; $('#login-token').value = '';
    try { await api('/api/login', { method: 'POST', body: JSON.stringify({ token }) }); state.authenticated = true; $('#auth-dialog').close(); updateAuth(); await loadData(true); } catch (error) { $('#login-error').textContent = error.message; } finally { button.disabled = false; }
  });
  $$('[data-close]').forEach(button => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
  ['detail-dialog', 'compare-dialog'].forEach(id => $(`#${id}`).addEventListener('close', () => { $$('video', $(`#${id}`)).forEach(video => video.pause()); }));
  $('#clear-selection').addEventListener('click', () => { state.selected.clear(); render(); });
  $('#compare-button').addEventListener('click', openComparison);
  $('#restart-videos').addEventListener('click', async () => {
    const videos = $$('video', $('#compare-content')); videos.forEach(video => { video.pause(); video.currentTime = 0; });
    const results = await Promise.allSettled(videos.map(video => video.play()));
    if (results.some(result => result.status === 'rejected')) notify('部分视频无法自动播放，所有视频已重置到起点；请使用视频控件播放。');
  });
  $$('[data-export]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { const response = await api(`/api/export?format=${button.dataset.export}&${query()}`); await download(response, `bench-runs.${button.dataset.export}`); notify('已导出当前筛选的运行数据。'); } catch (error) { notify(error.message, true); } finally { updateAuth(); }
  }));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });
  window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
  window.addEventListener('popstate', () => { const params = new URLSearchParams(location.search); for (const key of filterKeys) state.filters[key] = params.get(key) || (key === 'purpose' && !params.has('purpose') ? 'benchmark' : ''); fillOptions(); setView(location.hash.slice(1)); loadData(); });
  const params = new URLSearchParams(location.search);
  for (const key of filterKeys) state.filters[key] = params.get(key) || (key === 'purpose' && !params.has('purpose') ? 'benchmark' : '');
  try { const theme = localStorage.getItem('bench-theme'); if (['light', 'dark'].includes(theme)) document.documentElement.dataset.theme = theme; } catch (error) { console.warn('主题偏好不可读取；使用系统主题。', error.name); }
  $('#mode-label').textContent = snapshot ? '脱敏快照 · 只读' : '真实运行 · 可追溯';
  updateAuth(); setView(location.hash.slice(1)); loadData(true);
})();
