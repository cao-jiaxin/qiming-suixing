/* 录音转码（F08）字节级门禁
 *
 * 为什么单独做一个脚本，而不是塞进 test_a11y.js：
 * test_a11y.js 跑在 jsdom 里，而 jsdom 没有 AudioContext ——
 * recToWav() 会走"这里解不了，退回原始音频"那条分支。也就是说
 * **WAV 编码这段真身从来没被跑过**，而它恰恰是"按住说话识别不出来"的根因：
 * 上游只收 wav / mp3，实测 webm 直传就是 400 / code 1214。
 * 头里任何一个字段写错（声道数、位深、data 长度），上传上去同样是 400，
 * 而用户看到的只是一句"识别失败"。这里绕开 DOM，从 index.html 里抠出
 * **真实的** recToWav 源码，喂合成 PCM，逐字节核对它写出来的东西。
 *
 * 用法：  node wav_probe.js
 * 退出码：全部符合预期 = 0；有偏差 = 1（可以直接挂进 CI / 门禁）
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const HTML = path.join(__dirname, 'index.html');
const OUT = path.join(ROOT, 'results', 'wav_probe.json');

const html = fs.readFileSync(HTML, 'utf8');
const a = html.indexOf('async function recToWav');
/* 找**顶格**的那个 `}` 作为函数结尾。
   必须同时兼容 LF 与 CRLF：index.html 是 CRLF，只找 '\n}\n' 会返回 -1，
   而报错信息会指向"函数被改名了" —— 一个跟真实原因毫无关系的方向。
   （函数体内所有 `}` 都有缩进，所以第一个顶格 `}` 一定是它的结尾。） */
let end = html.indexOf('\r\n}\r\n', a), endLen = 3;
if (end < 0) { end = html.indexOf('\n}\n', a); endLen = 2; }
if (end < 0) { end = html.indexOf('\n}', a); endLen = 2; }
if (a < 0 || end < 0) {
  console.error('!! 在 index.html 里找不到 recToWav（被改名或挪走了？）');
  process.exit(1);
}
/* recToWav 里引用的是 window.AudioContext，所以求值前先把 window 摆好。
   每个用例都会换掉它 —— 这正是要测的：没有解码器时会不会安全退回。 */
global.window = {};
const recToWav = new Function(html.slice(a, end + endLen) + '\n return recToWav;')();

/* ---------------------------------------------------------------- 桩 */

function channel(n, fn) {
  const a2 = new Float32Array(n);
  for (let i = 0; i < n; i++) a2[i] = fn(i);
  return a2;
}

/* 假 AudioContext。真实浏览器里 decodeAudioData 把 webm/opus 或 mp4/aac
   解成 PCM，这里直接给 PCM —— 我们测的是**编码**，不是解码。 */
function fakeAC(cfg) {
  return function () {
    this.decodeAudioData = () => {
      if (cfg.reject) return Promise.reject(new Error('解码失败'));
      const n = cfg.length, chs = cfg.channels || 1;
      const data = [];
      for (let c = 0; c < chs; c++) data.push(cfg.channel(c, n));
      return Promise.resolve({
        numberOfChannels: chs,
        length: n,
        sampleRate: cfg.rate,
        getChannelData: (c) => data[c],
      });
    };
    this.close = () => {};
  };
}

/* ---------------------------------------------------------------- 解析 WAV */

function parseWav(buf) {
  const v = new DataView(buf);
  const tag = (off) => String.fromCharCode(
    v.getUint8(off), v.getUint8(off + 1), v.getUint8(off + 2), v.getUint8(off + 3));
  return {
    riff: tag(0), riffSize: v.getUint32(4, true),
    wave: tag(8), fmt: tag(12), fmtSize: v.getUint32(16, true),
    audioFormat: v.getUint16(20, true), channels: v.getUint16(22, true),
    sampleRate: v.getUint32(24, true), byteRate: v.getUint32(28, true),
    blockAlign: v.getUint16(32, true), bits: v.getUint16(34, true),
    dataTag: tag(36), dataSize: v.getUint32(40, true),
    total: buf.byteLength,
    sampleAt: (i) => v.getInt16(44 + i * 2, true),
  };
}

const blob = (bytes) => new Blob([new Uint8Array(bytes)]);

/* ---------------------------------------------------------------- 用例 */

const CASES = [];

/* ① 骨架：头字段一个都不能错。任何一个错了，上游就是 400。 */
CASES.push({
  名: '单声道 16k 直流 0.5',
  跑: async () => {
    global.window.AudioContext = fakeAC({
      length: 1000, rate: 16000, channel: () => channel(1000, () => 0.5),
    });
    const buf = await recToWav(blob(5000));
    if (!buf) return '返回了 null';
    const w = parseWav(buf);
    const bad = [];
    if (w.riff !== 'RIFF') bad.push('RIFF=' + w.riff);
    if (w.wave !== 'WAVE') bad.push('WAVE=' + w.wave);
    if (w.fmt !== 'fmt ') bad.push('fmt=' + w.fmt);
    if (w.dataTag !== 'data') bad.push('data=' + w.dataTag);
    if (w.audioFormat !== 1) bad.push('PCM=' + w.audioFormat);
    if (w.channels !== 1) bad.push('声道=' + w.channels);
    if (w.bits !== 16) bad.push('位深=' + w.bits);
    if (w.sampleRate !== 16000) bad.push('采样率=' + w.sampleRate);
    if (w.blockAlign !== 2) bad.push('块对齐=' + w.blockAlign);
    if (w.byteRate !== 32000) bad.push('字节率=' + w.byteRate);
    if (w.total !== 44 + 1000 * 2) bad.push('总长=' + w.total);
    if (w.dataSize !== 2000) bad.push('data长=' + w.dataSize);
    if (w.riffSize !== 36 + 2000) bad.push('riffSize=' + w.riffSize);
    return bad.length ? bad.join(' / ') : '';
  },
});

/* ② 幅值映射。写反符号或漏掉钳位，声音会削顶成噪声，识别率直接掉。 */
CASES.push({
  名: '满幅边界（-1 / +1）',
  跑: async () => {
    global.window.AudioContext = fakeAC({
      length: 4, rate: 16000,
      channel: () => Float32Array.from([-1, 1, 0, 2.5]),   // 最后一个测试越界钳位
    });
    const w = parseWav(await recToWav(blob(5000)));
    const got = [w.sampleAt(0), w.sampleAt(1), w.sampleAt(2), w.sampleAt(3)];
    const want = [-32768, 32767, 0, 32767];
    return got.join(',') === want.join(',') ? '' : `得到 ${got.join(',')}，应为 ${want.join(',')}`;
  },
});

/* ③ 多声道混音。手机通常是单声道，桌面端常常是双声道，而 WAV 头里
      我们声明的是 1 声道 —— 不混的话播放速度会翻倍，识别结果全错，
      而且从现象上完全看不出原因。L=+1 / R=-1 平均正好是 0，
      只取第一个声道的话会得到 +1，两种实现能区分开。 */
CASES.push({
  名: '双声道混单声道（L=1,R=-1 → 0）',
  跑: async () => {
    global.window.AudioContext = fakeAC({
      length: 2, rate: 44100, channels: 2,
      channel: (c, n) => channel(n, () => (c === 0 ? 1 : -1)),
    });
    const w = parseWav(await recToWav(blob(5000)));
    if (w.channels !== 1) return '声道数写成了 ' + w.channels;
    if (w.sampleAt(0) !== 0) return '混音结果 ' + w.sampleAt(0) + '，应为 0';
    return '';
  },
});

/* ④ 采样率要**原样透传**。我们刻意不做重采样（实测 8k/16k/48k 的同一段
      语音上传后识别结果完全一致），但头里必须写对 —— 写错会让上游
      按错误时长解读，切掉句首或句尾。 */
CASES.push({
  名: '48k 原样透传',
  跑: async () => {
    global.window.AudioContext = fakeAC({
      length: 4800, rate: 48000, channel: () => channel(4800, (i) => Math.sin(i / 10)),
    });
    const w = parseWav(await recToWav(blob(5000)));
    if (w.sampleRate !== 48000) return '采样率=' + w.sampleRate;
    if (w.byteRate !== 96000) return '字节率=' + w.byteRate;
    if (w.total !== 44 + 4800 * 2) return '总长=' + w.total;
    return '';
  },
});

/* ⑤⑥⑦ 三条"必须安全退回"的路径。转不了码不是致命错误 ——
      调用方会退回上传原始音频，由后端把"格式不支持"翻译成一句人话。
      在这里抛异常才是最糟的：录音已经结束，用户却什么反馈都收不到。 */
CASES.push({
  名: '空 Blob → null（不抛）',
  跑: async () => {
    global.window.AudioContext = fakeAC({ length: 10, rate: 16000, channel: () => channel(10, () => 0) });
    try { return (await recToWav(blob(0))) === null ? '' : '没有返回 null'; }
    catch (e) { return '抛了异常：' + e.message; }
  },
});
CASES.push({
  名: '没有 AudioContext → null（不抛）',
  跑: async () => {
    delete global.window.AudioContext;
    delete global.window.webkitAudioContext;
    try { return (await recToWav(blob(5000))) === null ? '' : '没有返回 null'; }
    catch (e) { return '抛了异常：' + e.message; }
  },
});
CASES.push({
  名: '解码失败 → null（不抛）',
  跑: async () => {
    global.window.AudioContext = fakeAC({ reject: true });
    try { return (await recToWav(blob(5000))) === null ? '' : '没有返回 null'; }
    catch (e) { return '抛了异常：' + e.message; }
  },
});
/* ⑧ 长度为 0 的 AudioBuffer：某些内核在极短录音上会给出空缓冲，
      那时如果继续算，写出来的会是一个"合法但空"的 wav —— 上传上去
      识别为空，用户得到"没有听清"，而真实原因是他根本没录到东西。 */
CASES.push({
  名: '空音频缓冲 → null',
  跑: async () => {
    global.window.AudioContext = fakeAC({ length: 0, rate: 16000, channel: () => new Float32Array(0) });
    try { return (await recToWav(blob(5000))) === null ? '' : '没有返回 null'; }
    catch (e) { return '抛了异常：' + e.message; }
  },
});

/* ---------------------------------------------------------------- 跑 */

(async () => {
  const rows = [];
  let bad = 0;
  console.log('用例                                 结果');
  for (const c of CASES) {
    let err = '';
    try { err = await c.跑(); }
    catch (e) { err = '异常：' + e.message; }
    const hit = !err;
    if (!hit) bad++;
    rows.push({ 用例: c.名, 符合: hit, 说明: err });
    console.log((hit ? '  ✅ ' : '  ❌ ') + c.名.padEnd(34) + (hit ? '' : err));
  }
  console.log('');
  console.log(bad === 0 ? '全部符合预期 ✅' : bad + ' 条不符预期 ❌');

  try {
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify({
      标准: '录音转码 F08：16-bit PCM WAV（上游只收 wav / mp3）',
      用例: rows, 不符预期数: bad,
      生成时间: new Date().toISOString(),
    }, null, 2), 'utf8');
    console.log('报告已写入 ' + OUT);
  } catch (e) {
    console.error('写报告失败: ' + e.message);
  }
  process.exit(bad === 0 ? 0 : 1);
})();
