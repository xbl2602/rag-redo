/* ============================================================
   总览星图（BC-18）：图谱页的"轨道星图"。

   每个库是一根绕中心转的连续发光光管，文件沿管子按内容相近排开；内容越集中的那一段
   点越密，光管就只是略微鼓起（密度驱动，不是一团团的球）；颜色按内容分组、沿管子交融。
   文件的顺序、分组、和前一个文件的内容差距全部由核心算好（core/overview_map.py），
   这里只负责"画"：把差距换成间距、把点数换成粗细，零内容判断（BC-15 零业务逻辑）。

   算法与默认值来自 demos/orbital-overview.html（操作者 2026-09-30 看过效果后定下），
   正式版在"显存"上做了收紧——这个软件对显存很敏感，每一 MB 都要省：
   - 每个点只占 4 字节（文件序号 + 类别 + 库序号打包成一个整数）；点在管子里的随机落点
     用"点的编号"在显卡里现算，不存；颜色、大小、亮度每个文件存一份，放在小纹理里；
   - 星空、尘埃全部在着色器里按编号现算，零缓冲；中心光晕也是现算，不上传贴图；
   - 库名是网页文字，不做成贴图；
   - 不开抗锯齿、深度、模板缓冲，像素比封顶 1，请求低功耗显卡；
   - 只有图谱页看得见时才创建 WebGL；离开页面、窗口被藏起来一会儿，就把显卡资源连同
     WebGL 上下文整个交还；静止观看时帧率降到 30，拖动时 60。
   拖任何形状滑条都只改两张小纹理和几个参数，画面连续变形、不"刷新"（操作者明确要求）。
   ============================================================ */
import * as THREE from 'three';
import { OrbitControls } from '../vendor/three/OrbitControls.js';

/* ============================================================
   0. 常量
   ============================================================ */
const TAU = Math.PI * 2;
const clamp = (v, a, b) => (v < a ? a : (v > b ? b : v));
const fmt = n => Math.round(n).toLocaleString('zh-CN');
const hex2rgb = hex => [1, 3, 5].map(o => parseInt(hex.slice(o, o + 2), 16) / 255);

/* 内容分组的 16 色（核心最多分 16 组），第 17 个是"没有内容向量、没分组"的灰 */
const PALETTE = [
  '#38e1ff', '#4d8cff', '#7a6bff', '#b06bff', '#e05cff', '#ff5c9e', '#ff6b6b', '#ff9147',
  '#ffb03a', '#ffe066', '#c6f24e', '#5fe08a', '#2fe0c0', '#9ad0ff', '#d4a0ff', '#ffb3c7',
];
const UNGROUPED = '#7a8699';
const PAL_RGB = PALETTE.concat([UNGROUPED]).map(hex2rgb);
const LIB_HEX = ['#57d7ff', '#7cff9e', '#ffc46b', '#ff87c2', '#b69cff', '#ff9b6b', '#6be4d6', '#e8f06b'];
/* 每条轨道的倾角（绕 X、绕 Z、再绕 Y 转到哪个方位，单位度）：前 8 个手挑的，之后按序号生成 */
const TILTS = [[7, 4, 0], [-11, 7, 47], [16, -6, 112], [-19, 9, 26], [12, -10, 160], [-6, 14, 210], [20, 3, 275], [-15, -8, 320]];
/* 文件状态 → 亮度：未索引、排队识别、失败的文件画得暗一些 */
const STATE_DIM = { indexed: 1.0, pending: 0.5, ocr: 0.64, failed: 0.32 };

/* 形状（"半径 14 时的标定值"，实际按 sqrt(R/14) 缩放；含义见 demos/README.md） */
const TUBE_R0 = 0.36;      // 管半径（密度等于中位数处）
const BULGE_K = 0.6;       // 鼓起指数：管半径 ∝ 相对密度^(0.6 × 鼓起程度)
const THIN_FLOOR = 0.4;    // 最稀处最细收到管半径的几成：环始终连续，不会断
const THICK_CEIL = 6;      // 安全上限：再密也不超过管半径的几倍
const GAP_FLOOR = 0.15;    // 相邻文件间距的底数：没有它，很像的文件会挤成一颗单色的球
const GAP_REF = 0.5;       // 内容差距的参考尺度
const BELT_REF_R = 14;     // 标定半径
const DENS_BINS = 1024;    // 沿环的采样分辨率
const WEDGE = 32;          // 拾取用的角楔数
const FTEX_W = 4096;       // 每文件数据纹理的宽度（一格一个文件，按行折叠）
const BODY_EXT = 1.8;
/* 加法泼溅的能量守恒参考点（像素）。
 *
 * 真机症状（2026-09-29）：星图放大到 20 倍就整片纯白，缩回去默认视图又灰白看不出色相。
 * 原因不是数据错——组号、调色板、文件颜色都对得上，而是**加法混合的固有行为**：
 * 一个点的屏幕覆盖面积 ∝ gl_PointSize²，而 gl_PointSize ∝ 1/相机距离，所以放大时
 * 每个点的面积按缩放平方涨，同一像素上累加的点亮度也跟着涨，直接冲破 1.0 被裁剪。
 * 三个通道各自裁到 1.0 → 必然是白色，色相信息就这么丢的。
 *
 * 所以按面积反比把每个点的强度压回去，总能量与缩放无关，色相在任何缩放下都稳定。
 * 取 8px 作参考点：默认缩放下 norm≈1，**完全不改变现有观感**，只在放大时开始起作用。
 */
const AREA_REF = 8.0;
const ORBIT_VOL_K = 2 * Math.PI * Math.PI * TUBE_R0 * TUBE_R0 / BELT_REF_R;
const SIZE_FILE = [0.065, 0.14], SIZE_CHUNK = 0.034, SIZE_PAGE = 0.026;
const KIND_FILE = 0, KIND_CHUNK = 1, KIND_PAGE = 2;
/* 点数上限：超过时块和页按比例抽样显示（每个文件的大点一定保留），轨道与粗细仍按真实点数算。
   40 万点 × 4 字节 = 1.6 MB；再多肉眼也分不出来，只是白占显存。 */
const POINT_CAP = 400000;
const MAX_LIBS = 48;       // 每个库占 4 个 uniform 向量，48 个库远在显卡下限之内
const MAX_FILES = 1 << 22; // 点数据里文件序号占 22 位
const N_FIELD = 9000, N_BAND = 8000, N_DUST = 6000;
/* 点在管子里的随机落点：用点的编号做哈希现算（显卡、拾取两边同一个公式），种子固定 */
const SEED = 0x9E3779B9;
const TRUNC = 0.90202;     // 1 - exp(-0.97² / (2·0.45²))：截面偏移是截断在 0.97 的二维高斯（宽 0.45）
const RELEASE_HIDDEN_MS = 20000;   // 窗口被藏起来这么久就交还显卡资源
/* 帧间隔：按累计时间排帧（不是"隔几帧画一次"），60Hz 和 144Hz 的屏上都稳定在约 60 / 30 帧 */
const FRAME_ACTIVE_MS = 1000 / 62, FRAME_IDLE_MS = 1000 / 31, ACTIVE_WINDOW_MS = 1500;

/* 默认值由操作者 2026-09-30 定下（BC-18）：鼓起 0.15、聚合 5.2、交融 5.0、体积光 0.35，其余外观 1；
   目标密度 2750、容许范围 2、轨道最小间距 1、最内轨道离中心 1。 */
const DEFAULTS = Object.freeze({
  size: 1.0, speed: 1.0, spread: 1.0, bulge: 0.15, gather: 5.2, mix: 5.0, body: 0.35, bg: 1.0,
  dens: 2750, densTol: 2.0, orbitGap: 1.0, rMin: 1.0,
});
const ui = Object.assign({ mask: [1, 1, 1], rings: true }, DEFAULTS);

/* ---------- 点偏移的哈希（与着色器里的 hsh() 逐位一致） ---------- */
function hsh(x) {
  x ^= x >>> 16; x = Math.imul(x, 0x7feb352d);
  x ^= x >>> 15; x = Math.imul(x, 0x846ca68b | 0);
  x ^= x >>> 16;
  return x >>> 0;
}
const OFF = new Float64Array(3);
function pointOffsets(v) {
  let h = hsh((v ^ SEED) >>> 0); const u1 = (h >>> 8) / 16777216;
  h = hsh(h); const u2 = (h >>> 8) / 16777216;
  h = hsh(h); const u3 = (h >>> 8) / 16777216;
  h = hsh(h); const u4 = (h >>> 8) / 16777216;
  OFF[0] = clamp(Math.sqrt(-2 * Math.log(1 - u1)) * Math.cos(TAU * u2), -3, 3);
  const rr = 0.45 * Math.sqrt(-2 * Math.log(1 - TRUNC * u3));
  OFF[1] = rr * Math.cos(TAU * u4); OFF[2] = rr * Math.sin(TAU * u4);
  return OFF;
}

/* ============================================================
   1. 数据：把核心给的按列数组变成画图用的结构（只在换数据时做一次，CPU 内存）
   ============================================================ */
let D = null;          // 当前数据：{ libs, nPts, nDraw, pts, info, nf, sampled, skipped }

function orient(li) {
  const t = li < TILTS.length ? TILTS[li] : [((li * 37) % 36) - 18, ((li * 23) % 24) - 12, (li * 137.5) % 360];
  const d2r = THREE.MathUtils.degToRad;
  const rot = new THREE.Matrix4().makeRotationFromEuler(new THREE.Euler(d2r(t[0]), 0, d2r(t[1]), 'XYZ'));
  const m2 = new THREE.Matrix4().makeRotationY(d2r(t[2]));
  const U = new THREE.Vector3(1, 0, 0).applyMatrix4(rot).applyMatrix4(m2);
  const V = new THREE.Vector3(0, 0, 1).applyMatrix4(rot).applyMatrix4(m2);
  return { U, V, nrm: new THREE.Vector3().crossVectors(U, V).normalize() };
}

function prepare(payload) {
  const src = (payload && payload.libs) || [];
  const use = [];
  let skipped = 0;
  src.forEach((L, idx) => {
    if (L && L.rel && L.rel.length && use.length < MAX_LIBS) use.push([L, idx]);
    else skipped++;
  });
  let nFiles = 0, nPts = 0;
  for (const [L] of use) { nFiles += L.rel.length; nPts += L.points; }
  if (nFiles > MAX_FILES) return { libs: [], nPts: 0, nDraw: 0, nf: 0, sampled: false, skipped: src.length, tooMany: true };
  /* 抽样比例只作用于块和页：文件大点全部保留 */
  const ratio = nPts > POINT_CAP ? Math.max(0, (POINT_CAP - nFiles) / Math.max(1, nPts - nFiles)) : 1;

  const libs = [];
  let fileBase = 0, nDraw = 0;
  for (const [L, idx] of use) {
    const li = libs.length, N = L.rel.length;
    const lib = {
      li, src: idx, name: String(L.lib), color: LIB_HEX[li % LIB_HEX.length], N, fileBase,
      dPrev: new Float32Array(N), wOrd: new Float32Array(N), rgbOrd: new Float32Array(N * 3),
      nDrawChunks: new Int32Array(N), nDrawPages: new Int32Array(N), group: new Int16Array(N),
      nPts: 0, R: 1, thin: 1, kappa: 1.3, rot: 0, speed: 0.1, dens: 0,
      phaseT: ((li * 0.6180339887) + 0.137) % 1,     // 各库的接缝错开，不都在同一个角度
      anchor: new Float64Array(N), gap: new Float64Array(N),
      lam: new Float64Array(DENS_BINS), cr: new Float64Array(DENS_BINS), cg: new Float64Array(DENS_BINS),
      cb: new Float64Array(DENS_BINS), rad: new Float64Array(DENS_BINS),
      fileStart: new Int32Array(N + 1), wedges: [],
    };
    Object.assign(lib, orient(li));
    /* 没有内容向量的文件（核心给的差距是 1）排在最后：按本库差距的中位数铺开，
       否则聚合强度一大，这几个"不知道内容"的文件会占掉大半圈 */
    const known = [];
    for (let i = 0; i < N; i++) if (L.group[i] >= 0) known.push(+L.gap[i] || 0);
    const med = known.length ? Float64Array.from(known).sort()[known.length >> 1] : GAP_REF;
    for (let i = 0; i < N; i++) {
      const g = L.group[i] >= 0 && L.group[i] < PALETTE.length ? L.group[i] : -1;
      lib.group[i] = g;
      lib.dPrev[i] = g >= 0 ? Math.max(0, +L.gap[i] || 0) : med;
      const ch = Math.max(0, L.chunks[i] | 0), pg = Math.max(0, L.pages[i] | 0);
      lib.wOrd[i] = 1 + ch + pg;
      lib.nPts += lib.wOrd[i];
      lib.nDrawChunks[i] = ratio < 1 ? Math.round(ch * ratio) : ch;
      lib.nDrawPages[i] = ratio < 1 ? Math.round(pg * ratio) : pg;
      const c = PAL_RGB[g >= 0 ? g : PALETTE.length];
      lib.rgbOrd[i * 3] = c[0]; lib.rgbOrd[i * 3 + 1] = c[1]; lib.rgbOrd[i * 3 + 2] = c[2];
      lib.fileStart[i] = nDraw;
      nDraw += 1 + lib.nDrawChunks[i] + lib.nDrawPages[i];
    }
    lib.fileStart[N] = nDraw;
    libs.push(lib);
    fileBase += N;
  }

  /* 每个点 4 字节：低 22 位文件序号 | 2 位类别 | 高 8 位库序号 */
  const pts = new Uint32Array(nDraw);
  const rows = Math.max(1, Math.ceil(fileBase / FTEX_W));
  const info = new Uint8Array(FTEX_W * rows * 4);
  let w = 0;
  for (let k = 0; k < libs.length; k++) {
    const lib = libs[k], L = use[k][0];
    for (let i = 0; i < lib.N; i++) {
      const fi = lib.fileBase + i, head = (fi | (lib.li << 24)) >>> 0;
      pts[w++] = head;
      for (let c = 0; c < lib.nDrawChunks[i]; c++) pts[w++] = (head | (KIND_CHUNK << 22)) >>> 0;
      for (let p = 0; p < lib.nDrawPages[i]; p++) pts[w++] = (head | (KIND_PAGE << 22)) >>> 0;
      /* 每文件一格：组号、松散度亮度、状态亮度、大点尺寸。越不像邻居越"松"，暗一点 */
      const lf = 1 - 0.22 * clamp((0.85 - (+L.loose[i] || 0)) / 0.7, 0, 1);
      const o = fi * 4;
      info[o] = lib.group[i] >= 0 ? lib.group[i] : 255;
      info[o + 1] = Math.round(lf * 255);
      info[o + 2] = Math.round((STATE_DIM[L.state[i]] || STATE_DIM.pending) * 255);
      info[o + 3] = Math.round(Math.min(1, Math.max(0, L.chunks[i] | 0) / 45) * 255);
    }
  }
  return { libs, nPts, nDraw, nf: fileBase, rows, pts, info, sampled: ratio < 1, ratio, skipped };
}

/* ============================================================
   2. 形状：锚点、剖面、轨道（拖滑条时每帧最多算一次，几毫秒）
   ============================================================ */
/* 环形高斯平滑，可一次处理几条同长数组。σ 不大时补边直接卷积；σ 很大时三遍滑动平均近似，
   比整圈还宽就是整圈平均（滑条放大 10 倍后 σ 可以超过整圈，老写法会卡死、下标越界成 NaN） */
function smoothWrap(arrs, sigma) {
  if (!Array.isArray(arrs)) arrs = [arrs];
  const n = arrs[0].length;
  if (sigma <= 64) {
    const r = Math.min(n >> 1, Math.max(1, Math.ceil(sigma * 3)));
    const k = new Float64Array(2 * r + 1);
    let wsum = 0;
    for (let d = -r; d <= r; d++) { const w = Math.exp(-(d * d) / (2 * sigma * sigma)); k[d + r] = w; wsum += w; }
    for (let d = 0; d <= 2 * r; d++) k[d] /= wsum;
    const ext = new Float64Array(n + 2 * r);
    for (const a of arrs) {
      for (let j = 0; j < n + 2 * r; j++) ext[j] = a[((j - r) % n + n) % n];
      for (let i = 0; i < n; i++) {
        let s = 0;
        for (let d = 0; d <= 2 * r; d++) s += ext[i + d] * k[d];
        a[i] = s;
      }
    }
    return;
  }
  let w = Math.round(Math.sqrt(4 * sigma * sigma + 1));
  if (w % 2 === 0) w++;
  for (const a of arrs) {
    if (w >= n) {
      let m = 0; for (let i = 0; i < n; i++) m += a[i];
      a.fill(m / n);
      continue;
    }
    const half = w >> 1, tmp = new Float64Array(n);
    for (let pass = 0; pass < 3; pass++) {
      let run = 0;
      for (let d = -half; d <= half; d++) run += a[(d + n) % n];
      for (let i = 0; i < n; i++) {
        tmp[i] = run / w;
        run += a[(i + half + 1) % n] - a[(i - half + n) % n];
      }
      a.set(tmp);
    }
  }
}

/* 按当前滑条算一个库的 ① 锚点（写进文件纹理）和 ② 沿环剖面（写进剖面纹理） */
function shapeLibrary(lib, fileData, profData) {
  const R = lib.R, N = lib.N, C = TAU * R, nb = DENS_BINS, h = C / nb;
  const r0 = TUBE_R0 * Math.sqrt(R / BELT_REF_R) * ui.spread * lib.thin;
  const mixS = Math.max(0.02, ui.mix) * r0;              // 每个文件的点沿环散开多宽：越宽，相邻颜色混得越开

  /* ① 锚点：相邻两个文件的间距 = (底数 + 内容差距)^聚合强度，再按比例铺满整圈 */
  const s = lib.anchor, gap = lib.gap;
  let gsum = 0;
  for (let i = 0; i < N; i++) { gap[i] = Math.pow(GAP_FLOOR + Math.min(3, lib.dPrev[i] / GAP_REF), ui.gather); gsum += gap[i]; }
  let acc = lib.phaseT * C;
  for (let i = 0; i < N; i++) { acc += gap[i] * C / gsum; s[i] = acc; fileData[lib.fileBase + i] = acc; }

  /* ② 沿环点密度（权重 = 这个文件的真实点数），按点实际散开的宽度抹开；颜色同样抹开给体积光用 */
  const { lam, cr, cg, cb, rad } = lib;
  lam.fill(0); cr.fill(0); cg.fill(0); cb.fill(0);
  for (let i = 0; i < N; i++) {
    let k = Math.floor(s[i] / h) % nb; if (k < 0) k += nb;
    const w = lib.wOrd[i];
    lam[k] += w; cr[k] += w * lib.rgbOrd[i * 3]; cg[k] += w * lib.rgbOrd[i * 3 + 1]; cb[k] += w * lib.rgbOrd[i * 3 + 2];
  }
  smoothWrap([lam, cr, cg, cb], Math.sqrt(mixS * mixS + (1.5 * r0) ** 2) / h);

  /* ③ 管半径 = r0 × 相对密度^κ，两头用平滑的 max / min 收住（处处可导，从细到粗没有折角） */
  const ref = Math.max(1e-9, Float64Array.from(lam).sort()[nb >> 1]);
  const kap = BULGE_K * ui.bulge, P = 4;
  for (let k = 0; k < nb; k++) {
    let x = Math.pow(Math.max(1e-6, lam[k] / ref), kap);
    x = Math.pow(x ** P + THIN_FLOOR ** P, 1 / P);
    x = 1 / Math.pow(x ** -P + THICK_CEIL ** -P, 1 / P);
    rad[k] = r0 * x;
  }
  smoothWrap(rad, Math.max(1, 0.5 * r0 / h));
  let maxR = 0;
  const row0 = lib.li * 2 * nb * 4, row1 = row0 + nb * 4;
  for (let k = 0; k < nb; k++) {
    const l = Math.max(1e-9, lam[k]);
    maxR = Math.max(maxR, rad[k]);
    profData[row0 + k * 4] = rad[k];
    profData[row0 + k * 4 + 1] = l / ref;
    profData[row1 + k * 4] = cr[k] / l; profData[row1 + k * 4 + 1] = cg[k] / l; profData[row1 + k * 4 + 2] = cb[k] / l;
  }

  /* 拾取用的角楔：点按文件顺序连续存放，每个楔对应一段连续的点序号 */
  const wedges = [];
  let cur = -1, st = 0;
  for (let i = 0; i < N; i++) {
    let a = (s[i] / R) % TAU; if (a < 0) a += TAU;
    const wd = Math.min(WEDGE - 1, Math.floor(a / TAU * WEDGE));
    if (wd !== cur) {
      if (cur >= 0) wedges.push({ start: st, end: lib.fileStart[i], w: cur });
      cur = wd; st = lib.fileStart[i];
    }
  }
  if (cur >= 0) wedges.push({ start: st, end: lib.fileStart[N], w: cur });
  lib.wedges = wedges;
  lib.r0 = r0; lib.mixS = mixS; lib.h = h; lib.kappa = maxR / r0;
  lib.pickPad = maxR + 3 * mixS + 0.5;
}

/* 轨道：按点数定半径让密度落在 [目标 ÷ 容许, 目标 × 容许]，相邻管面至少隔"最小间距"，
   最内管面离中心至少"最内距离"；为留间距被推得太稀 / 太密时，把管子调细 / 调粗把密度拉回区间 */
function layoutOrbits(libs) {
  const ord = libs.slice().sort((a, b) => a.nPts - b.nPts);
  const k = ORBIT_VOL_K * ui.spread * ui.spread, dens = Math.max(1e-6, ui.dens);
  const R = ord.map(l => Math.sqrt(l.nPts / (k * dens)));
  const thin = ord.map(() => 1);
  for (let pass = 0; pass < 3; pass++) {
    const ext = ord.map((l, i) => TUBE_R0 * Math.sqrt(Math.max(R[i], 0.1) / BELT_REF_R) * ui.spread * thin[i] * l.kappa);
    const sep = i => ui.orbitGap + ext[i] + ext[i + 1];
    for (let it = 0; it < 80; it++) {
      for (let i = 0; i + 1 < R.length; i++) {
        const need = sep(i) - (R[i + 1] - R[i]);
        if (need > 0) { R[i] -= need / 2; R[i + 1] += need / 2; }
      }
      R[0] = Math.max(R[0], ui.rMin + ext[0]);
    }
    R[0] = Math.max(R[0], ui.rMin + ext[0]);
    for (let i = 0; i + 1 < R.length; i++) R[i + 1] = Math.max(R[i + 1], R[i] + sep(i));
    const lo = dens / ui.densTol, hi = dens * ui.densTol;
    for (let i = 0; i < R.length; i++) {
      const raw = ord[i].nPts / (k * R[i] * R[i]);
      thin[i] = clamp(raw < lo ? Math.sqrt(raw / lo) : (raw > hi ? Math.sqrt(raw / hi) : 1), 0.15, 6);
    }
  }
  ord.forEach((l, i) => { l.R = R[i]; l.thin = thin[i]; });
  for (const lib of libs) {
    lib.dens = lib.nPts / (ORBIT_VOL_K * (ui.spread * lib.thin) ** 2 * lib.R * lib.R);
    lib.speed = 0.15 * Math.sqrt(8.2 / lib.R);           // 外圈转得慢；角度是累计的，速度变了不会跳
  }
}

/* CPU 侧的管半径插值，和着色器里的 prof() 同一个公式（拾取要和画面一致） */
function radAt(lib, x) {
  const nb = DENS_BINS, u = x / lib.h - 0.5, k0 = Math.floor(u), t = u - k0, a = ((k0 % nb) + nb) % nb;
  return lib.rad[a] * (1 - t) + lib.rad[(a + 1) % nb] * t;
}

/* ============================================================
   3. 着色器
   ============================================================ */
const HASH_GLSL = `
uint hsh(uint x){ x ^= x >> 16u; x *= 0x7feb352du; x ^= x >> 15u; x *= 0x846ca68bu; x ^= x >> 16u; return x; }
float rnd(inout uint h){ h = hsh(h); return float(h >> 8u) * (1.0 / 16777216.0); }`;

function shapeGlsl(nlib) {
  return `
precision highp float;
precision highp int;
precision highp sampler2D;
uniform sampler2D uFile, uInfo, uProf;
uniform vec4 uLibA[${nlib}], uLibB[${nlib}];
uniform vec3 uLibU[${nlib}], uLibV[${nlib}];
const float NB = ${DENS_BINS}.0;
vec4 prof(float s, float C, int row){
  float u = s / C * NB - 0.5;
  float k0 = floor(u), t = u - k0;
  int a = int(mod(k0, NB)), b = int(mod(k0 + 1.0, NB));
  return mix(texelFetch(uProf, ivec2(a, row), 0), texelFetch(uProf, ivec2(b, row), 0), t);
}`;
}

function pointVS(nlib) {
  return shapeGlsl(nlib) + HASH_GLSL + `
uniform float uSizeK;
uniform int uHover, uSel;
uniform vec3 uKindMask;
uniform vec3 uPal[${PAL_RGB.length}];
attribute uint aPt;        // 低 22 位文件序号 | 2 位类别（文件/块/页） | 高 8 位库序号
varying vec3 vColor;
varying float vFade, vPx, vSpike, vNorm;
const float TAU = 6.28318530718;
void main(){
  int fi = int(aPt & 0x3FFFFFu), kind = int((aPt >> 22u) & 3u), li = int(aPt >> 24u);
  float on = kind == 0 ? uKindMask.x : (kind == 1 ? uKindMask.y : uKindMask.z);
  if (on < 0.5){ gl_Position = vec4(0.0, 0.0, 2.0, 1.0); gl_PointSize = 0.0; return; }
  /* 落点：沿环一个截断在 ±3 的高斯数（乘散开宽度），截面一个截断在 0.97 的二维高斯（乘当地管半径） */
  uint h = hsh(uint(gl_VertexID) ^ ${SEED >>> 0}u);
  float u1 = float(h >> 8u) * (1.0 / 16777216.0);
  float u2 = rnd(h), u3 = rnd(h), u4 = rnd(h);
  float y = clamp(sqrt(-2.0 * log(1.0 - u1)) * cos(TAU * u2), -3.0, 3.0);
  float rr = 0.45 * sqrt(-2.0 * log(1.0 - ${TRUNC} * u3));
  float cx = rr * cos(TAU * u4), cz = rr * sin(TAU * u4);
  vec4 A = uLibA[li], B = uLibB[li];
  ivec2 ft = ivec2(fi % ${FTEX_W}, fi / ${FTEX_W});
  float s = texelFetch(uFile, ft, 0).r + y * A.w;
  vec4 inf = texelFetch(uInfo, ft, 0);      // 组号 / 松散度亮度 / 状态亮度 / 大点尺寸
  vec4 pr = prof(s, A.y, int(B.y + 0.5));
  float ang = s / A.x + B.x;
  vec3 Uv = uLibU[li], Vv = uLibV[li];
  vec3 p = (Uv * cos(ang) + Vv * sin(ang)) * (A.x + cx * pr.x) + cross(Uv, Vv) * (cz * pr.x);
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * mv;
  float sel = float(fi == uSel);
  float hit = max(float(gl_VertexID == uHover), kind == 0 ? sel : 0.35 * sel);
  float size = kind == 0 ? mix(${SIZE_FILE[0]}, ${SIZE_FILE[1]}, inf.a) : (kind == 1 ? ${SIZE_CHUNK} : ${SIZE_PAGE});
  vPx = size * (1.0 + 1.4 * hit) * uSizeK / max(0.001, -mv.z);
  gl_PointSize = max(1.0, vPx);
  /* 能量守恒（见 AREA_REF 的注释）：按点面积的倒数压强度。
   * **双向夹紧**是必须的：只写下界的话，缩到远处时 area 被 max(…,1.0) 兜成 1，
   * 算出来是 AREA_REF²=64 倍亮度，远处直接烧成噪点。上界 1.0 表示"比参考点还小
   * 就不补偿"——远处维持原样，只有放大到超过 8px 时才开始按平方反比压。 */
  float area = max(vPx, 1.0);
  vNorm = clamp((${AREA_REF.toFixed(1)} * ${AREA_REF.toFixed(1)}) / (area * area), 0.02, 1.0);
  vSpike = kind == 0 ? 0.5 : 0.0;
  int g = int(inf.r * 255.0 + 0.5);
  vec3 base = uPal[g < ${PALETTE.length} ? g : ${PALETTE.length}];
  float factor = (kind == 0 ? inf.b : (kind == 1 ? 0.90 : 0.82)) * inf.g;
  /* 越密的地方单个点越暗：叠加后仍是"管芯发白、往外带色"，不是一片过曝的白 */
  float dim = clamp(0.62 * inversesqrt(max(0.5, pr.y)), 0.06, 0.8);
  vColor = mix(base * factor * dim, vec3(1.0), 0.75 * hit);
  vFade = clamp(1.3 - (-mv.z) / 210.0, 0.22, 1.0);
}`;
}
const POINT_FS = `
varying vec3 vColor;
varying float vFade, vPx, vSpike, vNorm;
void main(){
  vec2 q = gl_PointCoord - 0.5;
  float d = length(q) * 2.0;
  if (d > 1.0) discard;
  float core = pow(max(0.0, 1.0 - d), 2.2);
  float halo = smoothstep(1.0, 0.04, d) * 0.26;
  float bar = max(0.0, 1.0 - min(abs(q.x), abs(q.y)) * 26.0) * max(0.0, 1.0 - d * 1.05);
  float spike = bar * smoothstep(5.0, 17.0, vPx) * vSpike;
  /* vNorm 等比缩放三个通道：色相（通道之间的比例）不变，所以放大也不会洗白。 */
  gl_FragColor = vec4(vColor * (core + halo + spike) * vFade * vNorm, 1.0);
}`;

/* 体积光：每库沿环每一格一片正对镜头的方片，按"视线穿过小球的厚度"出亮度，一串叠起来就是有体积的光管 */
function bodyVS(nlib) {
  return shapeGlsl(nlib) + `
attribute vec2 iKey;       // x=库序号 y=第几格
uniform float uSizeK;
varying vec2 vQ;
varying vec3 vCol;
varying float vGain;
const float EXT = ${BODY_EXT.toFixed(2)};
void main(){
  int li = int(iKey.x + 0.5), k = int(iKey.y + 0.5);
  vec4 A = uLibA[li], B = uLibB[li];
  int row = int(B.y + 0.5);
  vec4 pr = texelFetch(uProf, ivec2(k, row), 0);
  float h = A.y / NB, r = max(pr.x, 1e-4), q = pr.y;
  float m = exp2(floor(log2(max(1.0, 0.3 * r / h))));
  if (mod(iKey.y, m) > 0.5){ gl_Position = vec4(2.0, 2.0, 2.0, 1.0); vGain = 0.0; return; }
  float ang = (iKey.y + 0.5) * h / A.x + B.x;
  vec3 c = (uLibU[li] * cos(ang) + uLibV[li] * sin(ang)) * A.x;
  vec4 mv = modelViewMatrix * vec4(c, 1.0);
  mv.xy += position.xy * 2.0 * r * EXT;
  vQ = position.xy * 2.0 * EXT;
  vCol = texelFetch(uProf, ivec2(k, row + 1), 0).rgb;
  float I = q >= 1.0 ? min(1.0, 0.42 + 0.16 * log(q)) : 0.42 * clamp(sqrt(q), 0.55, 1.0);
  vGain = I * m * h / (1.2 * r) * clamp(1.3 - (-mv.z) / 210.0, 0.22, 1.0);
  /* 和点层同一套能量守恒（见 AREA_REF）：体积光是加法混合，方片的屏幕面积同样
   * ∝ 1/距离²，放大 20 倍时整根管子会一起冲破 1.0 裁成白色——这正是真机看到的
   * "拉到 20x 纯白"。上面那个 m/r 只管**几何密度**（管子变细时抽稀），管不了缩放。
   * 方片的世界半宽是 2*r*EXT，按同样公式换算成屏幕像素后再夹紧。 */
  float sHalf = 2.0 * r * EXT * uSizeK / max(0.001, -mv.z);
  vGain *= clamp((${AREA_REF.toFixed(1)} * ${AREA_REF.toFixed(1)}) / max(1.0, sHalf * sHalf), 0.02, 1.0);
  gl_Position = projectionMatrix * mv;
}`;
}
const BODY_FS = `
uniform float uBody;
varying vec2 vQ;
varying vec3 vCol;
varying float vGain;
const float EXT = ${BODY_EXT.toFixed(2)};
void main(){
  float b = length(vQ);
  if (b > EXT) discard;
  float fill = pow(max(0.0, 1.0 - b * b), 0.75);
  float core = pow(1.0 + b * b / 0.05, -1.5);
  float halo = exp(-b * b / 1.2) * 0.05;
  vec3 col = vCol * (fill * 0.55 + halo) + mix(vCol, vec3(1.0), 0.55) * core * 0.40;
  col *= smoothstep(EXT, EXT - 0.4, b);
  gl_FragColor = vec4(col * vGain * uBody, 1.0);
}`;

/* 背景：天空（星云 + 银河带）跟着镜头走；远星、星尘全部按编号在着色器里现算，不占缓冲 */
const BAND_N = new THREE.Vector3(0.34, 0.83, 0.44).normalize();
const BAND_E1 = new THREE.Vector3(1, 0, 0).cross(BAND_N).normalize();
const BAND_E2 = BAND_N.clone().cross(BAND_E1).normalize();
const SKY_VS = `
varying vec3 vDir;
void main(){
  vDir = position;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;
const SKY_FS = `
uniform float uBg;
uniform vec3 uBandN;
varying vec3 vDir;
float hash(vec3 p){ p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float noise(vec3 x){
  vec3 i = floor(x), f = fract(x);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
float fbm(vec3 p){ float a = 0.5, s = 0.0; for (int i = 0; i < 5; i++){ s += a * noise(p); p = p * 2.03 + 11.7; a *= 0.5; } return s; }
void main(){
  vec3 d = normalize(vDir);
  float lat = dot(d, uBandN);
  float band = exp(-lat * lat / 0.03);
  float n1 = fbm(d * 2.4 + 3.0);
  float n2 = fbm(d * 4.8 + n1 * 1.7);
  float cloud = smoothstep(0.42, 0.82, n2);
  float hue = fbm(d * 1.4 + 7.0);
  vec3 neb = mix(vec3(0.20, 0.07, 0.36), vec3(0.03, 0.20, 0.34), smoothstep(0.38, 0.62, hue));
  neb = mix(neb, vec3(0.36, 0.08, 0.20), smoothstep(0.60, 0.78, hue));
  vec3 col = neb * cloud * (0.30 + 0.70 * band) * 0.55;
  float lane = smoothstep(0.40, 0.72, fbm(d * 7.0 + 1.3));
  col += vec3(0.09, 0.11, 0.16) * band * (0.5 + 0.5 * n1) * (1.0 - 0.65 * lane);
  col += vec3(0.020, 0.028, 0.050) * (0.6 + 0.4 * n1);
  gl_FragColor = vec4(col * uBg, 1.0);
}`;
const STAR_VS = HASH_GLSL + `
uniform float uClock, uBg;
uniform vec3 uBandN, uE1, uE2;
varying vec3 vC;
varying float vS;
const float TAU = 6.28318530718;
const vec3 TEMP[7] = vec3[7](vec3(0.61,0.69,1.0), vec3(0.67,0.75,1.0), vec3(0.79,0.84,1.0), vec3(0.97,0.97,1.0),
                             vec3(1.0,0.96,0.92), vec3(1.0,0.82,0.63), vec3(1.0,0.80,0.44));
const float TCUM[7] = float[7](0.06, 0.16, 0.32, 0.58, 0.78, 0.92, 1.01);
void main(){
  uint h = uint(gl_VertexID) * 2654435761u ^ 0x57a25u;
  bool band = gl_VertexID >= ${N_FIELD};
  vec3 d;
  if (!band){
    float u = rnd(h) * 2.0 - 1.0, th = rnd(h) * TAU, s = sqrt(1.0 - u * u);
    d = vec3(s * cos(th), u, s * sin(th));
  } else {
    float th = rnd(h) * TAU;
    float g = sqrt(-2.0 * log(1.0 - rnd(h))) * cos(TAU * rnd(h));
    float lat = clamp(g * 0.10, -0.5, 0.5);
    d = (uE1 * cos(th) + uE2 * sin(th)) * cos(lat) + uBandN * sin(lat);
  }
  gl_Position = projectionMatrix * modelViewMatrix * vec4(d * 1000.0, 1.0);
  float m = pow(rnd(h), 3.2);                 // 亮度幂律分布：绝大多数暗的小星，偶尔一颗很亮
  float r = rnd(h);
  vec3 c = TEMP[6];
  for (int i = 0; i < 7; i++){ if (r <= TCUM[i]){ c = TEMP[i]; break; } }
  float b = (band ? 0.22 : 0.30) + 0.95 * m;
  vS = 1.1 + 4.2 * m + rnd(h) * 0.6;
  gl_PointSize = vS;
  float t0 = rnd(h);
  float tw = 0.72 + 0.28 * sin(uClock * (0.5 + fract(t0 * 7.31) * 2.3) + t0 * TAU);
  vC = c * b * tw * uBg;
}`;
const STAR_FS = `
varying vec3 vC;
varying float vS;
void main(){
  vec2 q = gl_PointCoord - 0.5;
  float d = length(q) * 2.0;
  if (d > 1.0) discard;
  float core = exp(-d * d * 9.0), halo = exp(-d * d * 2.6) * 0.30;
  float bar = max(0.0, 1.0 - min(abs(q.x), abs(q.y)) * 16.0) * max(0.0, 1.0 - d) * smoothstep(3.5, 7.0, vS) * 0.55;
  gl_FragColor = vec4(vC * (core + halo + bar), 1.0);
}`;
const DUST_VS = HASH_GLSL + `
uniform float uTime, uSizeK, uBg, uDustK;
varying vec3 vC;
const float TAU = 6.28318530718;
void main(){
  uint h = uint(gl_VertexID) * 2246822519u ^ 0xd057u;
  float r = sqrt(9.0 + rnd(h) * (34.0 * 34.0 - 9.0));
  float a0 = rnd(h) * TAU;
  float g = sqrt(-2.0 * log(1.0 - rnd(h))) * cos(TAU * rnd(h));
  float y = clamp(g * (0.8 + 0.07 * r), -6.0, 6.0);
  float ws = 0.03 + 0.06 * pow(rnd(h), 2.0);
  float t = rnd(h), b = 0.10 + 0.22 * pow(rnd(h), 2.0);
  float a = a0 + uTime * 0.11 * inversesqrt(max(r, 1.0));    // 内快外慢，缓慢打转
  vec4 mv = modelViewMatrix * vec4(vec3(r * cos(a), y, r * sin(a)) * uDustK, 1.0);
  gl_Position = projectionMatrix * mv;
  float px = ws * uSizeK / max(0.001, -mv.z);
  gl_PointSize = max(1.0, px);
  vC = vec3(0.55 + 0.35 * t, 0.62 + 0.1 * t, 1.0 - 0.1 * t) * b * uBg * clamp(px, 0.35, 1.0) * clamp(1.3 - (-mv.z) / 210.0, 0.22, 1.0);
}`;
const DUST_FS = `
varying vec3 vC;
void main(){
  float d = length(gl_PointCoord - 0.5) * 2.0;
  if (d > 1.0) discard;
  gl_FragColor = vec4(vC * pow(1.0 - d, 1.8), 1.0);
}`;
/* 中心空心线框星：线框球心在原点，法线就是归一化的位置，不另存 */
const WIRE_VS = `
uniform float uTime, uSpin;
varying float vFace, vPulse;
void main(){
  float c = cos(uTime * uSpin), s = sin(uTime * uSpin);
  vec3 p = vec3(position.x, position.y * c - position.z * s, position.y * s + position.z * c);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  vFace = 0.09 + 0.91 * smoothstep(-0.4, 0.8, normalize(normalMatrix * normalize(position)).z);
  vPulse = 0.82 + 0.18 * sin(uTime * 0.9);
}`;
const WIRE_FS = `
uniform vec3 uColor;
uniform float uOpacity;
varying float vFace, vPulse;
void main(){ gl_FragColor = vec4(uColor * vFace * vPulse * uOpacity, 1.0); }`;
/* 中心光晕：正对镜头的方片，径向渐变在着色器里算（原型用的是一张 256² 的贴图） */
const GLOW_VS = `
varying vec2 vQ;
void main(){
  vQ = position.xy * 2.0;
  vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
  mv.xy += position.xy * 11.0;
  gl_Position = projectionMatrix * mv;
}`;
const GLOW_FS = `
varying vec2 vQ;
void main(){
  float d = length(vQ);
  if (d > 1.0) discard;
  vec4 a = vec4(0.608, 0.863, 1.0, 0.6), b = vec4(0.353, 0.667, 1.0, 0.18), c = vec4(0.235, 0.471, 1.0, 0.0);
  vec4 g = d < 0.22 ? mix(a, b, d / 0.22) : mix(b, c, (d - 0.22) / 0.78);
  gl_FragColor = vec4(g.rgb * g.a, 1.0);
}`;
const RING_VS = `
void main(){ gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const RING_FS = `
uniform vec3 uColor;
void main(){ gl_FragColor = vec4(uColor * 0.22, 1.0); }`;

/* ============================================================
   4. 显卡资源：只在看得见时存在
   ============================================================ */
let host = null, hooks = {}, labelsEl = null;
let gl = null;              // 当前的 WebGL 世界；null = 没占显卡
let visible = false, hiddenTimer = 0, shapeDirty = false;
let camSaved = null;        // 离开页面时记住视角，回来接着看
let pendingCam = null;      // 跨会话记住的机位：mount() 读出来、create() 建好相机后回放
let selRef = null;          // 当前选中的文件 { li(内部序号), i }
let fitPending = true;

/* 纯加色，而且不写画布透明度：three 自带的加色混合会把透明度也叠满，背景被挖出黑圆盘 */
const ADD = {
  blending: THREE.CustomBlending, blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor, blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor, blendDstAlpha: THREE.OneFactor,
  depthWrite: false, depthTest: false, transparent: true,
};

function create() {
  if (gl || !host) return;
  const canvas = document.createElement('canvas');
  canvas.className = 'sm-canvas';
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: false, alpha: true, depth: false, stencil: false,
      powerPreference: 'low-power', preserveDrawingBuffer: false, premultipliedAlpha: true,
    });
  } catch (e) {
    fail('这台电脑拿不到 WebGL（显卡驱动或系统设置限制），星图无法显示。');
    return;
  }
  if (!renderer.capabilities.isWebGL2) {
    renderer.dispose(); renderer.forceContextLoss();
    fail('星图需要 WebGL2，这台电脑的显卡或驱动不支持。');
    return;
  }
  renderer.setPixelRatio(1);                            // 像素比封顶 1：高分屏上画面缓冲只有 1/4
  renderer.setClearColor(0x000000, 0);
  host.insertBefore(canvas, host.firstChild);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(46, 1, 0.3, 3000);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true; controls.dampingFactor = 0.07;
  controls.minDistance = 2.2; controls.maxDistance = 400;
  controls.rotateSpeed = 0.62; controls.zoomSpeed = 0.9;

  const U = {
    uTime: { value: 0 }, uClock: { value: 0 }, uSizeK: { value: 1 }, uHover: { value: -1 }, uSel: { value: -1 },
    uKindMask: { value: new THREE.Vector3(...ui.mask) }, uBody: { value: ui.body }, uBg: { value: ui.bg },
    uPal: { value: PAL_RGB.map(c => new THREE.Vector3(c[0], c[1], c[2])) },
    uFile: { value: null }, uInfo: { value: null }, uProf: { value: null },
    uLibA: { value: [] }, uLibB: { value: [] }, uLibU: { value: [] }, uLibV: { value: [] },
    uBandN: { value: BAND_N }, uE1: { value: BAND_E1 }, uE2: { value: BAND_E2 }, uDustK: { value: 1 },
  };
  gl = {
    canvas, renderer, scene, camera, controls, U, raf: 0, last: performance.now(), nextDraw: 0, lastInput: 0,
    mouse: null, down: null, frames: 0, fps: 0, tween: null, spin: 1, owned: [], data: null, statsAt: performance.now(),
  };

  /* 背景与中心星：和数据无关，建一次 */
  const skyGroup = new THREE.Group();
  gl.skyGroup = skyGroup;
  scene.add(skyGroup);
  const skyGeo = new THREE.SphereGeometry(1400, 32, 16);
  skyGeo.deleteAttribute('normal'); skyGeo.deleteAttribute('uv');
  const sky = own(new THREE.Mesh(skyGeo, new THREE.ShaderMaterial({
    uniforms: { uBg: U.uBg, uBandN: U.uBandN }, vertexShader: SKY_VS, fragmentShader: SKY_FS, side: THREE.BackSide, ...ADD,
  })));
  sky.renderOrder = -10;
  skyGroup.add(sky);
  const stars = own(new THREE.Points(emptyGeo(N_FIELD + N_BAND),
    new THREE.ShaderMaterial({ uniforms: U, vertexShader: STAR_VS, fragmentShader: STAR_FS, ...ADD })));
  stars.renderOrder = -9;
  skyGroup.add(stars);
  scene.add(own(new THREE.Points(emptyGeo(N_DUST),
    new THREE.ShaderMaterial({ uniforms: U, vertexShader: DUST_VS, fragmentShader: DUST_FS, ...ADD }))));
  buildCenterStar(scene, U);

  canvas.addEventListener('webglcontextlost', onContextLost, false);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerleave', onPointerLeave);
  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('wheel', markInput, { passive: true });
  controls.addEventListener('change', markInput);
  /* 相机一动就把机位存下来（savePrefs 内部有 400ms 防抖，拖动时不会每帧写盘）。
   * 这条覆盖所有改变机位的途径：拖拽、滚轮缩放、双击聚焦——它们全都发 change。 */
  controls.addEventListener('change', savePrefs);
  gl.ro = new ResizeObserver(resize);
  gl.ro.observe(host);
  resize();
  if (D) buildData();
  /* 优先用上次记住的机位；没有才 fit 到默认取景。
   * pendingCam 由 mount() 里的 loadPrefs() 填好——那时 gl 还没建，相机读不到，
   * 所以必须把记录留到这一步再回放。 */
  if (camSaved) {
    camera.position.copy(camSaved.pos); controls.target.copy(camSaved.target); controls.update();
  } else if (pendingCam) {
    restoreCamera(pendingCam);
  } else fit(false);
  gl.raf = requestAnimationFrame(frame);
}

function own(obj) { obj.frustumCulled = false; gl.owned.push(obj); return obj; }
/* 没有任何顶点属性的几何体：点的位置、颜色全在着色器里按编号现算 */
function emptyGeo(n) {
  const g = new THREE.BufferGeometry();
  g.setDrawRange(0, n);
  return g;
}
function wire(pos, color, opacity, spin, U) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  return own(new THREE.LineSegments(g, new THREE.ShaderMaterial({
    uniforms: { uTime: U.uTime, uSpin: { value: spin }, uColor: { value: new THREE.Vector3(...hex2rgb(color)) }, uOpacity: { value: opacity } },
    vertexShader: WIRE_VS, fragmentShader: WIRE_FS, ...ADD,
  })));
}
function buildCenterStar(scene, U) {
  const group = new THREE.Group();
  const P = (deg, t, radius) => { const a = deg * Math.PI / 180, r = Math.cos(a) * radius; return [r * Math.cos(t), Math.sin(a) * radius, r * Math.sin(t)]; };
  const ll = [];
  const lat = 16, lon = 28, rad = 1.35;
  for (let i = 0; i <= lat; i++) {
    const deg = -86 + 172 * i / lat;
    for (let j = 0; j < lon; j++) ll.push(...P(deg, TAU * j / lon, rad), ...P(deg, TAU * (j + 1) / lon, rad));
  }
  for (let j = 0; j < lon; j++) {
    const t = TAU * j / lon;
    for (let i = 0; i < lat; i++) ll.push(...P(-86 + 172 * i / lat, t, rad), ...P(-86 + 172 * (i + 1) / lat, t, rad));
  }
  group.add(wire(ll, '#cdefff', 0.95, 0.21, U));
  for (const [radius, detail, color, opacity, spin] of [[1.62, 1, '#5fc8ff', 0.55, -0.14], [1.92, 0, '#8a72ff', 0.34, 0.10]]) {
    const ico = new THREE.IcosahedronGeometry(radius, detail);
    const wg = new THREE.WireframeGeometry(ico);
    group.add(wire(Array.from(wg.attributes.position.array), color, opacity, spin, U));
    ico.dispose(); wg.dispose();
  }
  const eq = [];
  for (let j = 0; j < 180; j++) {
    const t0 = TAU * j / 180, t1 = TAU * (j + 1) / 180;
    eq.push(Math.cos(t0) * 2.2, 0, Math.sin(t0) * 2.2, Math.cos(t1) * 2.2, 0, Math.sin(t1) * 2.2);
  }
  group.add(wire(eq, '#4aa8ff', 0.6, 0, U));
  const pg = new THREE.PlaneGeometry(1, 1);
  pg.deleteAttribute('normal'); pg.deleteAttribute('uv');
  const glow = own(new THREE.Mesh(pg, new THREE.ShaderMaterial({ vertexShader: GLOW_VS, fragmentShader: GLOW_FS, ...ADD })));
  glow.frustumCulled = false;
  group.add(glow);
  scene.add(group);
}

/* 换数据（或重新占用显卡）时：建点、体积光、轨道线、三张数据纹理 */
function buildData() {
  if (!gl) return;
  dropData();
  if (!D || !D.libs.length) { renderLabels(); return; }
  const { scene, U } = gl;
  const nlib = D.libs.length;
  const fileTex = new THREE.DataTexture(new Float32Array(FTEX_W * D.rows), FTEX_W, D.rows, THREE.RedFormat, THREE.FloatType);
  const infoTex = new THREE.DataTexture(D.info, FTEX_W, D.rows, THREE.RGBAFormat, THREE.UnsignedByteType);
  const profTex = new THREE.DataTexture(new Float32Array(DENS_BINS * 2 * nlib * 4), DENS_BINS, 2 * nlib, THREE.RGBAFormat, THREE.FloatType);
  for (const t of [fileTex, infoTex, profTex]) {
    t.minFilter = t.magFilter = THREE.NearestFilter; t.generateMipmaps = false; t.needsUpdate = true;
  }
  U.uFile.value = fileTex; U.uInfo.value = infoTex; U.uProf.value = profTex;
  U.uLibA.value = D.libs.map(() => new THREE.Vector4());
  U.uLibB.value = D.libs.map(l => new THREE.Vector4(l.rot, l.li * 2, 0, 0));
  U.uLibU.value = D.libs.map(l => l.U.clone());
  U.uLibV.value = D.libs.map(l => l.V.clone());

  const pg = new THREE.BufferGeometry();
  pg.setAttribute('aPt', new THREE.BufferAttribute(D.pts, 1));   // Uint32 → 显卡上是整数属性
  pg.setDrawRange(0, D.nDraw);
  const points = new THREE.Points(pg, new THREE.ShaderMaterial({ uniforms: U, vertexShader: pointVS(nlib), fragmentShader: POINT_FS, ...ADD }));
  points.frustumCulled = false;

  const plane = new THREE.PlaneGeometry(1, 1);
  const bg = new THREE.InstancedBufferGeometry();
  bg.index = plane.index;
  bg.setAttribute('position', plane.attributes.position);
  const m = nlib * DENS_BINS, iKey = new Float32Array(m * 2);
  for (let li = 0, q = 0; li < nlib; li++) for (let kb = 0; kb < DENS_BINS; kb++, q++) { iKey[q * 2] = li; iKey[q * 2 + 1] = kb; }
  bg.setAttribute('iKey', new THREE.InstancedBufferAttribute(iKey, 2));
  bg.instanceCount = m;
  const body = new THREE.Mesh(bg, new THREE.ShaderMaterial({ uniforms: U, vertexShader: bodyVS(nlib), fragmentShader: BODY_FS, ...ADD }));
  body.frustumCulled = false;

  /* 轨道参考线：所有库共用一条单位圆，按各自的朝向和半径摆放 */
  const circ = [];
  for (let j = 0; j < 256; j++) circ.push(Math.cos(TAU * j / 256), Math.sin(TAU * j / 256), 0);
  const ringGeo = new THREE.BufferGeometry();
  ringGeo.setAttribute('position', new THREE.Float32BufferAttribute(circ, 3));
  const rings = new THREE.Group();
  for (const lib of D.libs) {
    const ring = new THREE.LineLoop(ringGeo, new THREE.ShaderMaterial({
      uniforms: { uColor: { value: new THREE.Vector3(...hex2rgb(lib.color)) } }, vertexShader: RING_VS, fragmentShader: RING_FS, ...ADD,
    }));
    ring.matrixAutoUpdate = false;
    ring.frustumCulled = false;
    lib.ring = ring;
    rings.add(ring);
  }
  rings.visible = ui.rings;
  scene.add(points, body, rings);
  gl.data = { points, body, rings, ringGeo, plane, fileTex, infoTex, profTex };
  renderLabels();
  applyShape();
  if (fitPending) { fitPending = false; fit(false); }
}

function dropData() {
  if (!gl || !gl.data) return;
  const { points, body, rings, ringGeo, plane, fileTex, infoTex, profTex } = gl.data;
  gl.scene.remove(points, body, rings);
  points.geometry.dispose(); points.material.dispose();
  body.geometry.dispose(); body.material.dispose(); plane.dispose();
  for (const r of rings.children) r.material.dispose();
  ringGeo.dispose();
  fileTex.dispose(); infoTex.dispose(); profTex.dispose();
  gl.data = null;
}

/* 离开页面：把显卡资源连同 WebGL 上下文整个交还（BC-18："离开即释放"） */
function destroy() {
  if (!gl) return;
  cancelAnimationFrame(gl.raf);
  camSaved = { pos: gl.camera.position.clone(), target: gl.controls.target.clone() };
  dropData();
  for (const o of gl.owned) {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  }
  gl.ro.disconnect();
  gl.controls.dispose();
  const c = gl.canvas;
  c.removeEventListener('webglcontextlost', onContextLost, false);
  const lost = gl.renderer.getContext().isContextLost();
  gl.renderer.dispose();
  if (!lost) gl.renderer.forceContextLoss();          // 已经被系统收回的上下文不能再"交还"一次
  c.remove();
  gl = null;
  if (hooks.hover) hooks.hover(null);
  if (hooks.stats) hooks.stats({ fps: 0, vramMB: 0, points: D ? D.nDraw : 0, released: true });
}

function onContextLost(e) {
  /* 不是自己交还的（驱动重置、显存吃紧被系统收回）：稍后重建一次 */
  e.preventDefault();
  if (!gl) return;
  destroy();
  setTimeout(() => { if (visible && !document.hidden) create(); }, 1000);
}

function fail(msg) { if (hooks.error) hooks.error(msg); }

/* 按当前滑条重算所有库的轨道、锚点和剖面，上传纹理 */
function applyShape() {
  if (!gl || !gl.data || !D) return;
  const libs = D.libs, fd = gl.data.fileTex.image.data, pd = gl.data.profTex.image.data, U = gl.U;
  /* 排轨道要用到管子最粗处（κ），κ 又是形状算出来的：先排、再算形状；κ 变化超过 5% 就再排一次 */
  for (let pass = 0; pass < 2; pass++) {
    const before = libs.map(l => l.kappa);
    layoutOrbits(libs);
    for (const lib of libs) {
      shapeLibrary(lib, fd, pd);
      U.uLibA.value[lib.li].set(lib.R, TAU * lib.R, lib.r0, lib.mixS);
    }
    if (libs.every((l, i) => Math.abs(l.kappa - before[i]) < 0.05 * before[i])) break;
  }
  let rMax = 0;
  for (const lib of libs) {
    lib.ring.matrix.makeBasis(lib.U, lib.V, lib.nrm).multiply(new THREE.Matrix4().makeScale(lib.R, lib.R, lib.R));
    lib.ring.matrixWorldNeedsUpdate = true;
    rMax = Math.max(rMax, lib.R + lib.kappa * lib.r0);
  }
  D.rMax = rMax;
  U.uDustK.value = Math.max(1, rMax / 22);
  gl.data.fileTex.needsUpdate = true;
  gl.data.profTex.needsUpdate = true;
  renderOrbitTable();
}

/* ============================================================
   5. 每帧：公转、拾取、标签、统计
   ============================================================ */
function resize() {
  if (!gl) return;
  const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
  gl.renderer.setSize(w, h, false);
  gl.canvas.style.width = w + 'px'; gl.canvas.style.height = h + 'px';
  gl.camera.aspect = w / h;
  gl.camera.updateProjectionMatrix();
  gl.U.uSizeK.value = h / (2 * Math.tan(THREE.MathUtils.degToRad(gl.camera.fov) / 2)) * ui.size;
}
function markInput() { if (gl) gl.lastInput = performance.now(); }

function frame(now) {
  if (!gl) return;
  gl.raf = requestAnimationFrame(frame);
  /* 帧率封顶：拖动、滚轮、调滑条时 60，静止观看 30 —— 公转很慢，30 帧看不出差别，显卡负载减半 */
  const active = now - gl.lastInput < ACTIVE_WINDOW_MS || gl.tween;
  const step = active ? FRAME_ACTIVE_MS : FRAME_IDLE_MS;
  if (now < gl.nextDraw) return;
  gl.nextDraw = now - gl.nextDraw > step ? now + step : gl.nextDraw + step;   // 落后太多（刚切回来）就重新对齐，不补帧
  const dt = Math.min(0.05, (now - gl.last) / 1000);
  gl.last = now;
  const U = gl.U;
  /* 选中文件时公转停下来（看详情时它不会转走），关掉详情再慢慢转起来 */
  gl.spin += ((selRef ? 0 : 1) - gl.spin) * Math.min(1, dt * (selRef ? 14 : 3));
  const sp = ui.speed * gl.spin;
  U.uTime.value += dt * sp;
  U.uClock.value += dt;
  if (D && gl.data) for (const lib of D.libs) { lib.rot += dt * sp * lib.speed; U.uLibB.value[lib.li].x = lib.rot; }
  if (shapeDirty) { shapeDirty = false; applyShape(); }
  if (gl.tween) stepTween(now);
  gl.controls.update();
  gl.skyGroup.position.copy(gl.camera.position);
  if (gl.mouse && !gl.down) pick(false);
  placeLabels();
  gl.renderer.render(gl.scene, gl.camera);
  gl.frames++;
  if (now - gl.statsAt > 500) {
    gl.fps = gl.frames * 1000 / (now - gl.statsAt);      // 按真实经过的时间算（dt 被截断过，不能拿来算帧率）
    gl.frames = 0; gl.statsAt = now;
    if (hooks.stats) hooks.stats({ fps: gl.fps, vramMB: vramBytes() / 1048576, points: D ? D.nDraw : 0, zoom: zoomRatio() });
  }
}

/* 估算这张图占的显存：画面缓冲（前后两份）+ 点缓冲 + 体积光 + 线框 + 三张数据纹理 */
function vramBytes() {
  if (!gl) return 0;
  const size = gl.renderer.getDrawingBufferSize(new THREE.Vector2());
  let b = size.x * size.y * 4 * 2;
  if (D && gl.data) {
    const nlib = D.libs.length;
    b += D.nDraw * 4;
    b += nlib * DENS_BINS * 8 + 4 * 12 + 6 * 2;
    b += 256 * 12;
    b += FTEX_W * D.rows * 4 * 2;
    b += DENS_BINS * 2 * nlib * 16;
  }
  for (const o of gl.owned) {
    const pos = o.geometry && o.geometry.attributes.position;
    if (pos) b += pos.array.byteLength;
    if (o.geometry && o.geometry.index) b += o.geometry.index.array.byteLength;
  }
  return b;
}

/* 拾取：先按角楔包围球粗筛，再逐点算与射线的距离；点的位置用和着色器同一套公式重算 */
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), tmpV = new THREE.Vector3();
function raySphere(O, Dd, c, r) {
  const ox = c.x - O.x, oy = c.y - O.y, oz = c.z - O.z;
  const tca = ox * Dd.x + oy * Dd.y + oz * Dd.z;
  if (tca < 0) return -1;
  return ox * ox + oy * oy + oz * oz - tca * tca <= r * r ? tca : -1;
}
function pick(isClick) {
  const U = gl.U;
  U.uHover.value = -1;
  if (!gl.mouse || !D || !gl.data) { if (hooks.hover) hooks.hover(null); return null; }
  const r = gl.canvas.getBoundingClientRect();
  ndc.set((gl.mouse.x - r.left) / r.width * 2 - 1, -((gl.mouse.y - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, gl.camera);
  const O = ray.ray.origin, Dd = ray.ray.direction, pts = D.pts;
  let best = -1, bestLib = null, bestD2 = 0.05;
  for (const lib of D.libs) {
    const bound = lib.R * Math.sin(TAU / WEDGE / 2) + lib.pickPad;
    const R = lib.R, Ux = lib.U.x, Uy = lib.U.y, Uz = lib.U.z, Vx = lib.V.x, Vy = lib.V.y, Vz = lib.V.z;
    const Nx = lib.nrm.x, Ny = lib.nrm.y, Nz = lib.nrm.z;
    for (const wg of lib.wedges) {
      const mid = (wg.w + 0.5) * TAU / WEDGE + lib.rot;
      tmpV.set(0, 0, 0).addScaledVector(lib.U, Math.cos(mid) * R).addScaledVector(lib.V, Math.sin(mid) * R);
      if (raySphere(O, Dd, tmpV, bound) < 0) continue;
      for (let v = wg.start; v < wg.end; v++) {
        const kind = (pts[v] >>> 22) & 3;
        if (!ui.mask[kind]) continue;
        const fi = pts[v] & 0x3FFFFF;
        const off = pointOffsets(v);
        const s = lib.anchor[fi - lib.fileBase] + off[0] * lib.mixS, rr = radAt(lib, s);
        const th = s / R + lib.rot, rad = R + off[1] * rr, hh = off[2] * rr;
        const cs = Math.cos(th) * rad, sn = Math.sin(th) * rad;
        const X = Ux * cs + Vx * sn + Nx * hh, Y = Uy * cs + Vy * sn + Ny * hh, Z = Uz * cs + Vz * sn + Nz * hh;
        const ox = X - O.x, oy = Y - O.y, oz = Z - O.z;
        const tc = ox * Dd.x + oy * Dd.y + oz * Dd.z;
        if (tc < 0) continue;
        const d2 = ox * ox + oy * oy + oz * oz - tc * tc;
        if (d2 < bestD2) { bestD2 = d2; best = v; bestLib = lib; }
      }
    }
  }
  if (best < 0) { if (hooks.hover && !isClick) hooks.hover(null); return null; }
  U.uHover.value = best;
  const kind = (pts[best] >>> 22) & 3, fi = pts[best] & 0x3FFFFF, i = fi - bestLib.fileBase;
  /* sub：第几个块 / 第几页（从 1 数）；块和页被抽样显示时只是近似编号 */
  const sub = best - bestLib.fileStart[i] - (kind === KIND_PAGE ? bestLib.nDrawChunks[i] : 0);
  const ref = { li: bestLib.src, i, kind: ['file', 'chunk', 'page'][kind], sub };
  if (!isClick && hooks.hover) hooks.hover(ref, gl.mouse.x, gl.mouse.y);
  return ref;
}

function onPointerMove(e) { gl.mouse = { x: e.clientX, y: e.clientY }; }
function onPointerLeave() { gl.mouse = null; gl.U.uHover.value = -1; if (hooks.hover) hooks.hover(null); }
function onPointerDown(e) { gl.down = { x: e.clientX, y: e.clientY, b: e.button }; markInput(); }
function onPointerUp(e) {
  const d = gl.down;
  gl.down = null;
  if (!d || d.b !== 0 || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5) return;
  gl.mouse = { x: e.clientX, y: e.clientY };
  const ref = pick(true);
  if (hooks.select) hooks.select(ref);
}

/* 库名：网页文字，每帧投影到屏幕上（不做成贴图，零显存） */
function renderLabels() {
  if (!labelsEl) return;
  labelsEl.textContent = '';
  if (!D) return;
  for (const lib of D.libs) {
    const el = document.createElement('div');
    el.className = 'sm-label';
    el.style.setProperty('--c', lib.color);
    el.textContent = lib.name;
    labelsEl.appendChild(el);
    lib.labelEl = el;
  }
}
function placeLabels() {
  if (!D || !labelsEl) return;
  labelsEl.style.display = ui.rings ? '' : 'none';
  if (!ui.rings) return;
  const w = host.clientWidth, h = host.clientHeight;
  for (const lib of D.libs) {
    if (!lib.labelEl) continue;
    tmpV.copy(lib.U).multiplyScalar(lib.R + 1.9).addScaledVector(lib.nrm, 0.7).project(gl.camera);
    if (tmpV.z > 1 || tmpV.z < -1) { lib.labelEl.style.display = 'none'; continue; }
    lib.labelEl.style.display = '';
    lib.labelEl.style.transform = 'translate(' + ((tmpV.x + 1) / 2 * w).toFixed(1) + 'px,' + ((1 - tmpV.y) / 2 * h).toFixed(1) + 'px) translate(-50%,-50%)';
  }
}

/* 镜头：取景、缩放、飞到某个文件 */
function defaultDist() { return Math.max(12, ((D && D.rMax) || 12) * 2.7 + 4); }
function zoomRatio() { return gl ? defaultDist() / gl.camera.position.distanceTo(gl.controls.target) : 1; }
function tweenTo(pos, target, ms) {
  if (!gl) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce || !ms) {
    gl.camera.position.copy(pos); gl.controls.target.copy(target); gl.controls.update();
    return;
  }
  gl.tween = { t0: performance.now(), ms, p0: gl.camera.position.clone(), q0: gl.controls.target.clone(), p1: pos.clone(), q1: target.clone() };
}
function stepTween(now) {
  const t = Math.min(1, (now - gl.tween.t0) / gl.tween.ms), e = 1 - Math.pow(1 - t, 3);
  gl.camera.position.lerpVectors(gl.tween.p0, gl.tween.p1, e);
  gl.controls.target.lerpVectors(gl.tween.q0, gl.tween.q1, e);
  if (t >= 1) gl.tween = null;
}
function fit(animate) {
  if (!gl) { camSaved = null; return; }
  const d = defaultDist();
  tweenTo(new THREE.Vector3(0, 0.49, 0.87).normalize().multiplyScalar(d), new THREE.Vector3(), animate ? 650 : 0);
}
function zoom(f) {
  if (!gl) return;
  const t = gl.controls.target, off = gl.camera.position.clone().sub(t);
  const len = clamp(off.length() * f, gl.controls.minDistance, gl.controls.maxDistance);
  tweenTo(t.clone().add(off.setLength(len)), t.clone(), 280);
  markInput();
}
function fileWorld(lib, i, out) {
  const a = lib.anchor[i] / lib.R + lib.rot;
  return out.set(0, 0, 0).addScaledVector(lib.U, Math.cos(a) * lib.R).addScaledVector(lib.V, Math.sin(a) * lib.R);
}

/* ============================================================
   6. 调节面板：滑条一律对数刻度、拖动时同步生效（不出现"正在生成"）
   ============================================================ */
const SL_STEPS = 1000;
const requestShape = () => { shapeDirty = true; markInput(); };
const SLIDERS = [
  { group: '外观与形状' },
  { key: 'size', label: '点大小', lo: 0.04, hi: 24, unit: '×', live: () => resize() },
  { key: 'speed', label: '公转速度', lo: 0.01, hi: 30, unit: '×', zero: true, live: () => {} },
  { key: 'spread', label: '光带粗细', lo: 0.05, hi: 18, unit: '×', live: requestShape },
  { key: 'bulge', label: '鼓起程度', lo: 0.01, hi: 20, unit: '×', zero: true, live: requestShape },
  { key: 'gather', label: '聚合强度', lo: 0.01, hi: 52, zero: true, live: requestShape },
  { key: 'mix', label: '颜色交融', lo: 0.02, hi: 50, live: requestShape },
  { key: 'body', label: '体积光', lo: 0.01, hi: 20, unit: '×', zero: true, live: () => { if (gl) gl.U.uBody.value = ui.body; } },
  { group: '轨道与密度' },
  { key: 'dens', label: '目标密度', lo: 50, hi: 50000, live: requestShape,
    tip: '管子里的点有多挤。调大 → 各条轨道往里收、更挤；调小 → 往外放、更松' },
  { key: 'densTol', label: '密度容许范围', lo: 1, hi: 20, unit: '×', live: requestShape,
    tip: '密度可以偏离目标多少倍。为了留间距被迫偏离更多时，把管子调细 / 调粗补回来；1 = 严格等于目标' },
  { key: 'orbitGap', label: '轨道最小间距', lo: 0.1, hi: 12, zero: true, live: requestShape,
    tip: '相邻两条轨道的管子外沿之间至少留多远' },
  { key: 'rMin', label: '最内轨道离中心', lo: 0.1, hi: 40, live: requestShape,
    tip: '最内圈的管子外沿离中心至少多远（中心线框球半径约 2.2）' },
  { group: '背景' },
  { key: 'bg', label: '背景亮度', lo: 0.01, hi: 10, unit: '×', zero: true, live: () => { if (gl) gl.U.uBg.value = ui.bg; } },
];
function sliderVal(sl, pos) {
  if (sl.zero) return pos <= 0 ? 0 : sl.lo * Math.pow(sl.hi / sl.lo, (pos - 1) / (SL_STEPS - 1));
  return sl.lo * Math.pow(sl.hi / sl.lo, pos / SL_STEPS);
}
function sliderPos(sl, v) {
  if (sl.zero && v <= 0) return 0;
  const t = Math.log(clamp(v, sl.lo, sl.hi) / sl.lo) / Math.log(sl.hi / sl.lo);
  return Math.round(sl.zero ? 1 + t * (SL_STEPS - 1) : t * SL_STEPS);
}
const fmtSlider = v => (v === 0 ? '0' : v < 0.1 ? v.toFixed(3) : v < 10 ? v.toFixed(2) : v < 1000 ? v.toFixed(1) : fmt(v));

let panelEl = null;
  /* ---------- 视图偏好持久化 ----------
   *
   * 为什么存 localStorage 而不是 SettingsStore（真机问题，2026-09-29）：
   * `DEFAULTS` 里的 12 个滑块 + 图层 mask + 轨道线 + 相机角度，之前**完全没有**持久化，
   * 每次打开星图都回到默认值——用户调好的视角得每次重来一遍。
   *
   * 为什么不走 `save_settings`：那条路只接受 `settings_schema.py` 里登记过的键
   * （contract_bridge.py：`if key not in FIELDS: continue` 静默跳过），而 §8.5 要求
   * 登记的键必须有**真实读取点**、禁止"假开关"。相机角度没有任何插件会读，登记它
   * 正好撞上这条禁令；而且它会在设置页冒出来，占一个跟系统设置无关的条目。
   * 这些是纯前端视图状态——不进配置、不跨进程、只在窗口里生效，localStorage 才是
   * 它该待的地方，也不用碰 37 个方法的桥接契约。
   *
   * 键名带 `v1`：以后想改默认值或加参数时，老记录能被识别出来丢掉，而不是把上一版
   * 的键名/数量当成新结构读进来。 */
  const PREFS_KEY = 'ragredo.starmap.ui.v1';
  let prefsTimer = null;

  function savePrefs() {
    /* 拖滑条是连续事件，每帧都写一次 localStorage 会卡手。攒 400ms 再落。 */
    if (prefsTimer) clearTimeout(prefsTimer);
    prefsTimer = setTimeout(() => {
      prefsTimer = null;
      try {
        const payload = { v: 1, mask: ui.mask.slice(), rings: !!ui.rings };
        for (const sl of SLIDERS) if (!sl.group) payload[sl.key] = ui[sl.key];
        if (gl && gl.camera && gl.controls) {
          payload.cam = {
            pos: gl.camera.position.toArray().map((x) => Math.round(x * 1e4) / 1e4),
            target: gl.controls.target.toArray().map((x) => Math.round(x * 1e4) / 1e4),
          };
        }
        localStorage.setItem(PREFS_KEY, JSON.stringify(payload));
      } catch (e) {
        /* 隐私模式/配额满/被禁用：视图偏好丢了不影响功能，不能因此抛错中断拖动。 */
      }
    }, 400);
  }

  function loadPrefs() {
    let raw = null;
    try { raw = localStorage.getItem(PREFS_KEY); } catch (e) { return; }
    if (!raw) return;
    let saved;
    try { saved = JSON.parse(raw); } catch (e) { return; }
    if (!saved || saved.v !== 1) return;
    /* 逐项夹回合法范围再采纳：宁可滑块停在端点，也不能让一条被手改坏的记录
     * 把半径/密度算成 0 或 NaN，那样整张图会直接空掉而且看不出原因。 */
    for (const sl of SLIDERS) {
      if (sl.group || !(sl.key in saved)) continue;
      const v = +saved[sl.key];
      if (!isFinite(v)) continue;
      ui[sl.key] = sl.zero ? (Math.abs(v) < 1e-9 ? DEFAULTS[sl.key] : v) : Math.min(sl.hi, Math.max(sl.lo, v));
    }
    if (Array.isArray(saved.mask) && saved.mask.length === 3) {
      ui.mask = saved.mask.map((x) => (x ? 1 : 0));
    }
    if (typeof saved.rings === 'boolean') ui.rings = saved.rings;
    if (gl) {
      if (gl.U.uKindMask.value) gl.U.uKindMask.value.set(...ui.mask);
      if (gl.data) gl.data.rings.visible = ui.rings;
      if (gl.U.uBody) gl.U.uBody.value = ui.body;
    }
    /* 相机另算：gl 此刻可能还没建（mount 早于 create），那时留给 create 之后回放。 */
    return (saved.cam && Array.isArray(saved.cam.pos) && saved.cam.pos.length === 3) ? saved.cam : null;
  }

  function restoreCamera(cam) {
    if (!cam || !gl || !gl.camera || !gl.controls) return;
    try {
      gl.camera.position.fromArray(cam.pos);
      gl.controls.target.fromArray(cam.target);
      gl.controls.update();
    } catch (e) { /* 同上：坏记录直接忽略，回落到默认机位 */ }
  }

  function buildPanel() {
  if (!panelEl) return;
  let h = '';
  for (const sl of SLIDERS) {
    if (sl.group) { h += '<div class="gp-cap">' + sl.group + '</div>'; continue; }
    h += '<div class="sm-sl"' + (sl.tip ? ' title="' + sl.tip + '"' : '') + '><div class="sm-sl-top"><span>' + sl.label
      + '</span><span class="mono" data-v="' + sl.key + '"></span></div>'
      + '<input type="range" min="0" max="' + SL_STEPS + '" step="1" data-s="' + sl.key + '" aria-label="' + sl.label + '"></div>';
    if (sl.key === 'rMin') h += '<div class="sm-orbits" data-orbits></div>';
  }
  h += '<div class="gp-cap">图层</div>'
    + ['文件（大点）', '块', '页（PDF 页库）'].map((t, k) => '<label class="sm-chk"><input type="checkbox" data-k="' + k + '"> ' + t + '</label>').join('')
    + '<label class="sm-chk"><input type="checkbox" data-rings> 轨道参考线与库名</label>'
    + '<div class="sm-reset"><button type="button" class="btn btn-sm btn-ghost" data-reset>恢复默认</button></div>';
  panelEl.innerHTML = h;
  for (const sl of SLIDERS) {
    if (sl.group) continue;
    const el = panelEl.querySelector('[data-s="' + sl.key + '"]'), lab = panelEl.querySelector('[data-v="' + sl.key + '"]');
    sl.el = el; sl.lab = lab;
      el.addEventListener('input', () => { ui[sl.key] = sliderVal(sl, +el.value); showSlider(sl); sl.live(); markInput(); savePrefs(); });
    }
    panelEl.querySelectorAll('[data-k]').forEach(cb => {
      cb.addEventListener('change', () => {
        ui.mask[+cb.getAttribute('data-k')] = cb.checked ? 1 : 0;
        if (gl) gl.U.uKindMask.value.set(...ui.mask);
        savePrefs();
      });
    });
    panelEl.querySelector('[data-rings]').addEventListener('change', e => {
      ui.rings = e.target.checked;
      if (gl && gl.data) gl.data.rings.visible = ui.rings;
      savePrefs();
    });
  panelEl.querySelector('[data-reset]').addEventListener('click', resetUi);
  syncPanel();
}
function showSlider(sl) { sl.lab.textContent = fmtSlider(ui[sl.key]) + (sl.unit || ''); }
function syncPanel() {
  if (!panelEl) return;
  for (const sl of SLIDERS) {
    if (sl.group || !sl.el) continue;
    sl.el.value = sliderPos(sl, ui[sl.key]);
    showSlider(sl);
  }
  panelEl.querySelectorAll('[data-k]').forEach(cb => { cb.checked = !!ui.mask[+cb.getAttribute('data-k')]; });
  panelEl.querySelector('[data-rings]').checked = ui.rings;
  renderOrbitTable();
}
    function resetUi() {
      Object.assign(ui, DEFAULTS, { mask: [1, 1, 1], rings: true });
      if (gl) {
        gl.U.uBody.value = ui.body; gl.U.uBg.value = ui.bg; gl.U.uKindMask.value.set(1, 1, 1);
        if (gl.data) gl.data.rings.visible = true;
        resize();
      }
      syncPanel();
      requestShape();
      /* "恢复默认"也得立刻写盘，否则关掉再打开又回到这一次的值，看起来像没生效。 */
      savePrefs();
    }
function renderOrbitTable() {
  const el = panelEl && panelEl.querySelector('[data-orbits]');
  if (!el) return;
  if (!D || !D.libs.length || !gl || !gl.data) { el.innerHTML = ''; return; }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  el.innerHTML = '<table><tr><td></td><td>半径</td><td>密度/目标</td><td>管子</td></tr>'
    + D.libs.map(l => '<tr><td><i style="background:' + l.color + '"></i>' + esc(l.name) + '</td><td>' + l.R.toFixed(1)
      + '</td><td>' + (l.dens / ui.dens).toFixed(2) + '×</td><td>'
      + (l.thin < 0.98 ? '细 ' + l.thin.toFixed(2) + '×' : (l.thin > 1.02 ? '粗 ' + l.thin.toFixed(2) + '×' : '—')) + '</td></tr>').join('')
    + '</table>';
}

/* ============================================================
   7. 对外接口（app.js 通过 window.RagStarMap 调用）
   ============================================================ */
document.addEventListener('visibilitychange', () => {
  clearTimeout(hiddenTimer);
  if (document.hidden) {
    /* 窗口最小化 / 被遮住：先停帧；藏够一会儿就把显卡交还（索引时模型要用显存） */
    if (gl) cancelAnimationFrame(gl.raf);
    hiddenTimer = setTimeout(() => { if (document.hidden) destroy(); }, RELEASE_HIDDEN_MS);
  } else if (visible) {
    if (gl) {
      cancelAnimationFrame(gl.raf);                       // 防止重复开两个绘制循环
      gl.last = gl.statsAt = performance.now(); gl.frames = 0;
      gl.raf = requestAnimationFrame(frame);
    } else create();
  }
});

const api = {
  palette: PALETTE.slice(),
  ungrouped: UNGROUPED,
  defaults: DEFAULTS,
  /* host：画布所在的舞台；panel：调节面板的内容区；hooks：hover/select/stats/error 回调 */
  mount(stage, panel, h) {
    host = stage; panelEl = panel || null; hooks = h || {};
    labelsEl = host.querySelector('.sm-labels');
    /* 先回放偏好再建面板：面板一建就 syncPanel()，那时候 ui 必须已经是最终值，
     * 否则滑块会先显示默认、随后又被改一次，看起来像"自己跳了一下"。 */
    const cam = loadPrefs();
    pendingCam = cam;
    buildPanel();
  },
  setData(payload) {
    const before = D ? D.libs.map(l => l.name).join('\n') : null;
    D = prepare(payload);
    selRef = null;
    /* 第一次有数据、或者看的库换了（范围变了、增删了库）：重新取景；只是内容更新就保持当前视角 */
    if (before !== D.libs.map(l => l.name).join('\n')) { fitPending = true; camSaved = null; }
    if (gl) { gl.U.uSel.value = -1; buildData(); } else renderLabels();
    return { libs: D.libs.length, points: D.nDraw, total: D.nPts, sampled: D.sampled, skipped: D.skipped, tooMany: !!D.tooMany };
  },
  show() {
    visible = true;
    clearTimeout(hiddenTimer);
    if (!gl && !document.hidden) create();
  },
  hide() {
    visible = false;
    destroy();
  },
  isLive() { return !!gl; },
  zoom(f) { zoom(f); },
  fit() { fit(true); },
  /* 选中一个文件（ref = {li: 数据里的库序号, i: 文件序号}；null 取消）：高亮、停转、镜头飞过去 */
  select(ref, fly) {
    selRef = null;
    let libIn = null;
    if (ref && D) {
      libIn = D.libs.find(l => l.src === ref.li) || null;
      if (libIn && ref.i >= 0 && ref.i < libIn.N) selRef = { li: libIn.li, i: ref.i };
    }
    if (!gl) return;
    gl.U.uSel.value = selRef ? libIn.fileBase + selRef.i : -1;
    if (selRef && fly && gl.data) {
      const p = fileWorld(libIn, selRef.i, new THREE.Vector3());
      const off = gl.camera.position.clone().sub(gl.controls.target);
      off.setLength(Math.min(off.length(), Math.max(6, libIn.R * 0.9)));
      tweenTo(p.clone().add(off), p, 700);
    }
  },
  resetUi,
  vramMB() { return vramBytes() / 1048576; },
};
window.RagStarMap = api;
window.dispatchEvent(new Event('starmapready'));
