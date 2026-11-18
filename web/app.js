'use strict';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const conversationId = params.get('conversationId') || '';
const token = params.get('token');
const tabs = [$('session-tab'), $('settings-tab')];
const presets = {
  default: [120],
  fast: [15, 25, 40, 60],
  balanced: [30, 45, 60, 90],
  patient: [60, 90, 120],
};
const phaseLabels = {
  idle: '待命',
  watching: '正在监测',
  tool_running: '正在执行工具',
  waiting: '等待自动恢复',
  resuming: '已续跑，等待输出',
  cancelled: '会话已停止',
  paused: '已暂停',
  limited: '已到重试上限',
  cooldown: '服务要求冷却',
  error: '需要手动处理',
  unavailable: '监测暂不可用',
};
const eventLabels = {
  resuming: '已续跑',
  paused: '暂停自动恢复',
  idle: '重新开启恢复',
};
let state;
let busy = false;
let dirty = false;
let processActive = false;
let refreshPending = null;
let processSignature = '';
let clockOffset = 0;
let noticeTimer;

function textElement(tagName, text, className) {
  const element = document.createElement(tagName);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function setText(id, text) {
  const element = $(id);
  if (element.textContent !== text) element.textContent = text;
}

function showNotice(text, ok = false) {
  clearTimeout(noticeTimer);
  setText('notice', text);
  $('notice').dataset.ok = String(ok);
  $('notice').hidden = !text;
  if (text && ok) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 3000);
}

function modelLabel(model) {
  const match = /^gemini-4-(carbon|hydrogen|argon)(?:-tiered)?-([a-z]+)$/.exec(model || '');
  if (!match) return model || '';
  const [family, effort] = match.slice(1).map((part) => part[0].toUpperCase() + part.slice(1));
  return 'Gemini 4 ' + family + ' ' + effort;
}

async function request(path, action) {
  const headers = { 'X-Sidecar-Token': token };
  const options = { headers, cache: 'no-store', signal: AbortSignal.timeout(10000) };
  if (action) {
    headers['Content-Type'] = 'application/json';
    options.method = 'POST';
    options.body = JSON.stringify(action);
  } else if (conversationId) {
    path += '?' + new URLSearchParams({ conversationId }).toString();
  }
  const response = await fetch(path, options);
  if (response.status === 403 || response.status === 401) {
    throw new Error('请在 Antigravity 侧栏重新打开 ant-extend。');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '操作暂未完成。');
  return data;
}

function showState() {
  const row = state.session;
  const attempts = row?.attempts || 0;
  const paused = state.paused || row?.paused;
  const phase = paused ? 'paused' : row?.phase || 'idle';
  const settings = state.settings;
  const windowLimited = phase === 'limited' && row.limitReason === 'window';
  const label = windowLimited ? '已到恢复时限' : phaseLabels[phase] || phaseLabels.idle;
  const status = state.enabled ? label : '自动恢复已关闭';
  setText('status', status);
  let tone = 'idle';
  if (state.enabled) {
    if (phase === 'error') tone = 'error';
    else if (['waiting', 'limited', 'cooldown', 'unavailable', 'paused'].includes(phase)) tone = 'attention';
    else if (row?.monitorActive) tone = 'active';
  }
  $('overview').dataset.tone = tone;

  setText('attempts', String(attempts));
  setText('limit', ' / ' + settings.maxRetries);
  $('model').hidden = !row?.model;
  setText('model', modelLabel(row?.model));

  $('settings-tab').disabled = false;
  const pause = $('session-pause');
  pause.hidden = !row;
  pause.disabled = busy || !state.enabled || state.paused;
  pause.dataset.paused = String(Boolean(row?.paused));
  pause.textContent = row?.paused ? '恢复本会话' : '暂停本会话';
  $('global-pause').disabled = busy || !state.enabled;
  setText('global-pause', state.paused ? '恢复全部' : '暂停全部');
  setText('global-status', !state.enabled ? '自动恢复已关闭'
    : state.paused ? '全部会话已暂停' : '全部会话自动恢复');

  let description = conversationId ? '新请求开始时自动监测。' : '打开会话后查看该轮重试。';
  if (!state.enabled || paused) {
    description = '当前请求可继续运行。';
  } else if (phase === 'waiting' && row.nextRetryAt) {
    const seconds = Math.max(0, Math.ceil(row.nextRetryAt - Date.now() / 1000 - clockOffset));
    description = '约 ' + seconds + ' 秒后继续同一会话。';
  } else if (phase === 'tool_running') {
    description = '等待工具完成或你确认。';
  } else if (phase === 'resuming') {
    description = '继续请求已提交。';
  } else if (phase === 'limited') {
    description = windowLimited ? '下一次重试已超出恢复时限。' : '本轮自动恢复次数已用完。';
  } else if (phase === 'cooldown') {
    description = '服务要求更长等待，本轮已停止短间隔重试。';
  } else if (phase === 'unavailable') {
    description = row.nativeFailure === 'unsupported_version'
      ? '当前版本尚未适配 Antigravity ' + row.nativeVersion + '。'
      : '与 Antigravity 的监测连接暂时断开。';
  } else if (phase === 'watching' && row.idleSeconds >= 5) {
    description = '已 ' + Math.floor(row.idleSeconds) + ' 秒没有新进展。';
  }
  setText('detail', description);
  $('carbon-hint').hidden = paused || !row?.model?.includes('carbon') || attempts < 2
    || !['waiting', 'limited', 'watching'].includes(phase)
    || (phase === 'watching' && row.idleSeconds < 10);

  const events = row?.events || [];
  $('history').hidden = events.length === 0;
  setText('history-count', String(events.length) + ' 条');
  if ($('history').open) showHistory(events);
  $('process').hidden = !row || !settings.progressView;
}

function showHistory(events) {
  $('history-content').replaceChildren(...events.slice().reverse().map((event) => {
    const line = document.createElement('li');
    line.dataset.phase = event.phase;
    const time = new Date(event.at * 1000).toLocaleTimeString('zh-CN', { hour12: false });
    let label = eventLabels[event.phase] || phaseLabels[event.phase] || '状态更新';
    if (event.source === 'migrated') label = '继承 ' + event.attempt + ' 次重试';
    else if (event.phase === 'waiting') label = '第 ' + event.attempt + ' 次，等待 ' + event.delaySeconds + ' 秒';
    line.append(textElement('time', time), textElement('span', label));
    return line;
  }));
}

async function refreshProcess() {
  const section = $('process');
  if ($('session-view').hidden || !section.open || section.hidden || !conversationId) return;
  const result = await request('/api/session');
  if (processActive && !result.active) section.open = false;
  processActive = Boolean(result.active);
  const signature = JSON.stringify(result);
  if (processSignature === signature) return;
  processSignature = signature;

  const content = $('process-content');
  content.replaceChildren();
  if (!result.available || !result.items.length) {
    const message = result.available ? '暂无额外过程。' : '暂时无法读取本轮过程。';
    content.append(textElement('p', message, 'caption'));
    return;
  }
  if (result.partial) content.append(textElement('p', '显示最近的步骤。', 'caption'));
  for (const item of result.items) {
    const row = document.createElement('div');
    row.className = 'activity';
    row.dataset.kind = item.kind;
    row.append(textElement('div', item.label, 'activity-label'));
    if (item.text) row.append(textElement('div', item.text, 'activity-text'));
    content.append(row);
  }
}

function updateDetectionField() {
  const custom = $('preset').value === 'custom';
  $('custom-detection').hidden = !custom;
  $('idle-timeouts').disabled = !custom;
}

function fillForm() {
  const settings = state.settings;
  $('early-recovery').checked = settings.earlyRecovery;
  $('progress-view').checked = settings.progressView;
  $('max-retries').value = settings.maxRetries;
  $('delays').value = settings.retryDelaysSeconds.join(', ');
  $('idle-timeouts').value = settings.idleTimeoutsSeconds.join(', ');
  $('preset').value = Object.keys(presets).find((key) =>
    presets[key].join(',') === settings.idleTimeoutsSeconds.join(',')) || 'custom';
  updateDetectionField();
  $('save').disabled = true;
}

function refresh() {
  if (refreshPending || busy) return refreshPending;
  refreshPending = request('/api/state')
    .then(async (next) => {
      state = next;
      clockOffset = state.now - Date.now() / 1000;
      showState();
      if (!dirty) fillForm();
      await refreshProcess();
    })
    .catch((error) => showNotice(error.message || '正在重新连接。'))
    .finally(() => { refreshPending = null; });
  return refreshPending;
}

async function act(payload) {
  if (busy || !state) return;
  const saving = payload.action === 'settings';
  busy = true;
  showState();
  $('save').disabled = true;
  try {
    if (saving) {
      $('settings-form').inert = true;
      await refreshPending;
    }
    await request('/api/action', payload);
    if (saving) {
      dirty = false;
      $('settings-actions').hidden = true;
    }
    showNotice(saving ? '设置已保存。' : '', true);
  } catch (error) {
    showNotice(error.message);
  } finally {
    busy = false;
    await refresh();
    $('settings-form').inert = false;
    $('save').disabled = !dirty;
    showState();
  }
}

function selectTab(selected) {
  for (const tab of tabs) {
    const active = tab === selected;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute('aria-controls')).hidden = !active;
  }
  if (selected === tabs[0]) void refresh();
}

for (const tab of tabs) tab.addEventListener('click', () => selectTab(tab));
$('view-tabs').addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  const tab = tabs.find((candidate) => candidate !== event.target && !candidate.disabled);
  if (!tab) return;
  event.preventDefault();
  selectTab(tab);
  tab.focus();
});

$('session-pause').addEventListener('click', () => {
  if (!state?.session) return;
  void act({ action: state.session.paused ? 'resume' : 'pause', conversationId });
});
$('global-pause').addEventListener('click', () => {
  if (!state) return;
  void act({ action: state.paused ? 'resume' : 'pause' });
});
$('history').addEventListener('toggle', () => {
  if ($('history').open) showHistory(state?.session?.events || []);
});
$('process').addEventListener('toggle', () => {
  if ($('process').open) refreshProcess().catch((error) => showNotice(error.message));
});
$('settings-form').addEventListener('input', (event) => {
  if (event.target === $('preset')) updateDetectionField();
  dirty = true;
  $('settings-actions').hidden = false;
  $('save').disabled = false;
});

function scheduleValue(id) {
  return $(id).value.split(/[,，、\s]+/).filter(Boolean).map(Number);
}

$('settings-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const settings = {
    earlyRecovery: $('early-recovery').checked,
    progressView: $('progress-view').checked,
    maxRetries: Number($('max-retries').value),
    idleTimeoutsSeconds: presets[$('preset').value] || scheduleValue('idle-timeouts'),
    retryDelaysSeconds: scheduleValue('delays'),
  };
  void act({ action: 'settings', settings });
});

async function tick() {
  if (!document.hidden || !state) await refresh();
  setTimeout(tick, document.hidden ? 8000 : 2000);
}

if (token) {
  void tick();
} else {
  setText('status', '在 Antigravity 内打开');
  setText('detail', '从会话右侧的 Extensions 中选择 ant-extend。');
  $('view-tabs').hidden = true;
}
