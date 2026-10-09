(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const snapshot = window.BENCH_SNAPSHOT != null;
  const source = snapshot ? window.BENCH_SNAPSHOT : null;
  // Snapshot records and mappings are immutable; live API responses are not cached.
  const searchTexts = snapshot ? new WeakMap() : null;
  const evidenceLists = snapshot ? new WeakMap() : null;
  const includedLists = snapshot ? new WeakMap() : null;
  const mediaURIs = snapshot ? new WeakMap() : null;
  let snapshotOptionsReady = false;
  const filterKeys = ['q', 'tool', 'model', 'task_id', 'purpose', 'prompt_version', 'status', 'date_from', 'date_to'];
  const scoreLabels = { compliance: '指令遵循', recognizability: '辨识度', motion: '运动表现', visual_detail: '视觉细节', interaction: '交互表现', completeness: '完整性' };
  const statusNames = { queued: '排队中', running: '运行中', generated: '待评估', completed: '已完成', failed: '失败', interrupted: '已中断', unavailable: '不可用', blocked: '被阻止' };
  const metricDefs = { cost_usd: ['已知费用', 'USD'], duration_ms: ['生成会话耗时', '秒'], total_tokens: ['总 Token', 'token'], evaluation_duration_ms: ['评估耗时', '秒'], api_duration_ms: ['API 耗时', '秒'], input_tokens: ['输入 Token', 'token'], output_tokens: ['输出 Token', 'token'], source_bytes: ['源码体积（辅助）', 'byte'], source_file_count: ['源码文件数（辅助）', 'file'] };
  const stages = [
    { id: 'desktop', name: '桌面首张', file: 'desktop.png' },
    { id: 'later', name: '后续采样', file: 'frame-2.png' },
    { id: 'final', name: '末次采样', file: 'frame-8.png' },
    { id: 'mobile', name: '移动截图', file: 'mobile.png' },
    { id: 'video', name: '桌面录像', file: 'animation.webm' }
  ];
  const narrowScreen = matchMedia('(max-width: 600px)');
  const previewPanels = new Set();
  let compareRequest = 0;
  let compareMode = 'media';
  let comparePair = [];
  const state = { runs: [], tasks: [], options: {}, view: 'gallery', filters: {}, selected: new Set(), selectionRuns: new Map(), compareLimit: 2, mixed: false, compareRuns: [], compareStage: 'desktop', compareIndex: 0, galleryStage: 'desktop', defaultTaskPending: false, authenticated: false, loaded: false, request: 0, detailRequest: 0, controller: null, trendMetric: 'duration_ms', trendPage: 0, blindQueue: [], blindIndex: 0, blindSignature: '', texture: false, loading: false };
  const staticMedia = snapshot && (source.format === 'static-media-v1' || (source.format === 'static-media-v2' && source.transport === 'external'));
  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const valueOf = (run, key) => key === 'duration_ms' ? run.duration_ms : key === 'evaluation_duration_ms' ? run.evaluation?.duration_ms : run.metrics?.[key];
  const text = value => value == null || value === '' ? 'unknown' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const number = value => finite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : 'unknown';
  const durationText = value => { const seconds = value / 1000; return seconds < 60 ? `${number(seconds)} 秒` : `${Math.floor(seconds / 60)} 分 ${number(seconds % 60)} 秒`; };
  const metricFormat = (value, key) => !finite(value) ? 'unknown' : key.includes('duration') ? durationText(value) : key === 'cost_usd' ? `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}` : number(value);
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
    if (!state.filters.task_id && !state.defaultTaskPending) url.searchParams.set('task_id', '');
    url.hash = state.view;
    try { history.replaceState(null, '', url); } catch (error) { notify('当前查看器无法保存筛选 URL；筛选仍然有效。'); }
  }
  function searchText(run) {
    if (searchTexts?.has(run)) return searchTexts.get(run);
    const value = JSON.stringify({ ...run, reviews: undefined }).toLocaleLowerCase();
    searchTexts?.set(run, value);
    return value;
  }
  function localFilter(runs) {
    const q = (state.filters.q || '').trim().toLocaleLowerCase();
    return runs.filter(run => {
      for (const key of ['tool', 'model', 'task_id', 'purpose', 'prompt_version', 'status']) if (state.filters[key] && String(run[key] ?? '') !== state.filters[key]) return false;
      const date = dateOf(run);
      if (state.filters.date_from && (!date || date < state.filters.date_from)) return false;
      if (state.filters.date_to && (!date || date > state.filters.date_to)) return false;
      if (q && !searchText(run).includes(q)) return false;
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
    cancelPreviewContext();
    ++state.request; if (state.controller) state.controller.abort(); setLoading(false);
    state.authenticated = false; state.loaded = false; state.runs = []; state.options = {}; state.tasks = []; state.selected.clear(); state.selectionRuns.clear(); state.compareRuns = []; state.blindQueue = []; state.blindSignature = '';
    ['detail-dialog', 'compare-dialog', 'zoom-dialog'].forEach(id => { releaseMedia($(`#${id}`)); $(`#${id}`).close(); });
    $('#detail-content').replaceChildren(); $('#compare-content').replaceChildren(); $('#zoom-content').replaceChildren();
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
      const signature = JSON.stringify([...entries]);
      if (select.filterOptionsSignature !== signature) {
        select.replaceChildren(el('option', { value: '', text: names[key] }), ...[...entries].map(([value, name]) => el('option', { value, text: name })));
        select.filterOptionsSignature = signature;
      }
      select.value = state.filters[key] || '';
    }
    for (const key of ['q', 'date_from', 'date_to']) $(`[name="${key}"]`, $('#filters')).value = state.filters[key] || '';
  }
  function inferOptions(runs) {
    const unique = key => [...new Set(runs.map(run => run[key]).filter(value => value != null))].sort();
    return { tools: unique('tool'), models: unique('model'), tasks: state.tasks.length ? state.tasks : [...new Map(runs.map(run => [run.task_id, { id: run.task_id, name: run.task_name || run.task_id }])).values()], prompt_versions: unique('prompt_version'), purposes: unique('purpose'), statuses: unique('status'), dates: unique('date') };
  }
  async function loadData(withOptions = false) {
    cancelPreviewContext();
    const request = ++state.request;
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    setLoading(true);
    try {
      if (snapshot) {
        if (!snapshotOptionsReady) {
          state.tasks = Array.isArray(source.tasks) ? source.tasks : [];
          state.options = { ...inferOptions(source.runs || []), ...(source.options || {}) };
          snapshotOptionsReady = true;
        }
        applyDefaultTask();
        state.runs = localFilter(Array.isArray(source.runs) ? source.runs : []);
        fillOptions();
      } else {
        if (withOptions) {
          const [options, tasks] = await Promise.all([json('/api/options', { signal: state.controller.signal }), json('/api/tasks', { signal: state.controller.signal })]);
          if (request !== state.request) return;
          state.options = options; state.tasks = tasks.tasks || []; applyDefaultTask(); fillOptions();
        }
        const data = await json(`/api/runs?${query()}`, { signal: state.controller.signal });
        if (request !== state.request) return;
        state.runs = Array.isArray(data.runs) ? data.runs : [];
        state.authenticated = true;
      }
      state.loaded = true;
      for (const run of state.runs) if (state.selected.has(run.id)) state.selectionRuns.set(run.id, run);
      state.trendPage = 0;
      $('#updated-at').textContent = snapshot ? '只读快照 · 选择仅限本页会话' : `更新于 ${new Date().toLocaleTimeString('zh-CN')}`;
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
  function taskOptions() {
    const entries = new Map();
    for (const item of [...(state.options.tasks || []), ...state.tasks]) { const task = typeof item === 'string' ? { id: item, name: item } : item; if (task?.id) entries.set(task.id, task); }
    for (const run of state.runs) if (run.task_id && !entries.has(run.task_id)) entries.set(run.task_id, { id: run.task_id, name: run.task_name || run.task_id });
    return [...entries.values()].sort((a, b) => (a.id === 'crocodile' ? -1 : b.id === 'crocodile' ? 1 : a.id.localeCompare(b.id)));
  }
  function applyDefaultTask() {
    if (!state.defaultTaskPending) return;
    state.filters.task_id = taskOptions()[0]?.id || '';
    state.defaultTaskPending = false; syncURL();
  }
  function chooseTask(id) {
    clearTimeout(filterTimer); state.filters.task_id = id; state.defaultTaskPending = false;
    fillOptions(); syncURL(); loadData();
  }
  function renderTaskContext() {
    const tasks = taskOptions(), selected = tasks.find(task => task.id === state.filters.task_id);
    $('#task-tabs').replaceChildren(...tasks.map(task => el('button', { type: 'button', 'data-task-id': task.id, class: task.id === state.filters.task_id ? 'active' : '', 'aria-pressed': String(task.id === state.filters.task_id), text: task.name || task.id, onclick: () => chooseTask(task.id) })), el('button', { type: 'button', 'data-task-id': '', 'aria-pressed': String(!state.filters.task_id), text: '全部任务', onclick: () => chooseTask('') }));
    const prompt = selected?.original_prompt || selected?.prompt;
    const versions = [...new Set(state.runs.map(run => text(run.prompt_version)))];
    const requirements = selected ? [el('p', { class: 'task-brief', text: prompt || '该任务未提供要求原文；请在运行详情查阅归档 Prompt。' }), el('details', { class: 'task-requirements' }, [el('summary', { text: `任务要求与 Prompt · 定义版本 ${text(selected.version)} · 当前运行版本 ${versions.join(' / ') || '无记录'}` }), el('p', { class: 'footnote', text: `以下为任务定义，不替代每次运行归档的 Prompt/hash。Rubric ${text(selected.rubric_version)}` }), el('pre', { text: text(selected.prompt || selected.original_prompt) }), ...(selected.dimensions ? [metadata(Object.entries(selected.dimensions).map(([key, value]) => [scoreLabels[key] || key, value]))] : [])])] : [el('p', { class: 'task-brief', text: '当前展示全部任务。比较始终限定同一任务；选择任务即可查看其要求。' })];
    $('#task-context').replaceChildren(...requirements);
  }
  function generationStatus(run) { return run.generation_status || run.status; }
  function entrypointStatus(run) {
    if (run.evaluation?.status && run.evaluation.status !== 'completed') return 'unknown';
    const check = (run.checks || []).find(item => item.name === 'entrypoint') || (run.evaluation?.checks || []).find(item => item.name === 'entrypoint');
    return ['pass', 'passed', 'ok'].includes(check?.status) ? 'pass' : ['fail', 'failed'].includes(check?.status) ? 'fail' : 'unknown';
  }
  function includedPaths(run) {
    if (!snapshot) return [];
    if (!includedLists.has(run)) includedLists.set(run, evidencePaths(run).filter(path => mediaURI(run, path)));
    return includedLists.get(run);
  }
  function reviewStatus(run) { const reviews = run.reviews || [], pending = reviews.filter(review => !review.blind && /AI.*建议/i.test(review.reviewer || '')).length; return reviews.length ? `${reviews.length} 条已有记录${pending ? ` · ${pending} 条非盲 AI 建议待人工复核` : ''}` : '未评'; }
  function attemptText(run) {
    if (run.retry_of) return `显式重试 · attempt ${text(run.attempt)} · retry_of ${run.retry_of}`;
    if (run.attempt === 0) return '首次 · attempt 0（本运行链）';
    return `独立记录 · attempt ${text(run.attempt)} · 未声明 retry_of`;
  }
  function renderSummary() {
    const runs = state.runs, total = runs.length;
    const models = new Set(runs.map(run => run.model)).size;
    const combinations = new Set(runs.map(run => JSON.stringify([run.model, run.task_id]))).size;
    const registered = runs.filter(run => evidencePaths(run).length).length;
    const included = runs.filter(run => includedPaths(run).length).length;
    $('#summary').replaceChildren(
      stat('具体尝试', String(total).padStart(2, '0'), '首次、重复记录与显式重试均保留'),
      stat('模型 × 任务组合', String(combinations).padStart(2, '0'), `${models} 个模型 · 重复尝试不增加覆盖`),
      stat('证据登记 / 尝试', `${registered} / ${total}`, '登记不等于验收或浏览器已加载'),
      stat(snapshot ? '公开包含证据 / 尝试' : '本地证据', snapshot ? `${included} / ${total}` : '按需读取', snapshot ? '安全媒体映射可用；加载结果在图下显示' : 'API 需认证；未预检全部附件')
    );
    $('#summary-explanation p').textContent = `当前筛选 ${total} 次尝试、${models} 个模型、${combinations} 个模型 × 任务组合。首次、重复记录与显式重试均保留；重复尝试不增加组合覆盖。证据登记 ${registered} 次${snapshot ? `，公开包含 ${included} 次` : '；本地 API 按需读取，未预检全部附件'}。登记、包含、浏览器已加载是不同事实，均不代表验收或视觉质量。`;
  }
  function empty(title = '当前条件下，没有运行。', detail = '调整上方筛选，或完成一次真实实验后刷新。') {
    return el('div', { class: 'empty' }, [el('div', { class: 'empty-symbol', text: '∅', 'aria-hidden': 'true' }), el('h3', { text: title }), el('p', { text: detail })]);
  }
  function render() {
    hideTooltip(); renderSummary(); renderTaskContext(); updateSelection();
    const blind = state.view === 'blind';
    $('#summary').hidden = blind; $('#summary-explanation').hidden = blind; $('#filters').hidden = blind; $('#task-workspace').hidden = blind;
    document.body.classList.toggle('snapshot', snapshot);
    $('#auth-button').hidden = snapshot; $('#refresh-button').hidden = snapshot;
    $$('[data-export]').forEach(button => { button.hidden = blind || snapshot; });
    $('#texture-toggle').hidden = !['batch', 'trend'].includes(state.view);
    $('#mode-label').textContent = snapshot ? '脱敏快照 · 只读' : blind ? '匿名审阅 · 信息遮蔽' : '真实运行 · 可追溯';
    $('#result-label').textContent = blind ? `匿名队列 · ${state.runs.length} 个候选运行 · 已隐藏模型 / 日期 / 费用及筛选内容` : `${snapshot ? '快照' : '当前筛选'} / ${state.runs.length} 运行 · ${new Set(state.runs.map(run => run.batch_id).filter(Boolean)).size} 批次`;
    const root = $('#view-content'); releaseMedia(root); root.replaceChildren();
    if (!state.loaded) { root.append(empty('实验数据尚未连接。', snapshot ? '快照无可用数据。' : '登录后加载真实运行；此处不会填入演示数据。')); return; }
    if (blind) { renderBlind(root); return; }
    if (state.view === 'matrix') { renderMatrix(root); return; }
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
      const items = all, width = 420, height = items.length * 38 + 32;
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
        attachTooltip(group, `${metricFormat(value, key)}${key.includes('duration') ? ` (${number(value)} ms)` : ''}\n${label}\n${text(run.id)} · ${dateOf(run) || 'unknown'}\n${text(run.tool)} · ${text(run.prompt_version)} · ${text(run.purpose)}\n${key === 'cost_usd' ? text(run.metrics?.cost_source) : key === 'duration_ms' ? 'manifest.duration_ms · 生成会话' : key === 'evaluation_duration_ms' ? 'evaluation.duration_ms' : text(run.metrics_source || run.metrics?.source || 'manifest.metrics')}`); svg.append(group);
        svg.append(svgEl('text', { x: 414, y: y + 13, 'text-anchor': 'end', class: 'value-label' }, key.includes('duration') ? number(value / 1000) : key === 'cost_usd' ? `$${value.toFixed(4)}` : number(value)));
      });
      panel.append(el('div', { class: 'chart-scroll' }, svg));
    }
    panel.append(tableDisclosure(['运行 / 日期', '工具 / 模型 / 条件', `${title} · ${unit}`, '来源'], all.map(({ run, value }) => [el('div', {}, [detailButton(run), el('small', { text: dateOf(run) })]), `${text(run.tool)} / ${text(run.model)} / ${text(run.task_id)} / ${text(run.prompt_version)} / ${text(run.purpose)} / ${effortText(run)}`, `${metricFormat(value, key)}${key.includes('duration') ? ` (${number(value)} ms)` : ''}`, key === 'cost_usd' ? text(run.metrics?.cost_source) : key === 'duration_ms' ? 'manifest.duration_ms' : key === 'evaluation_duration_ms' ? 'evaluation.duration_ms' : text(run.metrics_source || run.metrics?.source || 'manifest.metrics')])));
    panel.append(el('p', { class: 'footnote', text: `图与表均包含全部 ${all.length} 个已知值；${state.runs.length - all.length} 个 unknown 不绘为 0。不作为质量排名。` }));
    return panel;
  }
  function renderBatch(root) {
    const costs = state.runs.map(run => valueOf(run, 'cost_usd')).filter(finite);
    const failedCosts = state.runs.filter(run => generationStatus(run) === 'failed').map(run => valueOf(run, 'cost_usd')).filter(finite);
    root.append(el('div', { class: 'section-heading' }, [el('div', {}, [el('h2', { text: '费用、时间，各自衡量。' }), el('p', { text: `已知费用小计 ${costs.length ? metricFormat(costs.reduce((sum, value) => sum + value, 0), 'cost_usd') : 'unknown'} · ${costs.length}/${state.runs.length} 已知（${coverage(costs.length, state.runs.length)}），包含 ${failedCosts.length} 次生成失败尝试的已知费用。报告值不等于账单。` })]), el('button', { text: '同条件日期趋势', onclick: () => setView('trend') })]), el('p', { class: 'blind-note', text: '生成会话耗时不包含完整评估流水线；评估耗时仅显示 evaluation.duration_ms，缺失不推算。首 CLI 事件不是供应商 TTFT；源码体积不是质量分。各图展示全部已知运行，不跨条件合并排名。' }), el('div', { class: 'charts efficiency-charts' }, ['cost_usd', 'duration_ms', 'total_tokens', 'evaluation_duration_ms'].map(barPanel)), el('div', { class: 'section-heading' }, [el('h2', { text: '计量对应运行' }), el('span', { class: 'chip', text: '费用 / 时间 / Token 独立单位' })]), runTable());
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
  function runTable(runs = state.runs) {
    const rows = runs.map(run => {
      const checkbox = el('input', { type: 'checkbox', 'data-select-run': run.id, 'aria-label': `选择运行 ${run.id}`, checked: state.selected.has(run.id), onchange: () => toggleSelection(run) });
      return [checkbox, el('td', { class: 'run-name' }, [detailButton(run), el('small', { text: attemptText(run) }), el('small', { text: `批次 ${text(run.batch_id)}` })]), dateOf(run) || 'unknown', text(run.tool), text(run.model), text(run.task_name || run.task_id), text(run.prompt_version), statusChip(generationStatus(run)), entrypointStatus(run), text(run.evaluation?.status), checksText(run), snapshot ? `${includedPaths(run).length} / ${evidencePaths(run).length}` : `登记 ${evidencePaths(run).length} · 按需读取`, ...['cost_usd', 'duration_ms', 'evaluation_duration_ms', 'total_tokens'].map(key => el('td', { class: 'numeric', text: metricFormat(valueOf(run, key), key) }))];
    });
    return table(['选择', '运行 / 尝试 / 批次', '日期', '工具', '模型', '任务', 'Prompt', '生成会话', '入口检查', '评估状态', 'Checks', snapshot ? '公开包含 / 登记' : '证据', '费用 USD', '生成会话耗时', '评估耗时', '总 Token'], rows, 'run-table');
  }
  function selectionLimit() { return narrowScreen.matches ? 2 : state.compareLimit; }
  function selectedRuns() { return [...state.selected].map(id => state.selectionRuns.get(id)).filter(Boolean); }
  function selectButton(run) { return el('button', { type: 'button', 'data-select-run': run.id, 'aria-pressed': String(state.selected.has(run.id)), text: state.selected.has(run.id) ? '移出对比' : '加入对比', onclick: () => toggleSelection(run) }); }
  const conditionFields = [
    ['用途 Purpose', run => run.purpose], ['Prompt 版本', run => run.prompt_version], ['Prompt hash', run => run.prompt_hash || run.prompt?.sha256 || run.prompt?.hash || run.conditions?.prompt_hash],
    ['工具', run => run.tool], ['Effort 已应用', run => run.conditions?.reasoning_effort_applied], ['Effort 请求', run => run.conditions?.reasoning_effort_requested], ['Effort control', run => run.conditions?.reasoning_effort_control],
    ['Rubric', run => run.conditions?.rubric_version], ['CLI 版本', run => run.conditions?.cli_version], ['镜像', run => run.conditions?.image_id], ['渲染器', run => run.conditions?.renderer], ['运行 profile', run => run.conditions?.profile], ['隔离策略', run => run.conditions?.isolation],
    ['Runner hash', run => run.conditions?.runner_sha256], ['Adapter hash', run => run.conditions?.adapter_sha256], ['任务定义 hash', run => run.conditions?.task_definition_sha256], ['运行策略 hash', run => run.conditions?.runtime_policy_sha256], ['出口策略 hash', run => run.conditions?.egress_policy_sha256]
  ];
  function conditionDifferences(runs) {
    return conditionFields.map(([name, get]) => { const values = runs.map(get); const unknown = values.some(value => value == null || value === '' || value === 'unknown'); return { name, values, status: unknown ? 'unknown' : new Set(values.map(text)).size > 1 ? '不同' : '一致' }; });
  }
  function comparisonIssue(runs) {
    if (runs.length < 2) return '';
    if (runs.some(run => !run.task_id) || new Set(runs.map(run => run.task_id)).size !== 1) return '只能比较同一 task_id；请先移出其他任务的选择。跨条件查看也不能跨任务。';
    const differences = conditionDifferences(runs).filter(row => row.status !== '一致');
    if (!state.mixed && differences.length) return `条件不同或 unknown：${differences.map(row => row.name).join('、')}。请主动启用“跨条件查看”后再加入；未知不当作一致。`;
    return '';
  }
  function toggleSelection(run) {
    if (state.selected.has(run.id)) { state.selected.delete(run.id); state.selectionRuns.delete(run.id); }
    else {
      const issue = state.selected.size >= selectionLimit() ? `当前最多选择 ${selectionLimit()} 次。桌面可主动改为 3–4 次；手机最多 2 次。` : comparisonIssue([...selectedRuns(), run]);
      if (issue) { notify(issue, true); updateSelection(); $('#selection-warning').textContent = issue; return; }
      state.selected.add(run.id); state.selectionRuns.set(run.id, run); notify('');
    }
    updateSelection();
  }
  function updateSelection() {
    const runs = selectedRuns(), blind = state.view === 'blind', limit = selectionLimit();
    $('#comparison-tray').hidden = blind || !state.selected.size;
    document.body.classList.toggle('has-selection', !blind && !!state.selected.size);
    $('#selection-label').textContent = `已选择 ${state.selected.size} / ${limit} 次运行`;
    const outside = runs.filter(run => !state.runs.some(item => item.id === run.id)).length;
    const issue = comparisonIssue(runs);
    const overflow = state.selected.size > limit ? `现有 ${state.selected.size} 次选择全部保留；当前屏幕最多 ${limit} 次，请明确移出不保留的运行。` : '';
    $('#selection-warning').textContent = [outside ? `${outside} 次选择不在当前筛选 / 任务中，已保留。新任务请清空后重新选择。` : '', overflow, issue, state.mixed ? '跨条件查看已启用：不同 / 未知条件持续提示，不生成合并排名。' : '默认同题、同用途、同 Prompt；其他条件不同或未知需主动确认。'].filter(Boolean).join(' ');
    $('#compare-button').disabled = state.selected.size < 2 || !!overflow || !!issue;
    $('#selection-runs').replaceChildren(...runs.map(run => el('button', { class: 'selected-run', title: `移出 ${run.id}`, 'aria-label': `移出运行 ${run.id}`, text: `${text(run.model)} · ${text(run.id).slice(0, 8)} ×`, onclick: () => toggleSelection(run) })));
    $$('[data-select-run]').forEach(button => { const selected = state.selected.has(button.dataset.selectRun); if (button.tagName === 'INPUT') button.checked = selected; else { button.setAttribute('aria-pressed', String(selected)); button.textContent = selected ? '移出对比' : '加入对比'; } });
    $$('#compare-limit option').forEach(option => { option.disabled = Number(option.value) > (narrowScreen.matches ? 2 : 4); });
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
  function renderMatrix(root) {
    root.append(el('div', { class: 'section-heading' }, [el('div', {}, [el('h2', { text: '尝试，不等于覆盖。' }), el('p', { text: '按工具 / 模型 × 任务记录事实。每次尝试单独计数；没有记录仅指当前筛选，不推断从未测试。' })]), el('button', { text: '查看两任务（清除任务筛选）', onclick: () => chooseTask('') })]));
    root.append(el('p', { class: 'blind-note', text: '会话正常仅指 generation_status（旧记录使用 status）为 completed；入口 pass/fail 仅来自明确 entrypoint 检查，评估未完成或基础设施故障保留 unknown。评估 completed、检查通过和已包含证据均不等于视觉质量。附件登记、证据登记、公开包含与浏览器已加载是四件事。' }));
    const tasks = taskOptions(), groups = new Map();
    for (const run of state.runs) { const key = JSON.stringify([run.tool, run.model]); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(run); }
    if (!groups.size) { root.append(empty()); return; }
    const rows = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, group]) => [el('div', { class: 'matrix-model' }, [el('strong', { text: text(group[0].model) }), el('small', { text: text(group[0].tool) })]), ...tasks.map(task => {
      const runs = group.filter(run => run.task_id === task.id);
      if (!runs.length) return el('td', { class: 'matrix-cell' }, el('p', { class: 'muted', text: '当前筛选无记录' }));
      const entries = runs.map(entrypointStatus), evaluations = runs.map(run => text(run.evaluation?.status));
      const cell = el('td', { class: 'matrix-cell', 'data-task-cell': task.id }, [metadata([
        ['尝试次数', runs.length], ['会话正常次数', runs.filter(run => generationStatus(run) === 'completed').length],
        ['入口 pass / fail / unknown', ['pass', 'fail', 'unknown'].map(status => entries.filter(value => value === status).length).join(' / ')],
        ['评估 completed / 其他 / unknown', [evaluations.filter(value => value === 'completed').length, evaluations.filter(value => value !== 'completed' && value !== 'unknown').length, evaluations.filter(value => value === 'unknown').length].join(' / ')],
        ['有附件登记 / 有证据登记', `${runs.filter(run => run.artifacts?.length).length} / ${runs.filter(run => evidencePaths(run).length).length}`],
        [snapshot ? '公开包含证据次数' : '本地安全证据', snapshot ? runs.filter(run => includedPaths(run).length).length : '按需读取 · 非公开包']
      ])]);
      const disclosure = el('details', { class: 'matrix-attempts' }, [el('summary', { text: `查看 ${runs.length} 次具体运行 / 加入对比` }), ...stableRuns(runs).map(run => el('div', { class: 'matrix-run', 'data-run-id': run.id }, [detailButton(run), el('p', { class: 'footnote', text: `${dateOf(run)} · ${attemptText(run)} · 会话 ${text(generationStatus(run))} · 入口 ${entrypointStatus(run)} · ${checksText(run)}` }), selectButton(run)]))]);
      cell.append(disclosure); return cell;
    })]);
    root.append(table(['工具 / 模型', ...tasks.map(task => task.name || task.id)], rows, 'result-matrix'));
  }
  function stableRuns(runs) { return [...runs].sort((a, b) => String(a.started_at || a.date || '').localeCompare(String(b.started_at || b.date || '')) || String(a.id).localeCompare(String(b.id))); }
  function evidencePaths(run) {
    if (evidenceLists?.has(run)) return evidenceLists.get(run);
    const list = Array.isArray(run.evaluation?.evidence) ? run.evaluation.evidence : [];
    const paths = [...new Set(list.map(item => typeof item === 'string' ? item : item?.path).filter(path => safePath(path) && /\.(png|jpe?g|webp|gif|webm|mp4)$/i.test(path)))];
    evidenceLists?.set(run, paths);
    return paths;
  }
  function staticAsset(uri, video = false) {
    if (typeof uri !== 'string') return null;
    const match = /^media\/([a-f0-9]{64})\.(jpg|webm)$/.exec(uri);
    if (!match || !Object.prototype.hasOwnProperty.call(source.assets || {}, uri)) return null;
    const asset = source.assets[uri], mime = video ? 'video/webm' : 'image/jpeg';
    if (!asset || asset.sha256 !== match[1] || asset.mime !== mime || match[2] !== (video ? 'webm' : 'jpg') || !Number.isSafeInteger(asset.size) || asset.size <= 0) return null;
    return asset;
  }
  function mediaURI(run, path, thumbnail = false) {
    if (!snapshot) return resolveMediaURI(run, path, thumbnail);
    let uris = mediaURIs.get(run);
    if (!uris) { uris = new Map(); mediaURIs.set(run, uris); }
    const key = JSON.stringify([path, thumbnail]);
    if (!uris.has(key)) uris.set(key, resolveMediaURI(run, path, thumbnail));
    return uris.get(key);
  }
  function resolveMediaURI(run, path, thumbnail = false) {
    if (!safePath(path) || !evidencePaths(run).includes(path)) return null;
    const video = /\.(webm|mp4)$/i.test(path);
    if (!snapshot) return fileURL(run, path);
    const key = `${run.id}/${path}`;
    const mapping = staticMedia && thumbnail && !video ? source.thumbnails : source.evidence;
    if (!Object.prototype.hasOwnProperty.call(mapping || {}, key)) return null;
    const uri = mapping[key];
    if (staticMedia) {
      const asset = staticAsset(uri, video);
      if (!asset || (thumbnail && finite(asset.width) && asset.width > 480)) return null;
      return uri;
    }
    if (source.format != null && !(source.format === 'static-media-v2' && source.transport === 'inline')) return null;
    // Legacy offline data URIs only. An arbitrary relative URL never becomes an evidence URL.
    return typeof uri === 'string' && (video ? /^data:video\/(?:webm|mp4);base64,[A-Za-z0-9+/]+={0,2}$/i : /^data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/i).test(uri) ? uri : null;
  }
  const thumbnailObserver = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting && entry.target.isConnected) {
      const image = entry.target; thumbnailObserver.unobserve(image);
      if (image.evidenceURI) { image.src = image.evidenceURI; image.closest('.media-evidence').dataset.mediaState = 'loading'; }
    }
  }, { rootMargin: '80px 0px', threshold: 0.01 }) : null;
  function releaseMedia(root) {
    for (const panel of [...previewPanels]) if (root.contains(panel.node)) panel.destroy();
    $$('img', root).forEach(image => thumbnailObserver?.unobserve(image));
    $$('video', root).forEach(video => { video.pause(); video.removeAttribute('src'); $$('source', video).forEach(item => item.remove()); video.load(); });
  }
  const originalStatusNames = { ready: '已封装 · 尚未运行', missing_dependencies: '缺少依赖 · 可查看残缺交付', no_entrypoint: '未交付入口', not_reviewed: '尚未完成公开审核', withheld: '原作未公开', unsupported: '装载方式不支持' };
  function originalDescriptor(run) { return source?.format === 'static-media-v2' ? source.originals?.[run.id] : null; }
  function originalAvailable(run) { const descriptor = originalDescriptor(run); return !!window.BenchPreview && !!descriptor?.package && ['ready', 'missing_dependencies'].includes(descriptor.status); }
  function originalGuidance(run) {
    if (!snapshot) return '本地认证看板不执行原作。请导出 / 构建包含原作的 v2 报告后查看；附件下载保持可用。';
    if (source.format !== 'static-media-v2') return '此旧版报告不含实时原作运行器。请使用包含原作的 v2 导出报告；现有截图和录像仍可查看。';
    const descriptor = originalDescriptor(run);
    if (!descriptor) return '此运行未提供原作描述符；请检查 v2 导出报告。';
    if (!window.BenchPreview) return '此报告未包含原作运行器；请重新构建完整的 v2 报告。';
    if (['ready', 'missing_dependencies'].includes(descriptor.status) && !descriptor.package) return '原作封装缺失：此描述符未包含可运行包，请重新构建 v2 报告。';
    return `${originalStatusNames[descriptor.status] || '原作描述符状态未知'}${descriptor.missing?.length ? ` · 缺失：${descriptor.missing.map(text).join('、')}` : ''}`;
  }
  function originalButton(run) {
    return el('button', { type: 'button', class: 'run-original primary', 'data-run-original': run.id, text: originalAvailable(run) ? '运行原作' : '原作说明', title: originalGuidance(run), onclick: () => openOriginal(run) });
  }
  function stopPreviews(except = null) { for (const panel of previewPanels) if (panel !== except) panel.stop(); }
  function cancelPreviewContext() {
    stopPreviews(); ++state.detailRequest; ++compareRequest;
    for (const id of ['original-dialog', 'detail-dialog', 'compare-dialog', 'zoom-dialog']) { const dialog = $(`#${id}`); if (dialog.open) { releaseMedia(dialog); dialog.close(); } }
  }
  function previewPanel(run, { comparison = false, canRunTogether = () => false } = {}) {
    const descriptor = originalDescriptor(run);
    const status = el('p', { class: 'preview-state', role: 'status', text: originalGuidance(run) });
    const dimensions = el('p', { class: 'preview-dimensions', text: '视口尚未创建 · 不请求原作包' });
    const diagnostics = el('ul', { class: 'preview-diagnostics', 'aria-label': '原作诊断（非验收）' });
    const host = el('div', { class: 'preview-host', 'data-preview-host': run.id });
    let runtime = null, generation = 0, destroyed = false, viewport = 'fit';
    const start = el('button', { type: 'button', 'data-preview-start': run.id, class: 'primary', text: '启动原作', disabled: !originalAvailable(run), onclick: () => panel.start() });
    const stop = el('button', { type: 'button', 'data-preview-stop': run.id, text: '停止', disabled: true, onclick: () => panel.stop() });
    const restart = el('button', { type: 'button', 'data-preview-restart': run.id, text: '重新启动', disabled: true, onclick: () => panel.start() });
    const selector = el('select', { 'aria-label': `原作视口 ${run.id}`, 'data-preview-viewport': run.id, onchange: event => { viewport = event.target.value === 'fit' ? 'fit' : event.target.value === 'desktop' ? { width: 1440, height: 900 } : { width: 400, height: 800 }; runtime?.setViewport(viewport); } }, [el('option', { value: 'fit', text: '适应窗口' }), el('option', { value: 'desktop', text: '1440 × 900' }), el('option', { value: 'mobile', text: '400 × 800' })]);
    const node = el('section', { class: 'original-preview', 'data-original-run': run.id, 'data-preview-state': 'idle' }, [
      el('div', { class: 'preview-controls' }, [start, stop, restart, el('label', {}, ['渲染视口', selector]), ...(comparison ? [el('button', { type: 'button', 'data-original-fullwidth': run.id, text: '单作全宽', onclick: () => openOriginal(run) })] : [])]),
      status, el('p', { class: 'preview-guidance footnote', text: originalGuidance(run) }), dimensions, host,
      el('p', { class: 'preview-provenance mono', text: `run ${run.id} · 入口 ${descriptor?.entrypoint || 'unknown'} · SHA256 ${descriptor?.entry_sha256 || 'unknown'}` }),
      el('p', { class: 'footnote', text: `生成会话：${text(generationStatus(run))} · 入口检查：${entrypointStatus(run)} · 评估：${text(run.evaluation?.status)}。实时装载不改变历史结论。` }),
      el('p', { class: 'footnote', text: '隔离与资源装载适配；交互由原作执行，无通用暂停或动画同步。当前浏览器 / GPU / DPR / 动效偏好可能不同于历史评估。浏览器沙箱不等同于 CPU / 内存硬配额或绝对零外联。' }), diagnostics
    ]);
    const panel = { node,
      async start(allowPair = false) {
        if (destroyed || !node.isConnected || node.closest('[hidden]') || !node.closest('dialog[open]') || !originalAvailable(run) || document.hidden) return;
        if (!(allowPair || canRunTogether()) || narrowScreen.matches) stopPreviews(panel);
        runtime?.destroy(); runtime = null; const token = ++generation; diagnostics.replaceChildren();
        start.disabled = true; stop.disabled = false; restart.disabled = false;
        const current = () => !destroyed && token === generation && node.isConnected;
        try {
          runtime = window.BenchPreview.create(host, { runId: run.id, descriptor, transport: source.transport, viewport,
            onState: value => {
              if (!current()) return;
              node.dataset.previewState = value.status; status.textContent = value.message || value.status;
              if (value.status === 'loaded') status.textContent += ' · 已装载不代表验收通过';
              if (Number.isFinite(value.width) && Number.isFinite(value.height) && Number.isFinite(value.scale)) dimensions.textContent = `实际渲染视口 ${value.width} × ${value.height} CSS px · 显示缩放 ${Math.round(value.scale * 100)}%`;
              if (['error', 'stopped', 'unavailable'].includes(value.status)) { start.disabled = false; stop.disabled = true; }
            },
            onDiagnostic: value => { if (current() && diagnostics.children.length < 20) diagnostics.append(el('li', { text: `原作诊断（非验收）：${`${value?.kind || 'error'} · ${text(value?.message ?? value)}`.slice(0, 800)}` })); }
          });
          await runtime.start();
        } catch (error) {
          if (current()) { runtime?.destroy(); runtime = null; node.dataset.previewState = 'error'; status.textContent = `预览启动错误：${error.message}`; start.disabled = false; stop.disabled = true; }
        }
      },
      stop() {
        ++generation; runtime?.destroy(); runtime = null; host.replaceChildren();
        if (node.dataset.previewState !== 'idle') { node.dataset.previewState = 'stopped'; status.textContent = '已停止 · 沙箱和待处理启动已销毁；不会自动恢复'; }
        start.disabled = !originalAvailable(run); stop.disabled = true; restart.disabled = !originalAvailable(run);
      },
      destroy() { panel.stop(); destroyed = true; previewPanels.delete(panel); }
    };
    if (!originalAvailable(run)) { $('.preview-controls', node).hidden = true; dimensions.hidden = true; host.hidden = true; }
    previewPanels.add(panel); return panel;
  }
  function openOriginal(run) {
    cancelPreviewContext();
    const dialog = $('#original-dialog'), content = $('#original-content'); releaseMedia(content);
    $('#original-title').textContent = `${text(run.model)} · 原作实时预览`;
    const panel = previewPanel(run); content.replaceChildren(panel.node); dialog.showModal(); dialog.scrollTop = 0;
    if (originalAvailable(run)) panel.start();
  }
  function stagePath(run, stage) { const file = stages.find(item => item.id === stage)?.file; return file ? evidencePaths(run).find(path => path.split('/').at(-1) === file) : null; }
  function stageSelector(value, onChange, attrs = {}) {
    const select = el('select', { 'aria-label': '证据采样阶段', ...attrs, onchange: event => onChange(event.target.value) }, stages.map(stage => el('option', { value: stage.id, text: stage.name })));
    select.value = value; return select;
  }
  function media(run, path, anonymous = false, options = {}) {
    const video = /\.(webm|mp4)$/i.test(path || ''), thumbnail = !options.full && !video;
    const frame = el('div', { class: `media-frame${path?.split('/').at(-1) === 'mobile.png' ? ' portrait' : ''}` });
    const status = el('figcaption', { class: 'media-load-state', text: path ? video ? '未加载录像 · 点击播放才读取' : thumbnail ? '缩略图 · 接近视口才加载' : '全尺寸图 · 正在加载' : '此阶段未登记证据；不替换为其他阶段', role: 'status' });
    const figure = el('figure', { class: 'media-evidence', 'data-media-state': path ? 'idle' : 'missing', 'data-media-kind': video ? 'video' : thumbnail ? 'thumbnail' : 'full' }, [frame, status]);
    const uri = path ? mediaURI(run, path, thumbnail) : null;
    if (!uri) {
      figure.dataset.mediaState = 'missing';
      frame.append(el('div', { class: 'stage-missing' }, [el('span', { class: 'missing-mark', text: '—', 'aria-hidden': 'true' }), el('p', { text: path ? thumbnail && snapshot && staticMedia ? '未提供安全缩略图' : '未包含可安全预览的证据' : '此阶段暂无证据' })]));
      if (path) status.textContent = thumbnail && mediaURI(run, path) ? '可通过“放大”主动读取全尺寸图；不自动回退。' : '未包含 / 映射无效；登记不代表可读取。';
      return figure;
    }
    const element = el(video ? 'video' : 'img', video ? { controls: '', preload: 'none', playsinline: '', 'aria-label': anonymous ? '匿名视频证据' : `运行 ${run.id} 的桌面录像` } : { decoding: 'async', alt: anonymous ? '匿名图像证据' : `运行 ${text(run.id)} 的证据 ${path}` });
    const fail = message => { figure.dataset.mediaState = 'failed'; status.textContent = message; if (!frame.querySelector('.media-failure')) frame.append(el('div', { class: 'media-failure', text: message })); };
    element.addEventListener('error', () => { if (element.hasAttribute('src')) fail('本列证据加载失败；请检查会话或文件完整性。'); });
    element.addEventListener(video ? 'loadeddata' : 'load', () => { figure.dataset.mediaState = 'loaded'; status.textContent = `${video ? '桌面录像' : thumbnail ? '缩略图' : '全尺寸图'} · 浏览器已加载（不代表验收）`; });
    frame.append(element);
    if (video) {
      const play = el('button', { type: 'button', class: 'play-media', 'data-play-media': '', text: '播放此录像', onclick: () => { figure.playEvidence(false).catch(error => fail(`本列播放失败：${error.message}`)); } });
      figure.playEvidence = async restart => {
        frame.querySelector('.media-failure')?.remove();
        if (!element.hasAttribute('src')) { element.src = uri; figure.dataset.mediaState = 'loading'; status.textContent = '正在读取桌面录像…'; }
        if (restart) { element.pause(); element.currentTime = 0; }
        try { await element.play(); play.hidden = true; }
        catch (error) { play.hidden = false; fail(`本列播放失败：${error.message}`); throw error; }
      };
      figure.pauseEvidence = () => element.pause(); frame.append(play);
    } else if (thumbnail) {
      element.evidenceURI = uri;
      if (thumbnailObserver) thumbnailObserver.observe(element);
      else frame.append(el('button', { text: '加载缩略图', onclick: event => { element.src = uri; event.target.remove(); } }));
    } else {
      figure.loadEvidence = () => { if (!element.hasAttribute('src')) { element.src = uri; figure.dataset.mediaState = 'loading'; status.textContent = '全尺寸图 · 正在加载'; } };
      if (options.defer) status.textContent = '全尺寸图 · 切换到此列时加载';
      else figure.loadEvidence();
    }
    return figure;
  }
  function openZoom(run, path, anonymous = false) {
    stopPreviews();
    if (!path || /\.(webm|mp4)$/i.test(path) || !mediaURI(run, path)) { notify('当前阶段没有可放大的安全图像。'); return; }
    const dialog = $('#zoom-dialog'), content = $('#zoom-content'); releaseMedia(content);
    $('#zoom-title').textContent = anonymous ? '匿名图像 · 放大' : `${text(run.model)} · ${path.split('/').at(-1)}`;
    content.replaceChildren(media(run, path, anonymous, { full: true })); if (!dialog.open) dialog.showModal();
  }
  function stageViewer(run, initial = 'desktop', anonymous = false) {
    let selected = initial;
    const holder = el('div', { class: 'stage-content' });
    const zoom = el('button', { type: 'button', 'data-open-stage': anonymous ? '' : run.id, text: '放大当前图像', onclick: () => openZoom(run, stagePath(run, selected), anonymous) });
    const draw = () => { releaseMedia(holder); const path = stagePath(run, selected); holder.replaceChildren(media(run, path, anonymous)); zoom.hidden = selected === 'video'; zoom.disabled = !path || !mediaURI(run, path); };
    const selector = stageSelector(selected, value => { selected = value; draw(); }, { 'data-detail-stage': '' });
    const viewer = el('div', { class: 'stage-viewer' }, [el('div', { class: 'stage-controls' }, [selector, zoom]), holder, el('p', { class: 'footnote', text: '阶段名称来自采样文件，不是精确时间戳。录像为桌面捕获；公开派生媒体可能缩放或裁剪，不作像素级对齐。' })]);
    const extras = evidencePaths(run).filter(path => !stages.some(stage => path.split('/').at(-1) === stage.file));
    if (extras.length) viewer.append(el('details', {}, [el('summary', { text: `其他已登记证据 ${extras.length} 份（按需打开）` }), ...extras.map(path => el('button', { text: anonymous ? '打开其他证据' : path, onclick: () => { releaseMedia(holder); holder.replaceChildren(media(run, path, anonymous)); zoom.hidden = true; } }))]));
    draw(); return viewer;
  }
  function galleryCard(run) {
    const path = stagePath(run, state.galleryStage);
    return el('article', { class: `evidence-card${evidencePaths(run).length ? '' : ' no-registered-media'}`, 'data-run-id': run.id }, [
      el('div', { class: 'card-heading' }, [el('div', { class: 'card-title' }, [el('h3', { text: text(run.model) }), el('span', { class: 'chip', text: `${text(run.tool)} · ${text(run.prompt_version)}` })]), originalButton(run)]),
      media(run, path),
      el('div', { class: 'caption' }, [el('p', { class: 'run-identity', text: `${dateOf(run) || 'unknown'} · ${text(run.task_name || run.task_id)}` }), el('p', { class: 'mono run-id', text: text(run.id) }), el('p', { class: 'attempt-label', text: attemptText(run) }), el('p', { class: 'effort-label', text: `Effort ${effortText(run)}` }),
        el('div', { class: 'run-facts' }, [el('span', {}, ['会话 ', statusChip(generationStatus(run))]), el('span', { text: `入口 ${entrypointStatus(run)}` }), el('span', { text: `评估 ${text(run.evaluation?.status)}` }), el('span', { text: checksText(run) })]),
        el('div', { class: 'card-actions' }, [selectButton(run), el('button', { 'data-open-stage': run.id, text: '放大', disabled: !path || state.galleryStage === 'video' || !mediaURI(run, path), onclick: () => openZoom(run, path) }), el('button', { 'data-open-video': run.id, text: '看录像', disabled: !stagePath(run, 'video'), onclick: () => openDetail(run.id, 'video') }), detailButton(run, '本次详情 / 全部证据')])])
    ]);
  }
  function renderGallery(root) {
    const selector = stageSelector(state.galleryStage, value => { state.galleryStage = value; render(); }, { class: 'gallery-stage' });
    root.append(el('div', { class: 'section-heading gallery-heading' }, [el('div', {}, [el('h2', { text: '逐次作品' }), el('p', { text: '工具 / 模型分组 · 组内时间、ID 升序 · 不选 best / latest，失败但有证据同样展示。' })]), el('label', {}, ['采样阶段（非精确时间）', selector])]));
    const groups = new Map(), withoutMedia = state.runs.filter(run => !evidencePaths(run).length);
    for (const run of state.runs) { const key = JSON.stringify([run.tool, run.model]); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(run); }
    if (withoutMedia.length) {
      const counts = get => { const tally = new Map(); for (const run of withoutMedia) { const value = text(get(run)); tally.set(value, (tally.get(value) || 0) + 1); } return [...tally].sort(([a], [b]) => a.localeCompare(b)).map(([value, count]) => `${value} ${count}`).join(' / '); };
      const entries = withoutMedia.map(entrypointStatus);
      const history = el('details', { id: 'no-media-history', class: 'no-media-history', open: withoutMedia.length === state.runs.length ? '' : null }, [
        el('summary', {}, [el('strong', { text: `${withoutMedia.length} / ${state.runs.length} 次无媒体登记 · 展开每次尝试` }), el('span', { class: 'history-states' }, [el('span', { 'data-history-status': 'session', text: `会话 ${counts(generationStatus)}` }), el('span', { 'data-history-status': 'entrypoint', text: `入口 pass ${entries.filter(value => value === 'pass').length} / fail ${entries.filter(value => value === 'fail').length} / unknown ${entries.filter(value => value === 'unknown').length}` }), el('span', { 'data-history-status': 'evaluation', text: `评估 ${counts(run => run.evaluation?.status)}` })])]),
        el('p', { class: 'footnote', text: '这里只收折未登记可预览栅格 / 录像的尝试，不按会话成败筛选。无媒体不等于入口失败；实时原作另由标题入口主动启动。每次运行仍可选中、查详情及全部附件。' }),
        el('div', { class: 'gallery no-media-list' })
      ]);
      let populated = false;
      const populate = () => {
        if (populated) return;
        populated = true;
        $('.no-media-list', history).replaceChildren(...[...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).flatMap(([, runs]) => stableRuns(runs).filter(run => !evidencePaths(run).length).map(galleryCard)));
      };
      history.addEventListener('toggle', () => { if (history.isConnected && history.open) populate(); });
      if (history.open) populate();
      root.append(history);
    }
    const groupGrid = el('div', { class: 'model-groups' }); root.append(groupGrid);
    for (const [, allRuns] of [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const runs = allRuns.filter(run => evidencePaths(run).length); if (!runs.length) continue;
      const first = runs[0], missing = allRuns.length - runs.length;
      groupGrid.append(el('section', { class: 'model-group', 'aria-label': `${text(first.tool)} / ${text(first.model)}` }, [el('div', { class: 'model-group-heading' }, [el('h2', { text: text(first.model) }), el('p', { text: `${text(first.tool)} · 展示 ${runs.length} / ${allRuns.length} 次${missing ? `；${missing} 次无媒体见上方历史` : ' · 未收折'} · 时间 / ID 升序` })]), el('div', { class: 'gallery' }, stableRuns(runs).map(galleryCard))]));
    }
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
  async function openDetail(id, stage = 'desktop') {
    stopPreviews();
    const request = ++state.detailRequest;
    const dialog = $('#detail-dialog'), content = $('#detail-content'); releaseMedia(content);
    content.replaceChildren(el('p', { class: 'muted', text: '正在读取运行详情…' })); if (!dialog.open) dialog.showModal();
    try {
      const run = await getRun(id); if (!dialog.open || request !== state.detailRequest) return;
      $('#detail-title').textContent = text(run.id);
      const blocks = [detailBlock('原作实时预览', [originalButton(run), el('p', { class: 'footnote', text: originalGuidance(run) })], true), detailBlock('本次证据 · 按阶段查看', [stageViewer(run, stage), selectButton(run)], true)];
      blocks.push(detailBlock('实验条件 / Prompt', [metadata([
        ['运行状态', statusNames[run.status] || run.status], ['生成结论', run.generation_status || run.status], ['已记录 attempt（缺失为 unknown）', run.attempt], ['重试来源', run.retry_of], ['归档完整性', run.archive_status], ['评估结束', run.evaluation_finished_at], ['批次', run.batch_id], ['日期', dateOf(run)], ['工具 / 模型', `${text(run.tool)} / ${text(run.model)}`], ['任务', `${text(run.task_name)} (${text(run.task_id)})`], ['Purpose', run.purpose], ['Prompt 版本', run.prompt_version], ['条件指纹', run.condition_fingerprint], ['推理强度', effortText(run)], ['Prompt hash', run.prompt_hash || run.prompt?.sha256 || run.prompt?.hash], ['归档路径', run.archive_dir], ['开始 / 结束', `${text(run.started_at)} / ${text(run.finished_at)}`], ['错误', run.error]
      ]), el('details', {}, [el('summary', { text: 'Prompt 原文 / 元信息（如 manifest 提供）' }), el('div', { class: 'pre-scroll' }, el('pre', { text: text(run.prompt || run.prompt_text) }))]) ]));
      const metrics = run.metrics || {};
      blocks.push(detailBlock('计量与来源', [metadata([...Object.entries(metricDefs).map(([key, [label]]) => [label, metricFormat(valueOf(run, key), key)]), ['评估耗时来源', 'evaluation.duration_ms（不从时间戳 / 捕获时长推算）'], ['首 CLI 事件（非供应商 TTFT）', metricFormat(run.first_model_event_ms, 'duration_ms')], ['缓存读取', number(metrics.cache_read_tokens)], ['缓存写入', number(metrics.cache_write_tokens)], ['推理 Token', number(metrics.reasoning_tokens)], ['轮数', number(metrics.num_turns)], ['费用来源', metrics.cost_source], ['Metrics 来源', run.metrics_source || metrics.source || 'manifest.metrics（来源未声明）']]), el('p', { class: 'footnote', text: '生成会话耗时不是完整评估的端到端耗时。计量缺失是 unknown，零值仅在来源明确记录为 0 时展示。源码体积与帧变化均不是质量分。' })]));
      blocks.push(detailBlock('Checks / Evaluation', [el('p', { class: 'footnote', text: `Evaluation: ${text(run.evaluation?.status)} · 完成状态不自动转换为 pass` }), ...(run.checks?.length ? run.checks.map(check => el('p', {}, [el('span', { class: 'chip', text: `${text(check.status)} · ${text(check.name)}` }), el('span', { text: ` ${text(check.detail)}` })])) : [el('p', { class: 'muted', text: '未提供 Checks；未验收。' })]) ]));
      blocks.push(detailBlock('追加评分记录', [el('p', { class: 'footnote', text: reviewStatus(run) }), reviewList(run.reviews || [])]));
      const paths = evidencePaths(run);
      blocks.push(detailBlock('证据登记与公开包含', [el('p', { class: 'footnote', text: `附件登记 ${(run.artifacts || []).length} 份；安全证据登记 ${paths.length} 份；${snapshot ? `公开包含 ${includedPaths(run).length} 份` : '本地 API 按需读取，未预检文件'}。浏览器加载 / 失败另在预览下方显示。` }), el('ul', { class: 'evidence-paths' }, paths.map(path => el('li', { class: 'mono', text: `${path} · ${snapshot ? mediaURI(run, path) ? '公开包含' : '未包含 / 无效映射' : '已登记 · 未预检'}` })))], true));
      blocks.push(detailBlock('产物附件 / 完整性', [attachments(run)], true));
      blocks.push(detailBlock('文本日志', [logViewer(run)], true));
      blocks.push(detailBlock('Manifest · 原始记录', [el('details', {}, [el('summary', { text: '展开 JSON（纯文本，不执行内容）' }), el('div', { class: 'pre-scroll' }, el('pre', { text: JSON.stringify(run, null, 2) }))])], true));
      content.replaceChildren(el('div', { class: 'detail-grid' }, blocks));
    } catch (error) { if (request === state.detailRequest && dialog.open) content.replaceChildren(el('p', { class: 'error', text: error.message })); }
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
    stopPreviews();
    const request = ++compareRequest;
    if (state.selected.size < 2 || state.selected.size > selectionLimit() || comparisonIssue(selectedRuns())) { updateSelection(); return; }
    const dialog = $('#compare-dialog'), content = $('#compare-content'); releaseMedia(content); content.replaceChildren(el('p', { class: 'muted', text: '正在读取所选运行…' }));
    if (!dialog.open) dialog.showModal();
    compareMode = 'media'; comparePair = []; $('#compare-mode').value = compareMode;
    state.compareStage = 'desktop'; state.compareIndex = 0; $('#compare-stage').value = state.compareStage;
    const ids = [...state.selected];
    try {
      const runs = await Promise.all(ids.map(getRun));
      if (!dialog.open || request !== compareRequest || ids.join('|') !== [...state.selected].join('|')) return;
      state.compareRuns = runs; runs.forEach(run => state.selectionRuns.set(run.id, run)); renderComparison();
    } catch (error) { if (dialog.open && request === compareRequest) content.replaceChildren(el('p', { class: 'error', text: error.message })); }
  }
  function compareFacts(run) {
    return metadata([
      ['生成会话', text(generationStatus(run))], ['入口检查 / 评估', `${entrypointStatus(run)} / ${text(run.evaluation?.status)}`], ['检查', checksText(run)],
      ['生成会话耗时', metricFormat(run.duration_ms, 'duration_ms')], ['评估耗时', metricFormat(run.evaluation?.duration_ms, 'evaluation_duration_ms')],
      ['费用 / 来源', `${metricFormat(valueOf(run, 'cost_usd'), 'cost_usd')} / ${text(run.metrics?.cost_source)}`], ['总 Token', metricFormat(valueOf(run, 'total_tokens'), 'total_tokens')], ['评分状态', reviewStatus(run)]
    ]);
  }
  function showCompareSide(index) {
    state.compareIndex = index;
    $$('#compare-content .compare-column').forEach((column, i) => {
      column.hidden = narrowScreen.matches && i !== index;
      if (column.hidden) { for (const panel of previewPanels) if (column.contains(panel.node)) panel.stop(); $$('video', column).forEach(video => video.pause()); }
      else $$('.media-evidence', column).forEach(figure => figure.loadEvidence?.());
    });
    $$('#compare-ab [data-compare-index]').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.compareIndex) === index)));
  }
  function renderComparison() {
    const content = $('#compare-content'); releaseMedia(content); content.replaceChildren();
    const runs = state.compareRuns.filter(run => state.selected.has(run.id)), rows = conditionDifferences(runs), differences = rows.filter(row => row.status !== '一致');
    $('#compare-video-status').textContent = '';
    $('#compare-ab').replaceChildren();
    const live = compareMode === 'original';
    $('#compare-stage-label').hidden = live;
    $('#compare-stage-note').hidden = live;
    $('#compare-live-note').hidden = !live;
    $('#compare-video-controls').hidden = live || state.compareStage !== 'video';
    $('#start-original-pair').hidden = true;
    $('#compare-pair-picker').replaceChildren();
    const issue = comparisonIssue(runs), tooMany = !live && runs.length > selectionLimit();
    $('#compare-condition-warning').textContent = [state.mixed ? '跨条件查看 · 仅观察证据，不生成合并排名。' : '同题 / 同 Prompt 比较；模型身份不同不等于条件相同。', differences.length ? `不同或 unknown：${differences.map(row => `${row.name}（${row.status}）`).join('、')}。` : '已列条件一致；这不代表像素级对齐或质量结论。'].join(' ');
    $('#compare-conditions').replaceChildren(el('summary', { text: `查看逐项条件 · ${differences.length} 项不同 / unknown（不使用含模型身份的趋势指纹判定）` }), table(['条件', '判定', ...runs.map((run, i) => `${String.fromCharCode(65 + i)} · ${text(run.model)}`)], rows.map(row => [row.name, row.status, ...row.values.map(text)]), 'condition-table'));
    if (tooMany || issue || runs.length < 2) {
      content.append(el('div', { class: 'comparison-recovery' }, [el('h3', { text: tooMany ? `选择已全部保留；当前最多 ${selectionLimit()} 次` : '请调整比较对象或条件' }), el('p', { text: issue || '请明确选择保留哪两次；不会自动移除或替换运行。' }), ...runs.map(run => el('div', {}, [el('p', { class: 'mono', text: `${text(run.model)} · ${run.id}` }), el('button', { text: '移除此运行', onclick: () => { toggleSelection(run); renderComparison(); } })]))])); return;
    }
    if (live) { renderOriginalComparison(runs, content); return; }
    content.style.setProperty('--columns', runs.length);
    $('#compare-ab').replaceChildren(...runs.map((run, i) => el('button', { 'data-compare-index': i, 'aria-pressed': String(state.compareIndex === i), text: `${String.fromCharCode(65 + i)} · ${text(run.model)}`, onclick: () => showCompareSide(i) })));
    state.compareIndex = Math.min(state.compareIndex, runs.length - 1);
    content.replaceChildren(...runs.map((run, i) => {
      const path = stagePath(run, state.compareStage);
      return el('article', { class: 'compare-column', 'data-run-id': run.id }, [
        el('div', { class: 'compare-identity' }, [el('p', { class: 'eyebrow', text: `${String.fromCharCode(65 + i)} / ${text(run.tool)}` }), el('h3', { text: text(run.model) }), el('p', { class: 'condition-key', text: `${dateOf(run)} · ${text(run.task_name || run.task_id)} · ${text(run.prompt_version)}` }), el('p', { class: 'mono run-id', text: text(run.id) }), el('p', { class: 'attempt-label', text: attemptText(run) }), el('p', { class: 'effort-label', text: `Effort ${effortText(run)}` })]),
        media(run, path, false, { full: true, defer: narrowScreen.matches && i !== state.compareIndex }),
        el('div', { class: 'card-actions' }, [el('button', { 'data-open-stage': run.id, text: '放大当前图像', disabled: !path || state.compareStage === 'video' || !mediaURI(run, path), onclick: () => openZoom(run, path) }), detailButton(run, '运行详情')]),
        el('div', { class: 'compare-facts' }, compareFacts(run))
      ]);
    }));
    state.compareIndex = Math.min(state.compareIndex, runs.length - 1); showCompareSide(state.compareIndex);
  }
  function renderOriginalComparison(runs, content) {
    comparePair = comparePair.filter(id => runs.some(run => run.id === id));
    if (runs.length === 2) comparePair = runs.map(run => run.id);
    const picker = $('#compare-pair-picker');
    if (runs.length > 2) {
      picker.append(el('p', { class: 'footnote', text: `媒体比较的 ${runs.length} 次选择全部保留。实时模式最多两份，请明确勾选两份原作；不会自动取前两份。` }), ...runs.map(run => el('label', { class: 'checkbox-label' }, [el('input', { type: 'checkbox', 'data-original-pair': run.id, checked: comparePair.includes(run.id), onchange: event => {
        if (event.target.checked && comparePair.length >= 2) { event.target.checked = false; notify('实时原作最多两份；请先取消一份原作选择。', true); return; }
        comparePair = event.target.checked ? [...comparePair, run.id] : comparePair.filter(id => id !== run.id); renderComparison();
      } }), `${text(run.model)} · ${run.id}`])));
    }
    if (comparePair.length !== 2) { content.append(el('p', { class: 'comparison-recovery', text: '先在上方明确选择两份原作。未请求原作包，未创建运行实例。' })); return; }
    const pair = comparePair.map(id => runs.find(run => run.id === id));
    state.compareIndex = Math.min(state.compareIndex, 1); content.style.setProperty('--columns', 2);
    const pairPanels = []; let pairStarted = false;
    content.replaceChildren(...pair.map((run, i) => {
      const panel = previewPanel(run, { comparison: true, canRunTogether: () => pairStarted }); pairPanels.push(panel);
      return el('article', { class: 'compare-column', 'data-run-id': run.id }, [el('div', { class: 'compare-identity' }, [el('p', { class: 'eyebrow', text: `${String.fromCharCode(65 + i)} / ${text(run.tool)}` }), el('h3', { text: text(run.model) }), el('p', { class: 'mono run-id', text: run.id })]), panel.node, el('div', { class: 'compare-facts' }, compareFacts(run))]);
    }));
    $('#compare-ab').replaceChildren(...pair.map((run, i) => el('button', { 'data-compare-index': i, 'aria-pressed': String(state.compareIndex === i), text: `${String.fromCharCode(65 + i)} · ${text(run.model)}`, onclick: () => showCompareSide(i) })));
    const startPair = $('#start-original-pair'); startPair.hidden = narrowScreen.matches; startPair.disabled = !pair.every(originalAvailable);
    startPair.onclick = () => { if (narrowScreen.matches || compareMode !== 'original' || !$('#compare-dialog').open || comparisonIssue(pair)) return; stopPreviews(); pairStarted = true; pairPanels.forEach(panel => panel.start(true)); };
    showCompareSide(state.compareIndex);
  }
  async function controlComparisonVideos(action) {
    if (compareMode !== 'media' || state.compareStage !== 'video') return;
    const figures = $$('#compare-content .compare-column:not([hidden]) .media-evidence'), playable = figures.filter(figure => typeof figure.playEvidence === 'function');
    if (action === 'pause') { playable.forEach(figure => figure.pauseEvidence()); $('#compare-video-status').textContent = `已暂停 ${playable.length} 列；缺失 / 不可读取 ${figures.length - playable.length} 列。`; return; }
    const results = await Promise.allSettled(playable.map(figure => figure.playEvidence(action === 'restart')));
    const failures = results.filter(result => result.status === 'rejected').length;
    $('#compare-video-status').textContent = `可播放列 ${playable.length - failures}/${figures.length} · 加载 / 播放失败 ${failures} · 缺失 / 不可读取 ${figures.length - playable.length}。仅按各自录像起点控制，不代表动画时间或长度同步。`;
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
    const evidence = el('section', { class: 'panel blind-evidence' }, [el('p', { class: 'eyebrow', text: 'ANONYMOUS REVIEW' }), el('h3', { class: 'blind-anon', text: `样本 ${String(caseNumber).padStart(2, '0')}` }), stageViewer(run, 'desktop', true)]);
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
    cancelPreviewContext();
    state.view = ['batch', 'trend', 'gallery', 'matrix', 'blind', 'evidence'].includes(view) ? view : 'gallery';
    $$('[data-view]').forEach(button => { const active = button.dataset.view === state.view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    // Entering anonymous review must not leave an identifying dialog open.
    if (state.view === 'blind') { ['detail-dialog', 'compare-dialog', 'zoom-dialog'].forEach(id => { releaseMedia($(`#${id}`)); $(`#${id}`).close(); }); }
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
    cancelPreviewContext();
    for (const key of filterKeys) state.filters[key] = $(`[name="${key}"]`, $('#filters')).value;
    clearTimeout(filterTimer);
    if (state.filters.date_from && state.filters.date_to && state.filters.date_from > state.filters.date_to) { notify('开始日期不能晚于结束日期。', true); return; }
    state.defaultTaskPending = false; syncURL();
    filterTimer = window.setTimeout(() => loadData(), event.target.name === 'q' ? 250 : 0);
  }
  $('#filters').addEventListener('input', handleFilter);
  $('#filters').addEventListener('submit', event => { event.preventDefault(); clearTimeout(filterTimer); loadData(); });
  $('#reset-filters').addEventListener('click', () => { clearTimeout(filterTimer); state.filters = { purpose: 'benchmark', task_id: taskOptions()[0]?.id || '' }; fillOptions(); syncURL(); loadData(); });
  $$('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
  $('#theme-toggle').addEventListener('click', () => { const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches); setTheme(dark ? 'light' : 'dark'); });
  $('#texture-toggle').addEventListener('click', event => { state.texture = !state.texture; document.body.classList.toggle('texture', state.texture); event.target.setAttribute('aria-pressed', String(state.texture)); });
  $('#refresh-button').addEventListener('click', () => loadData(true));
  $('#auth-button').addEventListener('click', async () => {
    if (!state.authenticated) { openAuth(); return; }
    cancelPreviewContext();
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
  $$('[data-close]').forEach(button => button.addEventListener('click', () => { const dialog = $(`#${button.dataset.close}`); releaseMedia(dialog); dialog.close(); }));
  ['detail-dialog', 'compare-dialog', 'zoom-dialog', 'original-dialog'].forEach(id => {
    const dialog = $(`#${id}`);
    dialog.addEventListener('cancel', () => releaseMedia(dialog));
    dialog.addEventListener('close', () => { if (dialog.open) return; releaseMedia(dialog); if (id === 'detail-dialog') ++state.detailRequest; if (id === 'compare-dialog') ++compareRequest; });
  });
  $('#compare-mode').addEventListener('change', event => { stopPreviews(); compareMode = event.target.value; renderComparison(); });
  window.addEventListener('pagehide', () => { stopPreviews(); ++compareRequest; ++state.detailRequest; });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { stopPreviews(); ++compareRequest; ++state.detailRequest; } });
  $('#clear-selection').addEventListener('click', () => { state.selected.clear(); state.selectionRuns.clear(); updateSelection(); });
  $('#compare-button').addEventListener('click', openComparison);
  $('#compare-limit').addEventListener('change', event => { state.compareLimit = Number(event.target.value); updateSelection(); });
  $('#mixed-comparison').addEventListener('change', event => { state.mixed = event.target.checked; updateSelection(); });
  $('#compare-stage').addEventListener('change', event => { state.compareStage = event.target.value; renderComparison(); });
  $('#play-videos').addEventListener('click', () => controlComparisonVideos('play'));
  $('#pause-videos').addEventListener('click', () => controlComparisonVideos('pause'));
  $('#restart-videos').addEventListener('click', () => controlComparisonVideos('restart'));
  narrowScreen.addEventListener('change', () => { updateSelection(); if ($('#compare-dialog').open) renderComparison(); });
  $$('[data-export]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { const response = await api(`/api/export?format=${button.dataset.export}&${query()}`); await download(response, `bench-runs.${button.dataset.export}`); notify('已导出当前筛选的运行数据。'); } catch (error) { notify(error.message, true); } finally { updateAuth(); }
  }));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });
  window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
  window.addEventListener('popstate', () => { clearTimeout(filterTimer); const params = new URLSearchParams(location.search); state.defaultTaskPending = false; for (const key of filterKeys) state.filters[key] = params.get(key) || (key === 'purpose' && !params.has('purpose') ? 'benchmark' : ''); fillOptions(); setView(location.hash.slice(1)); loadData(); });
  const params = new URLSearchParams(location.search);
  state.defaultTaskPending = !filterKeys.some(key => params.has(key)) && ['', '#gallery'].includes(location.hash);
  for (const key of filterKeys) state.filters[key] = params.get(key) || (key === 'purpose' && !params.has('purpose') ? 'benchmark' : '');
  const offline = source?.offline;
  if (snapshot && staticMedia && offline?.path === 'offline.html' && /^[a-f0-9]{64}$/.test(offline.sha256) && Number.isSafeInteger(offline.size) && offline.size > 0) { $('#offline-download').href = 'offline.html'; $('#offline-download').hidden = false; $('#offline-download').title = `离线完整版 · ${number(offline.size)} bytes · SHA256 ${offline.sha256}`; }
  try { const theme = localStorage.getItem('bench-theme'); if (['light', 'dark'].includes(theme)) document.documentElement.dataset.theme = theme; } catch (error) { console.warn('主题偏好不可读取；使用系统主题。', error.name); }
  $('#mode-label').textContent = snapshot ? '脱敏快照 · 只读' : '真实运行 · 可追溯';
  updateAuth(); setView(location.hash.slice(1)); loadData(true);
})();
