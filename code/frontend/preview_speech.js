/* ================================================================
   播报文案审查工具

   作用：把"用户按「下一个」会依次听到什么"整句打印出来。

   为什么需要它：播报词是**听觉产品**，写代码时看到的只是字符串拼接，
   很难判断"连起来听是什么感觉"。而这个工具把结果摊平 ——
   能一眼看出某一句是不是太长、某个名字是不是让人听不懂、
   两句话之间是否重复。

   用法（在项目根目录）：
     NODE_PATH=<node workspace>/node_modules node code/frontend/preview_speech.js

   它给页面里所有可触摸元素铺上"不重叠的假坐标"（jsdom 没有布局引擎，
   getBoundingClientRect 一律返回 0，不铺的话所有元素都会被判成不可见），
   然后模拟连按「下一个」走满一圈。
   ================================================================ */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const HTML = path.join(__dirname, 'index.html');

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost/',
  beforeParse(win) {
    win.__spoken = [];
    win.SpeechSynthesisUtterance = function (t) { this.text = t; };
    win.speechSynthesis = {
      speaking: false,
      speak(u) {
        win.__spoken.push(u.text);
        if (u.onstart) u.onstart();
        if (u.onend) u.onend();
      },
      cancel() {},
      /* 给一份**故意把男声排在前面**的列表，模拟真实 Windows 环境，
         这样才能看出选声逻辑有没有真的生效。 */
      getVoices() {
        return [
          { lang: 'zh-CN', name: 'Microsoft Kangkang - Chinese (Simplified)', voiceURI: 'kangkang' },
          { lang: 'zh-CN', name: 'Microsoft Yunxi - Chinese (Simplified)', voiceURI: 'yunxi' },
          { lang: 'zh-CN', name: 'Microsoft Xiaoxiao - Chinese (Simplified)', voiceURI: 'xiaoxiao' },
        ];
      },
    };
    Object.defineProperty(win.navigator, 'vibrate', { configurable: true, value: () => true });
    win.fetch = () => Promise.resolve({
      ok: true,
      json: async () => ({ models: { mock_mode: false } }),
    });
  },
});

const win = dom.window;
const doc = win.document;

/* 铺假坐标之前先排掉"在真机上看不见"的元素。
   这一步是必须的，否则统计出来的是一个**假列表**：
   未开始的 SOP 控制区（#sopCard 是 inline display:none）会被算进去，
   于是工具报的项数比用户真机上能走到的多。
   工具报错了数字，就等于给了错的信息 —— 比不报更坏。
   （页面里原有的折叠"调试信息"区已随 UI 清理移除，但下面的 DETAILS
   分支保留：它防的是"以后又加一个折叠区"这件事，删掉只会重蹈覆辙。） */
function visible(el) {
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.style && n.style.display === 'none') return false;
    if (n.tagName === 'DETAILS' && !n.hasAttribute('open')) return false;
  }
  return true;
}

/* 铺假坐标：按文档顺序自上而下排，宽高给真实值量级。
   这样 navigableTargets() 的"有没有尺寸"过滤就能正常工作。 */
let y = 0;
doc.querySelectorAll('button, .chip, #q, .sec-title, #result, #sopStep, #demoBar').forEach((el) => {
  if (!visible(el)) return;
  const top = y;
  el.getBoundingClientRect = () => ({
    width: 320, height: 64, left: 0, top, right: 320, bottom: top + 64, x: 0, y: top,
  });
  y += 80;
});

const list = win.navigableTargets();
console.log('=== 顺序导航：' + list.length + ' 项 ===\n');
list.forEach((t, i) => {
  console.log(String(i + 1).padStart(2, ' ') + '. ' + t.label);
});

console.log('\n=== 连按「下一个」会依次听到（每句都要能独立听懂）===\n');
win.__spoken.length = 0;
for (let i = 0; i < list.length + 2; i++) {
  win.focusStep(1);
  const got = win.__spoken.filter((s) => s && /(共 \d+ 项|第 \d+ 项)/.test(s)).pop();
  const n = i === 0 ? '首' : String(i + 1);
  console.log(n.padStart(3, ' ') + ' → ' + (got || '（无播报）'));
}

const navOnly = win.__spoken.filter((s) => s && /(共 \d+ 项|第 \d+ 项)/.test(s));
const withTotal = navOnly.filter((s) => /共 \d+ 项/.test(s));
console.log('\n走完 ' + navOnly.length + ' 句，其中带总数的有 ' + withTotal.length + ' 句（应当是 1 句）');

/* 长度体检：中文 TTS 约 4.5 字/秒，超过 15 字就要 3 秒以上，
   而"走动中听"能接受的单句长度大概就是这个量级。 */
console.log('\n=== 长度体检（>15 字需要留意）===');
let long = 0;
list.forEach((t, i) => {
  const s = win.focusSpeech(t, i, list.length, null);
  if (s.length > 15) { long++; console.log('  ' + s.length + ' 字 ← ' + s); }
});
if (!long) console.log('  全部 ≤15 字');

dom.window.close();
