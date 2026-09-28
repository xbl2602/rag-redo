/* ============================================================
   Obsidian RAG GUI · 生产版渲染层
   - 所有后端调用统一走 API 薄封装（window.pywebview.api）
   - 推送监听：snapshot(1s) / log / preview
   - 图谱引擎移植自 demo7（力导向 + 涟漪 + 轨道 + Inspector）
   ============================================================ */
(function () {
'use strict';

/* ============ 基元 ============ */
var $ = function (id) { return document.getElementById(id); };
function $$id(ids) { return ids.map($); }
var API = new Proxy({}, {
  get: function (t, m) {
    return function () {
      var api = window.pywebview && window.pywebview.api;
      if (!api) return Promise.reject(new Error('后端桥未就绪'));
      return api[m].apply(api, arguments);
    };
  }
});
var RM = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
var SVGNS = 'http://www.w3.org/2000/svg';
var OFF = 6000;

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
/* 生产推送入口（问题47 附记·真根因）：bridge 每秒 evaluate_js 调
   window.__push(type, payloadJson)，但生产版从未定义它（只在 mock.js 里
   有）——守卫 `&&` 把全部快照/日志推送静默吞掉，KPI 与进度永远停在启动
   那一刻，只有直接 API 调用（toast/按钮）有反应。与 mock 版同语义
   （转 CustomEvent），谁后加载覆盖谁都不影响行为。 */
window.__push = function (type, payloadJson) {
  window.dispatchEvent(new CustomEvent(type, { detail: payloadJson }));
};
function escReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function fmtInt(n) { return (n == null ? '-' : Number(n).toLocaleString('zh-CN')); }
function fmtDur(sec) {
  if (sec == null) return '--';
  sec = Math.round(sec);
  if (sec < 60) return sec + '秒';
  var m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + '分' + (s ? s + '秒' : '');
  return Math.floor(m / 60) + '时' + (m % 60) + '分';
}
function fmtTs(ts) {
  if (ts == null) return '从未索引';
  var d = new Date(ts * 1000);
  var pad = function (x) { return (x < 10 ? '0' : '') + x; };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function fmtDay(ts) {
  if (ts == null) return '';
  var d = new Date(ts * 1000);
  var pad = function (x) { return (x < 10 ? '0' : '') + x; };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}
function hl(text, q) {
  var out = esc(text);
  if (!q) return out;
  var terms = q.trim().split(/\s+/).filter(function (t) { return t.length > 1; }).map(escReg)
    .sort(function (a, b) { return b.length - a.length; });
  if (!terms.length) return out;
  var re;
  try { re = new RegExp('(' + terms.join('|') + ')', 'gi'); } catch (e) { return out; }
  return out.replace(re, '<mark>$1</mark>');
}
function nl2p(html) {
  return String(html).split('\n').filter(Boolean).map(function (p) { return '<p>' + p + '</p>'; }).join('');
}
/* 渲染命中共用：
   - textOf：HTML → 纯文本（给 3 行预览切片用，避免 MD 标记裸露）；
   - hlHtml：标签感知的查询高亮（只碰标签外的文本，href/属性不动）；
   - 后端 search() 已给每条命中带 rendered_html，无则回退旧 nl2p 路径。 */
function textOf(html) {
  var d = document.createElement('div');
  d.innerHTML = String(html || '');
  return d.textContent || d.innerText || '';
}
function hlHtml(html, q) {
  if (!q) return html;
  var terms = q.trim().split(/\s+/).filter(function (t) { return t.length > 1; }).map(escReg)
    .sort(function (a, b) { return b.length - a.length; });
  if (!terms.length) return html;
  var re;
  try { re = new RegExp('(' + terms.join('|') + ')', 'gi'); } catch (e) { return html; }
  return String(html).split(/(<[^>]+>)/g).map(function (seg, i) {
    return (i % 2 === 1) ? seg : seg.replace(re, '<mark>$1</mark>');
  }).join('');
}
function snipTxt(r) {
  var txt = r.rendered_html ? textOf(r.rendered_html) : String(r.body || '');
  return txt.replace(/\s+/g, ' ').trim().slice(0, 220);
}
function hashStr(s) {
  var h = 0;
  for (var i = 0; i < s.length; i++) { h = ((h * 31 + s.charCodeAt(i)) >>> 0); }
  return h;
}
function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    var t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function debounce(fn, ms) {
  var t = null;
  return function () {
    var args = arguments, self = this;
    if (t) clearTimeout(t);
    t = setTimeout(function () { t = null; fn.apply(self, args); }, ms);
  };
}

/* ============ 全局状态 ============ */
var S = {
  libs: [],            // snapshot.libs
  scope: [],           // 选中的库名（空 = 全部）
  snap: null,
  firstSearchDone: false,
  lastHeartbeat: null,
  deadAlarmed: false,
  ioRunning: false
};
var PHASE_NAME = { idle: '空闲', scanning: '扫描', converting: '转换', embedding: '嵌入', writing: '写库', done: '完成', wemm: '页库同步' };
var HB_NAME = { idle: '空闲', running: '心跳正常', stalled: '心跳停滞', dead: '疑似卡死', done: '已完成' };
var LIB_COLORS = ['#34D399', '#7DB8FF', '#FBBF24', '#F472B6', '#A78BFA', '#FB923C', '#22D3EE', '#A3E635'];
var REASON_LABEL = {
  scanned: '扫描件待 OCR', unreadable: '无法解析', empty: '空文件',
  'extract-failed': '提取失败', tbd: '待处理'
};
var REASON_ADVICE = {
  scanned: '云端开启后下轮自动重试',
  unreadable: '文件损坏，建议重新导出后重建',
  empty: '已落终态，不再重试',
  'extract-failed': '修复源文件后可手动重建',
  tbd: '等待下轮索引'
};

/* ============ Toast / 弹层 ============ */
function toast(msg, kind) {
  var wrap = $('toastWrap');
  var t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  var icon = kind === 'err'
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>'
    : (kind === 'warn'
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>');
  t.innerHTML = icon + '<span></span>';
  t.lastChild.textContent = msg;
  wrap.appendChild(t);
  requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add('show'); }); });
  setTimeout(function () {
    t.classList.remove('show');
    setTimeout(function () { t.remove(); }, 350);
  }, 3200);
}
var escStack = [];
function openModal(id) {
  var m = $(id);
  if (!m || m.classList.contains('open')) return;
  m.classList.add('open');
  escStack.push(m);
  var f = m.querySelector('input, .btn');
  if (f) f.focus();
}
function hideOverlay(ov) {
  if (!ov) return;
  ov.classList.remove('open');
  escStack = escStack.filter(function (x) { return x !== ov; });
  if (ov.id === 'mConfirm' && pendingConfirm && pendingConfirm.onCancel) { pendingConfirm.onCancel(); pendingConfirm = null; }
}
var pendingConfirm = null;
function askConfirm(title, desc, warnTxt, okLabel) {
  return new Promise(function (resolve) {
    $('cfmTitle').textContent = title;
    $('cfmDesc').textContent = desc;
    $('cfmWarnTxt').textContent = warnTxt || '';
    $('cfmWarn').style.display = warnTxt ? '' : 'none';
    $('cfmOk').textContent = okLabel || '确认';
    pendingConfirm = { onCancel: function () { resolve(false); } };
    $('cfmOk').onclick = function () {
      pendingConfirm = null; hideOverlay($('mConfirm')); resolve(true);
    };
    $('cfmNo').onclick = function () { hideOverlay($('mConfirm')); };
    openModal('mConfirm');
  });
}
function closeTop() {
  if (escStack.length) { hideOverlay(escStack[escStack.length - 1]); return true; }
  if ($('logDrawer').classList.contains('open')) { toggleLog(false); return true; }
  if (G.insOpen) { closeGIns(); return true; }
  if ($('gPhysics').classList.contains('open')) { togglePhysics(false); return true; }
  if (G.qNode || G.searching) { clearGSearch(); toast('已回到全库全景'); return true; }
  return false;
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeTop(); }
});
Array.prototype.forEach.call(document.querySelectorAll('.overlay'), function (ov) {
  ov.addEventListener('click', function (e) { if (e.target === ov) hideOverlay(ov); });
});
Array.prototype.forEach.call(document.querySelectorAll('[data-close]'), function (b) {
  b.addEventListener('click', function () { hideOverlay(b.closest('.overlay')); });
});

/* ============ 主题 ============ */
function applyTheme(light) {
  document.documentElement.setAttribute('data-theme', light ? 'light' : '');
  $('iconMoon').style.display = light ? 'none' : '';
  $('iconSun').style.display = light ? '' : 'none';
  try { localStorage.setItem('rag-theme', light ? 'light' : 'dark'); } catch (e) {}
}
$('themeBtn').addEventListener('click', function () {
  applyTheme(document.documentElement.getAttribute('data-theme') !== 'light');
});

/* ============ 视图切换 ============ */
var VIEWS = ['graph', 'search', 'library', 'index', 'lab', 'diag', 'settings'];
function go(v) {
  S.view = v;
  VIEWS.forEach(function (name) {
    var el = $('view-' + name);
    if (el) el.classList.toggle('on', name === v);
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-nav]'), function (b) {
    b.classList.toggle('on', b.getAttribute('data-nav') === v && b.classList.contains('sb-item'));
  });
  Array.prototype.forEach.call(document.querySelectorAll('.nav-link'), function (b) {
    b.classList.toggle('on', b.getAttribute('data-nav') === v);
  });
  if (v === 'library') loadLibs();
  if (v === 'diag') { loadFailures(); loadWemmStatus(); }
  if (v === 'settings' && !SET.loaded) loadSettings();
  if (v !== 'graph') closeGIns();
}
Array.prototype.forEach.call(document.querySelectorAll('[data-nav]'), function (b) {
  b.addEventListener('click', function () { go(b.getAttribute('data-nav')); });
});

/* ============ 库范围（全局） ============ */
function scopeStr() { return S.scope.join(','); }
function scopeLabel() { return S.scope.length ? S.scope.length + ' 库' : '全部库'; }
function updateScopeUI() {
  $('libScopeTxt').textContent = scopeLabel();
  $('libScopeBtn').classList.toggle('set', S.scope.length > 0);
  $('scopeBtn').textContent = '范围：' + scopeLabel();
}
function openScopeModal() {
  var list = $('scopeList');
  list.innerHTML = S.libs.map(function (l) {
    var on = S.scope.indexOf(l.name) >= 0;
    return '<label class="scope-item"><input type="checkbox" data-sname="' + esc(l.name) + '"' + (on ? ' checked' : '')
      + '><span class="sn">' + esc(l.name) + '</span><span class="sc">' + fmtInt(l.chunks) + ' 块</span></label>';
  }).join('');
  openModal('mScope');
}
$('libScopeBtn').addEventListener('click', openScopeModal);
$('scopeBtn').addEventListener('click', openScopeModal);
$('scopeAllBtn').addEventListener('click', function () {
  Array.prototype.forEach.call($('scopeList').querySelectorAll('input'), function (i) { i.checked = true; });
});
$('scopeInvertBtn').addEventListener('click', function () {
  Array.prototype.forEach.call($('scopeList').querySelectorAll('input'), function (i) { i.checked = !i.checked; });
});
$('scopeOk').addEventListener('click', function () {
  var sel = [];
  Array.prototype.forEach.call($('scopeList').querySelectorAll('input'), function (i) {
    if (i.checked) sel.push(i.getAttribute('data-sname'));
  });
  if (sel.length && sel.length === S.libs.length) S.scope = [];
  else S.scope = sel;
  updateScopeUI();
  hideOverlay($('mScope'));
  G.applyScope();
});

/* ============ 快照驱动（动态岛 / KPI / 心跳 / 门锁） ============ */
function paintIsland(p, chunks) {
  var island = $('island');
  if (p.running) {
    island.classList.add('run', 'live');
    island.classList.remove('dead', 'stalled');
    $('iPhase').textContent = PHASE_NAME[p.phase] || p.phase;
    $('iBar').style.transform = 'scaleX(' + (p.pct / 100) + ')';
    $('iPct').textContent = Math.round(p.pct) + '%';
    $('iEta').textContent = p.pct > 2 ? '剩 ' + fmtDur(p.elapsed / p.pct * (100 - p.pct)) : '估时中';
    return;
  }
  island.classList.remove('run', 'live');
  island.classList.toggle('dead', p.heartbeat === 'dead');
  island.classList.toggle('stalled', p.heartbeat === 'stalled');
  var msg;
  if (p.heartbeat === 'dead') msg = '索引疑似卡死';
  else if (p.heartbeat === 'stalled') msg = '心跳停滞 · ' + (p.heartbeat_note || '等待进展');
  else if (p.heartbeat === 'done') msg = '索引完成 · ' + fmtInt(chunks) + ' 块';
  else if (p.busy) msg = '另一进程索引中';
  else msg = '就绪 · <b class="mono">' + fmtInt(chunks) + '</b> 块';
  $('islandMsg').innerHTML = p.heartbeat === 'idle' && !p.busy ? msg : esc(msg.replace(/<[^>]+>/g, ''));
}
function paintHeartCap(p) {
  var cap = $('heartCap');
  cap.className = 'hb-cap hb-' + (p.running ? 'run' : (p.heartbeat || 'idle'));
  var label = p.running ? (HB_NAME.running) : (HB_NAME[p.heartbeat] || p.heartbeat);
  if (p.heartbeat_note && (p.running || p.heartbeat === 'stalled')) label += ' · ' + p.heartbeat_note;
  $('heartTxt').textContent = label;
}
function paintStepper(p) {
  var order = ['scanning', 'converting', 'embedding', 'writing'];
  var cur = order.indexOf(p.phase);
  for (var i = 0; i < 5; i++) {
    var el = $('ph' + i);
    el.classList.toggle('done', p.running ? (cur > i || p.phase === 'done') : p.phase === 'done');
    el.classList.toggle('on', p.running && cur === i);
    if (i < 4) $('sl' + i).classList.toggle('done', p.running ? cur > i : p.phase === 'done');
  }
  $('idxBar').style.transform = 'scaleX(' + (p.pct / 100) + ')';
  $('idxPct').textContent = Math.round(p.pct) + '%';
  $('idxCounts').textContent = '文件 ' + p.files_done + '/' + p.files_total + ' · 块 ' + p.chunks_done + '/' + p.chunks_total
    + (p.library ? ' · ' + p.library : '');
  $('idxElapsed').textContent = '已用 ' + fmtDur(p.elapsed);
  $('idxEta').textContent = p.running && p.pct > 2 ? '剩余 ' + fmtDur(p.elapsed / p.pct * (100 - p.pct)) : '剩余 --';
  $('idxLib').textContent = p.running && p.library ? '目标库：' + p.library : '';
}
function sysLine(snap) {
  // 整机占用一行：显存已用/总量 · GPU利用率 · 功耗 · CPU · 看图模型状态。
  // 后端缺字段/失败一律显示 --（fail-open），WDDM 下拆不到进程归属，只报整卡。
  var g = snap.gpu || {}, wl = snap.wemm_live || {};
  var parts = [];
  if (g.ok) {
    parts.push('显存 ' + (g.mem_used_mb / 1024).toFixed(1) + '/' + (g.mem_total_mb / 1024).toFixed(1) + 'GB');
    parts.push('GPU ' + Math.round(g.util_pct) + '%');
    if (g.power_w != null) parts.push(Math.round(g.power_w) + 'W');
  } else {
    parts.push('显存 --');
  }
  parts.push('CPU ' + (snap.cpu != null ? Math.round(snap.cpu) + '%' : '--'));
  var wtxt = !wl.alive ? '看图服务未运行'
    : (wl.loaded ? '看图模型常驻' + (wl.gpu_mem_gb != null ? ' ' + wl.gpu_mem_gb + 'GB' : '') : '看图服务空闲（已卸载）');
  parts.push(wtxt);
  return parts.join(' · ');
}
function onSnapshot(snap) {
  S.snap = snap;
  S.libs = snap.libs || [];
  var p = snap.progress || {};
  paintIsland(p, snap.chunks);
  paintHeartCap(p);
  paintStepper(p);
  $('kpiFiles').textContent = fmtInt(snap.files);
  $('kpiChunks').textContent = fmtInt(snap.chunks);
  $('kpiLast').textContent = fmtDur(snap.last_elapsed);
  if (snap.device) $('kpiDevice').textContent = (snap.device.model || '') + (snap.device.cuda === true ? ' · CUDA' : snap.device.cuda === false ? ' · CPU' : '');
  // 门锁口径（问题47 附记）：不用 busy&&!running 双读拼凑（撕裂帧会冤枉 MCP），
  // 用后端同一快照判定的 task 归属。starting = 自己刚点的、进度未到，显示启动中。
  var t = snap.task || 'idle';
  var lock = (t === 'foreign');
  $('btnIncr').disabled = lock || p.running;
  $('btnFull').disabled = lock || p.running;
  $('busyNote').textContent = lock ? '另一进程正在索引（可能是 MCP 触发），本窗口暂不能启动新任务'
    : (t === 'starting' ? '任务启动中…' : '');
  $('btnStopIdx').disabled = !(p.running || t === 'starting');
  // DEAD 告警（每次进入 dead 只响一次）
  if (p.heartbeat === 'dead' && !S.deadAlarmed) {
    S.deadAlarmed = true;
    toast('索引疑似卡死：' + (p.heartbeat_note || '心跳超时，请查看日志'), 'err');
    toggleLog(true);
  }
  if (p.heartbeat !== 'dead') S.deadAlarmed = false;
  S.lastHeartbeat = p.heartbeat;
  // WEMM 状态行 + 整机占用行（问题47：显存/GPU/CPU/功耗只读显示）
  var w = snap.wemm || {};
  var base = w.backend === 'off'
    ? '页库：当前关闭。开启后为 PDF 课件与扫描件建立页级向量，支持以图搜图式定位。'
    : '页库：已开启 · 后端 <b>' + esc(w.backend) + '</b>（整轮结束用完即卸）';
  $('wemmStateTxt').innerHTML = base + '<br><span class="mono">' + esc(sysLine(snap)) + '</span>';
  var sysEl = $('idxSys');
  if (sysEl) sysEl.textContent = sysLine(snap);
  // 库名集合变化时才重建下拉选项（避免每秒重建）
  var nameKey = S.libs.map(function (l) { return l.name; }).join(',');
  if (nameKey !== S.libNamesKey) {
    S.libNamesKey = nameKey;
    buildFailLibOptions();
    buildWemmLibOptions();
  }
  // 库视图打开时轻量刷新（弹层打开期间不打断）
  if (libLoaded && S.view === 'library' && !escStack.length) loadLibs();
  if (G.ready) updateGStatus();
}

/* ============ 检索 ============ */
var relCache = {};   // lib|rel → note_relations 结果
var relOpen = {};
var bodyOpen = {};
var searching = false;
function scoreBadge(s) {
  // 来源行里的数值就是重排器给出的相关概率本身（0~1，0.5 = 无法判断）。
  // 2026-09-11 问题54：此前重排分被 sigmoid 两次压进 (0.5,0.731)、再由后端
  // 重标定回展示分，这里才需要 0.85/0.2 这套换算过的阈值；现在两套尺度合一，
  // 阈值对齐后端同一批常量——0.75 = retriever.CONF_TIER_STRONG（高相关线），
  // 0.30 = config.confidence_warn_threshold（低置信线）。改后端就要改这里。
  // 真没有分数时不得冒充 0──JS 里 Math.round(null*100) === 0，
  // 会把「无分数」画成「0 低置信」，与展开态的 '--'、与「有分数但极低」都冲突。
  if (s == null || isNaN(s)) return '<span class="badge badge-lo">-- 无分数</span>';
  var pct = Math.round(s * 100);
  if (s >= 0.75) return '<span class="badge badge-hi">' + pct + ' 高置信</span>';
  if (s >= 0.30) return '<span class="badge badge-mid">' + pct + ' 中置信</span>';
  return '<span class="badge badge-lo">' + pct + ' 低置信</span>';
}
function relHtml(key) {
  var d = relCache[key];
  var chips = function (arr) {
    if (!arr || !arr.length) return '<span style="font-size:12px;color:var(--ph)">无</span>';
    return '<div class="rel-chips">' + arr.map(function (n) { return '<span class="chip">' + esc(n) + '</span>'; }).join('') + '</div>';
  };
  return '<div class="rel">'
    + '<div class="rel-row"><span class="rel-k">出链</span>' + chips(d.outlinks) + '</div>'
    + '<div class="rel-row"><span class="rel-k">入链</span>' + chips(d.inlinks) + '</div>'
    + '</div>';
}
var lastResults = null, lastQuery = '';
var hitSel = 0;      // 当前指定的命中条目（主区详情跟随）
var hitOpen = null;  // 右侧列表中展开的行（单开；null=全部收起）
var srcView = false; // 命中详情视图：false=渲染视图（默认）/ true=Markdown 源码

function titleOf(r) {
  var stem = (r.rel || '').split('/').pop().replace(/\.(md|txt|docx|pdf)$/i, '');
  return stem || r.rel || '（无标题）';
}

function renderResults() {
  if (!lastResults) return;
  var all = lastResults.results;
  var notices = all.filter(function (r) { return r.notice; });
  var rows = all.filter(function (r) { return !r.notice; });
  if (hitSel >= rows.length) hitSel = Math.max(0, rows.length - 1);
  $('hitMeta').textContent = '共 ' + rows.length + ' 条命中 · 耗时 '
    + (lastResults.elapsed != null ? lastResults.elapsed.toFixed(1) : '--') + 's · 范围：' + scopeLabel();
  $('hitNotices').innerHTML = notices.map(function (n) {
    return '<div class="notice n-warn show" style="margin-bottom:12px"><span>' + esc(n.body) + '</span></div>';
  }).join('');

  // ---- 右侧：命中标题列表（全部默认收起，单开）----
  $('hitCount').textContent = rows.length;
  $('hitList').innerHTML = rows.map(function (r, i) {
    var open = hitOpen === i;
    var chunkInfo = r.chunk_total ? '块 ' + (r.chunk_idx + 1) + '/' + r.chunk_total : '整段';
    var body = open
      ? '<div class="hit-b">'
        + (r.heading ? '<div class="hit-heading">' + esc(r.heading) + '</div>' : '')
        + '<div class="hit-chip mono">' + chunkInfo + ' · 置信度 '
        + (r.confidence != null ? r.confidence.toFixed(2) : '--') + '</div>'
        + '<div class="hit-clip">' + hl(snipTxt(r), lastQuery) + '…</div>'
        + '<div class="r-actions" style="padding:8px 0 0">'
        + '<button class="btn btn-sm btn-ghost" data-doc="' + i + '">查看正文</button>'
        + '<button class="btn btn-sm btn-ghost" data-rel="' + i + '">' + (relOpen[i] ? '收起关联' : '关联笔记') + '</button>'
        + '<button class="btn btn-sm btn-ghost" data-open="' + i + '">打开源文件</button>'
        + '</div></div>'
      : '';
    return '<div class="hit-item' + (open ? ' open' : '') + (hitSel === i ? ' sel' : '') + '" data-hit="' + i + '">'
      + '<div class="hit-h"><div class="hit-txt"><div class="hit-title">' + esc(titleOf(r)) + '</div>'
      + '<div class="hit-sub mono">' + esc(r.lib) + ' · ' + chunkInfo + '</div></div>'
      + scoreBadge(r.confidence) + '</div>' + body + '</div>';
  }).join('');

  // ---- 主区：当前指定条目的完整阅读卡 ----
  var sel = rows[hitSel];
  if (!sel) {
    $('hitDetailCore').innerHTML = '<div class="empty"><div class="grotesk">没有命中</div><div>换个问法或扩大库范围</div></div>';
    return;
  }
  var expandAll = $('expandTgl').checked;
  var key = sel.lib + '|' + sel.rel;
  var relPart = relOpen[hitSel]
    ? (relCache[key] ? relHtml(key)
      : '<div class="rel"><div class="rel-loading"><span class="mini-spin"></span>正在查询双链关系…</div></div>')
    : '';
  var bodyBlock = expandAll
    ? (srcView
      ? '<pre class="lab-md mono" data-exp="' + hitSel + '" title="点击切回渲染视图">' + esc(sel.body || '') + '</pre>'
      : '<div class="r-full md-body" data-exp="' + hitSel + '" title="点击收起为 3 行预览">'
        + (sel.rendered_html ? hlHtml(sel.rendered_html, lastQuery) : nl2p(hl(sel.body || '', lastQuery))) + '</div>')
    : '<div class="r-snippet" data-exp="' + hitSel + '" title="点击展开全文">' + hl(snipTxt(sel), lastQuery) + '</div>';
  $('hitDetailCore').innerHTML =
    '<div class="r-head"><span class="r-lib">' + esc(sel.lib) + '</span><span class="r-path">' + esc(sel.rel) + '</span>' + scoreBadge(sel.confidence) + '</div>'
    + '<div class="hit-big-title">' + esc(titleOf(sel)) + '</div>'
    + (sel.heading ? '<div class="hit-heading" style="padding:2px 16px 0">' + esc(sel.heading) + '</div>' : '')
    + bodyBlock + relPart
    + '<div class="r-actions">'
    + '<button class="btn btn-sm btn-primary" data-doc="' + hitSel + '">查看正文</button>'
    + '<button class="btn btn-sm btn-ghost" data-rel="' + hitSel + '">' + (relOpen[hitSel] ? '收起关联' : '关联笔记') + '</button>'
    + '<button class="btn btn-sm btn-ghost" data-open="' + hitSel + '">打开源文件</button>'
    + '<span class="r-viewtgl"><button data-view="html" class="' + (!srcView ? 'on' : '') + '">渲染</button>'
    + '<button data-view="src" class="' + (srcView ? 'on' : '') + '">源码</button></span>'
    + (sel.chunk_total ? '<span class="r-more mono">命中块 ' + (sel.chunk_idx + 1) + '/' + sel.chunk_total + '</span>' : '<span class="r-more mono">整段命中</span>')
    + '</div>';
  $('searchWrap').style.display = 'block';
}
function doSearch() {
  var q = $('qInput').value.trim();
  if (!q) { toast('请输入要检索的内容', 'warn'); $('qInput').focus(); return; }
  if (searching) return;
  searching = true; lastQuery = q;
  relOpen = {}; bodyOpen = {};
  $('searchEmpty').style.display = 'none';
  $('searchWrap').style.display = 'none';
  $('searchErr').classList.remove('show');
  $('searchSkeleton').style.display = 'flex';
  $('searchBtn').disabled = true;
  // 加载活性指示：已耗时秒数递增，首次检索明确提示模型加载时长
  var skT0 = Date.now();
  $('skLiveTxt').textContent = S.firstSearchDone ? '正在检索…' : '正在加载嵌入模型并检索…';
  $('skLiveSec').textContent = '0s';
  var skTimer = setInterval(function () {
    $('skLiveSec').textContent = Math.round((Date.now() - skT0) / 1000) + 's';
  }, 500);
  API.search(q, parseInt($('topkSel').value, 10) || 5, scopeStr(), true).then(function (res) {
    searching = false;
    clearInterval(skTimer);
    $('searchSkeleton').style.display = 'none';
    $('searchBtn').disabled = false;
    S.firstSearchDone = true;
    if (res.error) {
      $('searchErrMsg').textContent = '检索失败：' + res.error;
      $('searchErr').classList.add('show');
      $('searchEmpty').style.display = 'block';
      return;
    }
    lastResults = res;
    if (!res.results.length) {
      $('searchEmpty').style.display = 'block';
      toast('没有命中任何片段，换个问法或扩大库范围', 'warn');
      return;
    }
    hitSel = 0; hitOpen = null;
    renderResults();
  }).catch(function (err) {
    searching = false;
    $('searchSkeleton').style.display = 'none';
    $('searchBtn').disabled = false;
    $('searchErrMsg').textContent = '检索失败：' + (err && err.message ? err.message : err);
    $('searchErr').classList.add('show');
    $('searchEmpty').style.display = 'block';
  });
}
$('searchBtn').addEventListener('click', doSearch);
$('qInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });
$('topkSel').addEventListener('change', function () {
  if (lastResults) { lastResults.results = lastResults.results.slice(0, parseInt(this.value, 10) || 5); hitSel = 0; hitOpen = null; renderResults(); }
});
$('expandTgl').addEventListener('change', function () { if (lastResults) renderResults(); });
$('searchWrap').addEventListener('click', function (e) {
  var hit = e.target.closest('[data-hit]');
  if (hit) {
    var i = +hit.getAttribute('data-hit');
    hitSel = i;
    hitOpen = (hitOpen === i) ? null : i;   // 单开：点新行自动收起旧行
    renderResults();
    return;
  }
  var t = e.target.closest('[data-exp],[data-rel],[data-open],[data-doc],[data-view]');
  if (!t) return;
  if (t.hasAttribute('data-view')) {
    srcView = (t.getAttribute('data-view') === 'src');
    if (!$('expandTgl').checked) $('expandTgl').checked = true;
    renderResults();
  } else if (t.hasAttribute('data-doc')) {
    var dr = lastResults.results.filter(function (r) { return !r.notice; })[+t.getAttribute('data-doc')];
    if (dr) openDoc(dr.lib, dr.rel, dr.heading || '');
  } else if (t.hasAttribute('data-exp')) {
    var ei = t.getAttribute('data-exp');
    bodyOpen[ei] = !bodyOpen[ei];
    renderResults();
  } else if (t.hasAttribute('data-rel')) {
    var idx = t.getAttribute('data-rel');
    if (relOpen[idx]) { delete relOpen[idx]; renderResults(); return; }
    relOpen[idx] = true;
    renderResults();
    var r = lastResults.results[+idx];
    var key = r.lib + '|' + r.rel;
    if (!relCache[key]) {
      API.note_relations(r.lib, r.rel).then(function (d) {
        relCache[key] = d;
        if (relOpen[idx]) renderResults();
      }).catch(function () {
        relCache[key] = { resolved: false, outlinks: [], inlinks: [] };
        if (relOpen[idx]) renderResults();
      });
    }
  } else if (t.hasAttribute('data-open')) {
    var rr = lastResults.results[+t.getAttribute('data-open')];
    API.open_source(rr.lib, rr.rel, rr.heading || '').then(function () {
      toast('已调用系统打开源文件');
    }).catch(function () { toast('打开失败', 'err'); });
  }
});

/* ============ 正文查看（检索命中 → GUI 内精读全文，不跳外部） ============ */
var DOC = { lib: '', rel: '', heading: '' };
function docShowTab(htmlMode) {
  $('docTabHtml').classList.toggle('on', htmlMode);
  $('docTabMd').classList.toggle('on', !htmlMode);
  $('docTabHtml').setAttribute('aria-selected', htmlMode ? 'true' : 'false');
  $('docTabMd').setAttribute('aria-selected', !htmlMode ? 'true' : 'false');
  $('docHtml').style.display = htmlMode ? '' : 'none';
  $('docMd').style.display = htmlMode ? 'none' : '';
}
function openDoc(lib, rel, heading) {
  DOC.lib = lib; DOC.rel = rel; DOC.heading = heading || '';
  $('docTitle').textContent = rel.split('/').pop() || rel;
  $('docMeta').textContent = lib + ' · ' + rel;
  $('docHtml').innerHTML = '<div class="doc-loading"><span class="mini-spin"></span>正在读取正文…</div>';
  $('docMd').textContent = '';
  $('docTrunc').style.display = 'none';
  docShowTab(true);
  openModal('mDoc');
  API.read_document(lib, rel).then(function (res) {
    if (!res.ok) {
      $('docMeta').textContent = lib + ' · ' + rel;
      $('docHtml').innerHTML = '<div class="empty"><div class="grotesk">正文不可用</div><div>'
        + esc(res.error || '未知错误') + '</div></div>';
      return;
    }
    $('docHtml').innerHTML = res.rendered_html || '';
    $('docMd').textContent = res.markdown || '';
    $('docMeta').textContent = lib + ' · ' + rel + ' · ' + (res.chars || 0) + '字 · '
      + (ROUTE_NAME[res.route] || res.route || '未知通道');
    if (res.truncated) {
      $('docTruncTxt').textContent = '文档超长，仅展示前 ' + (res.chars || 0)
        + ' 字，余下部分请用「打开源文件」查看。';
      $('docTrunc').style.display = 'flex';
    }
  }).catch(function (err) {
    $('docHtml').innerHTML = '<div class="empty"><div class="grotesk">读取失败</div><div>'
      + esc(err && err.message ? err.message : String(err)) + '</div></div>';
  });
}
$('docTabHtml').addEventListener('click', function () { docShowTab(true); });
$('docTabMd').addEventListener('click', function () { docShowTab(false); });
$('docOpen').addEventListener('click', function () {
  if (!DOC.lib) return;
  API.open_source(DOC.lib, DOC.rel, DOC.heading).then(function () {
    toast('已调用系统打开源文件');
  }).catch(function () { toast('打开失败', 'err'); });
});

/* ============ 库 ============ */
var libLoaded = false, LIBS_LIST = [];
function loadLibs() {
  API.list_libraries().then(function (list) {
    LIBS_LIST = list;
    libLoaded = true;
    renderLibs();
  }).catch(function () { toast('库列表加载失败', 'err'); });
}
function renderLibs() {
  $('libNote').textContent = '共 ' + LIBS_LIST.length + ' 个库参与索引，二进制格式受 Agent 门禁约束';
  $('libGrid').innerHTML = LIBS_LIST.map(function (l, i) {
    var dot = '<span class="st-dot ' + esc(l.state) + '" title="' + esc(l.state) + '"></span>';
    var fmts = (l.overrides || '').length
      ? '<span class="ov-sum">覆盖：' + esc(l.overrides) + '</span>'
      : '<span class="ov-sum">继承全局配置</span>';
    var issues = l.issues && Object.keys(l.issues).length
      ? Object.keys(l.issues).map(function (k) {
          return '<span class="fmt" style="color:var(--warn)">' + esc(k) + ' ×' + l.issues[k] + '</span>';
        }).join('')
      : '';
    var sm = l.summary || {};
    var summaryHtml = sm.text
      ? esc(sm.text) + (sm.source === 'user' ? ' <span class="fmt">已手动编辑</span>' : '')
      : '<span class="ov-sum">暂无简介 —— 点击「刷新简介」生成</span>';
    return '<div class="shell lib-card" data-libi="' + i + '"><div class="core lib-inner">'
      + '<div class="lib-top"><div style="min-width:0"><div class="lib-name">' + dot + esc(l.name) + '</div>'
      + '<div class="lib-path">' + esc(l.path) + '</div></div>'
      + '<div class="lib-stats"><div class="lib-stat"><b>' + fmtInt(l.blocks) + '</b><span>向量块</span></div>'
      + '<div class="lib-stat"><b class="mono" style="font-size:12px">' + esc(fmtTs(l.last_indexed)) + '</b><span>最近索引</span></div></div></div>'
      + '<div class="fmt-row">' + fmts + issues + '</div>'
      + '<div class="lib-summary">' + summaryHtml + '</div>'
      + '<div class="lib-ops">'
      + '<button class="btn btn-sm" data-act="cfg">配置</button>'
      + '<button class="btn btn-sm" data-act="sel">勾选范围</button>'
      + '<button class="btn btn-sm" data-act="sum">简介</button>'
      + '<button class="btn btn-sm btn-ghost" data-act="open">打开文件夹</button>'
      + '<span style="flex:1"></span>'
      + '<button class="btn btn-sm btn-ghost" data-act="rm" style="color:var(--err)">移除</button>'
      + '</div></div></div>';
  }).join('') || '<div class="shell"><div class="core"><div class="empty">还没有注册任何库，点击右上角「添加库」开始</div></div></div>';
}
$('libGrid').addEventListener('click', function (e) {
  var btn = e.target.closest('[data-act]');
  if (!btn) return;
  var card = btn.closest('[data-libi]');
  var lib = LIBS_LIST[+card.getAttribute('data-libi')];
  var act = btn.getAttribute('data-act');
  if (act === 'open') {
    API.open_path(lib.path).then(function () { toast('已打开「' + lib.name + '」所在文件夹'); });
  } else if (act === 'cfg') { openCfg(lib); }
  else if (act === 'sel') { openSel(lib); }
  else if (act === 'sum') { openSum(lib); }
  else if (act === 'rm') { openRm(lib); }
});

/* ============ 库简介（问题60：导航性内容简介，手动编辑 / 后台批量生成） ============
   单库刷新与"刷新全部简介"共用同一后台任务（start/poll，同提取试验台模式）：
   点了就立刻返回，关掉简介弹层、切到别的标签页都不影响任务继续跑；顶部常驻
   状态条显示进度，跑完弹 toast，不管这期间用户在不在这个弹层里。 */
var SUM = { lib: null };
var SUMBATCH = { polling: false, hidden: false, skip: new Set() };
function openSum(lib) {
  SUM.lib = lib.name;
  var sm = lib.summary || {};
  $('sumTitle').textContent = '库简介 · ' + lib.name;
  $('sumText').value = sm.text || '';
  $('sumMeta').textContent = sm.text
    ? (sm.source === 'user' ? '当前为用户手写内容' : '当前为 AI 生成内容（模型：' + (sm.model || '?') + '）')
    : '尚未生成过简介';
  openModal('mSum');
}
$('sumRefresh').addEventListener('click', function () {
  // 在这个库自己的弹窗里点刷新是针对性的明确动作，即使之前批量刷新时用户对
  // 这个库选过"不覆盖"，这里也要重新问一次，而不是被批量场景的免打扰记忆挡住。
  SUMBATCH.skip.delete(SUM.lib);
  startSumBatch([SUM.lib], false);
  toast('已在后台开始生成，关闭本窗口不影响进度');
});
$('sumSave').addEventListener('click', function () {
  var text = $('sumText').value.trim();
  API.set_library_summary(SUM.lib, text).then(function (res) {
    if (!res.ok) { toast('保存失败：' + (res.error || '未知错误'), 'err'); return; }
    var lib = LIBS_LIST.find(function (l) { return l.name === SUM.lib; });
    if (lib) lib.summary = { text: text, source: 'user' };
    renderLibs();
    hideOverlay($('mSum'));
    toast('简介已保存');
  }).catch(function () { toast('保存失败', 'err'); });
});

function startSumBatch(names, force) {
  API.refresh_library_summaries_batch(names, !!force).then(function (res) {
    if (!res.ok) { toast(res.error || '启动失败', 'err'); return; }
    SUMBATCH.polling = true;
    SUMBATCH.hidden = false;
    $('sumBatchBar').style.display = 'flex';
    sumBatchPoll();
  }).catch(function () { toast('启动失败', 'err'); });
}
function sumBatchPoll() {
  if (!SUMBATCH.polling) return;
  API.refresh_library_summaries_poll().then(function (st) {
    if (!SUMBATCH.polling) return;
    if (!SUMBATCH.hidden) {
      $('sumBatchTxt').textContent = st.running
        ? ('刷新简介中… ' + st.done + '/' + st.total + (st.current ? '（当前：' + st.current + '）' : ''))
        : '整理结果…';
    }
    if (!st.running) {
      SUMBATCH.polling = false;
      $('sumBatchBar').style.display = 'none';
      var okN = 0, failN = 0, confirmNames = [], alreadyDeclined = 0;
      Object.keys(st.results).forEach(function (name) {
        var r = st.results[name];
        var lib = LIBS_LIST.find(function (l) { return l.name === name; });
        if (r.ok) {
          okN++;
          if (lib) lib.summary = { text: r.text, source: 'ai' };
          SUMBATCH.skip.delete(name); // 已成功覆盖为 AI 内容，之前的"跳过"记忆作废
          if (SUM.lib === name) {
            $('sumText').value = r.text;
            $('sumMeta').textContent = '当前为 AI 生成内容';
          }
        } else if (r.needs_confirm) {
          // 用户这个批次里已经明确说过"不覆盖"的库，不再每次批量刷新都重新弹窗
          // 打断一次——只静默计入"已跳过"，除非用户专门打开这个库的简介弹窗
          // 再点一次刷新（openSum 的 sumRefresh 会清掉这条免打扰记忆）。
          if (SUMBATCH.skip.has(name)) { alreadyDeclined++; }
          else { confirmNames.push(name); }
        } else {
          failN++;
          toast('「' + name + '」生成失败：' + (r.error || '未知错误'), 'err');
        }
      });
      renderLibs();
      if (confirmNames.length) {
        if (confirm('以下 ' + confirmNames.length + ' 个库的简介是用户手写内容，是否一并覆盖：\n'
            + confirmNames.join('、'))) {
          startSumBatch(confirmNames, true);
          return;
        }
        confirmNames.forEach(function (n) { SUMBATCH.skip.add(n); });
      }
      var skippedTotal = confirmNames.length + alreadyDeclined;
      if (okN || failN || skippedTotal) {
        toast('简介刷新完成：' + okN + ' 个成功' + (failN ? '，' + failN + ' 个失败' : '')
          + (skippedTotal ? '，' + skippedTotal + ' 个手写简介已跳过（如需覆盖请到该库简介弹窗内单独刷新）' : ''));
      }
      return;
    }
    setTimeout(sumBatchPoll, 800);
  }).catch(function () { SUMBATCH.polling = false; $('sumBatchBar').style.display = 'none'; });
}
$('sumBatchBtn').addEventListener('click', function () {
  if (SUMBATCH.polling) { toast('已有刷新任务在运行', 'warn'); return; }
  var names = S.scope.length ? S.scope.slice() : LIBS_LIST.map(function (l) { return l.name; });
  if (!names.length) { toast('没有可刷新的库', 'warn'); return; }
  startSumBatch(names, false);
  toast('已在后台开始批量刷新 ' + names.length + ' 个库的简介');
});
$('sumBatchHide').addEventListener('click', function () {
  SUMBATCH.hidden = true;
  $('sumBatchBar').style.display = 'none';
});

/* ============ 勾选范围（问题44：库内文件/文件夹级勾选建模） ============ */
var SEL = { lib: null, sub: '', changes: {} };  // changes[path] = 'in'|'out'|'neutral'
function openSel(lib) {
  SEL.lib = lib.name; SEL.sub = ''; SEL.changes = {};
  $('selTitle').textContent = '勾选范围 · ' + lib.name;
  openModal('mSel');
  loadSelTree();
}
function selPendingCount() { return Object.keys(SEL.changes).length; }
function selMark(path, action) {
  if (action == null) delete SEL.changes[path];
  else SEL.changes[path] = action;
  $('selSave').disabled = !selPendingCount();
  $('selPend').textContent = selPendingCount() ? ('待保存 ' + selPendingCount() + ' 项') : '';
}
function loadSelTree() {
  $('selList').innerHTML = '<div class="sel-loading">读取中…</div>';
  $('selCrumb').innerHTML = ''; $('selFmts').innerHTML = '';
  API.selection_tree(SEL.lib, SEL.sub).then(function (r) {
    if (r.error) { $('selList').innerHTML = '<div class="sel-loading">' + esc(r.error) + '</div>'; return; }
    renderSelTree(r);
    renderSelCrumb(r);
    renderSelFmts(r);
    renderSelList(r);
  }).catch(function () { $('selList').innerHTML = '<div class="sel-loading">加载失败</div>'; });
}
var ICO_FOLDER = '<svg class="tree-ico sel-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';
var ICO_FILE = '<svg class="sel-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z"/><path d="M14 3v5h5"/></svg>';
SEL.expanded = { '': true };  // 目录树展开态（默认全收起：仅顶层可见）
function renderSelTree(r) {
  var cur = SEL.sub;
  // 当前目录的祖先链自动展开（经右栏下钻/面包屑跳转时保持可见）
  if (cur) {
    var ps = cur.split('/');
    for (var i = 1; i < ps.length; i++) SEL.expanded[ps.slice(0, i).join('/')] = true;
  }
  var parentSet = {};
  r.folders.forEach(function (f) {
    if (!f.path) return;
    var pp = f.path.split('/'); pp.pop();
    parentSet[pp.join('/')] = true;
  });
  function visible(f) {
    if (!f.path) return true;
    var parts = f.path.split('/');
    for (var i = 1; i < parts.length; i++) {
      if (!SEL.expanded[parts.slice(0, i).join('/')]) return false;
    }
    return true;
  }
  var h = r.folders.filter(visible).map(function (f) {
    var ind = f.depth * 14;  // 数字：先拼 'px' 再 +ind 会得 NaN（缩进全失效 = 平铺）
    var kids = !!parentSet[f.path] || f.path === '';
    var caret = kids
      ? '<span class="t-caret' + (SEL.expanded[f.path] ? ' open' : '') + '" data-caret="'
        + esc(f.path) + '" title="' + (SEL.expanded[f.path] ? '收起' : '展开') + '">▸</span>'
      : '<span class="t-caret none"></span>';
    var cls = 'sel-tree-row' + (f.path === cur ? ' on' : '')
      + (f.state === 'out' ? ' excluded' : '');
    var dot = f.state === 'root' ? '' :
      '<span class="t-dot ' + (f.state === 'in' || f.state === 'auto_in' ? 'in' : 'out')
      + (f.explicit ? ' exp' : '') + '" title="' + esc(f.state_text) + '"></span>';
    return '<div class="' + cls + '" style="padding-left:' + (4 + ind) + 'px">' + caret
      + '<button class="sel-tree-name" data-tree="' + esc(f.path) + '">' + ICO_FOLDER
      + '<span class="t-name">' + esc(f.name) + '</span></button>' + dot + '</div>';
  }).join('');
  $('selTree').innerHTML = h;
  Array.prototype.forEach.call($('selTree').querySelectorAll('[data-tree]'), function (b) {
    b.addEventListener('click', function () {
      SEL.sub = b.getAttribute('data-tree');
      if (SEL.sub) SEL.expanded[SEL.sub] = true;  // 进目录即展开其子层
      loadSelTree();
    });
  });
  Array.prototype.forEach.call($('selTree').querySelectorAll('[data-caret]'), function (c) {
    c.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var p = c.getAttribute('data-caret');
      SEL.expanded[p] = !SEL.expanded[p];
      renderSelTree(r);  // 纯前端展开收起，不重新拉数据
    });
  });
}
function renderSelCrumb(r) {
  var parts = r.sub ? r.sub.split('/') : [];
  var h = '<button class="sel-crumb-it" data-sub="">' + esc(r.lib) + '</button>';
  var acc = [];
  parts.forEach(function (p) {
    acc.push(p);
    h += '<span class="sel-crumb-sep">/</span><button class="sel-crumb-it" data-sub="' + esc(acc.join('/')) + '">' + esc(p) + '</button>';
  });
  $('selCrumb').innerHTML = h;
  Array.prototype.forEach.call($('selCrumb').querySelectorAll('.sel-crumb-it'), function (b) {
    b.addEventListener('click', function () {
      SEL.sub = b.getAttribute('data-sub');
      loadSelTree();
    });
  });
}
function renderSelFmts(r) {
  var exts = ['md', 'txt', 'pdf', 'docx'];
  var h = '<span class="sel-fmts-label">格式快捷：</span>';
  h += exts.map(function (x) {
    var on = (r.extensions || []).indexOf(x) >= 0;
    return '<button class="chip sug-chip' + (on ? ' on' : '') + '" data-ext="' + x + '" data-on="' + (on ? '1' : '') + '">'
      + (on ? '✓ ' : '') + x.toUpperCase() + (on ? ' · 收' : ' · 不收') + '</button>';
  }).join('');
  $('selFmts').innerHTML = h;
  Array.prototype.forEach.call($('selFmts').querySelectorAll('.sug-chip'), function (b) {
    b.addEventListener('click', function () {
      var ext = b.getAttribute('data-ext'), on = !!b.getAttribute('data-on');
      askConfirm('格式快捷切换：' + ext.toUpperCase() + ' → ' + (on ? '不收' : '收'),
        '全局格式开关是批量操作：' + (on
          ? '该格式被显式排除的文件将恢复纳入。'
          : '该格式所有文件的单独勾选会被清除并跟随取消（含你显式勾选过的）；文件夹级选择不受影响。'),
        null, on ? '取消该格式' : '收入该格式').then(function (yes) {
          if (!yes) return;
          API.selection_format_bulk(SEL.lib, ext, !on).then(function (res) {
            if (!res.ok) { toast(res.error || '操作失败', 'err'); return; }
            SEL.changes = {}; selMark(null);
            toast('已影响 ' + res.changed + ' 个显式条目');
            loadSelTree();
          });
        });
    });
  });
}
var FX_KNOWN = { md: 'md', pdf: 'pdf', docx: 'docx', txt: 'txt' };
function selRowHTML(it) {
  var effIn = it.state === 'in' || it.state === 'auto_in';
  var pend = SEL.changes[it.path];
  var pendTxt = pend ? '<span class="sel-pend-dot" title="待保存：' + pend + '">✎</span>' : '';
  var badgeCls = effIn ? 'sin' : 'sout';
  var ext = (it.ext || '').toLowerCase();
  var fxKey = FX_KNOWN[ext];
  var fx = it.dir
    ? ''
    : '<span class="fx ' + (fxKey ? 'fx-' + fxKey : 'fx-na') + '">'
      + esc(fxKey ? ext.toUpperCase() : (ext ? ext.slice(0, 5).toUpperCase() : '无类型')) + '</span>';
  var nameHTML = it.dir
    ? '<button class="sel-name sel-dir" data-drill="' + esc(it.path) + '">' + esc(it.name) + '</button>'
    : '<span class="sel-name">' + esc(it.name) + '</span>';
  var follow = it.explicit
    ? '<button class="sel-follow" data-follow="' + esc(it.path) + '" title="清除显式选择，恢复跟随格式">跟随</button>'
    : '';
  var ico = it.dir ? ICO_FOLDER : ICO_FILE;
  return '<div class="sel-row' + (it.dir ? ' is-dir' : (fxKey ? '' : ' na')) + '" data-path="' + esc(it.path) + '">'
    + '<label class="switch sel-ck"><input type="checkbox" data-selck="' + esc(it.path) + '"' + (effIn ? ' checked' : '') + '><i></i></label>'
    + ico + nameHTML + fx + pendTxt
    + '<span class="sel-badge ' + badgeCls + '">' + esc(it.state_text) + '</span>'
    + follow + '</div>';
}
function renderSelList(r) {
  // 自阻断表（问题47）：目录本身就在排除名单里 → 勾选即同位置打架，点击时
  // 立刻弹窗（不等保存）。文件永不自阻断（按类排除下的点名属个别例外）。
  SEL.blocked = {};
  r.dirs.concat(r.files).forEach(function (it) {
    if (it.self_blocked) SEL.blocked[it.path] = true;
  });
  var rows = r.dirs.map(selRowHTML).concat(r.files.map(selRowHTML));
  $('selList').innerHTML = rows.join('')
    || '<div class="sel-loading">（空目录）</div>';
  Array.prototype.forEach.call($('selList').querySelectorAll('[data-drill]'), function (b) {
    b.addEventListener('click', function () {
      SEL.sub = b.getAttribute('data-drill');
      loadSelTree();
    });
  });
  Array.prototype.forEach.call($('selList').querySelectorAll('[data-selck]'), function (ck) {
    ck.addEventListener('change', function () {
      var path = ck.getAttribute('data-selck');
      // 同位置打架（问题47，用户拍板）：勾一个本身就被排除名单指名的目录 →
      // 点击当场弹窗，不等保存。选项：仅本库移除排除并纳入 / 放弃本次勾选。
      if (ck.checked && SEL.blocked && SEL.blocked[path]) {
        askConfirm('同位置矛盾：' + path,
          '它本身就在目录排除名单（exclude_dirs）里，直接勾选纳入不会生效。\n' +
          '· 仅本库移除排除并纳入：只改本库覆盖，全局与其他库不动；\n' +
          '· 放弃本次勾选：保持排除现状。\n' +
          '（直接勾它下面的具体文件则属个别例外，无需弹窗、直接生效。）',
          null, '仅本库移除排除并纳入').then(function (yes) {
            if (!yes) { ck.checked = false; selMark(path, null); return; }
            API.selection_resolve_conflict(SEL.lib, path).then(function (res) {
              if (!res.ok) {
                toast(res.error || '解决失败', 'err');
                ck.checked = false; selMark(path, null); loadSelTree(); return;
              }
              selMark(path, null);
              toast('已移除本库排除并纳入，下轮索引生效');
              loadSelTree();
              if (libLoaded) loadLibs();
            }).catch(function () {
              toast('解决失败', 'err');
              ck.checked = false; selMark(path, null); loadSelTree();
            });
          });
        return;
      }
      // 勾 = 显式纳入；取消 = 显式排除（基准是当前生效态）
      selMark(path, ck.checked ? 'in' : 'out');
    });
  });
  Array.prototype.forEach.call($('selList').querySelectorAll('[data-follow]'), function (b) {
    b.addEventListener('click', function () {
      selMark(b.getAttribute('data-follow'), 'neutral');
      loadSelTree();
    });
  });
}
$('selSave').addEventListener('click', function () {
  if (!selPendingCount()) return;
  var changes = Object.keys(SEL.changes).map(function (p) {
    return { path: p, action: SEL.changes[p] };
  });
  API.selection_update(SEL.lib, changes).then(function (res) {
    if (!res.ok) { toast(res.error || '保存失败', 'err'); return; }
    SEL.changes = {}; selMark(null);
    toast('勾选已保存，下一轮索引自动应用');
    loadSelTree();
    if (libLoaded) loadLibs();
  }).catch(function () { toast('保存失败', 'err'); });
});
$('selGiveup').addEventListener('click', function () { SEL.changes = {}; selMark(null); loadSelTree(); });
$('selClose').addEventListener('click', function () { hideOverlay($('mSel')); });

/* 添加库 */
$('addLibBtn').addEventListener('click', function () {
  $('aName').value = ''; $('aPath').value = '';
  $('aName').classList.remove('bad'); $('aPath').classList.remove('bad');
  $('aNameErr').classList.remove('show'); $('aPathErr').classList.remove('show');
  openModal('mAdd');
});
$('addOk').addEventListener('click', function () {
  var name = $('aName').value.trim();
  var path = $('aPath').value.trim();
  var ok = true;
  if (!name) { $('aNameErr').classList.add('show'); $('aName').classList.add('bad'); ok = false; }
  else { $('aNameErr').classList.remove('show'); $('aName').classList.remove('bad'); }
  if (!path) { $('aPathErr').classList.add('show'); $('aPath').classList.add('bad'); ok = false; }
  else { $('aPathErr').classList.remove('show'); $('aPath').classList.remove('bad'); }
  if (!ok) return;
  API.add_library(path, name).then(function (res) {
    if (!res.ok) { toast(res.error || '添加失败', 'err'); return; }
    hideOverlay($('mAdd'));
    toast('已添加库「' + name + '」，下轮索引生效');
    loadLibs();
  });
});

/* 库配置弹层：覆盖 vs 继承 */
var CFG_KEYS = [
  { key: 'extensions', label: '索引格式 extensions', kind: 'fmt' },
  { key: 'agent_formats', label: 'Agent 门禁（二进制放行）', kind: 'gate' },
  { key: 'exclude_dirs', label: '排除目录 exclude_dirs', kind: 'list', ph: '如 .obsidian,.trash' },
  { key: 'exclude_files', label: '排除文件 exclude_files', kind: 'list', ph: '如 ~$*,desktop.ini' },
  { key: 'exclude_patterns', label: '排除前缀 exclude_patterns', kind: 'list', ph: '如 draft-*' },
  { key: 'chunk_char_limit', label: '单块最大字符 chunk_char_limit', kind: 'num' },
  { key: 'short_doc_char_limit', label: '整篇合并阈值 short_doc_char_limit', kind: 'num' },
  { key: 'collection', label: 'collection 名', kind: 'str', ph: '留空自动生成' }
];
var cfgLibName = null;
function cfgIsOver(key, ov) { return Object.prototype.hasOwnProperty.call(ov, key); }
function joinVal(v) { return Array.isArray(v) ? v.join(',') : (v == null ? '' : String(v)); }
function openCfg(lib) {
  cfgLibName = lib.name;
  $('cTitle').textContent = '库配置 · ' + lib.name;
  API.get_library_config(lib.name).then(function (cfg) {
    var eff = cfg.effective, ov = cfg.overrides || {};
    $('cBody').innerHTML = CFG_KEYS.map(function (k) {
      var over = cfgIsOver(k.key, ov);
      var val = eff[k.key];
      var badge = over ? '<span class="ov-badge over">覆盖</span>' : '<span class="ov-badge inh">继承</span>';
      var clear = over ? '<button class="cfg-clear" data-unset="' + k.key + '">清空恢复继承</button>' : '';
      var inh = over ? '<div class="cfg-inh-val">继承值：' + esc(GLOBAL_EFF[k.key] != null ? GLOBAL_EFF[k.key] : joinVal(val)) + '</div>' : '';
      var ctl = '';
      if (k.kind === 'fmt') {
        ctl = '<div class="cfg-fmts" style="margin:8px 0 0">' + ['md', 'txt', 'pdf', 'docx'].map(function (f) {
          var on = Array.isArray(val) && val.indexOf(f) >= 0;
          return '<label class="ck' + (on ? ' on' : '') + '"><input type="checkbox" value="' + f + '"' + (on ? ' checked' : '') + '>' + f + '</label>';
        }).join('') + '</div>';
      } else if (k.kind === 'gate') {
        // agent_formats 后端只收二进制子集（pdf/docx 中已启用的）；无二进制格式
        // 启用时开关禁用（与 Flet 端一致），免得"开了也落不了授权"误导人。
        var approved = Array.isArray(val) && val.length > 0;
        var binOn = ['pdf', 'docx'].filter(function (f) { return (eff.extensions || []).indexOf(f) >= 0; });
        var noBin = binOn.length === 0;
        ctl = '<div class="cfg-ctl" style="display:flex;align-items:center;gap:10px">'
          + '<label class="switch"><input type="checkbox" data-gate="1"' + (approved && !noBin ? ' checked' : '') + (noBin ? ' disabled' : '') + '><i></i></label>'
          + '<span class="cfg-hint" style="margin:0">' + (noBin ? '未启用二进制格式，无可授权项' : (approved ? '已批准 Agent 自动索引二进制格式' : 'Agent 触发的索引只处理文本类')) + '</span></div>'
          + '<div class="notice n-warn cfg-gate-warn"><span>AI Agent 将无法自动索引该库的二进制文件；批准一次长期有效，可随时撤销。</span></div>';
      } else if (k.kind === 'num') {
        ctl = '<div class="cfg-ctl"><input class="in-text in-num" data-ck="' + k.key + '" type="text" value="' + esc(val == null ? '' : val) + '"></div>';
      } else {
        ctl = '<div class="cfg-ctl"><input class="in-text" data-ck="' + k.key + '" type="text" value="' + esc(joinVal(val)) + '" placeholder="' + esc(k.ph || '') + '"></div>';
      }
      return '<div class="cfg-row"><div class="cfg-row-top"><span class="cfg-key">' + esc(k.label) + '</span>' + badge + clear + '</div>' + ctl + inh + '</div>';
    }).join('');
    window.__CFG = { eff: eff, ov: ov, allKeys: cfg.all_keys };
    bindCfgBody();
    openModal('mCfg');
  }).catch(function () { toast('库配置加载失败', 'err'); });
}
var GLOBAL_EFF = { // 继承值参考（全局默认，仅用于弹层里的「继承值」展示）
  extensions: 'md,pdf,docx', agent_formats: '', exclude_dirs: '.obsidian,.trash',
  exclude_files: '~$*', exclude_patterns: 'draft-*', chunk_char_limit: '600',
  short_doc_char_limit: '200', collection: ''
};
function bindCfgBody() {
  var body = $('cBody');
  Array.prototype.forEach.call(body.querySelectorAll('.ck input'), function (inp) {
    inp.addEventListener('change', function () { inp.closest('.ck').classList.toggle('on', inp.checked); });
  });
  var gate = $('cBody').querySelector('[data-gate]');
  if (gate) {
    var sync = function () {
      var warn = body.querySelector('.cfg-gate-warn');
      if (warn) warn.classList.toggle('show', !gate.checked);
    };
    gate.addEventListener('change', sync);
    sync();
  }
  Array.prototype.forEach.call(body.querySelectorAll('[data-unset]'), function (b) {
    b.addEventListener('click', function () {
      var key = b.getAttribute('data-unset');
      API.unset_library_config(cfgLibName, [key]).then(function () {
        toast('已恢复「' + key + '」为继承值');
        openCfg({ name: cfgLibName });
      });
    });
  });
}
$('cfgOk').addEventListener('click', function () {
  if (!cfgLibName || !window.__CFG) return;
  var eff = window.__CFG.eff;
  var updates = {};
  var fmtOn = [];
  Array.prototype.forEach.call($('cBody').querySelectorAll('.ck input'), function (inp) {
    if (inp.checked) fmtOn.push(inp.value);
  });
  updates.extensions = fmtOn.join(',');
  var gate = $('cBody').querySelector('[data-gate]');
  // 开 = 对话框里已勾选格式中的二进制交集（以后端刚存的 extensions 为准，
  // 用陈旧 eff 会在"取消 pdf 勾选+开门禁"时触发后端"未启用无法授权"）；关/禁用 = 空串转 unset
  var binOn = ['pdf', 'docx'].filter(function (f) { return fmtOn.indexOf(f) >= 0; });
  updates.agent_formats = (gate && gate.checked && !gate.disabled) ? binOn.join(',') : '';
  Array.prototype.forEach.call($('cBody').querySelectorAll('[data-ck]'), function (inp) {
    updates[inp.getAttribute('data-ck')] = inp.value.trim();
  });
  API.set_library_config(cfgLibName, updates).then(function (res) {
    var errs = res && res.errors ? Object.keys(res.errors) : [];
    if (errs.length) { toast('部分配置未通过校验：' + errs.map(function (k) { return k + ' ' + res.errors[k]; }).join('；'), 'err'); return; }
    hideOverlay($('mCfg'));
    toast('库配置已保存，下轮索引生效');
    loadLibs();
  });
});

/* 移除弹层 */
var rmLib = null;
function openRm(lib) {
  rmLib = lib;
  $('rName').textContent = lib.name;
  $('rKeep').classList.add('sel'); $('rDel').classList.remove('sel');
  $('rKeep').querySelector('input').checked = true;
  $('rDel').querySelector('input').checked = false;
  $('rChkLine').style.visibility = 'hidden';
  $('rConfirmChk').checked = false;
  $('rGo').disabled = false;
  openModal('mRm');
}
$('rKeep').addEventListener('click', function () {
  $('rKeep').classList.add('sel'); $('rDel').classList.remove('sel');
  $('rChkLine').style.visibility = 'hidden';
  $('rGo').disabled = false;
});
$('rDel').addEventListener('click', function () {
  $('rDel').classList.add('sel'); $('rKeep').classList.remove('sel');
  $('rChkLine').style.visibility = 'visible';
  $('rGo').disabled = true;
});
$('rConfirmChk').addEventListener('change', function () {
  $('rGo').disabled = !this.checked;
});
$('rGo').addEventListener('click', function () {
  if (!rmLib) return;
  var drop = $('rDel').querySelector('input').checked;
  var name = rmLib.name;
  API.remove_library(name, drop).then(function () {
    hideOverlay($('mRm'));
    toast(drop ? '已注销并删除「' + name + '」的索引数据' : '已注销「' + name + '」，原始文件已保留');
    rmLib = null;
    loadLibs();
    S.scope = S.scope.filter(function (n) { return n !== name; });
    updateScopeUI();
  });
});

/* ============ 索引 ============ */
$('btnIncr').addEventListener('click', function () {
  API.start_index(false, scopeStr()).then(function (res) {
    if (!res.ok) { toast(res.already_running ? '已有索引任务在运行' : '启动失败', 'warn'); return; }
    toast('增量索引已启动 · 范围：' + scopeLabel());
    go('index');
  });
});
$('btnFull').addEventListener('click', function () {
  var targets = !S.scope.length ? S.libs : S.libs.filter(function (l) { return S.scope.indexOf(l.name) >= 0; });
  if (!targets.length) { toast('当前范围内没有可重建的库', 'warn'); return; }
  var tf = 0, tc = 0;
  $('fullRows').innerHTML = targets.map(function (l) {
    tf += l.files; tc += l.chunks;
    return '<div class="full-row"><span class="n">' + esc(l.name) + '</span><span class="v">' + fmtInt(l.files) + ' 文件 · ' + fmtInt(l.chunks) + ' 块</span></div>';
  }).join('') + '<div class="full-row total"><span class="n">合计（约）</span><span class="v">' + fmtInt(tf) + ' 文件 · ' + fmtInt(tc) + ' 块</span></div>';
  openModal('mFull');
});
$('fullGo').addEventListener('click', function () {
  hideOverlay($('mFull'));
  API.start_index(true, scopeStr()).then(function (res) {
    if (!res.ok) { toast(res.already_running ? '已有索引任务在运行' : '启动失败', 'warn'); return; }
    toast('全量重建已启动，预计 3-5 分钟');
    go('index');
  });
});
function doStop() {
  API.stop_index().then(function (res) {
    if (res.ok && res.stopped) { toast('已停止本轮索引，已完成部分保留'); $('stopReason').textContent = ''; }
    else { $('stopReason').textContent = res.reason || '无法停止'; }
  }).catch(function () { $('stopReason').textContent = '停止请求失败，请查看日志'; });
}
$('btnStopIdx').addEventListener('click', doStop);
$('iStop').addEventListener('click', function (e) { e.stopPropagation(); doStop(); });

/* ============ 提取试验台 ============ */
var labPolling = false, labStart = 0;
function labShowTab(html) {
  $('labTabHtml').classList.toggle('on', html);
  $('labTabMd').classList.toggle('on', !html);
  $('labTabHtml').setAttribute('aria-selected', html ? 'true' : 'false');
  $('labTabMd').setAttribute('aria-selected', html ? 'false' : 'true');
  $('labHtml').style.display = html ? 'block' : 'none';
  $('labMd').style.display = html ? 'none' : 'block';
}
$('labTabHtml').addEventListener('click', function () { labShowTab(true); });
$('labTabMd').addEventListener('click', function () { labShowTab(false); });
$('labBackend').addEventListener('change', function () {
  $('labWarn').classList.toggle('show', this.value === 'mineru-cloud');
});
function labReset() {
  $('labLoading').style.display = 'none';
  $('labTimeout').style.display = 'none';
  $('labCancel').disabled = true;
  labPolling = false;
}
function wireBrowse(btnId, inputId, mode) {
  var b = $(btnId);
  if (!b) return;
  b.addEventListener('click', function () {
    var input = $(inputId);
    API.pick_path(mode, input ? input.value : '').then(function (r) {
      if (r && r.path && input) input.value = r.path;
    }).catch(function () { toast('打开选择窗口失败', 'err'); });
  });
}
wireBrowse('labBrowse', 'labPath', 'file');
wireBrowse('aPathBrowse', 'aPath', 'dir');
$('labRun').addEventListener('click', function () {
  var p = $('labPath').value.trim();
  if (!p) { toast('请先粘贴要提取的文件路径', 'warn'); $('labPath').focus(); return; }
  var backend = $('labBackend').value || null;
  API.preview_start(p, backend).then(function (res) {
    if (!res.ok) { toast(res.error || '启动失败', 'err'); return; }
    labStart = Date.now();
    labPolling = true;
    $('labEmpty').style.display = 'none';
    $('labHtml').style.display = 'none';
    $('labMd').style.display = 'none';
    $('labLoading').style.display = 'flex';
    $('labStatus').textContent = '运行中…';
    $('labCancel').disabled = false;
    labPoll();
  });
});
var ROUTE_NAME = { local: '本地直提', 'mineru-text': 'MinerU 结构识别', 'ocr:mineru-cloud': 'MinerU 云端 OCR', 'ocr:mineru-local': 'MinerU 本地解析', '源文件': '源文件直读', '-': '未知通道' };
function labPoll() {
  if (!labPolling) return;
  API.preview_poll().then(function (st) {
    if (!labPolling) return;
    var secs = (Date.now() - labStart) / 1000;
    $('labTimeout').style.display = secs > 120 ? 'flex' : 'none';
    if (st.done) {
      labReset();
      var r = st.result || {};
      if (r.ok && !r.reason) {
        $('labHtml').innerHTML = r.rendered_html || '';
        $('labMd').textContent = r.markdown || '';
        $('labEmpty').style.display = 'none';
        labShowTab(true);
        $('labStatus').textContent = '完成 · ' + (ROUTE_NAME[r.route] || r.route || '未知通道')
          + ' · ' + (r.chars || 0) + '字 · 管线' + (r.elapsed || 0) + 's';
      } else if (r.ok && r.reason) {
        // 管线正常跑完但未产出（扫描件跳过/缺 Key/空文件…）：如实展示原因，
        // 不再冒充"完成"。此前 reason 被桥丢弃，此分支永远到不了。
        var label = REASON_LABEL[r.reason] || r.reason;
        var advice = REASON_ADVICE[r.reason] || '';
        $('labEmpty').style.display = 'block';
        $('labEmpty').innerHTML = '<div class="grotesk">未产出 · ' + esc(label) + '</div><div>'
          + esc(advice || '换个后端或检查文件后重试') + '</div>';
        $('labStatus').textContent = '未产出 · ' + label;
        toast('提取未产出：' + label + (advice ? '（' + advice + '）' : ''), 'warn');
      } else {
        $('labEmpty').style.display = 'block';
        $('labStatus').textContent = '失败：' + (r.error || '未知错误');
        toast('提取失败：' + (r.error || '未知错误'), 'err');
      }
      return;
    }
    $('labStatus').textContent = '运行中… ' + Math.round(secs) + 's';
    setTimeout(labPoll, 500);
  }).catch(function () { labReset(); $('labStatus').textContent = '轮询失败'; });
}
$('labCancel').addEventListener('click', function () {
  API.preview_cancel().then(function () {
    labReset();
    $('labEmpty').style.display = 'block';
    $('labStatus').textContent = '已取消';
    toast('已取消提取任务', 'warn');
  });
});

/* ============ 诊断 ============ */
function buildFailLibOptions() {
  var sel = $('failLib');
  var cur = sel.value;
  sel.innerHTML = '<option value="">全部库</option>' + S.libs.map(function (l) {
    return '<option value="' + esc(l.name) + '">' + esc(l.name) + '</option>';
  }).join('');
  sel.value = cur || '';
}
function buildWemmLibOptions() {
  var sel = $('wemmLibSel');
  var cur = sel.value;
  sel.innerHTML = S.libs.map(function (l) {
    return '<option value="' + esc(l.name) + '">' + esc(l.name) + '</option>';
  }).join('');
  sel.value = cur || (S.libs[0] ? S.libs[0].name : '');
}
$('failLib').addEventListener('change', loadFailures);
var S_fail = { rows: [] };   // 供详情行展开用
function failBadge(reason) {
  if (reason === 'unreadable' || reason === 'extract-failed') {
    return '<span class="badge" style="background:var(--err-dim);color:var(--err)">' + esc(REASON_LABEL[reason] || reason) + '</span>';
  }
  if (reason === 'empty') {
    return '<span class="badge" style="background:var(--s2);color:var(--sec)">' + esc(REASON_LABEL[reason] || reason) + '</span>';
  }
  return '<span class="badge badge-lo">' + esc(REASON_LABEL[reason] || reason) + '</span>';
}
/* 白话解释：为什么会这样 + 怎么改/怎么避免。故障树每档尽量给出可执行动作。 */
var REASON_WHY = {
  scanned: '这份 PDF 里有没有文字层的页（纯扫描件/拍照件/“PPT 文字页+扫描图”混装），当前 OCR 后端没把它认进来——不是文件坏了。超页上限（默认 200 页）被跳过的也在这里。',
  unreadable: '文件打不开或读到一半出错，最常见是文件损坏、正在被别的程序占用（Word/WPS/OneDrive/杀软锁着）、或上传只传了一半。',
  'extract-failed': '提取这一步失败了：云端 OCR 报错（缺 Key / 额度用尽 / 网络中断 / 服务端限流）或本地解析抛异常。文件本身没坏，多半是当时环境问题。',
  empty: '程序成功打开并解析了，但里面没有任何正文（全是图片又没开 OCR、或整篇是空页），没有内容可索引。',
  tbd: '文件正文里满是“待办/占位”标记（TBD/TODO 等），被当成草稿跳过了——它还没写完，不是故障。',
  unknown: '索引中断/历史遗留导致状态不完整。'
};
var REASON_FIX = {
  scanned: '做法：①没开 OCR——设置里把 pdf_scan_backend 设为 mineru-cloud（需 Key）或 mineru-local（需装 mineru 环境），保存后重建自动重试转正；②超页上限——拆分成 200 页以下的小份再重建。',
  unreadable: '做法：关掉正在占用它的程序再重建；文件真损坏就从源头重新导出一份替换，下轮自动识别为新文件重新进来。',
  'extract-failed': '做法：多数情况什么都不用做，环境恢复后自动重试。若持续失败：①云端——去 mineru.net 看 Key 是否有效/额度是否用完，补好后重建；②本地——把报错日志复制给维护者。',
  empty: '做法：确认它到底该不该有内容。该有却读成空 → 可能是扫描件，参考“扫描件待 OCR”那条；确实是空文件 → 无需处理，它不会反复打扰你。',
  tbd: '做法：不用管。等这份文件写完（去掉占位标记、有实质内容）后自动就会进索引。',
  unknown: '做法：对这个库做一次全量重建（保险、干净的归宿）。'
};
function copyText(text, btn) {
  if (!text) return;
  function done(ok) {
    toast(ok ? '已复制日志' : '复制失败（浏览器限制），请到右侧日志面板手动复制', ok ? 'ok' : 'err');
    if (btn) { btn.textContent = ok ? '已复制 ✓' : '复制日志'; setTimeout(function () { btn.textContent = '复制日志'; }, 1600); }
  }
  function legacy() {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      done(document.execCommand('copy')); ta.remove();
    } catch (e) { done(false); }
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, legacy);
  } else { legacy(); }
}
function failDetailHtml(r, isAll) {
  var why = REASON_WHY[r.reason] || '';
  var fix = REASON_FIX[r.reason] || (REASON_ADVICE[r.reason] || '');
  var retry = r.will_retry
    ? '<div class="fd-line" style="color:var(--warn)">✅ 下轮索引会自动重试：OCR 后端/Key 和这条失败记录产生时不一样了，配置已具备转正条件。</div>'
    : '<div class="fd-line" style="color:var(--ph)">不会自动重试：原因和当前配置一致，改内容/换后端后重建才会再走一遍。</div>';
  var where = isAll ? '<div class="fd-line"><span class="libchip">' + esc(r.lib) + '</span> <span class="mono">' + esc(r.rel) + '</span></div>' : '';
  var btns = '<button class="btn btn-sm btn-ghost" data-fopen="1">打开源文件</button>'
    + (r.detail && r.detail.length
        ? '<button class="btn btn-sm btn-ghost" data-fcopy="1">复制日志</button>'
        : '<button class="btn btn-sm btn-ghost" disabled title="索引日志暂无该文件记录">复制日志</button>');
  return where
    + '<div class="fd-sec">为什么没进来</div>' + '<div class="fd-line fd-why">' + esc(why || '（无说明）') + '</div>'
    + '<div class="fd-sec">怎么处理</div>' + '<div class="fd-line fd-fix">' + esc(fix || '—') + '</div>'
    + retry
    + btns;
}
function loadFailures() {
  API.failures($('failLib').value).then(function (d) {
    S_fail.rows = d.rows || [];
    var hd = '共 ' + d.total + ' 条失败';
    if (d.healthy) hd += ' · 正常 ' + d.healthy + ' 份';
    if (d.multi) hd += '（全部库）';
    $('failTotal').textContent = hd;
    var isAll = !!d.multi;
    $('failBody').innerHTML = S_fail.rows.map(function (r, i) {
      var retry = r.will_retry
        ? ' <span class="mono" style="font-size:10.5px;color:var(--warn)">will_retry</span>'
        : '';
      var name = (isAll ? '<span class="libchip">' + esc(r.lib) + '</span> ' : '') + esc(r.rel);
      return '<tr class="frow" data-i="' + i + '"><td class="p" title="' + esc(r.rel) + '">' + name + '</td>'
        + '<td>' + failBadge(r.reason) + retry + ' <span class="mono fd-hint">详情▾</span></td>'
        + '<td class="g">' + esc(REASON_ADVICE[r.reason] || '') + '</td></tr>'
        + '<tr class="fd" data-i="' + i + '" style="display:none"><td colspan="3">' + failDetailHtml(r, isAll) + '</td></tr>';
    }).join('')
      || '<tr><td colspan="3" class="g" style="text-align:center;color:var(--ph)">没有失败记录</td></tr>';
  }).catch(function () { toast('失败明细加载失败', 'err'); });
}
$('failBody').addEventListener('click', function (e) {
  var cbtn = e.target.closest('[data-fcopy]');
  if (cbtn) {
    var crow = cbtn.closest('tr').previousElementSibling;
    var cr = S_fail.rows[+(crow && crow.getAttribute('data-i'))];
    if (cr && cr.detail) copyText(cr.detail.join('\n'), cbtn);
    return;
  }
  var btn = e.target.closest('[data-fopen]');
  if (btn) {
    var row = btn.closest('tr').previousElementSibling;
    var r = S_fail.rows[+(row && row.getAttribute('data-i'))];
    if (r) API.open_source(r.lib, r.rel, '').then(function (res) {
      toast(res.ok ? '已打开源文件' : ('打开失败：' + (res.error || '未知')), res.ok ? 'ok' : 'err');
    });
    return;
  }
  var tr = e.target.closest('tr.frow');
  if (!tr) return;
  var idx = tr.getAttribute('data-i');
  var det = tr.nextElementSibling;
  if (det && det.classList.contains('fd')) det.style.display = det.style.display === 'none' ? '' : 'none';
});
$('wemmProbeBtn').addEventListener('click', function () {
  var btn = this;
  btn.disabled = true;
  $('wemmProbeOut').innerHTML = '<span class="rel-loading"><span class="mini-spin"></span>正在探测 wemm_server…</span>';
  API.wemm_probe().then(function (r) {
    btn.disabled = false;
    $('wemmProbeOut').innerHTML = r.alive
      ? '<span class="badge badge-hi">服务在线</span> <span class="mono" style="font-size:11px;color:var(--sec)">' + esc(r.detail) + '</span>'
      : '<span class="badge" style="background:var(--err-dim);color:var(--err)">服务未运行</span> <span style="font-size:11.5px;color:var(--ph)">' + esc(r.detail) + ' · 可在设置中开启页级视觉索引</span>';
  }).catch(function () { btn.disabled = false; $('wemmProbeOut').textContent = '探测请求失败'; });
});
$('wemmLibSel').addEventListener('change', loadWemmStatus);
var S_wemm = { rows: [] };
function loadWemmStatus() {
  var lib = $('wemmLibSel').value;
  if (!lib) return;
  API.wemm_status(lib).then(function (d) {
    S_wemm.rows = d.rows || [];
    $('wemmBody').innerHTML = S_wemm.rows.map(function (r, i) {
      return '<tr class="wrow" data-i="' + i + '"><td class="p" title="' + esc(r.rel) + '">' + esc(r.rel) + '</td>'
        + '<td class="mono">' + (r.pages != null ? r.pages + ' 页' : '--') + '</td>'
        + '<td>' + (r.failed
          ? '<span class="badge" style="background:var(--err-dim);color:var(--err)">' + esc(r.reason || '失败') + '</span>'
          : '<span class="badge badge-hi">页库就绪</span>') + ' <span class="mono fd-hint">详情▾</span></td></tr>'
        + '<tr class="wd" data-i="' + i + '" style="display:none"><td colspan="3">'
        + (r.failed
          ? '<div class="fd-sec">为什么没建成</div>'
            + '<div class="fd-line fd-why">' + esc((REASON_WHY[r.reason] || '') + '（看图服务未就绪/渲染超时等都会导致单页失败，下轮会自动重试。）') + '</div>'
            + '<div class="fd-sec">怎么处理</div>'
            + '<div class="fd-line fd-fix">' + esc(REASON_FIX[r.reason] || '开启或重启 WEMM 看图服务后重建页库即可。') + '</div>'
          : '<div class="fd-line">已为这份 PDF 生成 ' + r.pages + ' 页向量 · ' + esc(r.lib) + '</div>')
        + '<button class="btn btn-sm btn-ghost" data-wopen="1">打开源文件</button></td></tr>';
    }).join('') || '<tr><td colspan="3" class="g" style="text-align:center;color:var(--ph)">该库暂无页向量数据' + (d.exists ? '' : '（页库不存在）') + '</td></tr>';
  }).catch(function () {});
}
$('wemmBody').addEventListener('click', function (e) {
  var btn = e.target.closest('[data-wopen]');
  if (btn) {
    var row = btn.closest('tr').previousElementSibling;
    var r = S_wemm.rows[+(row && row.getAttribute('data-i'))];
    if (r) API.open_source(r.lib, r.rel, '').then(function (res) {
      toast(res.ok ? '已打开源文件' : ('打开失败：' + (res.error || '未知')), res.ok ? 'ok' : 'err');
    });
    return;
  }
  var tr = e.target.closest('tr.wrow');
  if (!tr) return;
  var idx = tr.getAttribute('data-i');
  var det = tr.nextElementSibling;
  if (det && det.classList.contains('wd')) det.style.display = det.style.display === 'none' ? '' : 'none';
});
$('dupThresh').addEventListener('input', function () {
  $('dupThVal').textContent = parseFloat(this.value).toFixed(2);
});
$('dupRunBtn').addEventListener('click', function () {
  var btn = this;
  btn.disabled = true;
  $('dupList').innerHTML = '<div class="dup"><div class="rel-loading"><span class="mini-spin"></span>正在读全库文件做 MinHash 指纹，慢操作约十几秒…</div></div>';
  $('dupStats').textContent = '';
  API.dedup_run(parseFloat($('dupThresh').value)).then(function (d) {
    btn.disabled = false;
    if (d.error) { $('dupList').innerHTML = ''; toast('去重失败：' + d.error, 'err'); return; }
    $('dupStats').textContent = d.stats ? fmtInt(d.stats.files) + ' 文件 · ' + d.stats.clusters + ' 簇 · ' + d.stats.seconds + 's' : '';
    $('dupList').innerHTML = (d.clusters || []).map(function (c) {
      var cls = c.sim >= 0.9 ? 'badge-hi' : 'badge-mid';
      return '<div class="dup"><div class="dup-top"><span class="badge ' + cls + '">' + c.sim.toFixed(2) + '</span>'
        + '<div class="dup-pair">' + esc(c.a) + '<em>与 ' + esc(c.b) + ' 近似</em></div></div></div>';
    }).join('') || '<div class="dup"><div class="rel-loading">该阈值下没有发现近似重复对</div></div>';
  }).catch(function () { btn.disabled = false; $('dupList').innerHTML = ''; toast('去重分析失败', 'err'); });
});
$('expBtn').addEventListener('click', function () {
  if (S.ioRunning) return;
  S.ioRunning = true;
  var prog = $('ioProg');
  prog.classList.add('run');
  prog.querySelector('i').style.transform = 'scaleX(0)';
  void prog.offsetWidth;
  prog.querySelector('i').style.transform = 'scaleX(1)';
  API.export_run().then(function () {
    toast('导出已启动，子进程输出见日志');
    toggleLog(true);
    setTimeout(function () { prog.classList.remove('run'); S.ioRunning = false; }, 2600);
  }).catch(function () { prog.classList.remove('run'); S.ioRunning = false; toast('导出启动失败', 'err'); });
});
$('impBtn').addEventListener('click', function () {
  $('impText').value = '';
  $('impErr').classList.remove('show');
  $('impGo').disabled = true;
  openModal('mImport');
});
$('impText').addEventListener('input', function () {
  var ok = this.value === '我确认导入';
  $('impGo').disabled = !ok;
  $('impErr').classList.toggle('show', this.value.length > 0 && !ok);
});
$('impGo').addEventListener('click', function () {
  if ($('impText').value !== '我确认导入') return;
  API.import_run($('impText').value).then(function (res) {
    if (!res.ok) { toast(res.error || '导入失败', 'err'); return; }
    hideOverlay($('mImport'));
    toast('导入已启动，子进程输出见日志');
    toggleLog(true);
  });
});

/* ============ 设置 ============ */
var SET = { loaded: false, groups: [], orig: {} };
function loadSettings() {
  API.get_settings().then(function (data) {
    SET.groups = data.groups || [];
    SET.loaded = true;
    if (data.missing_keys && data.missing_keys.length) {
      $('setMissing').style.display = 'flex';
      $('setMissing').querySelector('span').textContent = '配置文件缺失以下键，已用默认值补齐：' + data.missing_keys.join(', ');
    }
    buildSettings();
  }).catch(function () { toast('设置加载失败', 'err'); });
}
function buildSettings() {
  $('sgNav').innerHTML = SET.groups.map(function (g, i) {
    return '<button class="sg-item' + (i === 0 ? ' on' : '') + '" data-sg="' + i + '">' + esc(g.title)
      + (g.level === 'advanced' ? '<span class="adv-tag">高级</span>' : '') + '</button>';
  }).join('');
  $('sgPanelWrap').innerHTML = SET.groups.map(function (g, i) {
    var rows = g.fields.map(function (f) {
      SET.orig[f.key] = f.value;
      return fieldRow(f);
    }).join('');
    return '<div class="sg-panel' + (i === 0 ? ' on' : '') + '" data-sp="' + i + '">'
      + '<div class="sg-group-title">' + esc(g.title) + '</div>'
      + '<div class="sg-group-desc">' + esc(g.desc || '') + '</div>'
      + rows + '</div>';
  }).join('');
  Array.prototype.forEach.call($('sgNav').querySelectorAll('.sg-item'), function (b) {
    b.addEventListener('click', function () {
      var idx = b.getAttribute('data-sg');
      Array.prototype.forEach.call($('sgNav').querySelectorAll('.sg-item'), function (x) { x.classList.toggle('on', x === b); });
      Array.prototype.forEach.call($('sgPanelWrap').querySelectorAll('.sg-panel'), function (p) {
        p.classList.toggle('on', p.getAttribute('data-sp') === idx);
      });
    });
  });
  bindSuggest();
  bindPickButtons();
}
function bindPickButtons() {
  Array.prototype.forEach.call($('sgPanelWrap').querySelectorAll('[data-pickfor]'), function (btn) {
    btn.addEventListener('click', function () {
      var input = $(btn.getAttribute('data-pickfor'));
      if (!input) return;
      API.pick_path(btn.getAttribute('data-pickmode'), input.value).then(function (r) {
        if (r && r.path) { input.value = r.path; toast('已选择路径'); }
      }).catch(function () { toast('打开选择窗口失败', 'err'); });
    });
  });
}
/* 需要原生选择弹窗的路径字段：key → 'dir' | 'file' */
var PICK_FIELDS = { vault: 'dir', wemm_python: 'file' };
function fieldRow(f) {
  var ctl = '';
  var ctlId = 'f_' + f.key;
  if (f.choices && f.choices.length) {
    ctl = '<select id="' + ctlId + '"' + (f.secret ? ' class="in-sel"' : ' class="in-sel"') + '>'
      + f.choices.map(function (c) {
        return '<option value="' + esc(c[0]) + '"' + (c[0] === f.value ? ' selected' : '') + '>' + esc(c[1]) + '</option>';
      }).join('') + '</select>';
  } else if (f.kind === 'bool') {
    var on = f.value === 'true';
    ctl = '<label class="switch"><input type="checkbox" id="' + ctlId + '"' + (on ? ' checked' : '') + '><i></i></label>';
  } else {
    var cls = 'in-text' + (f.secret ? ' in-pw' : '') + (f.kind === 'int' || f.kind === 'float' ? ' in-num' : (f.kind === 'list' ? ' in-list' : ''));
    ctl = '<input type="' + (f.secret ? 'password' : 'text') + '" id="' + ctlId + '" class="' + cls + '" value="' + esc(f.value) + '" autocomplete="off" '
      + (f.kind === 'int' || f.kind === 'float' ? 'style="width:90px"' : f.kind === 'list' ? 'style="width:240px"' : '') + '>';
    if (!f.secret && PICK_FIELDS[f.key]) {
      ctl += '<button type="button" class="btn btn-ghost btn-sm" data-pickfor="' + ctlId + '" data-pickmode="' + PICK_FIELDS[f.key] + '">浏览…</button>';
    }
  }
  var sug = '';
  if (f.suggest && f.suggest.length) {
    sug = '<div class="suggest-row" data-sugfor="' + ctlId + '">'
      + f.suggest.map(function (s) {
        var cur = s[0] === f.value;
        return '<button type="button" class="chip sug-chip' + (cur ? ' on' : '') + '" data-val="' + esc(s[0]) + '" data-label="' + esc(s[1]) + '">'
          + (cur ? '✓ ' : '') + esc(s[0]) + ' · ' + esc(s[1]) + '</button>';
      }).join('') + '</div>';
  }
  return '<div class="f-row"><div class="f-meta">'
    + '<div class="f-label">' + esc(f.label) + (f.rebuild ? '<span class="rebuild-mark" title="修改后需全量重建">⟳ 重建</span>' : '') + '</div>'
    + '<div class="f-help">' + esc(f.hint || '') + '</div>'
    + '<div class="f-err" id="ferr_' + f.key + '"></div>'
    + '</div><div class="f-ctl">' + ctl + '</div>' + sug + '</div>';
}
function bindSuggest() {
  Array.prototype.forEach.call($('sgPanelWrap').querySelectorAll('.suggest-row'), function (row) {
    var target = $(row.getAttribute('data-sugfor'));
    if (!target) return;
    Array.prototype.forEach.call(row.querySelectorAll('.sug-chip'), function (chip) {
      chip.addEventListener('click', function () {
        var val = chip.getAttribute('data-val'), label = chip.getAttribute('data-label') || '';
        if (target.type === 'checkbox') { target.checked = val === 'true'; return; }
        target.value = val;
        Array.prototype.forEach.call(row.querySelectorAll('.sug-chip'), function (c) {
          var on = c.getAttribute('data-val') === target.value;
          c.classList.toggle('on', on);
          c.textContent = (on ? '✓ ' : '') + c.getAttribute('data-val') + ' · ' + (c.getAttribute('data-label') || '');
        });
      });
    });
  });
}
function boolVal(el) { return el.checked ? 'true' : 'false'; }
$('saveBtn').addEventListener('click', function () {
  var updates = {};
  var gated = [];
  SET.groups.forEach(function (g) {
    g.fields.forEach(function (f) {
      var el = $('f_' + f.key);
      if (!el) return;
      updates[f.key] = f.kind === 'bool' ? boolVal(el) : el.value;
    });
  });
  // 云端同意门禁
  ['pdf_scan_backend', 'pdf_text_backend'].forEach(function (k) {
    var nv = updates[k], ov = SET.orig[k];
    if (nv && nv.indexOf('mineru-cloud') >= 0 && (ov == null || ov.indexOf('mineru-cloud') < 0)) gated.push(k);
  });
  var proceed = gated.length
    ? askConfirm('启用 MinerU 云端通道',
        '开启后，相应 PDF 页面将被上传至 MinerU 云端 API 做视觉识别。这是第三方在线服务，文件内容将离开本机。',
        '含敏感内容的扫描件不建议开启。API Key 仅存本机，不会写入日志。', '同意并保存')
    : Promise.resolve(true);
  proceed.then(function (yes) {
    if (!yes) {
      gated.forEach(function (k) {
        var el = $('f_' + k);
        if (el) {
          if (el.type === 'checkbox') el.checked = SET.orig[k] === 'true';
          else el.value = SET.orig[k];
        }
        delete updates[k];
      });
      toast('已取消启用云端通道，相关设置已回退', 'warn');
      if (!Object.keys(updates).length) return;
    }
    API.save_settings(updates).then(function (res) {
      var errs = (res && res.errors) || {};
      var keys = Object.keys(errs);
      Array.prototype.forEach.call($('sgPanelWrap').querySelectorAll('.f-err'), function (e) { e.classList.remove('show'); });
      if (keys.length) {
        $('setErr').style.display = 'block';
        $('setErr').textContent = '以下字段未通过校验：' + keys.map(function (k) { return k + '（' + errs[k] + '）'; }).join('、');
        keys.forEach(function (k) {
          var fe = $('ferr_' + k);
          if (fe) { fe.textContent = errs[k]; fe.classList.add('show'); }
        });
        return;
      }
      $('setErr').style.display = 'none';
      Object.keys(updates).forEach(function (k) { SET.orig[k] = updates[k]; });
      toast('已保存，配置已热读生效');
    }).catch(function () { toast('保存失败', 'err'); });
  });
});

/* ============ 日志区 ============ */
var logCursor = null, logOpen = false, errCount = 0, warnCount = 0;
function toggleLog(open) {
  logOpen = open === undefined ? !logOpen : open;
  $('logDrawer').classList.toggle('open', logOpen);
  if (logOpen && logCursor === null) pollLog();
}
$('logBtn').addEventListener('click', function () { toggleLog(); });
$('logCloseBtn').addEventListener('click', function () { toggleLog(false); });
$('logClear').addEventListener('click', function () {
  $('logBody').innerHTML = '<div class="log-line"><span class="lv-info">INFO</span><span class="log-time mono">--</span><span class="log-txt">视图已清空（仅本地显示，不影响日志文件）</span></div>';
  errCount = 0; warnCount = 0;
  $('logCountErr').textContent = '0';
  $('logCountWarn').textContent = '0';
});
$('logDirBtn').addEventListener('click', function () {
  API.get_static_path('log_dir').then(function (r) {
    return API.open_path(r.path);
  }).then(function () { toast('已打开日志目录'); });
});
function logLineEl(line) {
  var lv = 'info';
  if (/ ERROR /.test(line) || /ERROR:/.test(line)) { lv = 'error'; errCount++; }
  else if (/ WARNING /.test(line) || /WARNING:/.test(line)) { lv = 'warning'; warnCount++; }
  var m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$/);
  var time = m ? m[1] : '';
  var txt = m ? m[2] : line;
  var div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = '<span class="log-lv lv-' + lv + '">' + lv.toUpperCase() + '</span>'
    + '<span class="log-time mono">' + esc(time) + '</span><span class="log-txt"></span>';
  div.lastChild.textContent = txt;
  return div;
}
function appendLogLines(lines) {
  if (!lines || !lines.length) return;
  var body = $('logBody');
  var atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
  lines.forEach(function (l) { body.appendChild(logLineEl(String(l))); });
  $('logCountErr').textContent = String(errCount);
  $('logCountWarn').textContent = String(warnCount);
  if (atBottom) body.scrollTop = body.scrollHeight;
}
function pollLog() {
  if (!logOpen && logCursor !== null) return;
  API.log_tail(logCursor).then(function (d) {
    logCursor = d.cursor;
    appendLogLines(d.lines);
    setTimeout(pollLog, 3000);
  }).catch(function () { setTimeout(pollLog, 5000); });
}
window.addEventListener('log', function (e) {
  try {
    var d = typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail;
    appendLogLines(d.lines);
    if (d.cursor != null) logCursor = d.cursor;
  } catch (err) {}
});

/* ============ 图谱引擎（移植 demo7，数据来自 graph() 契约） ============ */
var G = {
  ready: false, nodes: [], byId: {}, links: [], owns: [], sem: [], semLoaded: false,
  libs: [], anchors: {}, themes: [],
  ly: { bilat: true, sem: false, own: true, page: false, cache: true, pdf: true, bounds: true, th: 0.62 },
  colorMode: 'lib', topK: 5,
  qNode: null, redges: [], searching: false, insOpen: false,
  cam: { x: 0, y: 0, k: 1 }, nodeEls: {}
};
var gStage, gWorld, panning = false, dragging = null, dragMoved = 0, panStart = null, downPos = null;
var sim = { alpha: 0, frame: 0, active: false };
var fitOnSettle = false, needRender = true, gRipples = [], gCamTween = null;
var gBilatEls = [], gOwnEls = [], gSemEls = [], gBoundEls = {}, gTagEls = {};
var G_DIM = 16, G_SIGMA = 1.0;

function gLibIdx(name) { return G.libs.indexOf(name); }
function gLibColor(name) { return LIB_COLORS[gLibIdx(name) % LIB_COLORS.length]; }
function baseName(rel) { return String(rel).split('\\').pop().split('/').pop(); }
function gLabel(n) {
  if (n.type === 'page') return 'p.' + n.page;
  return baseName(n.rel).replace(/\.(md|txt|docx|pdf)$/i, '');
}
function gVisible(n) {
  if (!n) return false;
  if (S.scope.length && S.scope.indexOf(n.lib) < 0) return false;
  if (n.type === 'page') return G.ly.page;
  if (n.type === 'cache') return G.ly.cache;
  if (n.type === 'pdf') return G.ly.pdf;
  return true;
}
function gOwnerOf(n) { return n._owner ? G.byId[n._owner] : null; }
function gPdfOf(n) {
  if (n.type === 'pdf') return n;
  var cur = n;
  for (var i = 0; i < 3; i++) {
    var o = gOwnerOf(cur);
    if (!o) return null;
    if (o.type === 'pdf') return o;
    cur = o;
  }
  return null;
}
function gPdfState(n) {
  if (n.type === 'md') return null;
  if (n.type === 'pdf') {
    var m = n.pipeline.mineru, w = n.pipeline.wemm;
    if (m === 'failed') return 'failed';
    if (m === 'queued' || m === 'none') return 'none';
    return w === 'done' ? 'ok' : 'partial';
  }
  var pdf = gPdfOf(n);
  return pdf ? gPdfState(pdf) : null;
}
/* 确定性语义向量（仅用于布局力学与近邻展示） */
function buildVectors() {
  var centroids = {};
  G.themes.forEach(function (t) {
    var rnd = mulberry32(hashStr('theme:' + t));
    var v = [];
    for (var i = 0; i < G_DIM; i++) v.push(rnd() * 2 - 1);
    centroids[t] = gNorm(v);
  });
  G.nodes.forEach(function (n) {
    if (n.type === 'page') { n.vec = null; return; }
    var rnd = mulberry32(hashStr(n.rel));
    var u = [];
    for (var i = 0; i < G_DIM; i++) u.push(rnd() * 2 - 1);
    gNorm(u);
    var ct = centroids[n.theme] || centroids.general || centroids[G.themes[0]];
    var v = [];
    for (var j = 0; j < G_DIM; j++) v.push(ct[j] + u[j] * G_SIGMA);
    n.vec = gNorm(v);
  });
  G.nodes.forEach(function (n) { if (n.type === 'page' && n._owner && G.byId[n._owner]) n.vec = G.byId[n._owner].vec; });
  // 语义引力对（布局用，与语义边图层无关）
  G.grav = [];
  var docs = G.nodes.filter(function (n) { return n.type !== 'page'; });
  for (var i = 0; i < docs.length; i++) {
    for (var j = i + 1; j < docs.length; j++) {
      var s = gCos(docs[i], docs[j]);
      if (s >= 0.62) G.grav.push({ a: docs[i].id, b: docs[j].id, s: s });
    }
  }
}
function gNorm(v) {
  var s = 0, i;
  for (i = 0; i < G_DIM; i++) s += v[i] * v[i];
  s = Math.sqrt(s) || 1;
  for (i = 0; i < G_DIM; i++) v[i] /= s;
  return v;
}
function gCos(a, b) {
  if (!a.vec || !b.vec) return 0;
  var s = 0;
  for (var i = 0; i < G_DIM; i++) s += a.vec[i] * b.vec[i];
  return s;
}
function ensureGradients() {
  var defs = $('gDefs');
  G.libs.forEach(function (name, i) {
    var c = gLibColor(name);
    var rg = document.createElementNS(SVGNS, 'radialGradient');
    rg.setAttribute('id', 'glowL' + i);
    rg.innerHTML = '<stop offset="0%" stop-color="' + c + '" stop-opacity="0.09"/><stop offset="60%" stop-color="' + c + '" stop-opacity="0.05"/><stop offset="100%" stop-color="' + c + '" stop-opacity="0"/>';
    defs.appendChild(rg);
  });
}
function gInitFrom(payload) {
  G.libs = payload.libs || [];
  G.themes = [];
  // 锚点按库数环形铺开（此前 8 个固定槽位，多库共用槽位直接叠罗汉）
  G.libs.forEach(function (name, i) {
    var a = (i / Math.max(1, G.libs.length)) * Math.PI * 2 - Math.PI / 2;
    G.anchors[name] = { ax: Math.cos(a) * 620, ay: Math.sin(a) * 480 };
  });
  G.nodes = (payload.nodes || []).map(function (raw, i) {
    var n = {
      id: raw.id, lib: raw.lib, rel: raw.rel, type: raw.type, chunks: raw.chunks || 0,
      updated: raw.updated, pipeline: raw.pipeline || { mineru: 'none', wemm: 'none' },
      page: raw.page, pages: raw.pages, fail: raw.fail_reason, theme: raw.theme || 'general',
      big: !!raw.big, t: ''
    };
    if (G.themes.indexOf(n.theme) < 0) G.themes.push(n.theme);
    n.t = gLabel(n);
    var an = G.anchors[n.lib] || { ax: 0, ay: 0 };
    var ang = (i * 2.399) % (Math.PI * 2);
    var r = n.type === 'page' ? 40 : (150 + (i % 9) * 65);
    n.x = an.ax + Math.cos(ang) * r;
    n.y = an.ay + Math.sin(ang) * r;
    n.vx = 0; n.vy = 0;
    G.byId[n.id] = n;
    return n;
  });
  (payload.edges || []).forEach(function (e) {
    if (!G.byId[e.a] || !G.byId[e.b]) return;
    if (e.kind === 'link') G.links.push([e.a, e.b]);
    else { G.owns.push({ a: e.a, b: e.b, rest: G.byId[e.a].type === 'page' ? 26 : 85 }); G.byId[e.a]._owner = e.b; }
  });
  buildVectors();
  ensureGradients();
  buildGraphDom();
  G.ready = true;
  // 超量节点自动进性能模式（去卡片阴影，见 CSS .g-stage.perf）
  if (G.nodes.length > 500) gStage.classList.add('perf');
  updateLabelDensity();
  $('gpSum').textContent = fmtInt(payload.stats ? payload.stats.nodes : G.nodes.length) + ' 节点 · 实时生效';
  renderLegend();
  if (RM) { settleSync(320); fitView(100); render(); }
  else { settleSync(180); fitView(100); startSim(0.3); }
  updateSemCount(); updateGStatus(); updateLibCounts();
  requestAnimationFrame(gLoop);
}
function buildGraphDom() {
  var wrap = $('gNodes');
  G.nodes.forEach(function (n) {
    var el = document.createElement('div');
    var st = gPdfState(n);
    el.className = 'g-node t-' + n.type + (n.big ? ' big' : '');
    el.dataset.id = n.id;
    el.dataset.lib = n.lib;
    if (st) el.dataset.state = st;
    if (n.type === 'pdf') {
      if (n.pipeline.mineru === 'failed') el.classList.add('st-failed');
      else if (n.pipeline.mineru === 'queued' || n.pipeline.mineru === 'none') el.classList.add('st-nocache');
    }
    el.innerHTML = '<div class="g-card"><span class="g-mark"></span><span class="g-label">' + esc(n.t) + '</span></div>';
    wrap.appendChild(el);
    G.nodeEls[n.id] = el;
  });
  G.links.forEach(function (e) {
    var p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('class', 'ge');
    $('gBilatG').appendChild(p);
    gBilatEls.push(p);
  });
  G.owns.forEach(function (e) {
    var p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('class', 'go');
    $('gOwnG').appendChild(p);
    gOwnEls.push(p);
  });
  G.libs.forEach(function (name, i) {
    var e = document.createElementNS(SVGNS, 'ellipse');
    e.setAttribute('fill', 'url(#glowL' + i + ')');
    e.setAttribute('stroke', gLibColor(name));
    e.setAttribute('stroke-opacity', '0.25');
    e.setAttribute('stroke-width', '1.2');
    e.setAttribute('stroke-dasharray', '7 9');
    $('gBoundsG').appendChild(e);
    gBoundEls[name] = { el: e, rx: 340, ry: 270, cx: G.anchors[name].ax, cy: G.anchors[name].ay };
    var tag = document.createElement('div');
    tag.className = 'g-libtag';
    tag.innerHTML = '<b>' + esc(name) + '</b><span></span>';
    $('gTags').appendChild(tag);
    gTagEls[name] = tag;
  });
  applyColorMode();
}
function renderLegend() {
  $('gLegendLibs').innerHTML = G.libs.map(function (name) {
    return '<span class="gl-lib"><i style="background:' + gLibColor(name) + '"></i><span>' + esc(name) + '</span></span>';
  }).join('');
}
function applyColorMode() {
  document.body.classList.toggle('g-mono', G.colorMode === 'mono');
  $('gLegendLibs').style.display = G.colorMode === 'lib' ? '' : 'none';
  $('gLegendState').style.display = G.colorMode === 'state' ? '' : 'none';
  $('gLegendMono').style.display = G.colorMode === 'mono' ? '' : 'none';
  Array.prototype.forEach.call($('gColorSeg').querySelectorAll('button'), function (b) {
    b.classList.toggle('on', b.getAttribute('data-cm') === G.colorMode);
  });
  G.nodes.forEach(function (n) {
    var el = G.nodeEls[n.id];
    if (!el) return;
    var mark = el.querySelector('.g-mark');
    if (!mark) return;
    if (G.colorMode === 'mono') { mark.style.background = ''; mark.style.borderColor = ''; return; }
    var st = el.dataset.state;
    var c = G.colorMode === 'lib' ? gLibColor(n.lib)
      : (st === 'ok' ? '#34D399' : st === 'partial' ? '#7DB8FF'
        : st === 'failed' ? '#F87171' : '');
    if (c) { mark.style.background = c; mark.style.borderColor = 'transparent'; }
    else { mark.style.background = ''; mark.style.borderColor = ''; }
  });
}
/* 力导向 */
function gA() { return G.nodes.filter(gVisible); }
function startSim(alpha) { sim.alpha = alpha || 1; sim.frame = 0; sim.active = true; requestRender(); }
function settleSync(iter) {
  for (var i = 0; i < iter; i++) {
    sim.alpha = Math.max(0.004, 1 - i / iter);
    simStep();
  }
  sim.active = false;
}
function simStep() {
  var A = sim.alpha, i, j, n, m, dx, dy, d2, d, fx, fy, a, b;
  var act = gA();
  for (i = 0; i < act.length; i++) {
    n = act[i];
    for (j = i + 1; j < act.length; j++) {
      m = act[j];
      if (n.orbit || m.orbit) continue;
      dx = n.x - m.x; dy = n.y - m.y;
      d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = (i % 3 + 1) * 0.6; dy = (j % 3 + 1) * 0.6; d2 = dx * dx + dy * dy; }
      if (d2 < 640000) {
        d = Math.sqrt(d2);
        fx = dx / d * (52000 / d2 * A);
        fy = dy / d * (52000 / d2 * A);
        n.vx += fx; n.vy += fy;
        m.vx -= fx; m.vy -= fy;
      }
    }
  }
  for (i = 0; i < G.grav.length; i++) {
    var gp = G.grav[i];
    a = G.byId[gp.a]; b = G.byId[gp.b];
    if (!gVisible(a) || !gVisible(b) || a.orbit || b.orbit) continue;
    dx = b.x - a.x; dy = b.y - a.y;
    d = Math.sqrt(dx * dx + dy * dy) || 1;
    fx = dx / d * ((gp.s - 0.62) * 10 * A);
    fy = dy / d * ((gp.s - 0.62) * 10 * A);
    a.vx += fx; a.vy += fy;
    b.vx -= fx; b.vy -= fy;
  }
  if (G.ly.bilat) {
    for (i = 0; i < G.links.length; i++) {
      a = G.byId[G.links[i][0]]; b = G.byId[G.links[i][1]];
      if (!gVisible(a) || !gVisible(b) || a.orbit || b.orbit) continue;
      dx = b.x - a.x; dy = b.y - a.y;
      d = Math.sqrt(dx * dx + dy * dy) || 1;
      fx = dx / d * ((d - 175) * 0.02 * A);
      fy = dy / d * ((d - 175) * 0.02 * A);
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    }
  }
  for (i = 0; i < G.owns.length; i++) {
    a = G.byId[G.owns[i].a]; b = G.byId[G.owns[i].b];
    if (!gVisible(a) || !gVisible(b)) continue;
    var rest = G.owns[i].rest, kk = a.type === 'page' ? 0.06 : 0.05;
    dx = b.x - a.x; dy = b.y - a.y;
    d = Math.sqrt(dx * dx + dy * dy) || 1;
    fx = dx / d * ((d - rest) * kk * A);
    fy = dy / d * ((d - rest) * kk * A);
    a.vx += fx; a.vy += fy;
    b.vx -= fx; b.vy -= fy;
  }
  for (i = 0; i < act.length; i++) {
    n = act[i];
    if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
    if (n.orbit) {
      var ob = n.orbit;
      var ox = (G.qNode ? G.qNode.x : 0) + Math.cos(ob.a) * ob.r;
      var oy = (G.qNode ? G.qNode.y : 0) + Math.sin(ob.a) * ob.r;
      n.vx = (ox - n.x) * 0.16;
      n.vy = (oy - n.y) * 0.16;
      n.x += n.vx; n.y += n.vy;
      continue;
    }
    var an = G.anchors[n.lib] || { ax: 0, ay: 0 };
    n.vx += (an.ax - n.x) * 0.0045 * A;
    n.vy += (an.ay - n.y) * 0.0045 * A;
    n.vx *= 0.86; n.vy *= 0.86;
    var sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
    if (sp > 26) { n.vx = n.vx / sp * 26; n.vy = n.vy / sp * 26; }
    n.x += n.vx; n.y += n.vy;
  }
  // 检索态：问题节点是辐射中心——非命中节点受径向排斥 drift 向外围，
  // 命中节点走 orbit 轨道聚拢。只在模拟进行时生效，清除检索后自动回落。
  if (G.qNode) {
    var hs = G.searchHits || {};
    for (i = 0; i < act.length; i++) {
      n = act[i];
      if (n.fixed || n.orbit || hs[n.id]) continue;
      dx = n.x - G.qNode.x; dy = n.y - G.qNode.y;
      d2 = dx * dx + dy * dy;
      if (d2 < 360000 && d2 > 1) {
        d = Math.sqrt(d2);
        var push = (600 - d) / 600 * 3.4 * A;
        n.vx += dx / d * push; n.vy += dy / d * push;
      }
    }
  }
  sim.alpha *= 0.982;
  sim.frame++;
  if (sim.alpha < 0.004 || sim.frame >= 360) sim.active = false;
}
/* 相机 */
function gStageRect() { return gStage.getBoundingClientRect(); }
/* 标签密度：缩小时（k<1.05）或可见节点>350，只留 hub/命中/选中/悬停标签。
   只做 class 翻转（状态变化时才碰 DOM），不逐节点操作。 */
function updateLabelDensity() {
  if (!gStage) return;
  var min = (G.cam.k < 1.05) || (gA().length > 350);
  if (gStage.classList.contains('labels-min') !== min) {
    gStage.classList.toggle('labels-min', min);
  }
}
function applyCam() {
  gWorld.style.transform = 'translate(' + G.cam.x + 'px,' + G.cam.y + 'px) scale(' + G.cam.k + ')';
  $('gZoomLabel').textContent = Math.round(G.cam.k * 100) + '%';
  updateLabelDensity();
}
function clampK(k) { return Math.min(2.4, Math.max(0.3, k)); }
function zoomAt(sx, sy, k2) {
  k2 = clampK(k2);
  var r = gStageRect();
  var lx = sx - r.left, ly = sy - r.top;
  G.cam.x = lx - (lx - G.cam.x) * k2 / G.cam.k;
  G.cam.y = ly - (ly - G.cam.y) * k2 / G.cam.k;
  G.cam.k = k2;
  applyCam();
  requestRender();
}
function fitView(margin) {
  var act = gA();
  if (!act.length) return;
  var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
  act.forEach(function (n) {
    if (n.x < minX) minX = n.x;
    if (n.x > maxX) maxX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.y > maxY) maxY = n.y;
  });
  var bw = Math.max(1, maxX - minX), bh = Math.max(1, maxY - minY);
  var r = gStageRect();
  var k = Math.min((r.width - margin * 2) / bw, (r.height - margin * 2) / bh);
  k = clampK(Math.min(k, 1.35));
  G.cam.k = k;
  G.cam.x = r.width / 2 - (minX + maxX) / 2 * k;
  G.cam.y = r.height / 2 - (minY + maxY) / 2 * k;
  applyCam();
  requestRender();
}
/* 边路径与渲染 */
function gEdgeD(a, b) {
  var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  var dx = b.x - a.x, dy = b.y - a.y;
  return 'M' + (a.x + OFF) + ',' + (a.y + OFF)
    + ' Q' + (mx - dy * 0.08 + OFF) + ',' + (my + dx * 0.08 + OFF)
    + ' ' + (b.x + OFF) + ',' + (b.y + OFF);
}
function renderBounds() {
  G.libs.forEach(function (k) {
    var B = gBoundEls[k];
    if (!B) return;
    var sx = 0, sy = 0, c = 0;
    G.nodes.forEach(function (n) { if (n.lib === k && gVisible(n)) { sx += n.x; sy += n.y; c++; } });
    var tx = c ? sx / c : G.anchors[k].ax;
    var ty = c ? sy / c : G.anchors[k].ay;
    B.cx += (tx - B.cx) * 0.02;
    B.cy += (ty - B.cy) * 0.02;
    if (G.ly.bounds) {
      B.el.style.display = '';
      B.el.setAttribute('cx', (B.cx + OFF).toFixed(1));
      B.el.setAttribute('cy', (B.cy + OFF).toFixed(1));
      B.el.setAttribute('rx', B.rx);
      B.el.setAttribute('ry', B.ry);
      gTagEls[k].style.display = '';
      gTagEls[k].style.transform = 'translate(' + B.cx.toFixed(1) + 'px,' + (B.cy - B.ry - 16).toFixed(1) + 'px) translate(-50%,-100%)';
    } else {
      B.el.style.display = 'none';
      gTagEls[k].style.display = 'none';
    }
  });
}
function updateLibCounts() {
  G.libs.forEach(function (k) {
    var c = 0;
    G.nodes.forEach(function (n) { if (n.lib === k && gVisible(n)) c++; });
    if (gTagEls[k]) gTagEls[k].querySelector('span').textContent = c + ' 节点';
  });
}
function requestRender() { needRender = true; }
function render() {
  renderBounds();
  G.nodes.forEach(function (n) {
    var el = G.nodeEls[n.id];
    if (el) el.style.transform = 'translate(' + n.x + 'px,' + n.y + 'px)';
  });
  if (G.qNode && G.nodeEls.qnode) {
    G.nodeEls.qnode.style.transform = 'translate(' + G.qNode.x + 'px,' + G.qNode.y + 'px)';
  }
  var i, e, a, b, p;
  for (i = 0; i < G.links.length; i++) {
    e = G.links[i]; p = gBilatEls[i];
    a = G.byId[e[0]]; b = G.byId[e[1]];
    if (G.ly.bilat && gVisible(a) && gVisible(b)) {
      p.style.display = '';
      p.setAttribute('d', gEdgeD(a, b));
    } else { p.style.display = 'none'; }
  }
  for (i = 0; i < G.owns.length; i++) {
    e = G.owns[i]; p = gOwnEls[i];
    a = G.byId[e.a]; b = G.byId[e.b];
    if (G.ly.own && gVisible(a) && gVisible(b)) {
      p.style.display = '';
      p.setAttribute('d', gEdgeD(a, b));
    } else { p.style.display = 'none'; }
  }
  for (i = 0; i < G.sem.length; i++) {
    e = G.sem[i]; p = gSemEls[i];
    a = G.byId[e.a]; b = G.byId[e.b];
    if (G.ly.sem && e.sim >= G.ly.th && gVisible(a) && gVisible(b)) {
      p.style.display = '';
      p.setAttribute('d', gEdgeD(a, b));
    } else { p.style.display = 'none'; }
  }
  for (i = 0; i < G.redges.length; i++) {
    var rr = G.redges[i];
    var bb = G.byId[rr.to];
    if (bb && G.qNode) rr.el.setAttribute('d', gEdgeD(G.qNode, bb));
  }
}
function gLoop() {
  requestAnimationFrame(gLoop);
  if (!G.ready) return;
  var busy = false;
  if (sim.active) { simStep(); busy = true; }
  else if (fitOnSettle) { fitOnSettle = false; fitView(100); }
  if (gCamTween) { gCamTween(); busy = true; }
  var now = performance.now();
  for (var i = gRipples.length - 1; i >= 0; i--) {
    var rp = gRipples[i];
    var t = (now - rp.t0) / 1500;
    if (t >= 1) { rp.el.remove(); gRipples.splice(i, 1); continue; }
    var e2 = 1 - Math.pow(1 - t, 3);
    rp.el.setAttribute('r', 30 + e2 * 300);
    rp.el.setAttribute('opacity', (0.5 * (1 - t)).toFixed(3));
    busy = true;
  }
  if (panning || dragging) busy = true;
  if (busy || needRender) { render(); needRender = false; }
}
/* 交互：拖拽 / 平移 / 缩放 */
function bindStage() {
  gStage.addEventListener('pointerdown', function (e) {
    if (e.button === 2) return;
    if (!G.ready) return;
    var nodeEl = e.target.closest ? e.target.closest('.g-node') : null;
    downPos = { x: e.clientX, y: e.clientY };
    dragMoved = 0;
    if (nodeEl) {
      var id = nodeEl.dataset.id;
      var n = id === 'qnode' ? G.qNode : G.byId[id];
      if (n) {
        dragging = { n: n, el: nodeEl, ox: n.x, oy: n.y, px: e.clientX, py: e.clientY, q: id === 'qnode' };
        nodeEl.classList.add('dragging');
        try { gStage.setPointerCapture(e.pointerId); } catch (err) {}
        e.preventDefault();
        return;
      }
    }
    panning = true;
    panStart = { x: e.clientX, y: e.clientY, cx: G.cam.x, cy: G.cam.y };
    try { gStage.setPointerCapture(e.pointerId); } catch (err) {}
    gStage.style.cursor = 'grabbing';
  });
  gStage.addEventListener('pointermove', function (e) {
    if (dragging) {
      dragMoved += Math.abs(e.movementX || 0) + Math.abs(e.movementY || 0);
      var n = dragging.n;
      n.x = dragging.ox + (e.clientX - dragging.px) / G.cam.k;
      n.y = dragging.oy + (e.clientY - dragging.py) / G.cam.k;
      requestRender();
      return;
    }
    if (panning) {
      G.cam.x = panStart.cx + (e.clientX - panStart.x);
      G.cam.y = panStart.cy + (e.clientY - panStart.y);
      applyCam();
      requestRender();
    }
  });
  gStage.addEventListener('pointerup', function (e) {
    if (dragging) {
      dragging.el.classList.remove('dragging');
      if (dragMoved < 5) {
        if (!dragging.q) openGIns(dragging.el.dataset.id);
      } else if (!RM && !dragging.q) {
        startSim(0.25);
      }
      dragging = null;
    } else if (panning) {
      panning = false;
      gStage.style.cursor = '';
      if (downPos) {
        var moved = Math.abs(e.clientX - downPos.x) + Math.abs(e.clientY - downPos.y);
        if (moved < 4) closeGIns();
      }
    }
  });
  gStage.addEventListener('pointercancel', function () {
    if (dragging) { dragging.el.classList.remove('dragging'); dragging = null; }
    panning = false;
    gStage.style.cursor = '';
  });
  gStage.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, G.cam.k * Math.exp(-e.deltaY * 0.0016));
  }, { passive: false });
  gStage.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  $('gzIn').addEventListener('click', function () {
    var r = gStageRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, G.cam.k * 1.25);
  });
  $('gzOut').addEventListener('click', function () {
    var r = gStageRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, G.cam.k / 1.25);
  });
  $('gzFit').addEventListener('click', function () {
    if (!RM) {
      gA().forEach(function (n) { n.vx = (Math.random() - 0.5) * 14; n.vy = (Math.random() - 0.5) * 14; });
      fitOnSettle = true;
      startSim(1);
    } else {
      fitView(100);
    }
    toast('已重置取景与力导向布局');
  });
}
/* Tooltip */
function bindTip() {
  var tip = $('gTip'), pin = null;
  function nodePath(n) {
    if (n.type === 'page') {
      var pdf = gPdfOf(n);
      return (pdf ? pdf.rel : n.rel) + ' · 第 ' + n.page + ' 页';
    }
    return n.rel;
  }
  gStage.addEventListener('pointerover', function (e) {
    var el = e.target.closest ? e.target.closest('.g-node') : null;
    if (!el || el === pin) return;
    pin = el;
    var id = el.dataset.id;
    if (id === 'qnode') {
      $('gTipPath').textContent = '检索 · 问题节点';
      $('gTipMeta').textContent = '语义检索的辐射中心';
    } else {
      var n = G.byId[id];
      if (!n) return;
      $('gTipPath').textContent = nodePath(n);
      var st = gPdfState(n), stTxt = '';
      if (st === 'partial') stTxt = ' · 有缓存 · 缺 WEMM';
      else if (st === 'failed') stTxt = ' · ' + (n.fail || 'OCR 失败');
      else if (st === 'none') stTxt = (n.type === 'pdf' && n.pipeline.mineru === 'queued') ? ' · OCR 排队中' : ' · 未识别 · 待 OCR';
      $('gTipMeta').textContent = n.lib + ' · ' + n.type + stTxt + (n.updated ? ' · 更新于 ' + fmtDay(n.updated) : '');
    }
    tip.classList.add('show');
  });
  gStage.addEventListener('pointermove', function (e) {
    if (!pin) return;
    tip.style.left = e.clientX + 'px';
    tip.style.top = e.clientY + 'px';
  });
  gStage.addEventListener('pointerout', function (e) {
    var el = e.target.closest ? e.target.closest('.g-node') : null;
    if (el && el === pin && (!e.relatedTarget || !e.relatedTarget.closest || !e.relatedTarget.closest('.g-node'))) {
      pin = null;
      tip.classList.remove('show');
    }
  });
}
/* 图谱检索：真契约 API.search → 问题节点 + 涟漪 + 轨道 */
var gTimers = [], G_SEQ = 0;
function clearGLit() {
  G.nodes.forEach(function (n) { var el = G.nodeEls[n.id]; if (el) el.classList.remove('lit'); });
  // 置信度角标跟随命中态一起清（残留会污染下一轮检索的显示）
  Array.prototype.forEach.call(document.querySelectorAll('#gNodes .g-conf'), function (c) { c.remove(); });
  applyGDim();
}
function applyGDim() {
  var qset = G.searchHits || null;
  G.nodes.forEach(function (n) {
    var el = G.nodeEls[n.id];
    if (!el) return;
    var dim = false;
    if (qset && !qset[n.id] && n.id !== G.insOpenId) dim = true;
    el.classList.toggle('dim', dim);
  });
  var keep = {};
  if (qset) Object.keys(qset).forEach(function (k) { keep[k] = 1; });
  if (G.qNode) keep.qnode = 1;
  if (G.insOpenId) keep[G.insOpenId] = 1;
  function edgeDim(a, b) { return qset ? !(keep[a] && keep[b]) : false; }
  var i, e;
  for (i = 0; i < G.links.length; i++) {
    e = G.links[i];
    gBilatEls[i].classList.toggle('dim', edgeDim(e[0], e[1]));
  }
  for (i = 0; i < G.sem.length; i++) {
    e = G.sem[i];
    gSemEls[i].classList.toggle('dim', edgeDim(e.a, e.b));
  }
  for (i = 0; i < G.owns.length; i++) {
    e = G.owns[i];
    gOwnEls[i].classList.toggle('dim', edgeDim(e.a, e.b));
  }
}
function clearGSearch() {
  gTimers.forEach(clearTimeout); gTimers = [];
  G_SEQ++;
  setGoBusy(false);
  G.searching = false;
  if (G.qNode) { if (G.nodeEls.qnode) G.nodeEls.qnode.remove(); G.nodeEls.qnode = null; G.qNode = null; }
  G.redges.forEach(function (r) { r.el.remove(); });
  G.redges = [];
  $('gOrbitG').innerHTML = '';
  $('gRippleG').innerHTML = '';
  gRipples = [];
  G.searchHits = null;
  G.searching = false;
  G.nodes.forEach(function (n) { n.orbit = null; });
  clearGLit();
  if (!RM) startSim(0.5);
  requestRender();
}
function setGoBusy(on) {
  var b = $('gGo');
  if (!b) return;
  b.disabled = on;
  b.textContent = on ? '检索中…' : '检索';
  b.classList.toggle('busy', on);
}
function runGSearch(q) {
  if (G.searching) { toast('检索进行中，请稍候…', 'warn'); return; }
  G.searching = true;
  setGoBusy(true);
  gTimers.forEach(clearTimeout); gTimers = [];
  G_SEQ++;
  G.nodes.forEach(function (n) { n.orbit = null; });
  if (G.qNode) { if (G.nodeEls.qnode) G.nodeEls.qnode.remove(); G.nodeEls.qnode = null; G.qNode = null; }
  G.redges.forEach(function (r) { r.el.remove(); });
  G.redges = [];
  $('gOrbitG').innerHTML = '';
  $('gRippleG').innerHTML = '';
  gRipples = [];
  G.searchHits = null;
  clearGLit();
  var seq = G_SEQ;
  // 图谱按文件节点点亮：topK 个块常归属 2~3 个文件（"只能出来两条"的根因）。
  // 多取候选（topK×4，至少 20）再按文件分组，展示上限 G_MAXHITS 个文件。
  var G_MAXHITS = 12;
  var fetchK = Math.max(G.topK * 4, 20);
  API.search(q, fetchK, scopeStr(), true).then(function (res) {
    if (seq !== G_SEQ) return;
    G.searching = false;
    setGoBusy(false);
    if (res.error) { setGoBusy(false); toast('检索失败：' + res.error, 'err'); return; }
    var seen = {};
    var files = [];
    (res.results || []).forEach(function (r) {
      if (r.notice) return;
      var id = r.lib + '|' + r.rel;
      var n = G.byId[id];
      if (!n || !gVisible(n)) return;
      if (!seen[id]) {
        seen[id] = { id: id, conf: r.confidence || 0, snip: r.body || '', blocks: 0 };
        files.push(seen[id]);
      }
      var f = seen[id];
      f.blocks++;
      if ((r.confidence || 0) > f.conf) { f.conf = r.confidence; f.snip = r.body || ''; }
    });
    files.sort(function (a, b) { return b.conf - a.conf; });
    var hits = files.slice(0, G_MAXHITS);
    G.lastSnips = {};
    files.forEach(function (f) { G.lastSnips[f.id] = f.snip; });
    if (!hits.length) { setGoBusy(false); toast('没有匹配到可见节点，换个问法或调整库范围', 'warn'); return; }
    var r0 = gStageRect();
    var c = s2w(r0.left + r0.width / 2 - 170, r0.top + r0.height * 0.36);
    G.qNode = { id: 'qnode', x: c.x, y: c.y, fixed: true };
    var el = document.createElement('div');
    el.className = 'g-node qnode';
    el.dataset.id = 'qnode';
    el.innerHTML = '<div class="g-card"><span class="g-mark"></span><span class="g-label">' + esc(q.trim()) + '</span><span class="q-x" role="button" aria-label="清除检索" title="回到全库全景">×</span></div>';
    $('gNodes').appendChild(el);
    G.nodeEls.qnode = el;
    var qx = el.querySelector('.q-x');
    if (qx) qx.addEventListener('click', function (ev) { ev.stopPropagation(); clearGSearch(); });
    // 三环封顶（此前半径 90+i*60 无界，命中一多就飞出屏幕）；半径按当前缩放折算，
    // 保证屏幕观感一致（缩小时 world 半径放大，放大时缩小）。
    var zk = 1 / Math.max(0.35, G.cam.k);
    var RINGS = [130 * zk, 220 * zk, 310 * zk];
    var ringN = Math.min(3, hits.length);
    for (var ri = 0; ri < ringN; ri++) {
      var oc = document.createElementNS(SVGNS, 'circle');
      oc.setAttribute('class', 'g-orbit-ring');
      oc.setAttribute('cx', (G.qNode.x + OFF).toFixed(1));
      oc.setAttribute('cy', (G.qNode.y + OFF).toFixed(1));
      oc.setAttribute('r', RINGS[ri].toFixed(1));
      oc.setAttribute('opacity', (0.20 - ri * 0.05).toFixed(3));
      $('gOrbitG').appendChild(oc);
    }
    var orbitDefs = [];
    (function () {
      var per = {};
      hits.forEach(function (h, i) {
        var rk = RINGS[i % RINGS.length];
        (per[rk] = per[rk] || []).push(i);
      });
      Object.keys(per).forEach(function (rk) {
        var arr = per[rk];
        arr.forEach(function (idx, j) {
          orbitDefs[idx] = { r: +rk, a: -Math.PI / 2 + (j * 2 * Math.PI) / arr.length };
        });
      });
    })();
    requestRender();
    G.searchHits = {};
    hits.forEach(function (h) { G.searchHits[h.id] = 1; });
    function applyResults() {
      if (seq !== G_SEQ || !G.qNode) return;
      hits.forEach(function (h, i) {
        var d2 = RM ? 0 : i * 90;
        gTimers.push(setTimeout(function () {
          if (seq !== G_SEQ) return;
          var n = G.byId[h.id];
          if (!n) return;
          var nel = G.nodeEls[h.id];
          nel.classList.add('lit');
          var conf = nel.querySelector('.g-conf');
          if (!conf) {
            conf = document.createElement('div');
            conf.className = 'g-conf';
            nel.appendChild(conf);
          }
          conf.textContent = h.conf.toFixed(2);
          var p = document.createElementNS(SVGNS, 'path');
          p.setAttribute('class', 'gr');
          p.setAttribute('stroke-width', (1.2 + h.conf * 2.4).toFixed(2));
          p.setAttribute('stroke-dasharray', '6 7');
          $('gRedgeG').appendChild(p);
          G.redges.push({ to: h.id, el: p });
          gTimers.push(setTimeout(function () { p.classList.add('on'); }, 30));
          var od = orbitDefs[i];
          if (od) {
            n.orbit = od;
            if (RM) {
              n.x = (G.qNode ? G.qNode.x : 0) + Math.cos(od.a) * od.r;
              n.y = (G.qNode ? G.qNode.y : 0) + Math.sin(od.a) * od.r;
            } else if (!sim.active) {
              startSim(0.6);
            }
          }
        }, d2));
      });
      applyGDim();
    }
    if (RM) { applyResults(); }
    else {
      [0, 380, 760].forEach(function (d3, i) {
        gTimers.push(setTimeout(function () {
          if (seq !== G_SEQ || !G.qNode) return;
          var cir = document.createElementNS(SVGNS, 'circle');
          cir.setAttribute('class', 'gr-c');
          cir.setAttribute('cx', G.qNode.x + OFF);
          cir.setAttribute('cy', G.qNode.y + OFF);
          cir.setAttribute('r', 30);
          cir.setAttribute('stroke-width', i === 2 ? 1.2 : 1.8);
          cir.setAttribute('opacity', '0.5');
          $('gRippleG').appendChild(cir);
          gRipples.push({ el: cir, t0: performance.now() });
        }, d3));
      });
      gTimers.push(setTimeout(applyResults, 950));
    }
    toast('已命中 ' + files.length + ' 个相关文件'
      + (files.length > hits.length ? '（轨道展示前 ' + hits.length + ' 个）' : ''));
  }).catch(function (err) {
    if (seq !== G_SEQ) return;
    G.searching = false;
    setGoBusy(false);
    toast('检索失败：' + (err && err.message ? err.message : err), 'err');
  });
}
function s2w(sx, sy) {
  var r = gStageRect();
  return { x: (sx - r.left - G.cam.x) / G.cam.k, y: (sy - r.top - G.cam.y) / G.cam.k };
}
$('gGo').addEventListener('click', function () {
  var q = $('gq').value.trim();
  if (!q) { toast('先输入一个问题', 'warn'); return; }
  runGSearch(q);
});
$('gq').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') {
    var q = $('gq').value.trim();
    if (q) runGSearch(q);
  }
});
$('gTopk').addEventListener('change', function () {
  G.topK = parseInt(this.value, 10) || 5;
});
['WEMM 页级视觉导航是怎么实现的？', 'MinerU 扫描件是怎么处理的？', 'Agent 门禁是怎么设计的？'].forEach(function (h) {
  var b = document.createElement('button');
  b.className = 'g-chip';
  b.type = 'button';
  b.textContent = h;
  b.addEventListener('click', function () {
    $('gq').value = h;
    runGSearch(h);
  });
  $('gHints').appendChild(b);
});
/* 图层开关 */
function updateSemCount() {
  if (!G.semLoaded) {
    $('semCount').textContent = '开启后需加载嵌入模型（约 30-60 秒）';
    return;
  }
  var c = 0;
  G.sem.forEach(function (e) {
    if (e.sim >= G.ly.th && gVisible(G.byId[e.a]) && gVisible(G.byId[e.b])) c++;
  });
  $('semCount').textContent = '阈值 ' + G.ly.th.toFixed(2) + ' · 当前 ' + c + ' 条';
}
function updateGStatus() {
  var ne = 0, i, e;
  for (i = 0; i < G.links.length; i++) {
    e = G.links[i];
    if (G.ly.bilat && gVisible(G.byId[e[0]]) && gVisible(G.byId[e[1]])) ne++;
  }
  for (i = 0; i < G.sem.length; i++) {
    e = G.sem[i];
    if (G.ly.sem && e.sim >= G.ly.th && gVisible(G.byId[e.a]) && gVisible(G.byId[e.b])) ne++;
  }
  for (i = 0; i < G.owns.length; i++) {
    e = G.owns[i];
    if (G.ly.own && gVisible(G.byId[e.a]) && gVisible(G.byId[e.b])) ne++;
  }
  var dev = S.snap && S.snap.device ? S.snap.device : {};
  $('gStatus').innerHTML = '<b>' + gA().length + '</b> 节点 · <b>' + ne + '</b> 边 · <b>' + fmtInt(S.snap ? S.snap.chunks : 0) + '</b> 块 · '
    + esc(dev.model || '') + (dev.cuda ? ' · CUDA' : '');
}
function gLayerChanged() {
  G.nodes.forEach(function (n) {
    var el = G.nodeEls[n.id];
    if (el) el.style.display = gVisible(n) ? '' : 'none';
  });
  updateLabelDensity();
  if (!RM) startSim(0.45); else { settleSync(120); fitView(100); }
  updateSemCount(); updateGStatus(); updateLibCounts();
  requestRender();
}
function bindLy(id, key) {
  $(id).addEventListener('change', function () {
    G.ly[key] = this.checked;
    gLayerChanged();
  });
}
bindLy('lyBilat', 'bilat');
bindLy('lyOwn', 'own');
bindLy('lyPage', 'page');
bindLy('lyCache', 'cache');
bindLy('lyPdf', 'pdf');
$('lyBounds').addEventListener('change', function () {
  G.ly.bounds = this.checked;
  requestRender();
});
/* 语义边：按需经 semantic_edges() 加载（先弹模型加载确认） */
$('lySem').addEventListener('change', function () {
  var cb = this;
  if (!cb.checked) { G.ly.sem = false; updateSemCount(); updateGStatus(); requestRender(); return; }
  if (G.semLoaded) { G.ly.sem = true; updateSemCount(); updateGStatus(); requestRender(); return; }
  cb.checked = false;
  askConfirm('加载语义相似边',
    '语义边由当前嵌入模型对「标题+相对路径」实时编码计算（余弦相似 ≥ 阈值），首次需要加载嵌入模型。',
    '预计耗时约 30-60 秒；纯读路径，不写任何文件、不产生索引副作用。', '加载并计算')
    .then(function (yes) {
      if (!yes) return;
      cb.checked = true;
      $('semCount').textContent = '正在加载嵌入模型并计算…';
      API.semantic_edges(scopeStr(), G.ly.th).then(function (res) {
        G.semLoaded = true;
        G.ly.sem = true;
        G.sem = (res.edges || []).map(function (e) { return { a: e.a, b: e.b, sim: e.sim }; });
        var g = $('gSemG');
        g.innerHTML = '';
        gSemEls = [];
        G.sem.forEach(function (e) {
          var p = document.createElementNS(SVGNS, 'path');
          p.setAttribute('class', 'gs');
          p.setAttribute('stroke-opacity', (0.10 + Math.max(0, e.sim - 0.5) * 0.85).toFixed(3));
          g.appendChild(p);
          gSemEls.push(p);
        });
        updateSemCount(); updateGStatus();
        requestRender();
        toast('语义边已加载 · ' + G.sem.length + ' 条');
      }).catch(function (err) {
        cb.checked = false;
        toast('语义边计算失败：' + (err && err.message ? err.message : err), 'err');
      });
    });
});
var semThReload = debounce(function () {
  if (!G.semLoaded) return;
  $('semCount').textContent = '按新阈值重算…';
  API.semantic_edges(scopeStr(), G.ly.th).then(function (res) {
    G.sem = (res.edges || []).map(function (e) { return { a: e.a, b: e.b, sim: e.sim }; });
    var g = $('gSemG');
    g.innerHTML = '';
    gSemEls = [];
    G.sem.forEach(function (e) {
      var p = document.createElementNS(SVGNS, 'path');
      p.setAttribute('class', 'gs');
      p.setAttribute('stroke-opacity', (0.10 + Math.max(0, e.sim - 0.5) * 0.85).toFixed(3));
      g.appendChild(p);
      gSemEls.push(p);
    });
    updateSemCount(); updateGStatus();
    requestRender();
  });
}, 700);
$('semTh').addEventListener('input', function () {
  G.ly.th = parseFloat(this.value);
  $('semThVal').textContent = G.ly.th.toFixed(2);
  if (G.semLoaded) { updateSemCount(); requestRender(); }
});
$('semTh').addEventListener('change', function () { semThReload(); });
$('gColorSeg').addEventListener('click', function (e) {
  var b = e.target.closest('button[data-cm]');
  if (!b) return;
  G.colorMode = b.getAttribute('data-cm');
  applyColorMode();
});
function togglePhysics(open) {
  var ph = $('gPhysics');
  var openNow = open === undefined ? !ph.classList.contains('open') : open;
  ph.classList.toggle('open', openNow);
  var ib = $('gInfoBtn');
  ib.classList.toggle('on', openNow);
  ib.setAttribute('aria-expanded', openNow ? 'true' : 'false');
}
$('gInfoBtn').addEventListener('click', function (e) {
  e.stopPropagation();
  togglePhysics();
});
document.addEventListener('click', function (e) {
  var ph = $('gPhysics');
  if (ph.classList.contains('open') && !e.target.closest('#gPhysics') && !e.target.closest('#gInfoBtn')) {
    togglePhysics(false);
  }
});
/* Inspector */
function giItem(id, label, simv) {
  var n = G.byId[id];
  if (!n) return '';
  return '<button class="gi-item" data-focus="' + esc(id) + '">'
    + '<span class="g-mark" style="background:' + gLibColor(n.lib) + ';border-color:transparent"></span>'
    + '<span class="nm">' + esc(label || n.t) + '</span>'
    + (simv != null ? '<span class="gi-sim">' + simv.toFixed(2) + '</span>' : '')
    + '<span class="arr">定位 →</span></button>';
}
function giLinks(arr) {
  if (!arr.length) return '<div class="gi-none">暂无</div>';
  return '<div class="gi-list">' + arr.map(function (id) { return giItem(id); }).join('') + '</div>';
}
function giSemTop(id, k) {
  var self = G.byId[id];
  var arr = [];
  G.nodes.forEach(function (m) {
    if (m.id === id || m.type === 'page' || !gVisible(m)) return;
    arr.push({ id: m.id, t: m.t, s: gCos(self, m) });
  });
  arr.sort(function (a, b) { return b.s - a.s; });
  return '<div class="gi-list">' + arr.slice(0, k).map(function (x) { return giItem(x.id, x.t, x.s); }).join('') + '</div>';
}
function giSnippetHtml(id) {
  var hit = G.lastSnips && G.lastSnips[id];
  return hit ? '<div class="gi-sec"><h5>命中片段</h5><div class="gi-snip">' + hl(hit, lastQuery) + '</div></div>' : '';
}
function openGIns(id) {
  var n = G.byId[id];
  if (!n) return;
  G.insOpenId = id;
  G.insOpen = true;
  G.nodes.forEach(function (x) { var el = G.nodeEls[x.id]; if (el) el.classList.remove('sel'); });
  G.nodeEls[id].classList.add('sel');
  applyGDim();
  $('giMark').style.background = gLibColor(n.lib);
  $('giMark').style.borderColor = 'transparent';
  $('giLib').textContent = n.lib;
  $('giBadge').innerHTML = '<span class="badge" style="background:var(--s3);color:var(--sec)">' + esc(n.type) + '</span>';
  $('giTitle').textContent = n.t;
  $('giPath').textContent = n.type === 'page' ? (gPdfOf(n) ? gPdfOf(n).rel : n.rel) + ' · 第 ' + n.page + ' 页' : n.rel;
  var meta = [];
  meta.push('更新 ' + (n.updated ? fmtDay(n.updated) : '--'));
  var outIds = [], inIds = [];
  if (n.type === 'md') {
    G.links.forEach(function (e) {
      if (e[0] === id) outIds.push(e[1]);
      if (e[1] === id) inIds.push(e[0]);
    });
    meta.push((outIds.length + inIds.length) > 0 ? (outIds.length + inIds.length) + ' 条双链' : '无显式双链');
  }
  if (n.type === 'cache' && n.chunks) meta.push(n.chunks + ' 块');
  $('giMeta').innerHTML = meta.map(function (m) { return '<span>' + esc(m) + '</span>'; }).join('');
  var h = giSnippetHtml(id);
  function pipeChips(nn) {
    var pc = '<div class="gi-sec"><h5>管线</h5><div class="gi-pipe">';
    var m = nn.pipeline.mineru, w = nn.pipeline.wemm;
    if (nn.type === 'md') pc += '<span class="gi-chip ok">extract v4 · local</span>';
    else if (m === 'done') pc += '<span class="gi-chip ok">mineru:vlm · ' + (nn.pages || '') + (nn.pages ? ' 页' : '') + '</span>';
    else if (m === 'queued') pc += '<span class="gi-chip wait">MinerU 排队中 · 下轮自动重试</span>';
    else if (m === 'failed') pc += '<span class="gi-chip bad">失败 · ' + esc(nn.fail || '提取失败') + '</span>';
    else pc += '<span class="gi-chip off">未识别 · 待 OCR</span>';
    if (nn.type !== 'md') {
      if (w === 'done') pc += '<span class="gi-chip ok">wemm 页库 · DPI 144</span>';
      else if (m === 'done') pc += '<span class="gi-chip off">页库未建立</span>';
    }
    pc += '</div></div>';
    return pc;
  }
  if (n.type === 'md') {
    h += pipeChips(n);
    if (outIds.length) h += '<div class="gi-sec"><h5>出链</h5>' + giLinks(outIds) + '</div>';
    if (inIds.length) h += '<div class="gi-sec"><h5>入链</h5>' + giLinks(inIds) + '</div>';
    if (!outIds.length && !inIds.length) {
      h += '<div class="gi-sec"><h5>语义近邻</h5><div class="gi-none">无显式链接。以下 Top3 来自语义向量：</div>' + giSemTop(id, 3) + '</div>';
    }
  } else if (n.type === 'pdf') {
    h += pipeChips(n);
    var cid = null;
    G.owns.forEach(function (e) { if (e.b === id && G.byId[e.a].type === 'cache') cid = e.a; });
    h += '<div class="gi-sec"><h5>关联缓存</h5>'
      + (cid ? '<div class="gi-list">' + giItem(cid) + '</div>' : '<div class="gi-none">尚无缓存节点，等待识别完成。</div>') + '</div>';
    if (n.pipeline.mineru === 'failed' || n.pipeline.wemm !== 'done') {
      h += '<div class="gi-sec"><h5>处置</h5><div class="gi-list"><button class="gi-item" data-godiag="1"><span class="nm">前往诊断查看失败明细与页库状态</span><span class="arr">→</span></button></div></div>';
    }
  } else if (n.type === 'cache') {
    h += pipeChips(n);
    h += '<div class="gi-sec"><h5>来源 PDF</h5><div class="gi-list">' + (n._owner ? giItem(n._owner) : '<div class="gi-none">未知</div>') + '</div></div>';
    var pc2 = 0;
    G.nodes.forEach(function (m) { if (m.type === 'page' && m._owner === id) pc2++; });
    h += '<div class="gi-sec"><h5>WEMM 页库</h5>'
      + (pc2 > 0 ? '<div class="gi-list"><button class="gi-item" data-pages="' + esc(id) + '"><span class="nm">' + pc2 + ' 页 · 定位页节点群</span><span class="arr">跳转 →</span></button></div>'
        : '<div class="gi-none">该文档未建页库。</div>') + '</div>';
  } else {
    var cache = gOwnerOf(n), pdf = gPdfOf(n);
    h += '<div class="gi-sec"><h5>管线</h5><div class="gi-pipe"><span class="gi-chip ok">p.' + n.page + ' · 1024 维 · DPI 144</span></div></div>';
    h += '<div class="gi-sec"><h5>所属文档</h5>'
      + '<div class="gi-kv"><span class="k">MD 缓存</span><span class="v">' + esc(cache ? cache.t : '--') + '</span></div>'
      + '<div class="gi-kv"><span class="k">PDF 原件</span><span class="v">' + esc(pdf ? pdf.t : '--') + '</span></div>'
      + '<div class="gi-list">' + (cache ? giItem(cache.id, '定位所属缓存') : '') + '</div></div>';
  }
  $('giBody').innerHTML = h;
  $('gIns').classList.add('open');
}
function closeGIns() {
  $('gIns').classList.remove('open');
  G.insOpen = false;
  if (G.insOpenId && G.nodeEls[G.insOpenId]) G.nodeEls[G.insOpenId].classList.remove('sel');
  G.insOpenId = null;
  applyGDim();
}
$('giClose').addEventListener('click', closeGIns);
$('giOpen').addEventListener('click', function () {
  var n = G.byId[G.insOpenId];
  if (!n) return;
  var target = n.type === 'page' ? (gPdfOf(n) || n) : n;
  API.open_source(target.lib, target.rel, '').then(function () { toast('已调用系统打开源文件'); });
});
$('giBody').addEventListener('click', function (e) {
  var t = e.target.closest('[data-focus],[data-pages],[data-godiag]');
  if (!t) return;
  if (t.hasAttribute('data-godiag')) { closeGIns(); go('diag'); return; }
  if (t.hasAttribute('data-pages')) { focusPages(t.getAttribute('data-pages')); return; }
  var id = t.getAttribute('data-focus');
  var n = G.byId[id];
  if (n && !gVisible(n)) {
    var key = n.type === 'page' ? 'page' : (n.type === 'cache' ? 'cache' : 'pdf');
    G.ly[key] = true;
    var cb = $(key === 'page' ? 'lyPage' : key === 'cache' ? 'lyCache' : 'lyPdf');
    if (cb) cb.checked = true;
    gLayerChanged();
    toast('已重新开启对应图层');
  }
  focusNode(id);
  openGIns(id);
});
function gTweenTo(wx, wy, k) {
  var r = gStageRect();
  var ox = $('gIns').classList.contains('open') ? 190 : 0;
  var tx = (r.width - ox * 2) / 2 - wx * k;
  var ty = r.height * 0.5 - wy * k;
  if (RM) {
    G.cam.x = tx; G.cam.y = ty; G.cam.k = clampK(k);
    applyCam();
    requestRender();
    return;
  }
  var from = { x: G.cam.x, y: G.cam.y, k: G.cam.k };
  var t0 = performance.now();
  gCamTween = function () {
    var t = Math.min(1, (performance.now() - t0) / 550);
    var e = 1 - Math.pow(1 - t, 3);
    G.cam.x = from.x + (tx - from.x) * e;
    G.cam.y = from.y + (ty - from.y) * e;
    G.cam.k = from.k + (clampK(k) - from.k) * e;
    applyCam();
    requestRender();
    if (t >= 1) gCamTween = null;
  };
}
function gFlash(id) {
  var el = G.nodeEls[id];
  if (!el) return;
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
  setTimeout(function () { el.classList.remove('flash'); }, RM ? 200 : 1150);
}
function focusNode(id) {
  var n = G.byId[id];
  if (!n) return;
  gTweenTo(n.x, n.y, Math.max(G.cam.k, 0.9));
  gFlash(id);
}
function focusPages(cacheId) {
  var pages = G.nodes.filter(function (n) { return n.type === 'page' && n._owner === cacheId; });
  if (!pages.length) { toast('该文档未建页库', 'warn'); return; }
  if (!G.ly.page) {
    G.ly.page = true;
    $('lyPage').checked = true;
    gLayerChanged();
    toast('已开启页节点图层');
  }
  var cx = 0, cy = 0;
  pages.forEach(function (p) { cx += p.x; cy += p.y; });
  gTweenTo(cx / pages.length, cy / pages.length, Math.max(G.cam.k, 1.0));
  pages.forEach(function (p) { gFlash(p.id); });
}
/* 库范围作用于图谱 */
G.applyScope = function () {
  if (!G.ready) return;
  gLayerChanged();
  applyGDim();
};

/* ============ 推送监听 ============ */
window.addEventListener('snapshot', function (e) {
  try {
    var snap = typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail;
    onSnapshot(snap);
  } catch (err) {}
});
window.addEventListener('preview', function (e) {
  try {
    var d = typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail;
    if (labPolling && d.done) labPoll();
  } catch (err) {}
});

/* ============ 启动 ============ */
function boot() {
  try {
    if (localStorage.getItem('rag-theme') === 'light') applyTheme(true);
  } catch (e) {}
  if (window.__RAG_MOCK && window.__RAG_MOCK.active) $('mockBadge').style.display = 'block';
  gStage = $('gStage');
  gWorld = $('gWorld');
  bindStage();
  bindTip();
  updateScopeUI();
  var booted = false;
  function start() {
    if (booted) return;
    booted = true;
    API.get_snapshot().then(onSnapshot).catch(function () { toast('快照获取失败，请检查后端', 'err'); });
    API.graph().then(gInitFrom).catch(function () {
      $('gpSum').textContent = '图谱加载失败';
      toast('图谱数据加载失败', 'err');
    });
    pollLog();
  }
  if (window.pywebview && window.pywebview.api) start();
  else {
    window.addEventListener('pywebviewready', start, { once: true });
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      if (window.pywebview && window.pywebview.api) { clearInterval(iv); start(); }
      else if (tries > 100) { clearInterval(iv); toast('后端桥连接超时', 'err'); }
    }, 200);
  }
}
boot();
})();
