(() => {
  'use strict';
  const seed = window.BENCH_GALLERY;
  if (!seed || seed.version !== 3 || seed.format !== 'progressive-static-v3') throw new Error('Invalid public gallery profile');
  const $ = selector => document.querySelector(selector);
  const narrow = window.matchMedia('(max-width: 719px)');
  const records = new Map(seed.runs.map(run => [run.id, run]));
  const initialStyle = document.querySelector('head style');
  let controller = new AbortController(), fullPromise = null, fullFetch = null, fullIntent = 0, returnURL = null;
  let sdkPromise = null, activePreview = null, historyRuns = [], imageObserver = null, fatal = false;
  const scoreLabels = { compliance: '指令遵循', recognizability: '辨识度', motion: '运动表现', visual_detail: '视觉细节', interaction: '交互表现', completeness: '完整性' };
  const scoreKeys = Object.keys(scoreLabels), scopes = seed.score_scopes || [];
  const state = { task: seed.defaults.task_id, purpose: seed.defaults.purpose, model: '', from: '', to: '', sort: '', source: seed.defaults.score_source, page: 0, selected: new Set(), mixed: false };
  const pageSize = () => narrow.matches ? seed.defaults.mobile_page_size : seed.defaults.page_size;
  const shortModel = model => text(model).replace(/^opencode\//, '');
  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const rankable = scope => Array.isArray(scope) && scope.length === 4 && scope[0] === 'visual-v1' && ['ai-suggestion', 'label-unverified'].includes(scope[1]) && typeof scope[2] === 'boolean' && typeof scope[3] === 'string' && !!scope[3].trim();
  const scopeLabel = scope => `${scope[0] || '协议 unknown'} · ${scope[1] === 'ai-suggestion' ? '非盲 AI 建议' : '来源标签未核实'} · ${scope[2] === true ? '盲评' : scope[2] === false ? '非盲评' : '盲评属性 unknown'} · ${scope[3] || 'Reviewer unknown'}`;
  const selectedScore = run => (run.scores || []).find(item => item.scope === state.source);
  function scoreValue(run, dimension = state.sort || 'compliance') {
  const summary = selectedScore(run), scope = scopes.find(item => JSON.stringify(item) === state.source);
  const value = summary?.values[scoreKeys.indexOf(dimension)];
  return rankable(scope) && finite(value) && value <= 4 ? value : null;
  }
  function sortedRuns(runs) {
  if (!state.sort) return runs;
  const groups = new Map();
  for (const run of runs) { const key = JSON.stringify([run.task_id, run.purpose, state.source]); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(run); }
  return [...groups].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).flatMap(([, group]) => group.map((run, index) => ({ run, index, value: scoreValue(run) })).sort((a, b) => (a.value === null) - (b.value === null) || (a.value !== null && b.value !== null ? b.value - a.value : 0) || a.index - b.index).map(item => item.run));
  }
  function scoreCard(run) {
  const summary = selectedScore(run), dimension = state.sort || 'compliance', value = scoreValue(run, dimension);
  const result = node('div', { class: 'work-score' }, [node('span', { text: `${scoreLabels[dimension]} ${value === null ? '未评' : value + '/4'}` })]);
  if (summary) {
  const scope = scopes.find(item => JSON.stringify(item) === summary.scope), valid = rankable(scope);
  result.append(node('details', { class: 'work-score-details' }, [node('summary', { text: '六项与来源' }), node('p', { text: `${scopeLabel(scope)}${valid ? '' : ' · 协议/来源条件未核实，不排序'} · ${text(summary.created_at)} · review ${text(summary.id)} · 原序 ${summary.index}` }), node('dl', {}, scoreKeys.flatMap((key, index) => [node('dt', { text: scoreLabels[key] }), node('dd', { text: valid && finite(summary.values[index]) && summary.values[index] <= 4 ? summary.values[index] + '/4' : '未评' })]))]));
    }
  return result;
  }
  function meterText(run) {
  const [ms, cost, source] = run.meter || [];
  const origin = source === 'cli_reported_unverified' ? 'CLI 未核验，非账单' : source ? '来源未核实（原始记录可查）' : '来源未声明';
  return `生成会话 ${finite(ms) ? ms === 0 ? '0 秒' : '约 ' + Math.round(ms / 1000) + ' 秒（approx）' : 'unknown'}；费用 ${finite(cost) ? '约 USD ' + cost.toFixed(2) + '（approx）' : 'unknown'} · ${origin}`;
  }
  const allowedOriginal = run => ['ready', 'missing_dependencies'].includes(run.original?.status);
  const idPattern = /^[a-f0-9]{32}$/;
  const imagePattern = /^media\/[a-f0-9]{64}\.(jpg|jpeg|png|webp|gif)$/;
  const text = value => value == null || value === '' ? 'unknown' : String(value);
  function node(tag, attrs = {}, children = []) {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
  if (key === 'text') result.textContent = value;
  else if (key === 'class') result.className = value;
  else if (key === 'disabled') result.disabled = Boolean(value);
  else result.setAttribute(key, String(value));
    }
  result.append(...children); return result;
  }
  function notice(message = '', error = false) {
  const target = $('#public-notice'); if (!target) return;
  target.textContent = message; target.hidden = !message; target.classList.toggle('error', error);
  }
  function pathURL(path, extension) {
  const pattern = new RegExp('^viewer/[a-f0-9]{64}\\.' + extension + '$');
  if (!pattern.test(path)) throw new Error('Invalid viewer resource path');
  const result = new URL(path, location.href);
  if (result.origin !== location.origin) throw new Error('Cross-origin viewer resource');
  return result.href;
  }
  function readURL() {
  const params = new URLSearchParams(location.search);
  const explicit = ['purpose', 'date_from', 'date_to', 'tool', 'model', 'prompt_version', 'status', 'q'].some(key => params.has(key));
  state.task = params.has('task_id') ? params.get('task_id') : explicit ? '' : seed.defaults.task_id;
  state.purpose = params.has('purpose') ? params.get('purpose') : seed.defaults.purpose;
  const sort = params.get('score_sort'), source = params.get('score_source');
  state.sort = sort && scoreKeys.includes(sort) ? sort : '';
  state.source = source && scopes.some(scope => JSON.stringify(scope) === source) ? source : seed.defaults.score_source;
  state.model = params.get('model_query') || ''; state.from = params.get('date_from') || ''; state.to = params.get('date_to') || '';
  state.page = /^\d+$/.test(params.get('gallery_page') || '') ? Number(params.get('gallery_page')) : 0;
  return ['tool', 'model', 'prompt_version', 'status', 'q'].some(key => params.get(key)) || Boolean(location.hash && location.hash !== '#gallery');
  }
  function syncURL(push = false) {
  const url = new URL(location.href);
  url.searchParams.set('task_id', state.task); url.searchParams.set('purpose', state.purpose);
  for (const [key, value] of [['model_query', state.model], ['date_from', state.from], ['date_to', state.to], ['score_sort', state.sort], ['score_source', state.source !== seed.defaults.score_source ? state.source : ''], ['gallery_page', state.page ? String(state.page) : '']]) {
  if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
  url.hash = 'gallery';
  try { history[push ? 'pushState' : 'replaceState'](null, '', url); } catch (error) { notice('无法保存链接；当前筛选仍有效。'); }
  }
  function matches(run) {
  return (!state.task || run.task_id === state.task) && (!state.purpose || run.purpose === state.purpose)
  && (!state.model.trim() || text(run.model).toLocaleLowerCase().includes(state.model.trim().toLocaleLowerCase()))
  && (!state.from || Boolean(run.date && run.date >= state.from)) && (!state.to || Boolean(run.date && run.date <= state.to));
  }
  function card(run) {
  const original = node('button', { 'data-run-original': run.id, class: 'primary', text: '运行原作', disabled: !allowedOriginal(run) });
  const selected = state.selected.has(run.id);
  const historyCount = Number(run.history_count) || 0;
  const history = node('span', { class: 'work-history-badge', title: historyCount ? '历史尝试' : '无历史尝试', text: historyCount ? `历史 ${historyCount + 1} 次 · 详情对照` : '最新' });
  const actions = node('div', { class: 'work-actions' }, [original,
  node('button', { 'data-select-run': run.id, 'aria-pressed': selected, text: selected ? '移出对比' : '加入对比' }),
  node('button', { 'data-zoom-run': run.id, text: '放大', disabled: !run.image }),
  node('button', { 'data-detail-run': run.id, text: '详情 ↗' })]);
  const cover = node('div', { class: 'work-cover' });
  if (run.image && imagePattern.test(run.image.thumb)) {
  cover.style.aspectRatio = `${run.image.width}/${run.image.height}`;
  cover.append(node('img', { class: 'work-image', 'data-src': run.image.thumb, width: run.image.width, height: run.image.height, alt: `${run.model} · ${run.id} · ${run.image.label}`, decoding: 'async' }));
    } else cover.append(node('p', { class: 'work-empty', text: run.registered_media ? '未登记桌面首图；不替换采样。' : '无媒体登记；保留入口与会话状态。' }));
  const profile = node('button', { 'data-model-profile': run.model, text: '模型全史 ↗', title: '该模型跨任务全史' });
  return node('article', { class: 'evidence-card' + (run.registered_media ? '' : ' no-registered-media'), 'data-run-id': run.id, 'data-model-route': run.model, 'data-score-group': JSON.stringify([run.task_id, run.purpose, state.source]) }, [cover,
  node('div', { class: 'work-heading' }, [node('div', { class: 'work-identity' }, [node('h2', { class: 'work-name', text: shortModel(run.model), title: run.model, 'aria-label': run.model }), node('p', { class: 'work-meta', text: `${text(run.tool)} · ${text(run.date)} · ${text(run.task_id)} · ${text(run.purpose)} · ${text(run.prompt_version)}` }), history, profile])]), scoreCard(run), node('p', { class: 'work-metrics', text: meterText(run) }),
  node('div', { class: 'work-status' }, [node('span', { text: `会话 ${text(run.status)}` }), node('span', { text: `入口 ${text(run.entry_status)}` }), node('span', { text: `评估 ${text(run.evaluation_status)}` })]), actions]);
  }
  function loadImage(image) {
  if (!image?.isConnected || image.hasAttribute('src') || !imagePattern.test(image.dataset.src || '')) return;
  image.onload = () => { image.closest('.work-cover')?.setAttribute('data-image-state', 'loaded'); };
  image.onerror = () => { image.closest('.work-cover')?.setAttribute('data-image-state', 'error'); };
  image.src = image.dataset.src; imageObserver?.unobserve(image);
  }
  function observeImages(afterScroll = false) {
  const images = [...document.querySelectorAll('#public-grid .work-image')];
  images.slice(0, narrow.matches ? 1 : 2).forEach(loadImage);
  if (afterScroll) images.forEach(image => imageObserver.observe(image));
  }
  function populateModels() {
  const host = $('#public-model-list'); if (!host) return;
  const models = [...new Set(seed.runs.map(run => run.model))].sort((a, b) => text(a).localeCompare(text(b)));
  host.replaceChildren(...models.map(model => node('button', {
  type: 'button', 'data-model-name': model, 'data-model-route': model, 'aria-label': model, title: model, 'aria-pressed': String(state.model.trim().toLocaleLowerCase() === text(model).toLocaleLowerCase()),
  text: shortModel(model)
    })));
  }
  function populateHistory() {
  if (!$('#public-history').open) return;
  $('#public-history-list').replaceChildren(...historyRuns.map(run => node('article', { class: 'history-row', 'data-run-id': run.id }, [
  node('div', {}, [node('strong', { text: text(run.model) }), node('p', { text: `${text(run.tool)} · ${text(run.task_id)} · ${text(run.date)} · ${run.id.slice(0, 8)} · 会话 ${text(run.status)} / 入口 ${text(run.entry_status)} / 评估 ${text(run.evaluation_status)} · 无桌面首图` })]),
  node('div', { class: 'history-actions' }, [node('button', { 'data-run-original': run.id, text: '运行原作', disabled: !allowedOriginal(run) }), node('button', { 'data-select-run': run.id, text: state.selected.has(run.id) ? '移出对比' : '加入对比' }), node('button', { 'data-detail-run': run.id, text: '详情与历史 ↗' })])] )));
  }
  function selection() {
  const runs = [...state.selected].map(id => records.get(id));
  $('#public-selection').hidden = !runs.length;
  $('#public-selection-label').textContent = `已选择 ${runs.length} / 2 次尝试`;
  $('#public-selection-names').textContent = runs.map(run => `${run.model} · ${run.id.slice(0, 8)}`).join(' / ');
  $('#public-compare').disabled = runs.length !== 2 || !state.mixed;
  document.querySelectorAll('[data-select-run]').forEach(button => {
  const selected = state.selected.has(button.dataset.selectRun); button.setAttribute('aria-pressed', String(selected)); button.textContent = selected ? '移出对比' : '加入对比';
    });
  }
  function render() {
  imageObserver.disconnect();
  const filtered = seed.runs.filter(matches), media = filtered.filter(run => run.registered_media), size = pageSize();
  historyRuns = filtered.filter(run => !run.registered_media);
  const ordered = sortedRuns(state.sort ? filtered : media);
  const pages = Math.max(1, Math.ceil(ordered.length / size)); state.page = Math.min(state.page, pages - 1);
  const shown = ordered.slice(state.page * size, (state.page + 1) * size);
  $('#public-grid').replaceChildren(...shown.map(card));
  if (!shown.length) $('#public-grid').append(node('p', { class: 'work-empty', text: historyRuns.length ? '无公开媒体；全部记录见下方。' : '无匹配记录。' }));
  $('#public-result-label').textContent = `${filtered.length} 最新记录 · 本页${shown.length} · 无媒体${historyRuns.length}`;
  const covered = filtered.filter(run => scoreValue(run) !== null).length;
  $('#public-score-coverage').textContent = `${scoreLabels[state.sort || 'compliance']} ${covered}/${filtered.length} 已评 · 当前口径${state.sort ? '，按同题/用途分组' : ''}${covered ? ' · 无综合分' : ' · 旧分不转给最新运行'}`;
  $('#public-sort').value = state.sort;
  const sourceControl = $('#public-score-source'); sourceControl.value = state.source || '';
  sourceControl.closest('label').hidden = !(scopes.length > 1 || scopes.some(scope => !rankable(scope)));
  $('#public-models summary').textContent = state.model.trim() ? '模型 · 已筛选' : '模型筛选';
  document.querySelectorAll('#public-task-tabs [data-task-id]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.taskId === state.task)));
  $('#public-model').value = state.model;
  populateModels();
  const purpose = $('#public-purpose');
  if (![...purpose.options].some(option => option.value === state.purpose)) purpose.append(node('option', { value: state.purpose, text: `未知用途：${state.purpose}` }));
  purpose.value = state.purpose;
  const pagination = $('#public-pages'); pagination.replaceChildren();
  if (pages > 1) pagination.append(node('button', { 'data-gallery-page': state.page - 1, disabled: state.page === 0, text: '上一页' }), node('span', { text: `${state.page + 1} / ${pages}` }), node('button', { 'data-gallery-page': state.page + 1, disabled: state.page === pages - 1, text: '下一页' }));
  const details = $('#public-history'); details.hidden = !!state.sort || !historyRuns.length;
  $('#public-history-label').textContent = `${historyRuns.length} 次无媒体记录 · 展开查看全部尝试`;
  $('#public-history-list').replaceChildren(); details.open = !state.sort && Boolean(historyRuns.length && !media.length); populateHistory();
  selection(); observeImages();
  }
  function cancelFull(restore = true) {
  ++fullIntent; fullFetch?.abort(); stopPreview();
  if (returnURL && restore) history.replaceState(null, '', returnURL);
  returnURL = null;
  }
  function toggleRun(run) {
  if (state.selected.has(run.id)) state.selected.delete(run.id);
  else {
  if (state.selected.size >= 2) { notice('限选两份；桌面完整版支持 3–4 次对照。', true); return; }
  if ([...state.selected].some(id => records.get(id).task_id !== run.task_id)) { notice('仅同题可比；请清空其他任务选择。', true); return; }
  state.selected.add(run.id);
    }
  notice(); selection();
  }
  function zoom(run) {
  if (!run.image || !imagePattern.test(run.image.full)) return;
  cancelFull();
  const dialog = $('#public-zoom'), image = $('#public-zoom-image');
  $('#public-zoom-title').textContent = `${run.model} · ${run.image.label}`;
  $('#public-zoom-state').textContent = '正在加载完整采样截图…';
  image.alt = `${run.id} · ${run.image.label}`;
  image.onload = () => { if (dialog.open) $('#public-zoom-state').textContent = '全尺寸历史截图已加载。'; };
  image.onerror = () => { if (dialog.open) $('#public-zoom-state').textContent = '图像加载失败，请关闭后重试。'; };
  image.src = run.image.full; if (!dialog.open) dialog.showModal();
  }
  function ensureSDK() {
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
  const descriptor = seed.runtime;
  if (!/^[a-f0-9]{64}$/.test(descriptor.sha256)) { reject(new Error('运行器摘要无效')); return; }
  const script = document.createElement('script'); script.src = pathURL(descriptor.path, 'js');
  script.integrity = 'sha256-' + btoa(String.fromCharCode(...descriptor.sha256.match(/../g).map(value => parseInt(value, 16)))); script.crossOrigin = 'anonymous';
  script.onload = () => window.BenchPreview?.create ? resolve(window.BenchPreview) : reject(new Error('运行器接口不存在'));
  script.onerror = () => { script.remove(); reject(new Error('运行器下载或完整性校验失败，请重试。')); };
  document.head.append(script);
    }).catch(error => { sdkPromise = null; throw error; });
  return sdkPromise;
  }
  function stopPreview() {
  const context = activePreview; if (!context) return;
  ++context.token; context.runtime?.destroy(); context.runtime = null;
  context.host.replaceChildren(); context.node.dataset.previewState = 'stopped'; context.status.textContent = '已停止 · 已清理沙箱；需手动重启';
  context.start.disabled = !allowedOriginal(context.run); context.stop.disabled = true; context.restart.disabled = !allowedOriginal(context.run);
  }
  function destroyPreview() {
  stopPreview(); if (activePreview) activePreview.alive = false; activePreview = null;
  }
  async function startPreview(context) {
  const valid = token => context.alive && activePreview === context && context.token === token && context.node.isConnected && $('#original-dialog')?.open && !document.hidden;
  if (!allowedOriginal(context.run) || !valid(context.token)) return;
  stopPreview(); const token = ++context.token;
  context.status.textContent = '正在装载经过校验的运行器…'; context.node.dataset.previewState = 'loading';
  context.start.disabled = true; context.stop.disabled = false; context.restart.disabled = false; context.diagnostics.replaceChildren();
  try {
  const sdk = await ensureSDK(); if (!valid(token)) return;
  context.runtime = sdk.create(context.host, { runId: context.run.id, descriptor: context.run.original, transport: 'external', viewport: context.viewport,
  onState(value) {
  if (!valid(token)) return;
  context.node.dataset.previewState = value.status; context.status.textContent = `${value.message || value.status}${value.status === 'loaded' ? ' · 已装载不代表验收通过' : ''}`;
  if (Number.isFinite(value.width) && Number.isFinite(value.height)) context.dimensions.textContent = `实际渲染 ${value.width} × ${value.height} CSS px · 缩放 ${Number.isFinite(value.scale) ? value.scale.toFixed(2) : 'unknown'}`;
  if (['error', 'stopped', 'unavailable'].includes(value.status)) { context.start.disabled = false; context.stop.disabled = true; }
        },
  onDiagnostic(value) { if (valid(token) && context.diagnostics.children.length < 20) context.diagnostics.append(node('li', { text: `${text(value?.kind)} · ${text(value?.message).slice(0, 640)}` })); }
      });
  await context.runtime.start();
    } catch (error) {
  if (!valid(token)) return;
  context.runtime?.destroy(); context.runtime = null; context.node.dataset.previewState = 'error';
  context.status.textContent = `原作启动失败：${error.message}`; context.start.disabled = false; context.stop.disabled = true;
    }
  }
  function original(run) {
  if (!allowedOriginal(run)) return;
  cancelFull(); destroyPreview();
  const dialog = $('#original-dialog'); $('#original-title').textContent = `${run.model} · 原作实时预览`;
  const start = node('button', { 'data-preview-start': run.id, text: '启动原作' });
  const stop = node('button', { 'data-preview-stop': run.id, text: '停止', disabled: true });
  const restart = node('button', { 'data-preview-restart': run.id, text: '重新启动', disabled: true });
  const viewport = node('select', { 'data-preview-viewport': run.id, 'aria-label': `原作视口 ${run.id}` }, [node('option', { value: 'fit', text: '适应窗口' }), node('option', { value: 'desktop', text: '1440 × 900' }), node('option', { value: 'mobile', text: '400 × 800' })]);
  const status = node('p', { class: 'preview-status', role: 'status', text: '等待明确启动' });
  const dimensions = node('p', { class: 'preview-dimensions' }), host = node('div', { class: 'preview-host' }), diagnostics = node('ul', { class: 'preview-diagnostics' });
  const guidance = run.original.status === 'missing_dependencies' ? `原作缺少交付依赖：${run.original.missing.join('、')}。保留真实残缺，不补文件。` : '真实原件在隔离沙箱内运行；不等于新的评分或验收结论。';
  const section = node('section', { class: 'original-preview', 'data-original-run': run.id, 'data-preview-state': 'idle' }, [node('div', { class: 'preview-controls' }, [start, stop, restart, node('label', {}, ['渲染视口', viewport])]), status, node('p', { class: 'preview-guidance', text: guidance }), dimensions, host,
  node('p', { class: 'preview-provenance', text: `run ${run.id} · 入口 ${run.original.entrypoint} · SHA256 ${run.original.entry_sha256}` }),
  node('p', { class: 'dialog-footnote', text: '原作独立运行，不支持通用暂停/动画同步。浏览器沙箱非容器断网，无 CPU/内存/GPU 硬配额；上方显示真实视口与缩放。' }), diagnostics]);
  $('#original-content').replaceChildren(section); if (!dialog.open) dialog.showModal();
  const context = activePreview = { run, node: section, host, status, dimensions, diagnostics, start, stop, restart, viewport: 'fit', token: 0, runtime: null, alive: true };
  start.addEventListener('click', () => startPreview(context)); stop.addEventListener('click', stopPreview); restart.addEventListener('click', () => startPreview(context));
  viewport.addEventListener('change', () => { context.viewport = viewport.value === 'desktop' ? { width: 1440, height: 900 } : viewport.value === 'mobile' ? { width: 400, height: 800 } : 'fit'; context.runtime?.setViewport(context.viewport); });
  startPreview(context);
  }
  async function digest(bytes) {
  const value = await crypto.subtle.digest('SHA-256', bytes); return [...new Uint8Array(value)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }
  async function readFull(signal) {
  const descriptor = seed.full;
  if (!Number.isSafeInteger(descriptor.size) || descriptor.size <= 0 || descriptor.size > 2 * 1024 * 1024) throw new Error('完整查看器大小超限');
  const response = await fetch(pathURL(descriptor.path, 'html'), { signal, credentials: 'omit', redirect: 'error' });
  if (!response.ok || response.redirected || !/^text\/html(?:;|$)/i.test(response.headers.get('Content-Type') || '')) throw new Error('完整查看器响应无效');
  const length = response.headers.get('Content-Length'), encoding = (response.headers.get('Content-Encoding') || 'identity').trim().toLowerCase();
  if (length !== null && (!/^\d+$/.test(length) || ((!encoding || encoding === 'identity') && Number(length) !== descriptor.size))) throw new Error('完整查看器响应长度不匹配');
  if (!response.body) throw new Error('完整查看器流不可用');
  const reader = response.body.getReader(), chunks = []; let size = 0;
  try {
  for (;;) { const { done, value } = await reader.read(); if (done) break; size += value.byteLength; if (size > descriptor.size) throw new Error('完整查看器解压后大小超限'); chunks.push(value); }
  if (size !== descriptor.size) throw new Error('完整查看器内容不完整');
    } finally { await reader.cancel().catch(error => console.info('Viewer stream cleanup:', error.name)); reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  if (await digest(bytes) !== descriptor.sha256) throw new Error('完整查看器 SHA256 不匹配');
  const documentCopy = new DOMParser().parseFromString(new TextDecoder('utf-8', { fatal: true }).decode(bytes), 'text/html');
  const scripts = [...documentCopy.querySelectorAll('script')];
  if (scripts.length !== 2 || scripts.some(script => script.hasAttribute('src')) || documentCopy.querySelector('base, iframe, object, embed')) throw new Error('完整查看器结构不匹配');
  const prefix = 'window.BENCH_SNAPSHOT='; if (!scripts[0].textContent.startsWith(prefix)) throw new Error('完整数据标识不匹配');
  const data = JSON.parse(scripts[0].textContent.slice(prefix.length).trim().replace(/;$/, ''));
  if (data.format !== 'static-media-v2' || data.transport !== 'external' || data.runs.length !== seed.counts.full_runs) throw new Error('完整记录身份不匹配');
  if (await digest(new TextEncoder().encode(scripts[1].textContent)) !== descriptor.script_sha256) throw new Error('可信查看器代码摘要不匹配');
  const code = scripts[1].textContent; scripts.forEach(script => script.remove()); return { documentCopy, data, code };
  }
  function waitReady(test) {
  return new Promise((resolve, reject) => {
  const deadline = performance.now() + 15000;
  const check = () => { if (test()) resolve(); else if (performance.now() > deadline) reject(new Error('完整查看器初始化超时')); else requestAnimationFrame(check); }; check();
    });
  }
  async function mountFull() {
  if (fullPromise && !fullFetch?.signal.aborted) return fullPromise;
  const request = new AbortController(); fullFetch = request;
  let operation;
  operation = (async () => {
  const bundle = await readFull(request.signal);
  if (request.signal.aborted) throw new DOMException('Viewer load cancelled', 'AbortError');
  destroyPreview(); imageObserver.disconnect(); controller.abort();
  const oldBody = document.createDocumentFragment(); oldBody.append(...document.body.childNodes);
  const stylesheet = node('style', { id: 'full-viewer-css', text: [...bundle.documentCopy.querySelectorAll('style')].map(style => style.textContent).join('\n') });
  initialStyle.before(stylesheet);
  document.body.className = 'public-full'; document.body.append(...bundle.documentCopy.body.childNodes);
  window.BENCH_SNAPSHOT = bundle.data;
  try {
  const executable = document.createElement('script'); executable.textContent = bundle.code; document.body.append(executable);
  await waitReady(() => $('#workspace')?.getAttribute('aria-busy') === 'false' && $('#mode-label')?.textContent.includes('只读'));
  document.documentElement.dataset.fullViewerReady = 'true';
      } catch (error) {
  fatal = true; document.body.className = 'public-gallery'; document.body.replaceChildren(oldBody);
  document.querySelectorAll('button,input,select').forEach(control => { control.disabled = true; });
  const reload = node('button', { text: '重新加载恢复画廊' }); reload.addEventListener('click', () => location.reload());
  const message = node('p', { class: 'public-notice error', role: 'alert', text: `查看器初始化失败：${error.message}；请重新加载，勿重复执行。` });
  document.body.prepend(message, reload); throw error;
      }
    })().catch(error => { if (!fatal && fullPromise === operation) fullPromise = null; throw error; });
  fullPromise = operation; return operation;
  }
  async function fullAction(action) {
  const intent = ++fullIntent;
  const selected = [...state.selected], mixed = state.mixed;
  destroyPreview(); if ($('#original-dialog')?.open) $('#original-dialog').close();
  const url = new URL(location.href); url.searchParams.set('task_id', state.task); url.searchParams.set('purpose', state.purpose); url.searchParams.delete('model_query'); url.searchParams.delete('gallery_page');
  if (action.view === 'scores') { url.searchParams.set('score_source', state.source || ''); if (state.sort) url.searchParams.set('score_sort', state.sort); }
  if (state.model.trim() && !url.searchParams.get('q')) url.searchParams.set('q', state.model.trim());
  if (action.run) { const run = records.get(action.run); url.searchParams.set('task_id', run.task_id); url.searchParams.set('purpose', run.purpose); url.searchParams.set('q', run.id); }
  if (action.kind === 'compare') {
  if (selected.length !== 2 || !mixed) { notice('请选择同题两次，明确允许跨条件查看。', true); return; }
  url.searchParams.set('task_id', records.get(selected[0]).task_id); url.searchParams.set('purpose', '');
  for (const key of ['q', 'date_from', 'date_to', 'tool', 'model', 'prompt_version', 'status']) url.searchParams.delete(key);
    }
  url.hash = action.view || 'gallery';
  try {
  if (!action.route && !returnURL) { returnURL = location.href; history.pushState(null, '', url); }
  else history.replaceState(null, '', url);
    } catch (error) { notice('视图链接保存失败；继续装载。'); }
  notice('装载完整记录中；保留当前作品。');
  try {
  await mountFull(); if (intent !== fullIntent) return;
  returnURL = null;
  if (typeof window.BENCH_GALLERY_ACTION !== 'function') throw new Error('可信查看器缺少 handoff 接口');
  await window.BENCH_GALLERY_ACTION({ ...action, selected, mixed });
    } catch (error) {
  if (error.name === 'AbortError' || intent !== fullIntent) return;
  const target = $('#public-notice') || $('#notice');
  if (target && !fatal) { target.hidden = false; target.classList.add('error'); target.replaceChildren(document.createTextNode(`装载失败：${error.message} `)); const retry = node('button', { text: '重试此动作' }); retry.addEventListener('click', () => fullAction(action), { once: true }); target.append(retry); }
    }
  }
  function changeFilter(push = true) { cancelFull(); state.page = 0; syncURL(push); notice(); render(); }
  function bindUI() {
  const options = { signal: controller.signal };
  imageObserver = new IntersectionObserver(entries => entries.forEach(entry => { if (entry.isIntersecting) loadImage(entry.target); }), { rootMargin: '80px', threshold: .01 });
  document.addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button || button.disabled) return;
  if (button.dataset.taskId !== undefined) { state.task = button.dataset.taskId; changeFilter(); }
  else if (button.dataset.modelName !== undefined) { const name = button.dataset.modelName; state.model = state.model.trim().toLocaleLowerCase() === name.trim().toLocaleLowerCase() ? '' : name; changeFilter(); }
  else if (button.dataset.galleryPage !== undefined) { cancelFull(); state.page = Math.max(0, Number(button.dataset.galleryPage)); syncURL(true); render(); $('#public-grid').scrollIntoView({ block: 'start' }); observeImages(true); }
  else if (button.dataset.fullView) fullAction({ kind: 'view', view: button.dataset.fullView });
  else if (button.dataset.publicClose) { if (button.dataset.publicClose === 'original-dialog') destroyPreview(); $('#' + button.dataset.publicClose).close(); }
  else if (button.dataset.modelProfile) fullAction({ kind: 'model', view: 'matrix', model: button.dataset.modelProfile });
  else {
  const id = button.dataset.runOriginal || button.dataset.selectRun || button.dataset.zoomRun || button.dataset.detailRun;
  if (!idPattern.test(id || '') || !records.has(id)) return;
  const run = records.get(id);
  if (button.dataset.runOriginal) original(run);
  else if (button.dataset.selectRun) toggleRun(run);
  else if (button.dataset.zoomRun) zoom(run);
  else if (button.dataset.detailRun) fullAction({ kind: 'detail', view: 'evidence', run: id });
      }
    }, options);
  $('#public-purpose').addEventListener('change', event => { state.purpose = event.target.value; changeFilter(); }, options);
  $('#public-model').addEventListener('input', event => { state.model = event.target.value; changeFilter(false); }, options);
  $('#public-sort').addEventListener('change', event => { state.sort = event.target.value; changeFilter(); }, options);
  $('#public-score-source').replaceChildren(...scopes.map(scope => node('option', { value: JSON.stringify(scope), text: scopeLabel(scope) })));
  $('#public-score-source').addEventListener('change', event => { state.source = event.target.value; changeFilter(); }, options);
  $('#public-mixed').addEventListener('change', event => { state.mixed = event.target.checked; selection(); }, options);
  $('#public-history').addEventListener('toggle', populateHistory, options);
  $('#public-clear').addEventListener('click', () => { state.selected.clear(); state.mixed = false; $('#public-mixed').checked = false; selection(); }, options);
  $('#public-compare').addEventListener('click', () => fullAction({ kind: 'compare', view: 'gallery' }), options);
  $('#original-dialog').addEventListener('cancel', destroyPreview, options); $('#original-dialog').addEventListener('close', () => { destroyPreview(); $('#original-content').replaceChildren(); }, options);
  $('#public-zoom').addEventListener('close', () => { $('#public-zoom-image').removeAttribute('src'); }, options);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopPreview(); }, options);
  window.addEventListener('pagehide', () => { cancelFull(); destroyPreview(); }, options);
  window.addEventListener('scroll', () => observeImages(true), { ...options, passive: true });
  narrow.addEventListener('change', () => { state.page = 0; syncURL(); render(); }, options);
  window.addEventListener('popstate', () => { cancelFull(false); const advanced = readURL(); render(); if (advanced) fullAction({ kind: 'view', view: location.hash.slice(1) || 'gallery', route: true }); }, options);
  $('#public-theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('bench-theme', document.documentElement.dataset.theme); } catch (error) { console.info('Theme preference not persisted:', error.name); }
    }, options);
  }
  try { const theme = localStorage.getItem('bench-theme'); if (['light', 'dark'].includes(theme)) document.documentElement.dataset.theme = theme; } catch (error) { console.info('Theme preference unavailable:', error.name); }
  $('#public-offline').href = seed.offline.path;
  const advanced = readURL(); bindUI(); render(); document.documentElement.dataset.galleryReady = 'true';
  if (advanced) fullAction({ kind: 'view', view: location.hash.slice(1) || 'gallery', route: true });
})();
