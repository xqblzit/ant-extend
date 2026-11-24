'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';
const RING_CX = 52;
const RING_CY = 52;
const RING_RADIUS = 41;
const RING_CIRC = 2 * Math.PI * RING_RADIUS;
const SPARK_LEFT = 4;
const SPARK_RIGHT = 152;
const SPARK_TOP = 6;
const SPARK_BOTTOM = 50;

const monitorNumber = new Intl.NumberFormat('zh-CN');
const monitorPercent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 });
const monitorClock = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
const monitorStates = {
  idle: '待命', generating: '正在生成', tool: '执行工具', approval: '等待确认',
  running: '运行中', unknown: '运行状态未识别',
};
const monitorCategories = {
  CHAT_MESSAGES: '聊天历史', SYSTEM_PROMPT: '系统提示词', TOOLS: '内置工具',
  RULES: '规则', SKILLS: '技能', SUBAGENTS: '子代理定义', PLUGINS: '插件',
  MCP_TOOLS: 'MCP 工具', FLOWS: '工作流', WORKFLOWS: '旧版工作流', SYSTEM_MESSAGES: '系统消息',
};
const monitorStepNames = {
  USER_INPUT: '用户输入', PLANNER_RESPONSE: '模型生成', SYSTEM_MESSAGE: '系统消息',
  CHECKPOINT: '压缩摘要', ERROR_MESSAGE: '错误信息', GENERIC: '通用步骤',
  RUN_COMMAND: '运行命令', VIEW_FILE: '读取文件', READ_FILE: '读取文件',
  CODE_ACTION: '文件操作', MCP_TOOL: 'MCP 工具', MCP_TOOL_CALL: 'MCP 工具',
};

let monitorPending = false;
let monitorWindowSignature = '';
let monitorDetail = null;
let lastWindowSample = null;
let lastConnection = '';
let monitorViewMode = 'list';
let selectedTreemapKey = null;
let hasUserToggledGroup = false;
let activeHoverKey = null;
let seededFromRequest = false;
const historySeries = [];
const openMonitorGroups = new Set();

function svgEl(tagName, attrs) {
  const node = document.createElementNS(SVG_NS, tagName);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, String(value));
    }
  }
  return node;
}

function setHidden(element, hidden) {
  if (!element) return;
  element.classList.toggle('is-hidden', Boolean(hidden));
  if (hidden) element.setAttribute('hidden', '');
  else element.removeAttribute('hidden');
}

function formatTokenCount(value) {
  return Number.isSafeInteger(value) ? monitorNumber.format(value) : '—';
}

function formatCompactToken(value) {
  if (!Number.isSafeInteger(value)) return '—';
  if (value < 1000) return String(value);
  return (value / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
}

function groupTitle(group) {
  return monitorCategories[group.type] || group.name || '未分类';
}

function groupHoverKey(group) {
  return group.type ? group.type : 'NAME:' + (group.name || '');
}

function groupSwatch(type) {
  const swatch = textElement('span', '', 'monitor-swatch');
  swatch.dataset.group = type;
  swatch.setAttribute('aria-hidden', 'true');
  return swatch;
}

function orderedByTokens(rows) {
  return rows.slice().sort((a, b) => (b.tokens ?? -1) - (a.tokens ?? -1));
}

function attachHoverData(el, group, shareDenominator, usedTotal) {
  const title = groupTitle(group);
  const tokensText = formatTokenCount(group.tokens);
  const winPct = shareDenominator > 0 && Number.isSafeInteger(group.tokens)
    ? monitorPercent.format(group.tokens / shareDenominator)
    : '—';
  const usedPct = usedTotal > 0 && Number.isSafeInteger(group.tokens)
    ? monitorPercent.format(group.tokens / usedTotal)
    : '';
  el.dataset.group = group.type || '';
  el.dataset.hoverKey = groupHoverKey(group);
  el.dataset.catTitle = title;
  el.dataset.catTokens = tokensText;
  el.dataset.catPct = winPct;
  el.dataset.catUsedPct = usedPct;
}

function applyChartHover(key, groupType, title, tokensText, winPct, usedPct) {
  activeHoverKey = key || null;
  const view = $('monitor-view');
  if (!view) return;

  for (const el of view.querySelectorAll('.ring-segment, .storage-seg')) {
    const match = el.dataset.hoverKey === activeHoverKey;
    el.classList.toggle('is-dimmed', Boolean(activeHoverKey && !match));
    el.classList.toggle('is-active', Boolean(activeHoverKey && match));
  }
  for (const el of view.querySelectorAll('.monitor-line, .monitor-group-detail > summary, .tm-tile')) {
    el.classList.toggle('is-highlighted', Boolean(activeHoverKey && el.dataset.hoverKey === activeHoverKey));
  }

  const defCenter = $('monitor-ring-center-default');
  const hovCenter = $('monitor-ring-center-hover');
  const inspect = $('monitor-inspect');

  if (activeHoverKey) {
    setHidden(defCenter, true);
    setHidden(hovCenter, false);
    setText('monitor-hover-pct', winPct || '—');
    setText('monitor-hover-label', title.length > 5 ? title.slice(0, 5) : title);
    $('monitor-inspect-dot').dataset.group = groupType || '';
    const detailParts = [title, tokensText + ' Token', '占窗口 ' + winPct];
    if (usedPct) detailParts.push('占已用 ' + usedPct);
    setText('monitor-inspect-text', detailParts.join(' · '));
    inspect.hidden = false;
  } else {
    setHidden(defCenter, false);
    setHidden(hovCenter, true);
    inspect.hidden = true;
  }
}

function renderRing(sample, occupancy, groups, shareDenominator, connection, requestSample) {
  const view = $('monitor-view');
  const windowCard = $('monitor-window');
  const ring = $('monitor-ring');
  const segmentsEl = $('monitor-ring-segments');
  const reqDot = $('monitor-ring-req');
  const health = $('monitor-health');
  const deltaBadge = $('monitor-delta');

  ring.dataset.available = String(occupancy !== null);
  let pressure = 'normal';
  let healthText = '';
  if (occupancy !== null) {
    if (occupancy >= 0.85) {
      pressure = 'high';
      healthText = '紧张';
    } else if (occupancy >= 0.6) {
      pressure = 'elevated';
      healthText = '偏高';
    } else {
      healthText = '健康';
    }
  }
  view.dataset.pressure = pressure;
  windowCard.dataset.pressure = pressure;
  setText('monitor-health', healthText);
  health.hidden = !healthText;

  if (!(occupancy > 0)) {
    segmentsEl.replaceChildren();
  } else {
    const denominator = Math.max(sample.limit, sample.used);
    const visible = sample.coverage === 'complete' && groups.length > 0
      ? groups.filter((group) => Number.isSafeInteger(group.tokens) && group.tokens > 0)
      : [{ type: 'TOTAL', name: '已占用', tokens: sample.used }];
    let cursor = 0;
    const circles = [];
    for (const group of visible) {
      const rawLen = group.tokens / denominator * RING_CIRC;
      if (rawLen <= 0) continue;
      const gap = visible.length > 1 ? Math.min(2.4, rawLen * 0.3) : 0;
      const segLen = Math.max(0.8, rawLen - gap);
      const segStart = cursor + gap / 2;
      cursor = Math.min(RING_CIRC, cursor + rawLen);
      const circle = svgEl('circle', {
        class: 'ring-segment',
        cx: RING_CX,
        cy: RING_CY,
        r: RING_RADIUS,
        tabindex: '0',
        'stroke-dasharray': segLen.toFixed(2) + ' ' + RING_CIRC.toFixed(2),
        'stroke-dashoffset': (-segStart).toFixed(2),
      });
      attachHoverData(circle, group, shareDenominator, sample.used);
      circles.push(circle);
    }
    segmentsEl.replaceChildren(...circles);
  }

  const hasReqUsed = Number.isSafeInteger(requestSample?.used);
  if (connection === 'ready' && occupancy !== null && hasReqUsed && sample.limit > 0) {
    const reqRatio = Math.min(1, Math.max(0, requestSample.used / sample.limit));
    const angle = -Math.PI / 2 + reqRatio * 2 * Math.PI;
    reqDot.setAttribute('cx', (RING_CX + RING_RADIUS * Math.cos(angle)).toFixed(2));
    reqDot.setAttribute('cy', (RING_CY + RING_RADIUS * Math.sin(angle)).toFixed(2));
    setHidden(reqDot, false);

    const deltaPts = Math.round((sample.used - requestSample.used) / sample.limit * 100);
    const deltaTokens = sample.used - requestSample.used;
    let deltaText = '与上次请求持平';
    if (deltaPts > 0) deltaText = '↑ +' + deltaPts + '% · +' + formatCompactToken(deltaTokens);
    else if (deltaPts < 0) deltaText = '↓ ' + deltaPts + '% · -' + formatCompactToken(Math.abs(deltaTokens));
    else if (deltaTokens > 0) deltaText = '↑ +' + formatCompactToken(deltaTokens);
    else if (deltaTokens < 0) deltaText = '↓ -' + formatCompactToken(Math.abs(deltaTokens));
    setText('monitor-delta', deltaText);
    deltaBadge.title = '最近请求 ' + formatTokenCount(requestSample.used) + ' / 当前 ' + formatTokenCount(sample.used) + ' Token';
    deltaBadge.hidden = false;
  } else {
    setHidden(reqDot, true);
    deltaBadge.hidden = true;
  }

  setText('monitor-percent', occupancy === null ? '—' : monitorPercent.format(occupancy));
  setText('monitor-ring-sub', occupancy === null ? '等待数据' : '已占用');
}

function renderStorageBar(sample, occupancy, groups, shareDenominator) {
  const bar = $('monitor-bar');
  bar.dataset.available = String(occupancy !== null);
  if (!(occupancy > 0)) {
    bar.replaceChildren();
    return;
  }
  const denominator = Math.max(sample.limit, sample.used);
  const visible = sample.coverage === 'complete' && groups.length > 0
    ? groups.filter((group) => Number.isSafeInteger(group.tokens) && group.tokens > 0)
    : [{ type: 'TOTAL', name: '已占用', tokens: sample.used }];
  bar.replaceChildren(...visible.map((group) => {
    const seg = textElement('span', '', 'storage-seg');
    seg.tabIndex = 0;
    seg.style.flexBasis = (group.tokens / denominator * 100).toFixed(2) + '%';
    attachHoverData(seg, group, shareDenominator, sample.used);
    return seg;
  }));
}

function updateAndRenderSparkline(sample, occupancy, connection, requestSample) {
  const line = $('monitor-spark-line');
  const area = $('monitor-spark-area');
  const dot = $('monitor-spark-dot');
  const halo = $('monitor-spark-halo');

  if (connection !== 'ready' || occupancy === null) {
    if (connection === 'no_conversation') {
      historySeries.length = 0;
      seededFromRequest = false;
    }
    line.removeAttribute('d');
    area.removeAttribute('d');
    setHidden(dot, true);
    setHidden(halo, true);
    setText('monitor-spark-badge', '—');
    setText('monitor-spark-foot', connection === 'no_conversation' ? '打开会话后采样' : '等待采样');
    return;
  }

  const cur = Math.min(1, Math.max(0, occupancy));
  const hasReq = Number.isSafeInteger(requestSample?.used) && sample.limit > 0;
  const req = hasReq ? Math.min(1, Math.max(0, requestSample.used / sample.limit)) : null;

  if (historySeries.length === 0) {
    if (req !== null) {
      historySeries.push(req, cur);
      seededFromRequest = true;
    } else {
      historySeries.push(cur);
    }
  } else {
    if (!seededFromRequest && req !== null && historySeries.length === 1) {
      historySeries.unshift(req);
      seededFromRequest = true;
    }
    const last = historySeries[historySeries.length - 1];
    if (Math.abs(cur - last) > 0.0005) {
      historySeries.push(cur);
      if (historySeries.length > 20) historySeries.shift();
    }
  }

  const height = SPARK_BOTTOM - SPARK_TOP;
  let lineD = '';
  let areaD = '';
  let lastX = SPARK_RIGHT;
  let lastY = SPARK_BOTTOM - cur * height;

  if (historySeries.length === 1) {
    const yStr = lastY.toFixed(1);
    lineD = 'M ' + SPARK_LEFT + ' ' + yStr + ' L ' + SPARK_RIGHT + ' ' + yStr;
    areaD = lineD + ' L ' + SPARK_RIGHT + ' ' + SPARK_BOTTOM + ' L ' + SPARK_LEFT + ' ' + SPARK_BOTTOM + ' Z';
  } else {
    const pts = historySeries.map((val, i) => ({
      x: SPARK_LEFT + i / (historySeries.length - 1) * (SPARK_RIGHT - SPARK_LEFT),
      y: SPARK_BOTTOM - val * height,
    }));
    lineD = 'M ' + pts[0].x.toFixed(1) + ' ' + pts[0].y.toFixed(1);
    for (let i = 1; i < pts.length; i++) {
      const mx = ((pts[i - 1].x + pts[i].x) / 2).toFixed(1);
      lineD += ' C ' + mx + ' ' + pts[i - 1].y.toFixed(1) + ', ' + mx + ' ' + pts[i].y.toFixed(1)
        + ', ' + pts[i].x.toFixed(1) + ' ' + pts[i].y.toFixed(1);
    }
    const lastPt = pts[pts.length - 1];
    lastX = lastPt.x;
    lastY = lastPt.y;
    areaD = lineD + ' L ' + lastPt.x.toFixed(1) + ' ' + SPARK_BOTTOM
      + ' L ' + pts[0].x.toFixed(1) + ' ' + SPARK_BOTTOM + ' Z';
  }

  line.setAttribute('d', lineD);
  area.setAttribute('d', areaD);
  dot.setAttribute('cx', lastX.toFixed(1));
  dot.setAttribute('cy', lastY.toFixed(1));
  halo.setAttribute('cx', lastX.toFixed(1));
  halo.setAttribute('cy', lastY.toFixed(1));
  setHidden(dot, false);
  setHidden(halo, false);

  let badge = monitorPercent.format(cur);
  if (historySeries.length >= 2) {
    const diffPts = Math.round((historySeries[historySeries.length - 1] - historySeries[0]) * 100);
    badge = diffPts > 0 ? '+' + diffPts + '%' : diffPts < 0 ? diffPts + '%' : '平稳';
  }
  setText('monitor-spark-badge', badge);
  const peak = Math.round(Math.max(...historySeries) * 100);
  setText('monitor-spark-foot', '峰值 ' + peak + '% · 警戒 85%');
}

function renderStructuralSplit(sample, groups) {
  const hasUsed = Number.isSafeInteger(sample?.used);
  const hasLimit = Number.isSafeInteger(sample?.limit) && sample.limit > 0;
  const complete = sample?.coverage === 'complete' && hasLimit && groups.length > 0;

  if (complete) {
    let chatTokens = 0;
    let baseTokens = 0;
    for (const group of groups) {
      const val = Number.isSafeInteger(group.tokens) ? group.tokens : 0;
      if (group.type === 'CHAT_MESSAGES') chatTokens += val;
      else baseTokens += val;
    }
    const freeTokens = Math.max(0, sample.limit - sample.used);
    const chatRatio = chatTokens / sample.limit;
    const baseRatio = baseTokens / sample.limit;
    const freeRatio = freeTokens / sample.limit;

    setText('split-chat-val', monitorPercent.format(chatRatio));
    setText('split-base-val', monitorPercent.format(baseRatio));
    setText('split-free-val', monitorPercent.format(freeRatio));
    $('split-chat-bar').style.width = Math.min(100, chatRatio * 100).toFixed(1) + '%';
    $('split-base-bar').style.width = Math.min(100, baseRatio * 100).toFixed(1) + '%';
    $('split-free-bar').style.width = Math.min(100, freeRatio * 100).toFixed(1) + '%';
    setText('monitor-split-badge', formatCompactToken(chatTokens + baseTokens));
  } else if (hasUsed && hasLimit) {
    const freeRatio = Math.max(0, sample.limit - sample.used) / sample.limit;
    setText('split-chat-val', '—');
    setText('split-base-val', '—');
    setText('split-free-val', monitorPercent.format(freeRatio));
    $('split-chat-bar').style.width = '0%';
    $('split-base-bar').style.width = '0%';
    $('split-free-bar').style.width = Math.min(100, freeRatio * 100).toFixed(1) + '%';
    setText('monitor-split-badge', '部分数据');
  } else {
    setText('split-chat-val', '—');
    setText('split-base-val', '—');
    setText('split-free-val', '—');
    $('split-chat-bar').style.width = '0%';
    $('split-base-bar').style.width = '0%';
    $('split-free-bar').style.width = '0%';
    setText('monitor-split-badge', '三层占比');
  }
}

function buildWashFill(tokens, maxTokens, shareDenominator, type) {
  const fill = textElement('span', '', 'monitor-row-fill');
  fill.dataset.group = type || '';
  fill.setAttribute('aria-hidden', 'true');
  let pct = 0;
  if (Number.isSafeInteger(tokens) && tokens > 0) {
    if (shareDenominator > 0) pct = Math.max(3, Math.min(100, tokens / shareDenominator * 100));
    else if (maxTokens > 0) pct = Math.max(3, Math.min(100, tokens / maxTokens * 100));
  }
  fill.style.width = pct.toFixed(1) + '%';
  return fill;
}

function formatChildName(group, item) {
  if (group.type === 'CHAT_MESSAGES') {
    const m = /^(\d+): (?:CORTEX_STEP_TYPE_)?([A-Z0-9_]+)$/.exec(item.name);
    if (m) return '步骤 ' + m[1] + ' · ' + (monitorStepNames[m[2]] || m[2]);
  }
  return item.name || '未命名项目';
}

function buildSubBar(group, rows) {
  const validRows = rows.filter((r) => Number.isSafeInteger(r.tokens) && r.tokens > 0);
  if (validRows.length === 0) return null;
  const childSum = validRows.reduce((sum, r) => sum + r.tokens, 0);
  const denom = Number.isSafeInteger(group.tokens) && group.tokens > 0
    ? Math.max(group.tokens, childSum)
    : childSum;
  if (!(denom > 0)) return null;

  const bar = textElement('div', '', 'monitor-sub-bar');
  bar.setAttribute('aria-hidden', 'true');
  const top = validRows.slice(0, 4);
  const restTokens = validRows.slice(4).reduce((sum, r) => sum + r.tokens, 0);
  top.forEach((item, idx) => {
    const seg = textElement('span', '', 'monitor-sub-seg sub-op-' + Math.min(idx, 3));
    seg.dataset.group = group.type;
    seg.style.width = (item.tokens / denom * 100).toFixed(2) + '%';
    bar.append(seg);
  });
  if (restTokens > 0) {
    const restSeg = textElement('span', '', 'monitor-sub-seg sub-op-rest');
    restSeg.dataset.group = group.type;
    restSeg.style.width = (restTokens / denom * 100).toFixed(2) + '%';
    bar.append(restSeg);
  }
  return bar;
}

function populateGroupDetail(container, group, shareDenominator) {
  const rows = orderedByTokens(group.children || []);
  if (!rows.length) {
    container.append(textElement('p', '宿主暂未返回此分类的逐项明细。', 'monitor-note'));
    return;
  }

  const subBar = buildSubBar(group, rows);
  if (subBar) container.append(subBar);

  const showShares = Boolean(shareDenominator);
  const list = textElement('ul', '', 'monitor-list monitor-item-list');
  list.dataset.shares = String(showShares);
  const more = textElement('button', '', 'quiet-button monitor-more');
  more.type = 'button';
  let shown = 0;

  function appendPage() {
    for (const item of rows.slice(shown, shown + 25)) {
      const line = textElement('li', '', 'monitor-item');
      const label = textElement('span', '', 'monitor-item-label');
      const pip = textElement('span', '', 'monitor-item-pip');
      pip.dataset.group = group.type;
      pip.setAttribute('aria-hidden', 'true');
      label.append(pip, textElement('span', formatChildName(group, item), 'monitor-line-name'));
      line.append(label, textElement('span', formatTokenCount(item.tokens), 'monitor-line-value'));
      if (showShares) {
        const shareText = Number.isSafeInteger(item.tokens)
          ? monitorPercent.format(item.tokens / shareDenominator)
          : '—';
        line.append(textElement('span', shareText, 'monitor-share'));
      }
      list.append(line);
    }
    shown = list.childElementCount;
    more.hidden = shown === rows.length;
    more.textContent = '显示更多（剩余 ' + formatTokenCount(rows.length - shown) + ' 项）';
  }

  container.append(list, more);
  more.addEventListener('click', appendPage);
  appendPage();
  if (group.partial) container.append(textElement('p', '宿主返回的逐项数据未覆盖此分类总量。', 'monitor-note'));
}

function renderTreemapInspector(inspector, sample, group, shareDenominator, isRequestMode) {
  if (!group) {
    inspector.hidden = true;
    inspector.replaceChildren();
    return;
  }
  inspector.hidden = false;
  const head = textElement('div', '', 'treemap-inspector-head');
  const titleWrap = textElement('div', '', 'treemap-inspector-title');
  titleWrap.append(groupSwatch(group.type), textElement('span', groupTitle(group)));
  const meta = textElement('div', '', 'treemap-inspector-meta');
  meta.append(textElement('strong', formatTokenCount(group.tokens)));
  if (shareDenominator > 0 && Number.isSafeInteger(group.tokens)) {
    meta.append(textElement('span', monitorPercent.format(group.tokens / shareDenominator)));
  }
  head.append(titleWrap, meta);

  const body = textElement('div', '', 'treemap-inspector-body');
  if (group.children && group.children.length > 0) {
    populateGroupDetail(body, group, shareDenominator);
  } else {
    const shareText = shareDenominator > 0 && Number.isSafeInteger(group.tokens)
      ? '占窗口 ' + monitorPercent.format(group.tokens / shareDenominator)
      : '占比未知';
    const usedText = sample?.used > 0 && Number.isSafeInteger(group.tokens)
      ? ' · 占已用 ' + monitorPercent.format(group.tokens / sample.used)
      : '';
    const hint = isRequestMode ? '宿主未返回此项的子条目。' : '切换至「最近请求」可展开逐项明细。';
    body.append(textElement('p', shareText + usedText + ' · ' + hint, 'monitor-note'));
  }
  inspector.replaceChildren(head, body);
}

function createTreemapTile(group, sample, shareDenominator, compact, primary) {
  const key = groupHoverKey(group);
  const tile = document.createElement('button');
  tile.type = 'button';
  tile.className = 'tm-tile' + (compact ? ' tm-tile-compact' : '');
  if (selectedTreemapKey === key) tile.classList.add('is-selected');
  attachHoverData(tile, group, shareDenominator, sample?.used || 0);

  const head = textElement('div', '', 'tm-tile-head');
  head.append(
    textElement('span', groupTitle(group), 'tm-tile-title'),
    textElement('span', tile.dataset.catPct, 'tm-tile-pct')
  );
  const tokensLine = textElement('div', formatCompactToken(group.tokens) + ' Token', 'tm-tile-tokens');

  if (primary && group.children && group.children.length > 0) {
    const topWrap = document.createElement('div');
    topWrap.append(head, tokensLine);
    const childrenWrap = textElement('div', '', 'tm-tile-children');
    const subBar = buildSubBar(group, orderedByTokens(group.children));
    if (subBar) childrenWrap.append(subBar);
    const legend = textElement('div', '', 'tm-child-legend');
    orderedByTokens(group.children).slice(0, 3).forEach((ch, idx) => {
      const row = textElement('div', '', 'tm-child-row');
      const nameWrap = textElement('span', '', 'tm-child-name');
      const pip = textElement('span', '', 'monitor-item-pip sub-op-' + Math.min(idx, 3));
      pip.dataset.group = group.type;
      nameWrap.append(pip, textElement('span', formatChildName(group, ch)));
      const pctStr = shareDenominator > 0 && Number.isSafeInteger(ch.tokens)
        ? monitorPercent.format(ch.tokens / shareDenominator)
        : formatCompactToken(ch.tokens);
      row.append(nameWrap, textElement('span', pctStr, 'tm-child-pct'));
      legend.append(row);
    });
    childrenWrap.append(legend);
    tile.append(topWrap, childrenWrap);
  } else {
    tile.append(head, tokensLine);
  }
  return tile;
}

function renderTreemap(box, inspector, sample, rows, shareDenominator, isRequestMode) {
  if (!rows.length) {
    box.hidden = true;
    box.replaceChildren();
    inspector.hidden = true;
    inspector.replaceChildren();
    return;
  }
  box.hidden = false;
  if (!selectedTreemapKey || rows.every((g) => groupHoverKey(g) !== selectedTreemapKey)) {
    selectedTreemapKey = groupHoverKey(rows[0]);
  }

  const totalTokens = rows.reduce((sum, g) => sum + (Number.isSafeInteger(g.tokens) && g.tokens > 0 ? g.tokens : 0), 0) || 1;
  const primary = rows[0];
  const rest = rows.slice(1);
  const nodes = [];

  const leftTile = createTreemapTile(primary, sample, shareDenominator, false, true);
  if (rest.length === 0) {
    leftTile.style.width = '100%';
    leftTile.style.height = '100%';
    nodes.push(leftTile);
  } else {
    const leftPct = Math.max(48, Math.min(64, Math.round((primary.tokens || 0) / totalTokens * 100)));
    leftTile.style.width = leftPct + '%';
    leftTile.style.height = '100%';
    leftTile.style.flexShrink = '0';
    nodes.push(leftTile);

    const rightCol = textElement('div', '', 'tm-col');
    const rowSlices = [rest.slice(0, 1), rest.slice(1, 3), rest.slice(3, 6), rest.slice(6)].filter((s) => s.length > 0);
    for (const slice of rowSlices) {
      const rowEl = textElement('div', '', 'tm-row');
      const rowSum = slice.reduce((sum, g) => sum + (Number.isSafeInteger(g.tokens) && g.tokens > 0 ? g.tokens : 1), 0);
      rowEl.style.flexGrow = String(Math.max(1, rowSum));
      rowEl.style.flexBasis = '0';
      const compact = slice.length >= 2;
      for (const item of slice) {
        const tile = createTreemapTile(item, sample, shareDenominator, compact, false);
        tile.style.flexGrow = String(Math.max(1, Number.isSafeInteger(item.tokens) && item.tokens > 0 ? item.tokens : 1));
        tile.style.flexBasis = '0';
        rowEl.append(tile);
      }
      rightCol.append(rowEl);
    }
    nodes.push(rightCol);
  }

  box.replaceChildren(...nodes);

  box.onclick = (event) => {
    const tile = event.target.closest('.tm-tile');
    if (!tile || !box.contains(tile)) return;
    selectedTreemapKey = tile.dataset.hoverKey;
    for (const el of box.querySelectorAll('.tm-tile')) {
      el.classList.toggle('is-selected', el.dataset.hoverKey === selectedTreemapKey);
    }
    const chosen = rows.find((g) => groupHoverKey(g) === selectedTreemapKey) || rows[0];
    renderTreemapInspector(inspector, sample, chosen, shareDenominator, isRequestMode);
  };

  const chosen = rows.find((g) => groupHoverKey(g) === selectedTreemapKey) || rows[0];
  renderTreemapInspector(inspector, sample, chosen, shareDenominator, isRequestMode);
}

function updateScopeSummary(sample, isRequestMode) {
  const el = $('monitor-scope-summary');
  if (!el) return;
  const target = isRequestMode ? sample : lastWindowSample;
  if (!target || !Number.isSafeInteger(target.used)) {
    el.hidden = true;
    return;
  }
  const hasLimit = Number.isSafeInteger(target.limit) && target.limit > 0;
  const text = formatCompactToken(target.used) + (hasLimit ? ' · ' + monitorPercent.format(target.used / target.limit) : '');
  setText('monitor-scope-summary', text);
  el.hidden = false;
}

function renderMonitorWindow(sample, connection) {
  const reqSample = monitorDetail?.sample || null;
  const signature = JSON.stringify([sample, connection, reqSample?.used ?? null, monitorViewMode, selectedTreemapKey]);
  if (signature === monitorWindowSignature) return;
  monitorWindowSignature = signature;
  lastWindowSample = sample || null;
  lastConnection = connection || '';

  const hasUsed = Number.isSafeInteger(sample?.used);
  const hasLimit = Number.isSafeInteger(sample?.limit) && sample.limit > 0;
  const occupancy = hasUsed && hasLimit ? sample.used / sample.limit : null;

  setText('monitor-used', formatTokenCount(sample?.used));
  setText('monitor-limit', formatTokenCount(sample?.limit));
  setText('monitor-free', formatTokenCount(occupancy === null ? null : Math.max(0, sample.limit - sample.used)));

  let message = '';
  if (connection === 'no_conversation') message = '打开一个会话后查看。';
  else if (connection !== 'ready') message = '暂时无法读取宿主数据。';
  else if (!hasUsed && !hasLimit) message = '宿主暂未返回上下文数据。';
  else if (!hasUsed) message = '宿主暂未返回已用量。';
  else if (!hasLimit) message = '宿主暂未返回窗口上限。';
  setText('monitor-window-note', message);
  $('monitor-window-note').hidden = !message;

  const groups = orderedByTokens(sample?.groups || []);
  const shareDenominator = hasLimit
    ? sample.limit
    : (sample?.coverage === 'complete' && sample.used > 0 ? sample.used : null);
  const showShares = Boolean(shareDenominator);

  renderRing(sample, occupancy, groups, shareDenominator, connection, reqSample);
  renderStorageBar(sample, occupancy, groups, shareDenominator);
  updateAndRenderSparkline(sample, occupancy, connection, reqSample);
  renderStructuralSplit(sample, groups);

  $('monitor-current').dataset.shares = String(showShares);
  const maxGroupTokens = Math.max(1, ...groups.map((g) => Number.isSafeInteger(g.tokens) ? g.tokens : 0));

  const listWrap = $('monitor-current-list-wrap');
  const mapWrap = $('monitor-current-map-wrap');
  listWrap.hidden = monitorViewMode !== 'list' || groups.length === 0;
  mapWrap.hidden = monitorViewMode !== 'map' || groups.length === 0;

  if (monitorViewMode === 'list') {
    const listEl = $('monitor-groups');
    listEl.classList.toggle('monitor-list-shell', groups.length > 0);
    listEl.replaceChildren(...groups.map((group) => {
      const line = textElement('li', '', 'monitor-line');
      attachHoverData(line, group, shareDenominator, sample?.used || 0);

      const left = textElement('span', '', 'monitor-row-left');
      left.append(groupSwatch(group.type), textElement('span', groupTitle(group), 'monitor-line-name'));

      const right = textElement('span', '', 'monitor-row-right');
      right.append(textElement('strong', line.dataset.catTokens, 'monitor-line-value'));
      if (showShares) {
        const share = textElement('span', line.dataset.catPct, 'monitor-share');
        share.title = hasLimit ? '占窗口上限的比例' : '占已用 Token 的比例';
        right.append(share);
      }
      line.append(buildWashFill(group.tokens, maxGroupTokens, shareDenominator, group.type), left, right);
      return line;
    }));
  } else {
    renderTreemap($('monitor-current-treemap'), $('monitor-current-inspector'), sample, groups, shareDenominator, false);
  }

  if ($('monitor-request').hidden) updateScopeSummary(sample, false);

  const groupNote = groups.length === 0 ? '暂无宿主分类数据。' : sample.coverage === 'partial'
    ? '部分分类数据缺失，或分类合计与宿主总量有差异。' : '';
  setText('monitor-groups-note', groupNote);
  $('monitor-groups-note').hidden = !groupNote;
  if (activeHoverKey) applyChartHover(null);
}

function renderMonitorStatus(report) {
  const connection = report.connection;
  const phase = report.runtime?.phase || 'unknown';
  const status = connection === 'ready' ? monitorStates[phase] || monitorStates.unknown
    : connection === 'no_conversation' ? '未关联会话' : '状态暂不可用';
  $('monitor-status').dataset.phase = phase;
  setText('monitor-phase', status);
  const tool = report.runtime?.tool;
  $('monitor-tool').hidden = !tool;
  setText('monitor-tool', tool ? '工具: ' + tool : '');
  const when = $('monitor-time');
  $('monitor-footer').hidden = !report.sampledAt;
  if (report.sampledAt) {
    const sampled = new Date(report.sampledAt * 1000);
    when.dateTime = sampled.toISOString();
    when.title = '最近读取宿主数据';
    setText('monitor-time', monitorClock.format(sampled));
  }
  renderMonitorWindow(report.window, connection);
}

function monitorGroupDetail(group, index, maxTokens, shareDenominator, usedTotal) {
  const panel = textElement('details', '', 'monitor-group-detail');
  const key = (group.type || '') + '/' + (group.name || '');
  if (openMonitorGroups.size === 0 && !hasUserToggledGroup && index === 0) {
    openMonitorGroups.add(key);
  }
  const showShares = Boolean(shareDenominator);

  const summary = document.createElement('summary');
  attachHoverData(summary, group, shareDenominator, usedTotal);

  const left = textElement('span', '', 'monitor-row-left');
  left.append(groupSwatch(group.type), textElement('span', groupTitle(group), 'monitor-line-name'));
  if (group.children && group.children.length > 0) {
    left.append(textElement('span', String(group.children.length), 'monitor-child-count'));
  }

  const right = textElement('span', '', 'monitor-row-right');
  right.append(textElement('span', summary.dataset.catTokens, 'monitor-line-value'));
  if (showShares) right.append(textElement('span', summary.dataset.catPct, 'monitor-share'));

  summary.append(buildWashFill(group.tokens, maxTokens, shareDenominator, group.type), left, right);
  panel.append(summary);

  summary.addEventListener('click', () => {
    hasUserToggledGroup = true;
  });
  panel.addEventListener('toggle', () => {
    if (panel.open) {
      openMonitorGroups.add(key);
      if (!panel.querySelector('.monitor-detail-body')) {
        const body = textElement('div', '', 'monitor-detail-body');
        populateGroupDetail(body, group, shareDenominator);
        panel.append(body);
      }
    } else {
      openMonitorGroups.delete(key);
    }
  });
  panel.open = openMonitorGroups.has(key);
  if (panel.open) {
    const body = textElement('div', '', 'monitor-detail-body');
    populateGroupDetail(body, group, shareDenominator);
    panel.append(body);
  }
  return panel;
}

function renderMonitorRequest(sample, pending = false) {
  const area = $('monitor-request-content');
  if (!$('monitor-request').hidden) updateScopeSummary(sample, true);

  if (!sample) {
    area.dataset.shares = 'false';
    area.replaceChildren(textElement('p', pending ? '正在读取请求明细。' : '最近一次请求的明细暂不可用。', 'monitor-note'));
    return;
  }

  const reqHasLimit = Number.isSafeInteger(sample.limit) && sample.limit > 0;
  const reqHasUsed = Number.isSafeInteger(sample.used);
  const shareDenominator = reqHasLimit
    ? sample.limit
    : (sample.coverage === 'complete' && sample.used > 0 ? sample.used : null);
  const showShares = Boolean(shareDenominator);
  area.dataset.shares = String(showShares);

  const reconcile = textElement('div', '', 'monitor-reconcile');
  const left = textElement('span', '最近一次请求 ');
  left.append(textElement('strong', formatTokenCount(sample.used)));
  if (reqHasLimit && reqHasUsed) {
    left.append(document.createTextNode(' (' + monitorPercent.format(sample.used / sample.limit) + ')'));
  }
  reconcile.append(left);

  if (reqHasUsed && Number.isSafeInteger(lastWindowSample?.used)) {
    const diff = sample.used - lastWindowSample.used;
    const diffText = diff === 0
      ? '与当前窗口一致'
      : '较当前差值 ' + (diff < 0 ? '-' : '+') + formatTokenCount(Math.abs(diff));
    reconcile.append(textElement('span', diffText));
  }

  const rows = orderedByTokens(sample.groups || []);
  const nodes = [reconcile];

  if (rows.length > 0) {
    if (monitorViewMode === 'list') {
      const shell = textElement('div', '', 'monitor-list-shell');
      const maxTokens = Math.max(1, ...rows.map((r) => Number.isSafeInteger(r.tokens) ? r.tokens : 0));
      shell.append(...rows.map((group, index) => monitorGroupDetail(group, index, maxTokens, shareDenominator, sample.used)));
      nodes.push(shell);
    } else {
      const box = textElement('div', '', 'treemap-box');
      const inspector = textElement('div', '', 'treemap-inspector');
      renderTreemap(box, inspector, sample, rows, shareDenominator, true);
      nodes.push(box, inspector);
    }
  } else {
    nodes.push(textElement('p', '宿主暂未返回请求分类明细。', 'monitor-note'));
  }

  if (sample.coverage !== 'complete') {
    nodes.push(textElement('p', '宿主暂未返回完整的请求分类。', 'monitor-note'));
  }
  area.replaceChildren(...nodes);
}

async function refreshMonitor() {
  if (!$('monitor-view') || $('monitor-view').hidden || document.hidden || !token || monitorPending) return;
  monitorPending = true;
  try {
    const latest = conversationId ? await request('/api/monitor') : { connection: 'no_conversation' };
    if (!latest.requestRef) monitorDetail = null;
    renderMonitorStatus(latest);

    if (latest.connection !== 'ready' || !latest.requestRef) {
      if (!$('monitor-request').hidden) renderMonitorRequest(null);
      return;
    }

    const requestVisible = !$('monitor-request').hidden;
    const cachedValid = monitorDetail && monitorDetail.reference === latest.requestRef
      && (monitorDetail.sample || Date.now() < monitorDetail.nextRead);

    if (cachedValid) {
      if (requestVisible) renderMonitorRequest(monitorDetail.sample);
      return;
    }

    if (requestVisible) renderMonitorRequest(null, true);
    try {
      const report = await request('/api/monitor', null, { request: '1' });
      monitorDetail = {
        reference: report.requestRef,
        sample: report.request,
        nextRead: Date.now() + 15000,
      };
      renderMonitorStatus(report);
      if (!$('monitor-request').hidden) renderMonitorRequest(report.request);
    } catch {
      monitorDetail = { reference: latest.requestRef, sample: null, nextRead: Date.now() + 15000 };
      if (!$('monitor-request').hidden) renderMonitorRequest(null);
    }
  } catch {
    renderMonitorStatus({ connection: 'unavailable' });
    if (!$('monitor-request').hidden) renderMonitorRequest(null);
    monitorDetail = null;
  } finally {
    monitorPending = false;
  }
}

function showMonitorScope(showRequest) {
  $('monitor-current-mode').setAttribute('aria-pressed', String(!showRequest));
  $('monitor-request-mode').setAttribute('aria-pressed', String(showRequest));
  $('monitor-current').hidden = showRequest;
  $('monitor-request').hidden = !showRequest;
  if (showRequest) {
    if (monitorDetail?.sample) renderMonitorRequest(monitorDetail.sample);
    void refreshMonitor();
  } else {
    updateScopeSummary(lastWindowSample, false);
  }
}

function setMonitorViewMode(mode) {
  if (monitorViewMode === mode) return;
  monitorViewMode = mode;
  $('monitor-view-list').setAttribute('aria-pressed', String(mode === 'list'));
  $('monitor-view-map').setAttribute('aria-pressed', String(mode === 'map'));
  monitorWindowSignature = '';
  if (lastWindowSample || lastConnection) {
    renderMonitorWindow(lastWindowSample, lastConnection);
  }
  if (!$('monitor-request').hidden) {
    renderMonitorRequest(monitorDetail?.sample || null);
  }
}

function bindChartInteractions() {
  const view = $('monitor-view');
  if (!view) return;
  view.addEventListener('pointerover', (event) => {
    const target = event.target.closest('[data-hover-key]');
    if (!target || !view.contains(target)) return;
    applyChartHover(target.dataset.hoverKey, target.dataset.group, target.dataset.catTitle,
      target.dataset.catTokens, target.dataset.catPct, target.dataset.catUsedPct);
  });
  view.addEventListener('pointerout', (event) => {
    const from = event.target.closest('[data-hover-key]');
    if (!from) return;
    const to = event.relatedTarget ? event.relatedTarget.closest('[data-hover-key]') : null;
    if (to && view.contains(to) && to.dataset.hoverKey === from.dataset.hoverKey) return;
    applyChartHover(null);
  });
  view.addEventListener('focusin', (event) => {
    const target = event.target.closest('.ring-segment[data-hover-key], .storage-seg[data-hover-key], .tm-tile[data-hover-key]');
    if (!target) return;
    applyChartHover(target.dataset.hoverKey, target.dataset.group, target.dataset.catTitle,
      target.dataset.catTokens, target.dataset.catPct, target.dataset.catUsedPct);
  });
  view.addEventListener('focusout', (event) => {
    const from = event.target.closest('.ring-segment[data-hover-key], .storage-seg[data-hover-key], .tm-tile[data-hover-key]');
    if (!from) return;
    const to = event.relatedTarget ? event.relatedTarget.closest('[data-hover-key]') : null;
    if (to && view.contains(to)) return;
    applyChartHover(null);
  });
}

bindChartInteractions();
$('monitor-current-mode').addEventListener('click', () => showMonitorScope(false));
$('monitor-request-mode').addEventListener('click', () => showMonitorScope(true));
$('monitor-view-list').addEventListener('click', () => setMonitorViewMode('list'));
$('monitor-view-map').addEventListener('click', () => setMonitorViewMode('map'));
document.addEventListener('visibilitychange', () => void refreshMonitor());
if (token) setInterval(refreshMonitor, 4000);
