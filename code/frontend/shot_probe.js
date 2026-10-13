/* 取景质检（F05b）阈值门禁
 *
 * 为什么单独做一个脚本，而不是塞进 test_a11y.js：
 * test_a11y.js 跑在 jsdom 里，而 jsdom 默认没有 canvas —— getContext('2d')
 * 返回 null，assessShot() 会走"环境没有 canvas，放行"那条分支。
 * 也就是说**质检的真身从来没被跑过**。这里绕开 DOM，直接从 index.html
 * 里抠出**真实的** shotStats / judgeShot 源码来跑合成图，测的就是线上那段代码。
 *
 * 用法：  node shot_probe.js
 * 退出码：全部符合预期 = 0；有偏差 = 1（可以直接挂进 CI / 门禁）
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const HTML = path.join(__dirname, 'index.html');
const OUT = path.join(ROOT, 'results', 'shot_quality_probe.json');

const html = fs.readFileSync(HTML, 'utf8');
const a = html.indexOf('const SHOT_W');
const b = html.indexOf('function assessShot');
if (a < 0 || b < 0 || b <= a) {
  console.error('!! 在 index.html 里找不到取景质检那段源码（常量块或函数被改名/挪走了？）');
  process.exit(1);
}
/* 拼成一个完整表达式在函数作用域里求值：const 声明不会外泄到模块作用域，
   所以必须一口气 eval，不能先 eval 常量再 eval 函数。 */
const core = html.slice(a, b);
const api = new Function(core + '\n return { shotStats, judgeShot, SHOT_MIN_MEAN, SHOT_MAX_MEAN, SHOT_MIN_RANGE, SHOT_MIN_LAP, SHOT_W };')();
const { shotStats, judgeShot } = api;

const W = 160, H = 120;

function mk(fn) {
  const d = new Uint8ClampedArray(W * H * 4);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const v = Math.max(0, Math.min(255, Math.round(fn(x, y))));
      const i = (y * W + x) * 4;
      d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
    }
  }
  return d;
}
function noise(x, y) {
  const s = Math.sin(x * 12.9898 + y * 78.233) * 43758.5453;
  return s - Math.floor(s);
}
function inBlob(x, y, cx, cy, r) {
  const dx = x - cx, dy = y - cy;
  return dx * dx + dy * dy < r * r;
}

/* 两侧都要覆盖：
   · "该拦的"拦不住 = 用户仍会收到基于糊图的荒谬结论；
   · "不该拦的"被拦 = 用户白白多按一次，还会怀疑软件坏了。
   阈值刻意压在保守一侧，因为后者的代价更容易被用户归因成"这软件有问题"。 */
const CASES = [
  ['正常户外（亮块+纹理+硬边）', 'ok', mk((x, y) => {
    let v = (x > 40 && x < 120 && y > 30 && y < 90) ? 200 : 110;
    if (inBlob(x, y, 30, 100, 12)) v = 40;
    if (inBlob(x, y, 130, 25, 10)) v = 250;
    return v + 30 * noise(x, y);
  })],
  ['正视力的文档 / 药盒（白底黑字）', 'ok', mk((x, y) => {
    const line = (Math.floor(y / 9) % 2 === 0) && (x % 13 < 9);
    return line ? 30 : 235;
  })],
  ['偏暗但可用的室内', 'ok', mk((x, y) => (x < 80 ? 55 : 70) + 22 * noise(x, y))],
  ['夜路（暗，但有路灯光斑）', 'ok', mk((x, y) => {
    const d = Math.hypot(x - 120, y - 40);
    return 18 + Math.max(0, 200 - d * 2.2) + 20 * noise(x, y);
  })],

  ['全黑（口袋误触 / 没开灯）', 'dark', mk(() => 4)],
  ['暗到看不见（mean≈20）', 'dark', mk((x, y) => 20 + 10 * noise(x, y))],
  ['过曝（对着灯 / 反光）', 'bright', mk(() => 252)],
  ['镜头被挡（均匀灰，无内容）', 'blank', mk(() => 128)],
  /* 暗镜头被挡会先命中"太暗" —— 这条提示对用户同样正确（他确实该换个方向），
     不为了"判得更准"去调换判据顺序。 */
  ['镜头被挡（暗且无内容）', 'dark', mk((x, y) => 8 + 6 * noise(x, y))],
  ['严重手抖（只剩低频渐变）', 'blur', mk((x, y) => 60 + 120 * (x / W))],
];

const rows = [];
let bad = 0;

for (const [name, want, px] of CASES) {
  const s = shotStats(px, W, H);
  const got = judgeShot(s).code;
  const hit = got === want;
  if (!hit) bad++;
  rows.push({ 用例: name, 期望: want, 实测: got, 符合: hit,
              mean: +s.mean.toFixed(1), range: s.range, lapvar: +s.lapvar.toFixed(1) });
}

/* 退化输入不能把功能挡死：空像素、单像素一律放行 */
const DEGENERATE = [
  ['空数组', new Uint8ClampedArray(0)],
  ['单像素', new Uint8ClampedArray([100, 100, 100, 255])],
  ['窄条 160×1', mk((x) => 120 + 40 * (x % 7))],
];
for (const [name, px] of DEGENERATE) {
  let got;
  try { got = judgeShot(shotStats(px, name === '窄条 160×1' ? 160 : 1, name === '窄条 160×1' ? 1 : 1)).code; }
  catch (e) { got = 'THROW:' + e.message; }
  const hit = got === 'ok';
  if (!hit) bad++;
  rows.push({ 用例: '退化输入 · ' + name, 期望: 'ok', 实测: got, 符合: hit });
}

console.log('阈值: mean∈[' + api.SHOT_MIN_MEAN + ',' + api.SHOT_MAX_MEAN + ']'
  + '  range≥' + api.SHOT_MIN_RANGE + '  lapvar≥' + api.SHOT_MIN_LAP);
console.log('');
console.log('判定   期望    用例                                     mean   range   lapvar');
for (const r of rows) {
  console.log(
    (r.实测 === 'ok' ? '放行' : '拦截').padEnd(6),
    r.期望.padEnd(6),
    (r.符合 ? '  ' : '✗ ') + r.用例.padEnd(34),
    (r.mean !== undefined ? r.mean.toFixed(1).padStart(6) : '     -'),
    (r.range !== undefined ? String(r.range).padStart(7) : '      -'),
    (r.lapvar !== undefined ? r.lapvar.toFixed(1).padStart(8) : '       -')
  );
}
console.log('');
console.log(bad === 0 ? '全部符合预期 ✅' : bad + ' 条不符预期 ❌');

try {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify({
    标准: '取景质检 F05b：太暗/过曝/无内容/糊 四类判据',
    阈值: { mean_min: api.SHOT_MIN_MEAN, mean_max: api.SHOT_MAX_MEAN,
            range_min: api.SHOT_MIN_RANGE, lapvar_min: api.SHOT_MIN_LAP, 缩略宽: api.SHOT_W },
    用例: rows, 不符预期数: bad,
    生成时间: new Date().toISOString(),
  }, null, 2), 'utf8');
  console.log('报告已写入 ' + OUT);
} catch (e) {
  console.error('写报告失败: ' + e.message);
}

process.exit(bad === 0 ? 0 : 1);
