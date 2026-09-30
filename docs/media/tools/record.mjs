// 用无界面 Chrome 打开 archify 生成的 HTML，调用其自带的 Archify.motion.recordWebm 录出 WebM，
// 并可顺带截一张静态 PNG 供人工检查。
// 用法：node record.mjs <input.html> <out.webm> <dark|light> [durationMs=4800] [shot.png]
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const [, , input, out, theme = 'dark', durationArg = '4800', shot] = process.argv;
if (!input || !out) {
  console.error('usage: node record.mjs <input.html> <out.webm> <dark|light> [durationMs] [shot.png]');
  process.exit(2);
}
const CHROME = process.env.ARCHIFY_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const port = 9300 + Math.floor(Math.random() * 500);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'rec-chrome-'));
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  '--no-first-run', '--disable-gpu', '--hide-scrollbars', '--window-size=1500,2400', 'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* 等 Chrome 起来 */ }
    await sleep(250);
  }
  throw new Error('Chrome 未能启动');
}

let id = 0;
const pending = new Map();
const ws = new WebSocket(await wsUrl());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
};
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id;
  pending.set(mid, { resolve, reject });
  ws.send(JSON.stringify({ id: mid, method, params }));
});

try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 2400, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }] });
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `try { localStorage.setItem('archify-theme', '${theme}'); } catch (e) {}`,
  });
  // archify 的录制最高按 1:1 出图；这里临时改成 2 倍分辨率（只改临时副本，不改交付的 HTML），
  // 之后由 ffmpeg 缩到目标宽度，文字会更清晰。
  // 同时把“每步间隔 0.16s、单条 1.75s”放慢（环境变量 STAGGER/EDGE_SEC），让先后顺序看得清；步数上限 12 也放开到 40。
  const stagger = Number(process.env.STAGGER || 0.55);
  const edgeSec = Number(process.env.EDGE_SEC || 2.0);
  const html = fs.readFileSync(input, 'utf8')
    .replace('Math.min(1, 1280 / vb.width)', 'Math.min(2, 1800 / vb.width)')
    .replaceAll('Math.min(12, authoredStep(element, index)) * 0.16', `Math.min(40, authoredStep(element, index)) * ${stagger}`)
    .replace('var duration = 1.75;', `var duration = ${edgeSec};`);
  const tmpHtml = path.join(profile, 'page.html');
  fs.writeFileSync(tmpHtml, html);
  const url = 'file:///' + tmpHtml.replace(/\\/g, '/');
  await send('Page.navigate', { url });
  await sleep(2500);
  const ok = await send('Runtime.evaluate', {
    expression: `document.documentElement.getAttribute('data-theme') + '|' + (window.Archify && Archify.motion && Archify.motion.canRecord())`,
    returnByValue: true,
  });
  console.log('theme|canRecord =', ok.result.value);

  if (shot) {
    const clip = await send('Runtime.evaluate', {
      expression: `(() => { const r = document.querySelector('.diagram-container svg').getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,scale:1}; })()`,
      returnByValue: true,
    });
    const png = await send('Page.captureScreenshot', { format: 'png', clip: clip.result.value });
    fs.writeFileSync(shot, Buffer.from(png.data, 'base64'));
  }

  const rec = await send('Runtime.evaluate', {
    expression: `Archify.motion.recordWebm({ duration: ${Number(durationArg)}, fps: 20 }).then(b => new Promise(r => { const f = new FileReader(); f.onload = () => r(f.result.split(',')[1]); f.readAsDataURL(b); }))`,
    awaitPromise: true,
    returnByValue: true,
    timeout: 60000,
  });
  fs.writeFileSync(out, Buffer.from(rec.result.value, 'base64'));
  console.log('WebM 已写出', out, fs.statSync(out).size, 'bytes');
} finally {
  ws.close();
  chrome.kill();
  await sleep(500);
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* 忽略 */ }
}
process.exit(0);
