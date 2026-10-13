/**
 * 前端无障碍交互测试（jsdom，无需真实浏览器）
 *
 * 为什么需要它：
 *   触摸探索是一套"按下报名字、抬起才执行、滑出即取消"的状态机，
 *   语法检查查不出这里的状态泄漏（比如滑出按钮后仍然执行了动作，
 *   对看不见屏幕的用户就是"我明明取消了它却动了"）。
 *
 * 运行：
 *   NODE_PATH=<node workspace>/node_modules node code/frontend/test_a11y.js
 *
 * 产出：results/ui_a11y_result.json
 */
'use strict';

const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.error('缺少 jsdom，请先执行：npm install jsdom');
  process.exit(2);
}

const HTML = path.resolve(__dirname, 'index.html');
const OUT = path.resolve(__dirname, '..', '..', 'results', 'ui_a11y_result.json');

const cases = [];
function check(name, passed, note) {
  cases.push({ case: name, pass: !!passed, note: note || '' });
  console.log(`  [${passed ? 'PASS' : 'FAIL'}] ${name}${note ? '   ← ' + note : ''}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- 造一个可控的假浏览器环境 ---------- */
function makeDom(opts) {
  opts = opts || {};
  const withTTS = opts.withTTS !== false;
  const secure = opts.secureContext !== false;      // 模拟 https / localhost
  const withMedia = opts.mediaDevices !== false;    // 模拟网页取景接口是否存在

  const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: opts.url || 'http://localhost/',
    beforeParse(win) {
      const spoken = [];
      const vibs = [];
      const fetches = [];
      win.__spoken = spoken;
      win.__vibrations = vibs;
      win.__fetches = fetches;

      /* 非安全上下文的本质不是"摄像头被拒绝"，而是 navigator.mediaDevices
         这个对象根本不存在。两种失败模式必须分开模拟，否则测不出真实行为。 */
      Object.defineProperty(win, 'isSecureContext', { configurable: true, value: secure });
      Object.defineProperty(win.navigator, 'mediaDevices', {
        configurable: true,
        value: withMedia
          ? { getUserMedia: opts.getUserMedia || (() => Promise.reject(new Error('测试环境不提供摄像头'))) }
          : undefined,
      });

      /* 录音链路（"按住说话" → ASR → 语音直达）需要 MediaRecorder。
         默认不注入，因为只有专门测语音通道的用例才需要它。
         stop() 里既派发 ondataavailable 又派发 onstop：真实的 MediaRecorder
         就是这个顺序，而本项目对音频做了**双重误触拦截**（时长 + 字节数），
         只补时长不补数据会被判成误触，测不到后面的识别路径。 */
      if (opts.mediaRecorder) {
        win.MediaRecorder = function (stream) {
          this.stream = stream;
          /* mimeType 是真实 MediaRecorder 的属性，前端拿它决定"转码失败时
             该给后端什么扩展名"。桩里必须也有，否则那半条分支测不到。 */
          this.mimeType = opts.recMime || 'audio/webm';
          this.start = () => {};
          this.stop = () => {
            if (this.ondataavailable) {
              this.ondataavailable({ data: new win.Blob([new Uint8Array(opts.audioBytes || 3000)]) });
            }
            if (this.onstop) this.onstop();
          };
        };
      }

      /* 取景质检的桩（2026-10-07 新增）。
         jsdom 默认没有 canvas —— getContext('2d') 返回 null，assessShot() 会走
         "环境没有 canvas，放行"那条分支，也就是说**质检的真身从来没被跑过**。
         这里补两件事，让它能被端到端驱动：
           · getContext 返回一个假 2D 上下文，getImageData 吐回指定的合成像素；
           · createImageBitmap 让它能"解码"系统相机拿回来的 File。
         阈值本身的正确性由 code/frontend/shot_probe.js 在真实源码上单独把关；
         这里管的是**接线**：拦不拦得住、拦住了说不说人话、算不出来时会不会误拦。 */
      if (opts.shot) {
        const px = makeShotPixels(opts.shot, 160, 120);
        win.HTMLCanvasElement.prototype.getContext = function () {
          return { drawImage() {}, getImageData: () => ({ data: new Uint8ClampedArray(px) }) };
        };
        win.createImageBitmap = () => Promise.resolve({ width: 640, height: 480, close() {} });
      }

      /* 结果自动可见（2026-10-07 新增）。
         jsdom 没实现 scrollIntoView（调用即抛），也没有 matchMedia。
         不补这两个桩，revealEl 会一路走 catch 分支，测到的只是"没崩" ——
         而"该不该滚""滚到哪""减弱动效时要不要平滑"这些真正要保证的事
         一件都测不到。 */
      const scrolls = [];
      win.__scrolls = scrolls;
      win.Element.prototype.scrollIntoView = function (opts) {
        scrolls.push({ el: this, opts: opts || null });
      };
      Object.defineProperty(win, 'matchMedia', {
        configurable: true,
        value: (q) => ({
          matches: !!opts.reduceMotion && /reduce/.test(q), media: q,
          addEventListener() {}, removeEventListener() {},
        }),
      });

      /* 录音转码（2026-10-07 新增）。
         真实浏览器里 recToWav 用 AudioContext.decodeAudioData 把 webm/mp4
         解成 PCM 再写成 WAV。jsdom 没有 AudioContext，不给桩它会走
         "解不出来 → 退回原始音频"那条路，于是**转码这段真身从来没被跑过**，
         而它恰恰是"按住说话识别不出来"的根因（上游只收 wav / mp3）。
         字节头的正确性由 wav_probe.js 在真实源码上单独把关；
         这里管的是接线：转出来的东西真的被当成 wav 上传了吗。 */
      if (opts.audioContext) {
        win.AudioContext = function () {
          this.decodeAudioData = () => Promise.resolve({
            numberOfChannels: opts.audioChannels || 1,
            length: opts.audioSamples || 16000,
            sampleRate: opts.audioRate || 16000,
            getChannelData: () => new Float32Array(opts.audioSamples || 16000),
          });
          this.close = () => {};
        };
      }

      /* 捕获定时器：录音"最长 25 秒"这条兜底必须有办法在测试里触发，
         否则要么真等 25 秒、要么永远测不到。
         只记录、不改变原有行为（定时器照常真的排上）。 */
      const timers = [];
      win.__timers = timers;
      const origSetTimeout = win.setTimeout.bind(win);
      win.setTimeout = function (fn, ms) {
        timers.push({ fn, ms });
        return origSetTimeout(fn, ms);
      };

      /* 拦截动态创建的 input.click()：jsdom 不会真的弹出文件选择框，
         这里只需记录它的属性，用来验证降级通道用的是 capture=environment。 */
      const inputs = [];
      win.__createdInputs = inputs;
      const origCreate = win.document.createElement.bind(win.document);
      win.document.createElement = function () {
        const el = origCreate.apply(null, arguments);
        if (String(arguments[0]).toLowerCase() === 'input') {
          el.click = function () {
            /* 连元素本身一起存下来：测试需要模拟"用户从系统相机返回"，
               即手动触发 onchange。否则 pickPhoto 的 Promise 要挂到
               180 秒兜底超时才结束，期间按钮的执行锁一直被占，
               "防重复提交"这类用例就没法干净地验证。 */
            inputs.push({ el, type: el.type, accept: el.accept, capture: el.getAttribute('capture') });
          };
        }
        return el;
      };

      if (withTTS) {
        /* __utterances 存的是**完整的 utterance 对象**（不只是文本）：
           音色这块要断言的是 rate / pitch / voice.name，只存文本就看不见它们。
           一条"改完参数但没接线"的改动，只断言文本是永远发现不了的。 */
        const utts = [];
        win.__utterances = utts;
        /* __voices 可在运行时替换，用来模拟"设备上装了哪些语音"。
           默认给一个能代表真实环境的列表：把男声放在**前面**——
           Windows 上就是这种情况，而"取第一个 zh 语音"恰好会取到男声。 */
        win.__voices = opts.voices || [
          { lang: 'zh-CN', name: 'Microsoft Kangkang - Chinese (Simplified)', voiceURI: 'kangkang' },
          { lang: 'zh-CN', name: 'Microsoft Yunxi - Chinese (Simplified)', voiceURI: 'yunxi' },
          { lang: 'zh-CN', name: 'Microsoft Huihui - Chinese (Simplified)', voiceURI: 'huihui' },
          { lang: 'zh-CN', name: 'Microsoft Xiaoxiao - Chinese (Simplified)', voiceURI: 'xiaoxiao' },
        ];
        win.SpeechSynthesisUtterance = function (text) { this.text = text; };
        /* __ttsSilent：模拟"接口存在但发不出声"。
           这是手机上最阴的一类故障（系统没装中文语音引擎、媒体音量为 0、
           内核把接口实现成空壳），speak() 不报错、就是不出声。
           做成运行时可切换的开关，才能测"误报会不会自愈"。 */
        win.__ttsSilent = !!opts.silentTTS;
        win.speechSynthesis = {
          speaking: false,
          pending: false,
          speak(u) {
            spoken.push(u.text);
            utts.push(u);
            if (win.__ttsSilent) return;          // 引擎没出声，也不给任何事件
            if (u.onstart) u.onstart();
            /* opts.holdTTS=true 时**故意不让它结束**，用来验证开麦时机 ——
               默认桩里 speak 立刻结束，"播报还在进行"这个状态根本走不到，测了等于没测。
               （2026-10-07 起契约是"到 VB_OPEN_MS 就开麦并主动掐断播报"，
               见 16b；仍然需要 holdTTS 才能复现"播报没念完"这个前提。） */
            if (!opts.holdTTS && u.onend) u.onend();
          },
          cancel() { spoken.push('«打断»'); },
          getVoices() { return win.__voices; },
        };
        /* 手动结束最后一句播报（配合 holdTTS 用）。 */
        win.__endSpeech = () => { const u = utts[utts.length - 1]; if (u && u.onend) u.onend(); };
      }

      Object.defineProperty(win.navigator, 'vibrate', {
        configurable: true,
        value: (p) => { vibs.push(p); return true; },
      });

      /* /api/meta 单独给一份可配置的返回：页面加载时会主动问一次后端的
         mock 状态，用来决定是否显示"演示模式"横幅。默认 mock_mode=false，
         由 opts.metaModels 覆盖。 */
      const metaModels = Object.assign({ mock_mode: false, mock_reason: '' },
        opts.metaModels || {});
      /* 记录 fetch 的第二个参数：部署版要把访问口令放进请求头，
         只记录 URL 是看不见它的（"改了但没接线"这类问题只有看 init 才发现）。 */
      const inits = [];
      win.__fetchInits = inits;
      win.fetch = (url, init) => {
        fetches.push(String(url));
        inits.push({ url: String(url), init: init || null });
        if (String(url).includes('/api/meta')) {
          return Promise.resolve({ json: async () => ({ models: metaModels }) });
        }
        /* 模拟后端拒绝（缺口令 401 / 太频繁 429）。
           用来验证"4xx 只念后端那句人话，不夹状态码"。 */
        if (opts.failPost) {
          return Promise.resolve({
            ok: false,
            status: opts.failPost.status || 401,
            json: async () => ({ detail: opts.failPost.detail || '' }),
          });
        }
        if (String(url).includes('/api/asr')) {          /* 允许运行时覆盖识别结果（win.__asrTextOverride）：
             一个 DOM 里要测多句不同的话，不能在 makeDom 时就把文案钉死。 */
          const t = win.__asrTextOverride != null ? win.__asrTextOverride : (opts.asrText || '');
          /* asr_available 要能单独关掉：后端没装 FunASR 时会回 false，
             前端必须据此改口播（"这里不能听写"）而不是说"没有听清"。 */
          return Promise.resolve({
            ok: true,
            json: async () => ({
              text: t, intent: { intent: '-' },
              asr_available: opts.asrAvailable !== false,
            }),
          });
        }
        /* SOP 三条链路的返回体必须成型：/api/sop/start 返回 session_id，
           否则前端 sid 是 undefined，"退回上一步"会被判成"没有进行中的流程"
           —— 那样测的是兜底文案，不是主路径。返回体字段直接照抄后端 main.py。 */
        if (String(url).includes('/api/sop/start')) {
          return Promise.resolve({
            ok: true,
            json: async () => (opts.sopStart || {
              session_id: 'S-TEST-1', flow_id: 'sop_medical', flow_title: '就医引导',
              total_steps: 3, index: 1,
              step: { id: 'st1', title: '进入门诊大厅' },
              speech: '开始就医引导。第一步：进入门诊大厅。请面向大厅正前方',
            }),
          });
        }
        if (String(url).includes('/api/sop/back')) {
          return Promise.resolve({
            ok: true,
            json: async () => (opts.sopBack || {
              finished: false, back: true, moved: true, index: 1, total_steps: 3,
              step: { id: 'st1', title: '进入门诊大厅' },
              speech: '已退回第 1 步，共 3 步：进入门诊大厅。请面向大厅正前方',
            }),
          });
        }
        if (String(url).includes('/api/sop/step')) {
          return Promise.resolve({
            ok: true,
            json: async () => (opts.sopStep || {
              finished: false, reached: true, index: 2, total_steps: 3,
              step: { id: 'st2', title: '挂号取号' },
              speech: '上一步完成。接下来：挂号取号。请举起手机对准前方',
            }),
          });
        }
        /* 场景理解的完整返回体（要带 distances 才能测距离表的无障碍入口）。
           字段照抄后端 pipeline.understand_scene 的返回。 */
        if (opts.sceneReply && String(url).includes('/api/understand')) {
          return Promise.resolve({ ok: true, json: async () => opts.sceneReply });
        }
        return Promise.resolve({
          json: async () => ({ speech: '测试播报', reply: '测试结果', model: 'test-model' }),
        });
      };
    },
  });
  return dom;
}

/* 合成"照片"像素，用来驱动取景质检。
   构造方式与 code/frontend/shot_probe.js 保持一致 ——
   那边在真实源码上验证阈值，这边验证接线，两边看的是同一套定义。 */
function makeShotPixels(kind, W, H) {
  const d = new Uint8ClampedArray(W * H * 4);
  const noise = (x, y) => {
    const s = Math.sin(x * 12.9898 + y * 78.233) * 43758.5453;
    return s - Math.floor(s);
  };
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      let v;
      if (kind === 'dark') v = 4;
      else if (kind === 'bright') v = 252;
      else if (kind === 'blank') v = 128;
      else if (kind === 'blur') v = 60 + 120 * (x / W);
      else v = ((x > 40 && x < 120 && y > 30 && y < 90) ? 200 : 110) + 30 * noise(x, y);
      v = Math.max(0, Math.min(255, Math.round(v)));
      const i = (y * W + x) * 4;
      d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
    }
  }
  return d;
}

/* ---------- 触摸事件工具 ---------- */
function touch(win, el, type, x, y) {
  const ev = new win.Event(type, { bubbles: true, cancelable: true });
  ev.touches = (x === null) ? [] : [{ clientX: x, clientY: y }];
  el.dispatchEvent(ev);
  return ev;
}
/* jsdom 不实现布局，getBoundingClientRect() 一律返回全 0。
   触摸探索依赖真实矩形，必须手工 stub；否则"滑出按钮取消"这类用例
   永远会通过（因为任何坐标都"不在"按钮内），等于没测。
   可传 left/top 把两个按钮摆到不重叠的位置——真实页面里按钮是垂直排列的，
   测试若让它们重叠，就测不出"滑到相邻按钮"这个真实场景。 */
function fakeRect(el, w, h, left, top) {
  left = left || 0; top = top || 0;
  el.getBoundingClientRect = () => ({
    left: left, right: left + w, top: top, bottom: top + h,
    width: w, height: h, x: left, y: top,
  });
}

const INSIDE = [50, 25];
const OUTSIDE = [500, 25];

/* 模拟"用户打开了系统相机但没选照片就返回"：驱动最后一次 file input 的 onchange。
   files 在 jsdom 里是空的，走的就是真实的"取消"分支。 */
async function finishPendingPhoto(win, file) {
  const ins = win.__createdInputs || [];
  const last = ins[ins.length - 1];
  if (!last || typeof last.el.onchange !== 'function') return false;
  if (file) {
    // input.files 在 jsdom 里是只读的，只能用 defineProperty 覆盖
    Object.defineProperty(last.el, 'files', { value: [file], configurable: true });
  }
  last.el.onchange();
  await sleep(80);
  return true;
}

/* 断言失败信息里不能直接 JSON.stringify(input 记录)：里面存着 DOM 元素，
   序列化会因循环引用抛错，把一条失败的断言变成一次测试崩溃。 */
const brief = (ins) => JSON.stringify(ins.map((i) => ({ type: i.type, accept: i.accept, capture: i.capture })));

/* 在**已有的** window 里走一次"点拍照 → 从系统相机返回一张图"。
   为什么必须复用同一个 window：结果后面那句补充提示是按次数轮流的，
   换一个 window 等于把计数清零，第二句（距离表那条）就永远测不到。 */
async function shootIn(win, doc) {
  const b = doc.querySelector('#btnScene');
  fakeRect(b, 100, 50);
  touch(win, b, 'touchstart', ...INSIDE);
  touch(win, b, 'touchend', ...INSIDE);
  await sleep(120);
  await finishPendingPhoto(win, new win.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  await sleep(400);
}

async function main() {
  console.log('\n===== 前端无障碍交互测试 =====\n');

  const dom = makeDom({ withTTS: true });
  const win = dom.window;
  const doc = win.document;
  await sleep(150);

  const gate = doc.querySelector('#gate');
  const btnScene = doc.querySelector('#btnScene');
  const btnManual = doc.querySelector('#btnManual');
  const spoken = win.__spoken;
  const vibs = win.__vibrations;

  // ---------- 1. 启动闸门 ----------
  check('闸门存在且初始可见',
    !!gate && !gate.classList.contains('gone') && gate.style.display !== 'none',
    gate ? '整屏可点，无关闭键' : '未找到 #gate');

  check('闸门有可访问名称（读屏可读）',
    !!gate && (gate.getAttribute('aria-label') || '').includes('轻触'),
    gate ? gate.getAttribute('aria-label') : '');

  // 关键断言：闸门打开前不能有任何播报（浏览器本来也会拒绝）
  check('闸门打开前不发声', spoken.length === 0, `已播报 ${spoken.length} 条`);

  spoken.length = 0;
  touch(win, gate, 'touchend', ...INSIDE);
  await sleep(30);

  check('触摸闸门后闸门关闭', gate.classList.contains('gone'));
  check('触摸闸门后播报就绪语（音频解锁同步发生）',
    spoken.some((s) => s.includes('已就绪')), spoken.filter(Boolean).join(' / '));
  check('触摸闸门后触发就绪震动', vibs.some((v) => Array.isArray(v)), JSON.stringify(vibs));

  await sleep(1300);   // 等首次引导（延迟 1.1s）
  check('首次进入播报触摸探索引导语',
    spoken.some((s) => s.includes('手指划过按钮')),
    spoken.filter((s) => s && s !== '«打断»').slice(-1)[0] || '');

  // ---------- 2. 触摸探索状态机 ----------
  // 两个按钮摆成上下不重叠，模拟真实页面的垂直排列（见 fakeRect 的说明）
  fakeRect(btnScene, 100, 50, 0, 0);        // (0,0)-(100,50)
  fakeRect(btnManual, 100, 50, 0, 200);     // (0,200)-(100,250)

  // 2a 按下即报名字
  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  check('手指按下按钮即播报按钮名（无需抬手）',
    spoken.includes('拍照看看周围'), spoken.filter(Boolean).join(' / '));
  check('手指按下按钮即高亮（低视力用户可见）',
    btnScene.classList.contains('focusring'));

  // 2a-2 手指从 A 滑到 B：必须改报 B 的名字
  //   touchmove 的事件目标是 touchstart 触发的那个元素，所以这一条**只能**
  //   靠 document 级分派实现。早期版本把 touchmove 挂在各元素自己身上，
  //   结果是"手指已经滑到 B 上，耳朵里听到的还是 A 的名字，抬手执行的却是 B"
  //   —— 两个通道给出矛盾信息，对看不见屏幕的用户是最糟糕的一种失败。
  spoken.length = 0;
  touch(win, btnScene, 'touchmove', 50, 225);
  check('手指滑到相邻按钮时改报新按钮名（document 级分派）',
    spoken.includes('已到达，直接下一步'), spoken.filter(Boolean).join(' / ') || '（无播报）');
  check('滑入新按钮后：新按钮高亮、旧按钮撤销高亮',
    btnManual.classList.contains('focusring') && !btnScene.classList.contains('focusring'));

  // 2b 滑出按钮 = 取消，抬手不得执行
  spoken.length = 0;
  touch(win, btnScene, 'touchmove', ...OUTSIDE);
  check('手指滑出按钮后取消高亮', !btnScene.classList.contains('focusring'));
  touch(win, btnScene, 'touchend', ...OUTSIDE);
  await sleep(60);
  check('手指滑出后再抬手：不执行动作（反悔窗口生效）',
    !spoken.some((s) => s.includes('请稍等')),
    spoken.filter(Boolean).join(' / ') || '（无播报）');

  // 2c 按在按钮内抬手 = 执行；同时验证"打断后让出一拍"
  //   动作开头会先 stopSpeak()，而"刚取消过"之后必须让出 80ms 再出声，
  //   否则 Chrome 会静默丢掉新的 utterance（不抛错、就是没声音）。
  //   断言方式是看**同步阶段没有出声**、等过 80ms 才有：
  //   若把 speak() 改回同步调用，这条会立刻失败。
  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  touch(win, btnScene, 'touchend', ...INSIDE);
  // 只看动作本身的播报。touchstart 的"报按钮名"本来就是同步的（探索反馈
  // 必须零延迟），不能把它算进来。
  const syncCount = spoken.filter((s) => s.includes('请稍等')).length;
  await sleep(160);
  const laterCount = spoken.filter((s) => s.includes('请稍等')).length;
  check('按在按钮内抬手：正常执行动作',
    laterCount > 0, spoken.filter((s) => s && s !== '«打断»').join(' / '));
  check('打断后不是同步出声，而是让出一拍（规避 Chrome 丢 utterance）',
    syncCount === 0 && laterCount > 0, `同步 ${syncCount} 条 → 160ms 后 ${laterCount} 条`);

  // 把上一次拍照收尾（模拟用户从系统相机返回但没有选照片）。
  //   不做这一步，btnScene 的执行锁会一直占着，下面 2d 的断言就会
  //   因为"被锁拦住"而通过——看起来是绿的，实际根本没测到 click 抑制。
  await finishPendingPhoto(win);

  // 2d 触摸后浏览器补发的 click 不得重复执行
  const before = spoken.filter((s) => s.includes('请稍等')).length;
  btnScene.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(60);
  const after = spoken.filter((s) => s.includes('请稍等')).length;
  check('触摸后补发的 click 被忽略（不会执行两次）', after === before,
    `执行次数 ${before} → ${after}`);

  // 2e 触屏笔记本：触摸过 A 按钮后，鼠标点 B 按钮仍须生效
  //    （这条曾真实失败：时间戳做成了全局变量，导致一次触摸会把
  //      接下来 900ms 内所有按钮的 click 全部吞掉）
  spoken.length = 0;
  btnManual.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(160);
  check('触摸过其他按钮后，鼠标点本按钮仍生效（时间戳按元素记录）',
    spoken.some((s) => s.includes('请先选择')), spoken.filter(Boolean).join(' / ') || '（无播报）');

  // 2f 键盘可操作
  spoken.length = 0;
  const kev = new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  btnManual.dispatchEvent(kev);
  await sleep(160);
  check('键盘 Enter 可操作（志愿者/评委兜底）',
    spoken.some((s) => s.includes('请先选择')), spoken.filter(Boolean).join(' / ') || '（无播报）');

  // ---------- 2g 翻页手势不得被当成按按钮 ----------
  //   真实场景：用户想往下看看还有什么功能，手指恰好按在按钮上起手往上滑。
  //   按钮高 64px，手指很容易一直留在按钮范围内 —— 抬手时 armed 仍为 true，
  //   于是"我想翻页"被执行成了"我要拍照"。判据用 scroll 事件而非位移阈值，
  //   因为读屏式探索滑动的位移同样很大，但页面不会滚动。
  console.log('\n--- 翻页 / 滚动意图 ---');

  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  doc.dispatchEvent(new win.Event('scroll'));      // scroll 不冒泡，直接派发到 document
  touch(win, btnScene, 'touchend', ...INSIDE);      // 手指始终没离开按钮
  await sleep(200);
  check('页面滚动过：抬手不执行按钮动作（翻页 ≠ 按按钮）',
    !spoken.some((s) => s.includes('请稍等')),
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');

  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);    // 新的一次触摸，复位滚动状态
  spoken.length = 0;                                // 按下时报的那一次名字是预期行为，不计入
  doc.dispatchEvent(new win.Event('scroll'));
  touch(win, btnScene, 'touchmove', 50, 225);       // 划到相邻的 btnManual
  check('翻页期间划过按钮不报名字（否则会刷出一串无意义点名，顶掉正在听的播报）',
    !spoken.some((s) => s.includes('已到达，直接下一步')),
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');
  touch(win, btnScene, 'touchend', 50, 225);

  // ---------- 2h 执行中的重复触摸：提示而不是并发再发一次 ----------
  console.log('\n--- 防重复提交 ---');
  const btnSceneCallsBefore = win.__fetches.filter((u) => u.includes('/api/understand')).length;
  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  touch(win, btnScene, 'touchend', ...INSIDE);
  await sleep(120);                                  // 动作已开始，卡在取图环节
  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  touch(win, btnScene, 'touchend', ...INSIDE);
  await sleep(120);
  check('执行中再次触摸 → 提示"正在处理"，不再发起第二次请求',
    spoken.some((s) => s.includes('正在处理'))
      && win.__fetches.filter((u) => u.includes('/api/understand')).length === btnSceneCallsBefore,
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');

  await finishPendingPhoto(win);
  spoken.length = 0;
  touch(win, btnScene, 'touchstart', ...INSIDE);
  touch(win, btnScene, 'touchend', ...INSIDE);
  await sleep(200);
  check('动作结束后锁被释放，可以再次执行（不会一次卡死永久失效）',
    spoken.some((s) => s.includes('请稍等')) || win.__fetches.some((u) => u.includes('/api/understand')),
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');
  await finishPendingPhoto(win);

  // ---------- 2i 高危动作的二次确认 ----------
  //   "呼叫人工志愿者"是全站唯一会对外产生真实后果的动作；
  //   "退出流程"会清空 SOP 进度。两者都紧邻其他按钮，误触代价极高。
  console.log('\n--- 高危动作二次确认 ---');
  const btnHelp = doc.querySelector('#btnHelp');
  fakeRect(btnHelp, 100, 50, 0, 400);
  /* 坐标必须落在 fakeRect 里：document 级的触摸分派是按坐标判定的，
     派发到元素但坐标落在别处，会被分派逻辑当成"手指已滑出"而撤销就绪态。 */
  const HELP_AT = [50, 420];
  const helpCalls = () => win.__fetches.filter((u) => u.includes('/api/help')).length;

  spoken.length = 0;
  touch(win, btnHelp, 'touchstart', ...HELP_AT);
  touch(win, btnHelp, 'touchend', ...HELP_AT);
  await sleep(200);
  check('高危动作第一次触摸不执行请求，只提示"再点一次确认"',
    helpCalls() === 0 && spoken.some((s) => s.includes('再点一次确认')),
    `已发请求 ${helpCalls()} 次 ｜ ${spoken.filter((s) => s && s !== '«打断»').join(' / ')}`);
  check('待确认状态有可见标记（低视力用户看得出按钮在等确认）',
    btnHelp.classList.contains('ask'));

  spoken.length = 0;
  touch(win, btnHelp, 'touchstart', ...HELP_AT);
  touch(win, btnHelp, 'touchend', ...HELP_AT);
  await sleep(200);
  check('第二次触摸才真正发出求助请求',
    helpCalls() === 1, `已发请求 ${helpCalls()} 次`);
  check('确认后"待确认"标记被清除（不留悬挂状态）', !btnHelp.classList.contains('ask'));

  // 2i-2 5 秒内不确认自动撤销
  spoken.length = 0;
  touch(win, btnHelp, 'touchstart', ...HELP_AT);
  touch(win, btnHelp, 'touchend', ...HELP_AT);
  await sleep(5400);
  check('确认提示 5 秒内无第二次触摸 → 自动撤销并播报已取消（不留上膛状态）',
    !btnHelp.classList.contains('ask') && spoken.some((s) => s.includes('已取消')),
    spoken.filter((s) => s.includes('已取消')).length + ' 次取消播报');

  // ---------- 2j 文字类型 chip 与按钮行为一致 ----------
  console.log('\n--- 文字类型选择 ---');
  const chips = Array.from(doc.querySelectorAll('#docChips .chip'));
  chips.forEach((c, i) => fakeRect(c, 80, 48, i * 90, 600));
  const chipDrug = chips.find((c) => c.dataset.t === 'drug');
  const chipDoc = chips.find((c) => c.dataset.t === 'doc');

  // chipDrug 的 fakeRect 是 (90,600)-(170,648)，触摸坐标必须落在里面：
  //   否则 document 级分派会判成"手指在别人身上"，把它的就绪态撤销掉。
  spoken.length = 0;
  touch(win, chipDrug, 'touchstart', 100, 620);
  check('触碰类型 chip 报出类型名', spoken.includes('文字类型，药品'),
    spoken.filter(Boolean).join(' / '));

  spoken.length = 0;
  touch(win, chipDrug, 'touchmove', 370, 620);       // 划到 chipDoc（360–440）
  check('手指从"药品"滑到"单据票据"时改报新名字（与按钮同一套分派）',
    spoken.includes('文字类型，单据票据'), spoken.filter(Boolean).join(' / ') || '（无播报）');

  spoken.length = 0;
  touch(win, chipDoc, 'touchend', 370, 620);
  await sleep(200);
  check('抬手才选中，且选中项正确切换',
    chipDoc.classList.contains('on') && !chipDrug.classList.contains('on'));
  check('选中后有"已选择××"语音回执（全盲用户否则无法确认是否生效）',
    spoken.some((s) => s.includes('已选择' + chipDoc.textContent.trim())),
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');
  check('选中项写入 aria-pressed（读屏软件可播报选中状态）',
    chipDoc.getAttribute('aria-pressed') === 'true'
      && chipDrug.getAttribute('aria-pressed') === 'false');
  check('chip 可被键盘聚焦（原实现只有 div 的键盘不可达）',
    chipDrug.hasAttribute('tabindex'));

  // ---------- 2k 结果区重听 ----------
  console.log('\n--- 结果区重听 ---');
  spoken.length = 0;
  touch(win, doc.querySelector('#result'), 'touchend', 50, 10);
  await sleep(200);
  check('结果区轻触可重听上一句（没听清不必重走整个流程）',
    spoken.filter((s) => s && s !== '«打断»').length > 0,
    spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');

  // ---------- 2l 模型文本必须转义后才进结果区 ----------
  //   结果区是 innerHTML 渲染的，而模型输出是自由文本。不转义的话
  //   "距离<5 米"里的 "<" 会被当成标签起始，把后面的内容整段吞掉 ——
  //   用户看到的是半句话，而且看不到任何报错。
  console.log('\n--- 结果区文本转义 ---');
  const btnRead = doc.querySelector('#btnRead');
  fakeRect(btnRead, 100, 50, 0, 800);
  win.fetch = () => Promise.resolve({
    ok: true,
    json: async () => ({ reply: '前方有<障碍物> 与 & 符号', speech: '前方有障碍' }),
  });
  touch(win, btnRead, 'touchstart', 50, 820);
  touch(win, btnRead, 'touchend', 50, 820);
  await sleep(150);
  await finishPendingPhoto(win, new win.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  await sleep(150);
  const rh = doc.querySelector('#result').innerHTML;
  check('模型返回的 < > & 被转义（不会吞掉内容、也不会注入标签）',
    rh.includes('&lt;障碍物&gt;') && rh.includes('&amp;') && !rh.includes('<障碍物>'),
    rh.slice(0, 70));

  // ---------- 3. 触觉反馈约定 ----------
  const hasTouch12 = vibs.some((v) => v === 12);
  const hasTap22 = vibs.some((v) => v === 22);
  check('震动分级：触碰 12ms / 确认 22ms / 就绪数组', hasTouch12 && hasTap22,
    JSON.stringify(vibs));

  check('结果到达用 30ms 单震，与"开始执行"的 22ms 区分开',
    vibs.some((v) => v === 30), JSON.stringify(vibs));

  check('待确认/已撤销用双短震 [10,40,10]，与就绪 [18,60,18] 不会混淆',
    vibs.some((v) => Array.isArray(v) && v[0] === 10 && v[1] === 40 && v[2] === 10),
    JSON.stringify(vibs));

  /* 对全盲用户震动是唯一的即时通道，出现"说不出意思的震动"等于制造噪音。
     这条断言把语义表钉死：只允许表里登记过的几种，禁止再出现裸数字
     （原代码散落着未登记的 20 与 30，就是靠这条防回退）。 */
  const ALLOWED_SINGLE = [12, 22, 30];
  const ALLOWED_PATTERNS = ['[18,60,18]', '[25,40,25]', '[12,120,12]', '[10,40,10]', '[40,80,40]'];
  const stray = vibs.filter((v) => (Array.isArray(v)
    ? ALLOWED_PATTERNS.indexOf(JSON.stringify(v)) < 0
    : ALLOWED_SINGLE.indexOf(v) < 0));
  check('震动只使用语义表里登记过的模式（无未登记的裸数字）',
    stray.length === 0, stray.length ? '未登记：' + JSON.stringify(stray) : '');

  dom.window.close();

  // ---------- 4. 无 TTS 环境（微信内置浏览器就是这种） ----------
  console.log('\n--- 降级路径：不支持语音合成的浏览器 ---');
  const dom2 = makeDom({ withTTS: false });
  const win2 = dom2.window;
  await sleep(150);
  const gate2 = win2.document.querySelector('#gate');
  touch(win2, gate2, 'touchend', ...INSIDE);
  await sleep(1300);
  check('无 TTS 时不崩溃，且不显示语音播报内容',
    !win2.document.querySelector('#result').innerHTML.includes('测试播报'));
  check('无 TTS 时提示改用系统读屏（降级文案已给出）',
    win2.document.querySelector('#result').innerHTML.includes('不支持语音播报'));
  dom2.window.close();

  /* ---------- 5. 非安全上下文降级（手机用局域网 IP 访问就是这种场景） ----------
     这是本项目最容易现场翻车的一条：http://192.168.x.x 下 navigator.mediaDevices
     根本不存在，页面会静默失去拍照能力 —— 用户按了按钮没有任何反应，
     而视障用户既看不见报错也听不到提示。降级必须覆盖到。 */
  console.log('\n--- 降级路径：非安全上下文（http://192.168.x.x）---');
  const dom3 = makeDom({ secureContext: false, mediaDevices: false });
  const win3 = dom3.window;
  await sleep(150);
  const gate3 = win3.document.querySelector('#gate');
  touch(win3, gate3, 'touchend', ...INSIDE);
  await sleep(1300);

  check('非安全上下文下不崩溃，结果区给出书面说明',
    win3.document.querySelector('#result').innerHTML.includes('系统相机'),
    win3.document.querySelector('#result').textContent.slice(0, 40));

  const btn3 = win3.document.querySelector('#btnScene');
  fakeRect(btn3, 100, 50);
  touch(win3, btn3, 'touchstart', ...INSIDE);
  touch(win3, btn3, 'touchend', ...INSIDE);
  await sleep(80);

  const ins = win3.__createdInputs || [];
  check('降级后改用 file input 取图（而不是静默失效）', ins.length > 0,
    `创建了 ${ins.length} 个 input`);
  check('降级后使用 capture=environment 直接调后置相机',
    ins.some((i) => i.capture === 'environment'), brief(ins));
  check('降级后仍限定为图片类型',
    ins.some((i) => i.accept === 'image/*'), brief(ins));

  // 模拟用户从系统相机返回但没拍照：必须给出"没有取到照片"，
  // 而不是永远停在"正在拍照识别"（视障用户既看不见也听不到原因）。
  await finishPendingPhoto(win3);
  check('用户取消拍照后不会卡在"正在拍照识别"（Promise 不得永久挂起）',
    win3.document.querySelector('#result').textContent.includes('没有取到照片'),
    win3.document.querySelector('#result').textContent.slice(0, 40));
  dom3.window.close();

  // ---------- 8. 演示模式必须"说破"（防静默降级） ----------
  // 背景：后端没有视觉模型 Key 时，所有"识别"都返回内置示例，接口 200、不报错。
  // 于是用户点"拍照读文字"会收到一段"这是室内走廊"的场景示例，
  // 却完全没有线索知道问题出在"没配 Key"。所以 mock 状态必须显式暴露。
  const dom4 = makeDom({
    withTTS: true,
    metaModels: { mock_mode: true, mock_reason: '未配置 ZHIPU_API_KEY，无可用视觉模型' },
  });
  const win4 = dom4.window;
  const doc4 = win4.document;
  const spoken4 = win4.__spoken;
  await sleep(60);   // 等 /api/meta 的探测返回

  const bar = doc4.querySelector('#demoBar');
  check('探测到 mock 后显示演示模式横幅',
    !!bar && bar.style.display !== 'none', bar ? bar.style.display : '未找到 #demoBar');
  check('横幅文案明确否定"这是识别结果"',
    !!bar && bar.textContent.includes('不是真实识别结果'),
    bar ? bar.textContent.slice(0, 46) : '');
  check('横幅给出真实原因（便于定位到缺 Key）',
    !!bar && bar.textContent.includes('ZHIPU_API_KEY'),
    bar ? bar.textContent.slice(0, 60) : '');
  // 只说"这是示例"不够——用户实测的下一句就是"我怎么样才能用真实的模型"。
  // 横幅必须同时给出**可执行**的下一步，否则它只是一句免责声明。
  check('横幅给出"怎么才能用真模型"的可执行路径',
    !!bar && bar.textContent.includes('解决')
    && (bar.textContent.includes('check_model') || bar.textContent.includes('set-key')),
    bar ? bar.textContent.slice(60, 140) : '');

  const gate4 = doc4.querySelector('#gate');
  touch(win4, gate4, 'touchend', ...INSIDE);
  await sleep(1300);
  check('开门引导语音里也说明了演示模式（全盲用户不能只靠看横幅）',
    spoken4.some((s) => s.includes('演示模式')),
    spoken4.filter(Boolean).join(' / ') || '（无播报）');

  // 横幅必须进触摸探索：看不见屏幕的用户也要能摸到并听懂
  fakeRect(bar, 400, 60, 0, 300);
  spoken4.length = 0;
  touch(win4, bar, 'touchstart', 200, 320);
  check('演示模式横幅可被触摸探索念出（不只是视觉提示）',
    spoken4.some((s) => s.includes('演示模式') && s.includes('示例')),
    spoken4.filter(Boolean).join(' / ') || '（无播报）');
  dom4.window.close();

  // 反向断言：真实模式（mock_mode=false）不得显示任何演示模式字样，
  // 否则正常演示时会被一句"这是示例"自毁可信度。
  const dom5 = makeDom({ withTTS: true });
  const doc5 = dom5.window.document;
  await sleep(60);
  const bar5 = doc5.querySelector('#demoBar');
  check('未探测到 mock 时不显示演示模式横幅',
    !!bar5 && bar5.style.display === 'none',
    bar5 ? bar5.style.display : '未找到 #demoBar');
  dom5.window.close();

  /* ================================================================
     9. "按钮按不准"这个问题——两条不依赖精度的通道

     问题的本质：视障用户**不可能**保证按到某个按钮，因为看不见它。
     所以正确做法不是"让按钮更容易被按到"，而是**不让"按准按钮"成为前提**：
       A. 固定底栏顺序导航 —— 把十几个分散目标压成 3 个位置固定的目标；
       B. 语音直达          —— 根本不用按。
     再加上"按偏了必须让人知道"，三件事合起来才算回答完这个问题。
     ================================================================ */
  console.log('\n--- 按钮按不准：固定底栏顺序导航 ---');
  const SRC = fs.readFileSync(HTML, 'utf8');

  const navCss = (SRC.match(/#dock\{[\s\S]*?\n  \}/) || [''])[0];
  check('底栏位置固定（position:fixed）—— 盲用户可借"摸到屏幕下边缘"可靠定位',
    /position\s*:\s*fixed/.test(navCss), navCss.slice(0, 48).replace(/\s+/g, ' '));

  const dom6 = makeDom({ withTTS: true });
  const win6 = dom6.window, doc6 = win6.document;
  const sp6 = win6.__spoken;
  await sleep(150);

  const bPrev = doc6.querySelector('#btnPrev');
  const bRun = doc6.querySelector('#btnRun');
  const bNext = doc6.querySelector('#btnNextItem');

  check('底栏三键存在且各有可访问名称（读屏/探索都能念出来）',
    !!bPrev && !!bRun && !!bNext
      && /上一个/.test(bPrev.getAttribute('aria-label') || '')
      && /执行/.test(bRun.getAttribute('aria-label') || '')
      && /下一个/.test(bNext.getAttribute('aria-label') || ''),
    [bPrev, bRun, bNext].map((b) => b && b.getAttribute('aria-label')).join(' | '));

  // 首次引导必须把"底部三键"这条出路说清楚，否则等于没做
  touch(win6, doc6.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1300);
  check('首次引导里就告知了底部三个固定键（不告诉用户等于没有）',
    sp6.some((s) => s.includes('上一个') && s.includes('下一个')),
    sp6.filter((s) => s && s !== '«打断»').slice(-1)[0] || '');
  /* 长按「执行」是一条**隐含手势**：界面上没有任何视觉线索能让用户偶然发现它。
     首次引导是唯一的教学时机 —— 不在这里说，用户就只能在真正紧急的时候
     第一次尝试它。所以这条也钉死。 */
  check('首次引导里就告知了"长按执行键可以直接呼叫志愿者"',
    sp6.some((s) => s.includes('长按') && s.includes('人工志愿者')),
    sp6.filter((s) => s && s !== '«打断»').slice(-1)[0] || '');

  /* 把参与遍历的项摆到不重叠的位置；底栏三键也单独摆开，
     否则触摸坐标会落到别的元素上，被 document 级分派判成"手指不在这个键上"。 */
  fakeRect(doc6.querySelector('#btnScene'), 100, 50, 0, 0);
  fakeRect(doc6.querySelector('#btnRead'), 100, 50, 0, 100);
  fakeRect(doc6.querySelector('#btnHelp'), 100, 50, 0, 200);
  fakeRect(bPrev, 100, 50, 0, 900);
  fakeRect(bRun, 100, 50, 110, 900);
  fakeRect(bNext, 100, 50, 220, 900);
  const NAV_Y = 920;

  const tapNext = () => { touch(win6, bNext, 'touchstart', 270, NAV_Y); touch(win6, bNext, 'touchend', 270, NAV_Y); };
  const tapPrev = () => { touch(win6, bPrev, 'touchstart', 50, NAV_Y); touch(win6, bPrev, 'touchend', 50, NAV_Y); };
  const tapRun = () => { touch(win6, bRun, 'touchstart', 160, NAV_Y); touch(win6, bRun, 'touchend', 160, NAV_Y); };

  sp6.length = 0;
  tapNext();
  await sleep(150);
  check('按「下一个」会播报序号与项目名（首次额外报一次总数）',
    sp6.some((s) => s.includes('第 1 项') && s.includes('共 3 项') && s.includes('拍照看看周围')),
    sp6.filter(Boolean).join(' / ') || '（无播报）');
  check('选中的项同时高亮（低视力用户需要看到"当前是谁"）',
    doc6.querySelector('#btnScene').classList.contains('focusring'));

  sp6.length = 0;
  tapNext();
  await sleep(150);
  /* 第 2 项 = 「拍照读文字」，因为它是**页面上**排在「拍照看看周围」之后的下一个。
     顺序导航与"眼睛看到的顺序 / 手指从上往下摸到的顺序"必须是同一个 ——
     低视力用户才不会看到选中框在页面上下乱窜，
     全盲用户也才能用"我摸到哪儿了"来校准"这是第几项"。
     紧急求助的可达性改由长按底栏「执行」键承担（不依赖顺序），见 14d。 */
  check('第 2 项是版式上的下一个（顺序 = 页面从上到下，不跳版式）',
    sp6.some((s) => s.includes('第 2 项') && s.includes('拍照读文字')),
    sp6.filter(Boolean).join(' / ') || '（无播报）');

  tapNext(); await sleep(120);
  tapNext(); await sleep(150);
  check('走到末尾会说"这是最后一项"，再按一次绕回第 1 项',
    sp6.some((s) => s.includes('这是最后一项')) && sp6.some((s) => s.includes('第 1 项')),
    sp6.filter(Boolean).join(' / ') || '（无播报）');

  sp6.length = 0;
  tapPrev();
  await sleep(150);
  check('按「上一个」回到第 3 项（可逆，摸过头了不用绕一圈）',
    sp6.some((s) => s.includes('第 3 项') && s.includes('呼叫人工志愿者')),
    sp6.filter(Boolean).join(' / ') || '（无播报）');

  /* 「执行」键必须能替用户触发当前选中项 ——
     这正是"不需要按准"的关键：用户只碰一个固定位置的键，
     而作用对象由顺序导航决定。 */
  sp6.length = 0;
  tapNext();                       // 绕回第 1 项：拍照看看周围
  await sleep(150);
  tapRun();
  await sleep(250);
  check('按「执行」会触发当前选中项（用户不必在页面里找到它）',
    doc6.querySelector('#result').textContent.includes('正在拍照识别'),
    doc6.querySelector('#result').textContent.slice(0, 30));
  await finishPendingPhoto(win6);

  check('「执行」键的可访问名称会带上当前对象（读屏能念出"即将执行什么"）',
    /执行：/.test(bRun.getAttribute('aria-label') || ''),
    bRun.getAttribute('aria-label'));

  // ---------- 9b. 按偏了必须让人知道 ----------
  console.log('\n--- 按偏了：空白处不能再静默 ---');
  /* 顺序很重要：先测"拖动经过空白"，此时节流计数器还是干净的；
     否则会被 tap 用例的节流挡住，看起来通过、实际没测到手势判定。 */
  sp6.length = 0;
  touch(win6, doc6.body, 'touchstart', 600, 600);
  touch(win6, doc6.body, 'touchmove', 700, 700);      // 位移 > 16px = 拖动探索
  touch(win6, doc6.body, 'touchend', 700, 700);
  await sleep(150);
  check('拖动探索经过空白不播报（否则手指扫过几屏会刷出一串废话，顶掉正在听的内容）',
    !sp6.some((s) => s.includes('空白')), sp6.filter(Boolean).join(' / ') || '（无播报）');

  sp6.length = 0;
  touch(win6, doc6.body, 'touchstart', 600, 600);     // 轻触：几乎没移动
  touch(win6, doc6.body, 'touchend', 600, 600);
  await sleep(150);
  check('轻触空白会明确告知"这里是空白"——"没按上"与"应用卡死"从此可以区分',
    sp6.some((s) => s.includes('空白')), sp6.filter(Boolean).join(' / ') || '（无播报）');
  check('空白提示同时给出出路（指向底部固定三键，而不是只说"你按错了"）',
    sp6.some((s) => s.includes('空白') && (s.includes('执行') || s.includes('下一个'))),
    sp6.filter((s) => s.includes('空白')).join(' / '));

  sp6.length = 0;
  touch(win6, doc6.body, 'touchstart', 600, 600);
  touch(win6, doc6.body, 'touchend', 600, 600);
  await sleep(150);
  check('连续轻触空白不会刷屏（6 秒节流）',
    !sp6.some((s) => s.includes('空白')), sp6.filter(Boolean).join(' / ') || '（无播报）');

  // ---------- 9c. 重听必须重播"结果"，而不是最后摸到的按钮名 ----------
  console.log('\n--- 结果区重听：内容正确性 ---');
  fakeRect(doc6.querySelector('#result'), 500, 100, 0, 400);
  sp6.length = 0;
  touch(win6, doc6.querySelector('#btnScene'), 'touchstart', 50, 25);   // 探索：只报名字
  touch(win6, doc6.querySelector('#btnScene'), 'touchmove', 600, 600);  // 滑走，避免执行
  await sleep(120);
  sp6.length = 0;
  touch(win6, doc6.querySelector('#result'), 'touchend', 250, 450);
  await sleep(150);
  /* 这是一条防回归断言。原实现里 speak() 无条件写 lastSpeech，
     而触摸探索每碰一个按钮都会 speak(按钮名) —— 于是"重听"播出来的是
     "拍照看看周围"而不是上一次的识别结果。用户想重听环境描述，
     听到的却是按钮名字，且完全无从解释。
     断言写成两半：① 重播的必须是一条**真实结果**（此刻是"没有取到照片"，
     由上面 finishPendingPhoto 产生，是最后一次业务播报）；
     ② 绝不能是刚才摸到的那个按钮名。 */
  check('重听重播的是上一次的结果，而不是最后摸到的按钮名',
    sp6.some((s) => s.includes('没有取到照片')) && !sp6.some((s) => s === '拍照看看周围'),
    sp6.filter(Boolean).join(' / ') || '（无播报）');

  // ---------- 9d. 失败之后要留下"再来一次"的路 ----------
  console.log('\n--- 失败后重试 ---');
  win6.fetch = () => Promise.reject(new Error('网络断开'));
  sp6.length = 0;
  touch(win6, doc6.querySelector('#btnScene'), 'touchstart', 50, 25);
  touch(win6, doc6.querySelector('#btnScene'), 'touchend', 50, 25);
  await sleep(150);
  await finishPendingPhoto(win6, new win6.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  await sleep(250);
  check('连接失败时播报里直接给出"可以重试"的出路',
    sp6.some((s) => s.includes('重试')), sp6.filter(Boolean).join(' / ') || '（无播报）');

  sp6.length = 0;
  touch(win6, doc6.querySelector('#result'), 'touchend', 250, 450);
  await sleep(320);
  check('失败后轻触结果区即可重试（不必重新在屏幕上摸索到那个按钮）',
    sp6.some((s) => s.includes('正在重试')), sp6.filter(Boolean).join(' / ') || '（无播报）');
  await finishPendingPhoto(win6, new win6.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  dom6.window.close();

  // ---------- 10. 语音直达：不按任何按钮也能用 ----------
  console.log('\n--- 语音直达（完全不依赖按压精度） ---');
  const FAKE_STREAM = { getTracks: () => [{ stop() {} }] };

  async function speakPhrase(text) {
    const domV = makeDom({
      withTTS: true,
      mediaRecorder: true,
      asrText: text,
      getUserMedia: () => Promise.resolve(FAKE_STREAM),
    });
    const wv = domV.window, dv = wv.document;
    await sleep(150);
    const vb = dv.querySelector('#btnVoice');
    vb.dispatchEvent(new wv.Event('pointerdown', { bubbles: true }));
    /* 按住时长必须覆盖两段：① 前 300ms 的"轻点保护"（低于它视为探索，不录音）；
       ② 再 VB_OPEN_MS(380ms) 才开麦；③ 录音本身还有 400ms 的误触下限。
       所以 900ms 是"一次正常说话"的最短模拟。
       （2026-10-07 前这里要 1.5 秒以上，因为开麦条件是"名字念完"。
       开麦时机改成固定上限之后，900ms 反而更宽裕了。） */
    await sleep(900);
    wv.dispatchEvent(new wv.Event('pointerup'));
    await sleep(800);                                   // 等 ASR 返回 + 复述 + 路由
    return { win: wv, doc: dv, spoken: wv.__spoken };
  }

  const vScene = await speakPhrase('帮我看看周围有什么');
  check('说"看看周围"直达拍照理解（不需要按任何按钮）',
    vScene.doc.querySelector('#result').textContent.includes('正在拍照识别'),
    vScene.doc.querySelector('#result').textContent.slice(0, 26));
  check('识别结果会先复述一遍再执行（嘈杂环境下这是唯一的纠错机会）',
    vScene.spoken.some((s) => s.includes('你说的是')),
    vScene.spoken.filter(Boolean).join(' / ') || '（无播报）');
  vScene.win.close();

  const vRead = await speakPhrase('念一下这盒药上写的什么');
  check('说"念一下药"直达读文字，而不是被当成"看看"去做环境描述',
    vRead.doc.querySelector('#result').textContent.includes('正在读取文字'),
    vRead.doc.querySelector('#result').textContent.slice(0, 26));
  vRead.win.close();

  const vMed = await speakPhrase('带我挂号看病');
  check('说"挂号看病"直达就医引导',
    vMed.win.__fetches.some((u) => u.includes('/api/sop/start')),
    vMed.win.__fetches.filter((u) => u.includes('/api/')).join(' / '));
  vMed.win.close();

  const vNone = await speakPhrase('今天天气不错');
  check('没匹配上功能时不能装死：把"可以说什么"念出来',
    vNone.spoken.some((s) => s.includes('可以说')), vNone.spoken.filter(Boolean).join(' / '));
  vNone.win.close();

  // ---------- 11. 录音：心跳反馈 + 松开回执 ----------
  console.log('\n--- 按住说话：录音期间的反馈 ---');
  const dom7 = makeDom({
    withTTS: true, mediaRecorder: true, asrText: '看看周围',
    getUserMedia: () => Promise.resolve(FAKE_STREAM),
  });
  const win7 = dom7.window;
  await sleep(150);
  const sp7 = win7.__spoken, vb7 = win7.__vibrations;
  win7.document.querySelector('#btnVoice').dispatchEvent(new win7.Event('pointerdown', { bubbles: true }));
  /* 3200ms = 300ms（轻点保护，之后才开麦）+ 2900ms（等过一次 2.5s 的心跳）。
     少了前 300ms，量到的会是"还没开始录"，而不是"录音期间有没有心跳"。 */
  await sleep(3200);
  check('录音期间有周期性心跳震动（否则无法判断"还在录吗"）',
    vb7.some((v) => Array.isArray(v) && v[0] === 12 && v[1] === 120),
    JSON.stringify(vb7.filter((v) => Array.isArray(v))));
  win7.dispatchEvent(new win7.Event('pointerup'));
  await sleep(200);
  check('松开手指有明确回执（否则用户会怀疑没松开，把话再说一遍）',
    sp7.some((s) => s.includes('录音结束')), sp7.filter(Boolean).join(' / ') || '（无播报）');
  dom7.window.close();

  // ---------- 12. SOP 步骤文案对 TTS 友好 ----------
  console.log('\n--- 文案可朗读性 ---');
  check('SOP 步骤不出现斜杠（"第 3 / 6 步"会被读成"斜杠"，或干脆读不通）',
    !/\$\{curIndex\}\s*\/\s*\$\{totalSteps\}/.test(SRC)
    && !/\$\{j\.index\}\/\$\{j\.totalSteps\}/.test(SRC)
    && SRC.includes('共 ${totalSteps} 步'),
    '第 N 步，共 M 步');

  // ---------- 13. 紧急长按：跳过确认的快速求助 ----------
  console.log('\n--- 紧急场景：不能要求用户"先找到按钮再确认两次" ---');
  const dom8 = makeDom({ withTTS: true });
  const win8 = dom8.window, doc8 = win8.document;
  const sp8 = win8.__spoken;
  await sleep(150);

  const btnHelp8 = doc8.querySelector('#btnHelp');
  const bNext8 = doc8.querySelector('#btnNextItem');
  const bRun8 = doc8.querySelector('#btnRun');
  fakeRect(btnHelp8, 100, 50, 0, 0);
  fakeRect(bNext8, 100, 50, 0, 900);
  fakeRect(bRun8, 100, 50, 110, 900);
  const tap8 = (el, x, y) => { touch(win8, el, 'touchstart', x, y); touch(win8, el, 'touchend', x, y); };
  const helpCalls8 = () => win8.__fetches.filter((u) => u.includes('/api/help')).length;

  tap8(bNext8, 50, 920);
  await sleep(150);
  check('焦点停在「呼叫人工志愿者」时，「执行」键会说明"长按可直接呼叫"',
    /长按/.test(bRun8.getAttribute('aria-label') || ''),
    bRun8.getAttribute('aria-label'));

  /* 反向断言：长按提示必须与焦点**严格同步**。
     如果它一旦出现过就永久留在可访问名称里，用户在别的功能上按「执行」
     会以为自己也能长按，而实际什么都不会发生 —— 比不说更糟。
     这里加一个普通项（btnScene）来制造交替。 */
  fakeRect(doc8.querySelector('#btnScene'), 100, 50, 0, 300);
  tap8(bNext8, 50, 920);                       // 求助 → 拍照看看周围
  await sleep(150);
  const offLabel = bRun8.getAttribute('aria-label') || '';
  tap8(bNext8, 50, 920);                       // 回到求助
  await sleep(150);
  check('长按提示与焦点严格同步：离开求助项后不再提示，回来才提示',
    !/长按/.test(offLabel) && /长按/.test(bRun8.getAttribute('aria-label') || ''),
    `离开时「${offLabel}」→ 回来时「${bRun8.getAttribute('aria-label')}」`);

  // 普通单击：仍必须二次确认 —— 长按通道绝不能削弱日常保护
  sp8.length = 0;
  tap8(bRun8, 160, 920);
  await sleep(220);
  check('普通单击仍要二次确认（长按通道不得削弱日常保护）',
    helpCalls8() === 0 && sp8.some((s) => s.includes('再点一次确认')),
    `已发请求 ${helpCalls8()} 次 ｜ ${sp8.filter((s) => s && s !== '«打断»').join(' / ')}`);

  // 长按 1.5 秒：直接发出，且松手不能再执行一遍
  sp8.length = 0;
  touch(win8, bRun8, 'touchstart', 160, 920);          // 真实手指会先产生 touchstart
  bRun8.dispatchEvent(new win8.Event('pointerdown', { bubbles: true }));
  await sleep(1750);
  check('长按「执行」键 1.5 秒 → 直接呼叫人工志愿者（紧急时不必三步操作）',
    helpCalls8() === 1, `已发请求 ${helpCalls8()} 次`);
  check('长按触发时有就绪双震（用户能感觉到"已经帮你发出去了"）',
    win8.__vibrations.some((v) => Array.isArray(v) && v[0] === 18),
    JSON.stringify(win8.__vibrations.filter((v) => Array.isArray(v))));

  touch(win8, bRun8, 'touchend', 160, 920);            // 松手
  await sleep(300);
  check('长按触发后松手不会再执行一遍聚焦项（一次长按只产生一个动作）',
    helpCalls8() === 1, `已发请求 ${helpCalls8()} 次`);
  dom8.window.close();

  /* ================================================================
     14. 纠错通道、项序、以及"摸到却不知道是什么"的区域

     这一节全部来自同一个问题域："视障用户既按不准、也不该被迫按准"。
     按不准 ⇒ 需要顺序导航与语音直达（前面已测）；
     但**导航到了、语音说了之后仍然可能判断错**（把走廊尽头当服务台），
     而原实现里 SOP 的 index 只能 +1，误判一次就只能退出重来。
     ================================================================ */
  console.log('\n--- 误判「已到达」之后：必须能退回去 ---');
  const domA = makeDom({ withTTS: true });
  const winA = domA.window, docA = winA.document, spA = winA.__spoken;
  await sleep(150);

  docA.querySelector('#btnSopMedical').click();        // 起一个真实的 SOP 会话
  await sleep(250);
  const backCalls = () => winA.__fetches.filter((u) => u.includes('/api/sop/back')).length;

  const btnBackA = docA.querySelector('#btnBack');
  check('SOP 控制区里有「退回上一步」，且初始就是可达的（不是只在文档里写着）',
    !!btnBackA && (btnBackA.getAttribute('aria-label') || '').includes('退回上一步'),
    btnBackA ? btnBackA.getAttribute('aria-label') : '未找到 #btnBack');

  fakeRect(btnBackA, 100, 50, 0, 300);
  fakeRect(docA.querySelector('#sopStep'), 200, 30, 0, 400);

  spA.length = 0;
  btnBackA.click();
  await sleep(300);
  check('点「退回上一步」会真的调用后端退回接口（不是前端假装退了一步）',
    backCalls() === 1, `已发请求 ${backCalls()} 次`);
  check('退回后播报"已退回第 N 步，共 M 步"（用户能确认自己退到了哪儿）',
    spA.some((s) => s && s.includes('退回') && s.includes('共')), spA.filter(Boolean).join(' / ') || '（无播报）');

  /* 进度条的名字必须**现算**：写死"第 1 步"的话，用户走到第 4 步摸到它，
     听到的是"第 1 步" —— 那是错误信息，比不报更糟。 */
  spA.length = 0;
  touch(winA, docA.querySelector('#sopStep'), 'touchstart', 100, 410);
  await sleep(120);
  const stepLabelA = docA.querySelector('#sopStep').getAttribute('aria-label') || '';
  check('SOP 进度条摸到会念出"当前"进度（名字现算，不是绑定时的快照）',
    stepLabelA.includes('共') && stepLabelA.includes('步'), stepLabelA);
  domA.window.close();

  /* ---------- 14b. 流程走完之后仍要留着纠错入口 ---------- */
  console.log('\n--- 流程走完：不能把「退回去」一起收走 ---');
  const domC1 = makeDom({ withTTS: true, sopStep: { finished: true, reached: true, index: 3, total_steps: 3, step: null, speech: '就医引导已完成，祝你顺利。' } });
  const winC1 = domC1.window, docC1 = winC1.document;
  await sleep(150);
  docC1.querySelector('#btnSopMedical').click();
  await sleep(250);
  docC1.querySelector('#btnManual').click();          // 推进到"完成"
  await sleep(300);
  check('流程完成后 SOP 卡片不消失（否则用户听到"已完成"才发现认错地方，却已无处可退）',
    docC1.querySelector('#sopCard').style.display !== 'none',
    'display=' + (docC1.querySelector('#sopCard').style.display || '(默认)'));
  check('流程完成后「退回上一步」仍在（最后一步的误判也能挽回）',
    docC1.querySelector('#btnBack').style.display !== 'none');
  check('流程完成后隐藏「拍照确认」「已到达」（避免用户对着已结束的流程继续推进）',
    docC1.querySelector('#btnNext').style.display === 'none'
    && docC1.querySelector('#btnManual').style.display === 'none');
  domC1.window.close();

  /* ---------- 14c. 语音：退回去 / 再说一遍 ---------- */
  console.log('\n--- 语音直达：纠错与重听（完全不按按钮） ---');
  const domV2 = makeDom({
    withTTS: true, mediaRecorder: true,
    getUserMedia: () => Promise.resolve(FAKE_STREAM),
  });
  const winV2 = domV2.window, docV2 = winV2.document, spV2 = winV2.__spoken;
  await sleep(150);
  docV2.querySelector('#btnSopMedical').click();       // 先建立会话，否则退的是"没有流程"分支
  await sleep(250);

  async function say(w, text) {
    w.__asrTextOverride = text;
    const vb = w.document.querySelector('#btnVoice');
    vb.dispatchEvent(new w.Event('pointerdown', { bubbles: true }));
    await sleep(900);      // 300ms 轻点保护 + 400ms 误触下限，见 speakPhrase 的说明
    w.dispatchEvent(new w.Event('pointerup'));
    await sleep(750);
  }

  const backCalls2 = () => winV2.__fetches.filter((u) => u.includes('/api/sop/back')).length;
  spV2.length = 0;
  await say(winV2, '退回去，我弄错了');
  check('说"退回去"直达「退回上一步」（纠错不必再在屏幕里找那个按钮）',
    backCalls2() >= 1, `已发请求 ${backCalls2()} 次 ｜ ${spV2.filter(Boolean).join(' / ')}`);

  spV2.length = 0;
  await say(winV2, '再说一遍');
  check('说"再说一遍"重播上一次播报（嘈杂环境下"没听清"是常态）',
    spV2.some((s) => s && s.includes('退回')), spV2.filter(Boolean).join(' / ') || '（无播报）');

  spV2.length = 0;
  await say(winV2, '念一下这盒药');
  /* 观测点说明：不需要真的取到照片 —— 走到"正在读取文字"就足以证明
     执行的是「拍照读文字」。这里刻意读 #result 而不是听播报：
     两个功能（看看周围 / 读文字）的填充语都是"我看一下，请稍等"，
     靠播报分不出来；只有结果区的文案是各自独有的。
     同时断言没有重播上一条（"已退回…"），证明"再说一遍"没有抢走这句：
     "念"字同时命中读文字与重听两条路由，谁赢完全由排序决定，必须钉死。 */
  check('"再说一遍"这条路由没有把"念一下药"抢走（读文字仍然优先于重听）',
    docV2.querySelector('#result').textContent.includes('正在读取文字')
    && !spV2.some((s) => s && s.includes('已退回')),
    docV2.querySelector('#result').textContent.slice(0, 24) + ' ｜ ' + spV2.filter(Boolean).join(' / '));
  domV2.window.close();

  /* ---------- 14d. 长按「执行」= 紧急呼叫，**不依赖焦点停在哪一项** ---------- */
  console.log('\n--- 长按「执行」：任何位置都能直接呼叫 ---');
  const domLP = makeDom({ withTTS: true });
  const winLP = domLP.window, docLP = winLP.document, spLP = winLP.__spoken;
  await sleep(150);
  const bSceneLP = docLP.querySelector('#btnScene');
  const bHelpLP = docLP.querySelector('#btnHelp');
  const bNextLP = docLP.querySelector('#btnNextItem');
  const bRunLP = docLP.querySelector('#btnRun');
  fakeRect(bSceneLP, 100, 50, 0, 0);
  fakeRect(bHelpLP, 100, 50, 0, 100);
  fakeRect(bNextLP, 100, 50, 0, 900);
  fakeRect(bRunLP, 100, 50, 110, 900);
  const helpCallsLP = () => winLP.__fetches.filter((u) => u.includes('/api/help')).length;

  /* 故意把焦点停在**第 1 项**（拍照看看周围），而不是求助项。
     顺序导航改成"页面从上到下"之后，求助项落在第 10 位左右 ——
     紧急时按十下「下一个」是不现实的。这条用例钉死的就是：
     长按的可达性**不依赖**焦点在哪一项。 */
  touch(winLP, bNextLP, 'touchstart', 50, 920);
  touch(winLP, bNextLP, 'touchend', 50, 920);
  await sleep(150);
  check('（前提）焦点停在第 1 项「拍照看看周围」，不是求助项',
    (bRunLP.getAttribute('aria-label') || '').includes('拍照看看周围'),
    bRunLP.getAttribute('aria-label'));

  spLP.length = 0;
  bRunLP.dispatchEvent(new winLP.Event('pointerdown', { bubbles: true }));
  await sleep(600);
  check('长按不足一秒半不发工单（搁一下手指不该惊动志愿者）',
    helpCallsLP() === 0, `已发请求 ${helpCallsLP()} 次`);

  await sleep(1200);                        // 累计超过 HELP_LONGPRESS_MS = 1500
  check('长按满一秒半：无论焦点停在哪一项，都直接呼叫人工志愿者',
    helpCallsLP() === 1 && spLP.some((s) => s && s.includes('正在直接呼叫人工志愿者')),
    `已发请求 ${helpCallsLP()} 次 ｜ ` + spLP.filter(Boolean).join(' / '));

  bRunLP.dispatchEvent(new winLP.Event('pointerup', { bubbles: true }));
  await sleep(350);
  check('松手不会顺手把聚焦项也执行一遍（说了做什么就只做什么）',
    helpCallsLP() === 1 && !docLP.querySelector('#result').textContent.includes('正在拍照识别'),
    docLP.querySelector('#result').textContent.slice(0, 24));
  domLP.window.close();

  /* ---------- 14e. 导航项序 = 页面从上到下（不另排） ---------- */
  console.log('\n--- 导航项序：与页面从上到下一致 ---');
  const domN = makeDom({ withTTS: true });
  const winN = domN.window, docN = winN.document, spN = winN.__spoken;
  await sleep(150);
  const chipsN = Array.from(docN.querySelectorAll('#docChips .chip'));
  const bSceneN = docN.querySelector('#btnScene');
  const bReadN = docN.querySelector('#btnRead');
  const bHelpN = docN.querySelector('#btnHelp');
  const bPrevN = docN.querySelector('#btnPrev');
  const bRunN = docN.querySelector('#btnRun');
  const bNextN = docN.querySelector('#btnNextItem');
  fakeRect(bSceneN, 100, 50, 0, 0);
  fakeRect(bHelpN, 100, 50, 0, 100);
  fakeRect(bReadN, 100, 50, 0, 200);
  fakeRect(chipsN[0], 100, 50, 0, 300);
  fakeRect(chipsN[1], 100, 50, 0, 400);
  fakeRect(bPrevN, 100, 50, 0, 900);
  fakeRect(bRunN, 100, 50, 110, 900);
  fakeRect(bNextN, 100, 50, 220, 900);

  /* 前提校验：版式（DOM）里"文字类型"芯片确实排在「拍照读文字」**前面**。
     没有这条，下面那条断言可能是凑巧成立的（比如芯片本来就在后面）。 */
  check('（前提）DOM 版式里文字类型芯片排在「拍照读文字」之前',
    !!(chipsN[0].compareDocumentPosition(bReadN) & 4));
  check('（前提）fakeRect 摆的垂直次序与 DOM 次序不同，才能验出"用的是哪一套"',
    Number(bHelpN.getBoundingClientRect().top) < Number(bReadN.getBoundingClientRect().top)
    && !!(bReadN.compareDocumentPosition(bHelpN) & 4));

  const tapNextN = () => { touch(winN, bNextN, 'touchstart', 270, 920); touch(winN, bNextN, 'touchend', 270, 920); };
  const navSeq = [];
  for (let i = 0; i < 5; i++) {
    spN.length = 0;
    tapNextN();
    await sleep(120);
    navSeq.push(spN.filter((s) => s && s.includes('第') && s.includes('项')).pop() || '');
  }
  const navPretty = navSeq.map((s) => s.replace('下一个功能', '')).join(' ｜ ');
  check('第 1 项 = 页面上第一个按钮「拍照看看周围」', navSeq[0].includes('拍照看看周围'), navSeq[0]);
  check('第 2 项 = 版式上紧随其后的「文字类型」芯片（顺序不跳过版式）',
    navSeq[1].includes('文字类型'), navSeq[1]);
  check('第 4 项 = 「拍照读文字」（芯片之后才是它，与眼睛看到的顺序相同）',
    navSeq[3].includes('拍照读文字'), navPretty);
  check('第 5 项 = 「呼叫人工志愿者」，并且说出来这是最后一项',
    navSeq[4].includes('呼叫人工志愿者') && navSeq[4].includes('最后一项'), navSeq[4]);
  domN.window.close();

  /* ---------- 14f. 摸到"信息性区域"不能再报"这里是空白" ---------- */
  console.log('\n--- 摸到输入框/分组标题：必须说明用途 ---');
  const domQ = makeDom({ withTTS: true });
  const winQ = domQ.window, docQ = winQ.document, spQ = winQ.__spoken;
  await sleep(120);
  /* 必须先把闸门打开：空白提示的第一道门就是 gateOpened ——
     闸门还盖着整屏时，抬手是"打开应用"，不该被解释成"你按偏了"。
     不打开闸门就测空白，测到的是这个前置条件，不是空白提示本身。 */
  touch(winQ, docQ.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1400);
  const qEl = docQ.querySelector('#q');
  const titleEl = docQ.querySelector('.sec-title');
  fakeRect(qEl, 200, 40, 0, 0);
  fakeRect(titleEl, 200, 30, 0, 100);

  spQ.length = 0;
  touch(winQ, qEl, 'touchstart', 100, 20);
  await sleep(120);
  check('摸到"补充说明"输入框会说明它是干什么的（原来只会被当成空白）',
    spQ.some((s) => s && s.includes('输入框')), spQ.filter(Boolean).join(' / ') || '（无播报）');

  spQ.length = 0;
  touch(winQ, titleEl, 'touchstart', 100, 115);
  await sleep(120);
  check('摸到分组标题会念出这一组是干什么的（发现不了的分组等于不存在）',
    spQ.some((s) => s && s.includes('这一组')), spQ.filter(Boolean).join(' / ') || '（无播报）');

  spQ.length = 0;
  fakeRect(docQ.querySelector('.card'), 200, 60, 0, 600);   // 一块真的没有语义的卡片背景
  touch(winQ, docQ.querySelector('.card'), 'touchstart', 100, 605);
  touch(winQ, docQ.querySelector('.card'), 'touchend', 100, 605);
  await sleep(150);
  check('真正的空白仍然报"空白"（新加的说明没有把空白提示顶掉）',
    spQ.some((s) => s && s.includes('空白')), spQ.filter(Boolean).join(' / ') || '（无播报）');
  domQ.window.close();

  /* ---------- 14g. 取图降级必须进语音通道 ---------- */
  console.log('\n--- 取图降级：不能只写在屏幕文字里 ---');
  const domF = makeDom({ withTTS: true, secureContext: false, mediaDevices: false });
  const winF = domF.window, docF = winF.document;
  await sleep(120);
  touch(winF, docF.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1600);
  check('非 HTTPS 环境下，取图降级为「系统相机」这件事会**念出来**（全盲用户才可能知道）',
    winF.__spoken.some((s) => s && s.includes('系统相机')),
    winF.__spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');
  check('同时仍然保留屏幕文字说明（低视力用户与陪同者需要）',
    docF.querySelector('#result').textContent.includes('系统相机'));
  domF.window.close();

  /* ================================================================
     15. 播报词与音色
     起因是用户实测反馈的两件事：
       · 连按兜底导航时，每一格都在念"共 11 项"，念到后面全是冗余；
       · 播报声音太生硬，想要温柔一点的。
     ================================================================ */

  console.log('\n--- 15a. 顺序导航：总数只在第一次说 ---');
  const domT = makeDom({ withTTS: true });
  const winT = domT.window, docT = winT.document, spT = winT.__spoken;
  await sleep(150);
  touch(winT, docT.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1300);

  const bNextT = docT.querySelector('#btnNextItem');
  const bPrevT = docT.querySelector('#btnPrev');
  fakeRect(docT.querySelector('#btnScene'), 100, 50, 0, 0);
  fakeRect(docT.querySelector('#btnHelp'), 100, 50, 0, 100);
  fakeRect(docT.querySelector('#btnRead'), 100, 50, 0, 200);
  fakeRect(bNextT, 100, 50, 220, 900);
  fakeRect(bPrevT, 100, 50, 0, 900);

  const tapNT = () => { touch(winT, bNextT, 'touchstart', 270, 920); touch(winT, bNextT, 'touchend', 270, 920); };
  const tapPT = () => { touch(winT, bPrevT, 'touchstart', 50, 920); touch(winT, bPrevT, 'touchend', 50, 920); };
  /* 只看"序号/总数"那几句 —— 触摸探索报的"下一个功能"要滤掉，
     否则过滤条件太宽，什么改动都能"匹配上"。 */
  const navLinesT = () => spT.filter((s) => s && /(共 \d+ 项|第 \d+ 项)/.test(s)).join(' ｜ ');

  spT.length = 0;
  tapNT();
  await sleep(150);
  const l1 = navLinesT();
  check('第一次按「下一个」会报出总数（"一共几项"只建立一次整体概念）',
    /共 \d+ 项/.test(l1) && /第 1 项/.test(l1), l1 || '（无播报）');

  spT.length = 0;
  tapNT();
  await sleep(150);
  const l2 = navLinesT();
  /* 这就是用户抱怨的那一句。总数是一次性信息，从第二遍开始纯粹是冗余：
     中文 TTS 下"共 11 项"要约 1.2 秒，连按 11 次等于白等 12 秒，
     而且每次都盖住真正想听的名字。 */
  check('第二次移动**不再**念总数（用户实测抱怨的正是这一句）',
    /第 2 项/.test(l2) && !/共 \d+ 项/.test(l2), l2 || '（无播报）');

  spT.length = 0;
  tapNT();
  await sleep(150);
  const l3 = navLinesT();
  check('走到最后一项会明确说"这是最后一项"（边界信息比总数更有用）',
    /最后一项/.test(l3), l3 || '（无播报）');

  spT.length = 0;
  tapNT();
  await sleep(150);
  const l4 = navLinesT();
  check('从末尾绕回开头时说"回到第 1 项"（否则用户会以为自己按错了）',
    /回到第 1 项/.test(l4), l4 || '（无播报）');
  check('绕回时同样不重复念总数', !/共 \d+ 项/.test(l4), l4 || '（无播报）');

  spT.length = 0;
  tapPT();
  await sleep(150);
  const l5 = navLinesT();
  check('从开头往回绕时说"绕到最后一项"（两个方向都要能解释清楚）',
    /绕到最后一项/.test(l5), l5 || '（无播报）');

  /* 可选项数量变了必须重新报一次总数 ——
     否则那个"共 3 项"会被继续用在一个已经变成 4 项的列表上，成为**错误信息**。 */
  docT.querySelector('#sopCard').style.display = 'block';
  fakeRect(docT.querySelector('#btnNext'), 100, 50, 0, 300);
  spT.length = 0;
  tapNT();
  await sleep(150);
  const l6 = navLinesT();
  check('可选项数量变化后会重新报总数（数字不对时宁可再说一遍）',
    /共 4 项/.test(l6), l6 || '（无播报）');

  console.log('\n--- 15b. 音色：避开男声、参数落到 utterance 上 ---');
  /* 观察点用"已就绪"这句开门语：它是应用发出的第一句话，
     也决定了用户对"这个应用听起来怎么样"的第一印象。 */
  const u0 = winT.__utterances.find((u) => u.text && u.text.includes('已就绪'));
  /* 反向前提断言：证明测试用的语音列表里第一个**确实是男声** ——
     没有这条，下面"选中女声"可能只是碰巧成立
     （比如列表本来就恰好没有男声，那测了等于没测）。 */
  check('（前提）测试用的语音列表里第一个是男声，"取第一个"确实会取到它',
    /kangkang/i.test(winT.__voices[0].name), winT.__voices[0].name);
  check('默认音色避开排在前面的男声，选中柔和女声（晓晓）',
    !!u0 && !!u0.voice && /xiaoxiao/i.test(u0.voice.name),
    u0 && u0.voice ? u0.voice.name : '（没有设置 voice）');
  check('语速落到温柔档 0.95（原来 1.02 偏赶人；慢一点才从容）',
    !!u0 && Math.abs(u0.rate - 0.95) < 1e-9, u0 ? String(u0.rate) : '（无）');
  check('音调只抬到 1.04（抬多了会变尖、刺耳，反而更累）',
    !!u0 && Math.abs(u0.pitch - 1.04) < 1e-9, u0 ? String(u0.pitch) : '（无）');

  const domM = makeDom({ withTTS: true, voices: [
    { lang: 'zh-CN', name: 'Microsoft Kangkang - Chinese (Simplified)', voiceURI: 'kangkang' },
  ] });
  const winM = domM.window, docM = winM.document;
  await sleep(150);
  touch(winM, docM.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1400);
  const uM = winM.__utterances.find((u) => u.text && u.text.includes('已就绪'));
  check('设备上只装了男声时依然出声（不能拿"没有温柔声音"当作没有声音的理由）',
    !!uM && !!uM.voice && /kangkang/i.test(uM.voice.name),
    uM && uM.voice ? uM.voice.name : '（没有设置 voice）');
  domM.window.close();

  console.log('\n--- 15c. 播报设置：能调速、记得住 ---');
  const domS = makeDom({ withTTS: true });
  const winS = domS.window, docS = winS.document, spS = winS.__spoken;
  await sleep(150);

  /* 「换个声音」已按用户要求撤掉。这里做**反向断言**把它钉住：
     删功能最怕两种情况 —— 界面没了代码还在跑（残留的语音入口会
     报出一个屏幕上根本不存在的按钮名），或者代码删了界面还挂着一个
     点不动的按钮。两头都要查。 */
  check('「换个声音」的按钮与语音入口都已移除（不留点不动的死按钮）',
    !docS.querySelector('#btnVoiceNext') && !winS.findVoiceRoute('换个声音'),
    'btnVoiceNext=' + !!docS.querySelector('#btnVoiceNext')
      + '，语音命中=' + JSON.stringify(winS.findVoiceRoute('换个声音')));

  check('页面里仍留着可触摸的播报调速键（自动挑错了也还有救）',
    !!docS.querySelector('#btnSlower') && !!docS.querySelector('#btnFaster'));

  const infoS = docS.querySelector('#spkInfo');
  check('设置区显示当前在用哪个声音（用户得知道自己正在听的是谁）',
    /正在使用/.test(infoS.textContent) && /xiaoxiao/i.test(infoS.textContent),
    infoS.textContent);
  check('不再报"第几个、共几个可选"（换不了的声音，报了也走不到）',
    !/第 \d+ 个，共 \d+ 个/.test(infoS.textContent), infoS.textContent);

  const bSlower = docS.querySelector('#btnSlower');
  const bFaster = docS.querySelector('#btnFaster');
  fakeRect(bSlower, 100, 50, 0, 500);
  fakeRect(bFaster, 100, 50, 120, 500);
  const tapV = (el, x) => { touch(winS, el, 'touchstart', x, 505); touch(winS, el, 'touchend', x, 505); };
  const lastUtt = () => winS.__utterances[winS.__utterances.length - 1];

  /* 基准语速：先说一句，取它实际用的 rate。
     原来这个基准是从"换声音"那次试听的 utterance 上取的，
     按钮撤掉后没有那条 utterance 了，改成自己造一条。 */
  spS.length = 0;
  winS.speak('基准');
  await sleep(150);
  const rate0 = lastUtt().rate;

  tapV(bSlower, 50);
  await sleep(220);
  const uSlow = lastUtt();
  check('点「慢一点」会真的降低语速，并立刻生效到下一句',
    uSlow.rate < rate0, rate0 + ' → ' + uSlow.rate);

  tapV(bFaster, 170);
  await sleep(220);
  const uFast = lastUtt();
  check('点「快一点」会真的提高语速',
    uFast.rate > uSlow.rate, uSlow.rate + ' → ' + uFast.rate);

  spS.length = 0;
  for (let i = 0; i < 12; i++) { tapV(bSlower, 50); await sleep(70); }
  await sleep(250);
  check('语速调到下限会停住并明确告知（不能让用户一直点却听不到任何反应）',
    spS.some((s) => s && s.includes('已经是最慢的')),
    spS.filter(Boolean).slice(-2).join(' / ') || '（无播报）');
  check('下限没有被突破（拖到负速度会让播报彻底不可用）',
    lastUtt().rate >= 0.75, String(lastUtt().rate));

  const saved = winS.localStorage.getItem('qm_speech_pref_v1') || '';
  check('偏好写进 localStorage（重开页面不会被重置回默认，等于没得调）',
    /"rate"/.test(saved) && /"name"/.test(saved), saved || '（没写）');
  domS.window.close();
  domT.window.close();

  /* ================================================================
     16. 「按住说话」必须可达
     它是整个应用最重要的输入通道，但原来连 TOUCH_TARGETS 都没注册：
     摸到它会命中"空白区域"分支，顺序导航里也没有它。
     原因是"按下即录音，所以不敢播报"，于是干脆不报 ——
     保护本身没错，代价（这个按钮对视障用户等于不存在）却被忽略了。
     ================================================================ */
  console.log('\n--- 16a. 按住说话：能被摸到、能被导航找到 ---');
  const domVC = makeDom({
    withTTS: true, mediaRecorder: true, asrText: '看看周围',
    getUserMedia: () => Promise.resolve(FAKE_STREAM),
  });
  const winVC = domVC.window, docVC = winVC.document, spVC = winVC.__spoken;
  await sleep(150);
  const vbVC = docVC.querySelector('#btnVoice');
  const bNextVC = docVC.querySelector('#btnNextItem');
  const bPrevVC = docVC.querySelector('#btnPrev');
  const bRunVC = docVC.querySelector('#btnRun');
  fakeRect(vbVC, 320, 64, 0, 700);
  fakeRect(bNextVC, 100, 50, 220, 900);
  fakeRect(bPrevVC, 100, 50, 0, 900);
  fakeRect(bRunVC, 100, 50, 110, 900);
  const tapVC = (el, x, y) => { touch(winVC, el, 'touchstart', x, y); touch(winVC, el, 'touchend', x, y); };

  spVC.length = 0;
  tapVC(bNextVC, 270, 920);
  await sleep(200);
  check('「按住说话」能通过顺序导航被选中并报出名字',
    spVC.some((s) => s && s.includes('按住说话')),
    spVC.filter(Boolean).join(' / ') || '（无播报）');
  check('选中它时「执行」键会说明"按住不放"（它的操作方式与别的项不同）',
    /按住不放/.test(bRunVC.getAttribute('aria-label') || ''),
    bRunVC.getAttribute('aria-label'));

  spVC.length = 0;
  touch(winVC, vbVC, 'touchstart', 50, 720);
  touch(winVC, vbVC, 'touchend', 50, 720);
  await sleep(200);
  check('摸到「按住说话」报的是名字，而不是"这里是空白"',
    spVC.some((s) => s && s.includes('按住说话')) && !spVC.some((s) => s && s.includes('空白')),
    spVC.filter(Boolean).join(' / ') || '（无播报）');

  /* 轻点 vs 按住：靠"按下的时长"区分（300ms）。
     不区分的话，用户每次轻点「执行」都会录进一段无意义的静音。 */
  const asrCalls = () => winVC.__fetches.filter((u) => u.includes('/api/asr')).length;
  const pressRun = async (holdMs) => {
    bRunVC.dispatchEvent(new winVC.Event('pointerdown', { bubbles: true }));
    touch(winVC, bRunVC, 'touchstart', 160, 920);
    await sleep(holdMs);
    touch(winVC, bRunVC, 'touchend', 160, 920);
    winVC.dispatchEvent(new winVC.Event('pointerup'));
    await sleep(350);
  };

  const asrBefore = asrCalls();
  spVC.length = 0;
  await pressRun(120);
  check('轻点「执行」不会开麦（否则每次点它都录一段静音）',
    asrCalls() === asrBefore && !docVC.querySelector('#result').textContent.includes('正在录音'),
    docVC.querySelector('#result').textContent.slice(0, 24));
  check('轻点「执行」会给出"按住不要松开"的指引（点了没反应最伤信任）',
    spVC.some((s) => s && s.includes('按住不要松开')),
    spVC.filter(Boolean).join(' / ') || '（无播报）');

  await pressRun(1000);
  await sleep(600);
  check('按住「执行」不放会真的开始录音（一个固定位置的键就能说话）',
    asrCalls() > asrBefore, '已发 ASR 请求 ' + asrCalls() + ' 次');
  domVC.window.close();

  console.log('\n--- 16b. 按住说话：开麦不再等待播报念完 ---');
  /* 这一节对应 2026-10-07 交互评审的 P0-2。
     原判据是 `!speaking`（名字念完才开麦），而"按住说话"四个字要念 1.1~1.3 秒，
     用户的肌肉记忆却是**按下就说**（微信、所有语音输入都是这样）——
     说"帮我看看这是什么"，前 1 秒被切掉，识别出来是"是什么"，
     而用户只会得出"识别不准"的结论。
     1 秒的话被丢掉是致命的，0.4 秒的机器音只是噪声，两者代价不对等。
     新契约：判定为"按住"后再等 VB_OPEN_MS 就开麦，并主动掐断自己的播报。 */
  const domHU = makeDom({
    withTTS: true, holdTTS: true, mediaRecorder: true, asrText: '看看周围',
    getUserMedia: () => Promise.resolve(FAKE_STREAM),
  });
  const winHU = domHU.window, docHU = winHU.document;
  await sleep(150);
  const vbHU = docHU.querySelector('#btnVoice');
  const recTextHU = () => docHU.querySelector('#result').textContent;
  fakeRect(vbHU, 320, 64, 0, 700);
  winHU.__spoken.length = 0;
  vbHU.dispatchEvent(new winHU.Event('pointerdown', { bubbles: true }));
  touch(winHU, vbHU, 'touchstart', 50, 720);

  /* 轮询而不是固定等待：要拿到的正是"多久开麦"这个数字本身。
     它既是断言，也是回归时最该被一眼看到的量。 */
  let openMs = -1;
  for (let i = 0; i < 24; i++) {            // 最多等 1200ms
    await sleep(50);
    if (recTextHU().includes('正在录音')) { openMs = (i + 1) * 50; break; }
  }
  check('播报没念完也会开麦（不再等"名字念完"，用户按下就能说）',
    openMs > 0, openMs > 0 ? openMs + 'ms 后开麦' : '1.2 秒内都没开麦');
  check('开麦延迟有上限：不超过 650ms（原实现要等 1.1~1.3 秒）',
    openMs > 0 && openMs <= 650, openMs + 'ms');
  check('开麦时主动掐断自己的播报（否则机器音会被录进音频送去识别）',
    winHU.__spoken.includes('«打断»'),
    winHU.__spoken.filter(Boolean).join(' / ') || '（无播报）');
  check('名字仍然先报出去了（用户知道摸到的是什么，只是会被掐断在后半句）',
    winHU.__spoken.some((s) => s && s.includes('按住说话')),
    winHU.__spoken.filter(Boolean).join(' / ') || '（无播报）');
  winHU.dispatchEvent(new winHU.Event('pointerup'));
  await sleep(200);
  domHU.window.close();

  /* ---------- 16. 语音出声实测（手机上的"没声音"必须说清为什么）----------
     这一节的由来：真实手机上面临的失败是"点了没反应、也不报错"。
     而「speechSynthesis 对象存在」根本不等于「能出声」——
     系统没装中文语音引擎、媒体音量为 0、内核把接口做成空壳，
     这三种都静默失败。语音是本产品唯一的输出通道，静默失败 = 产品不存在。 */
  console.log('\n--- 语音出声实测（verifySpeech）---');

  // a) 引擎存在但不出声：必须给出可执行的排查步骤
  const domVS = makeDom({ silentTTS: true });
  const winVS = domVS.window, docVS = winVS.document;
  await sleep(150);
  check('页面暴露了实测入口 verifySpeech（否则静默失败无从判定）',
    typeof winVS.verifySpeech === 'function' && typeof winVS.noteVoiceError === 'function');
  const vbVS = docVS.querySelector('#voiceBar');
  check('语音故障横幅默认不显示（不能一进来就吓人）',
    !!vbVS && vbVS.style.display === 'none');

  touch(winVS, docVS.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(200);
  winVS.verifySpeech();                    // 直接实测，省掉 3.6 秒等待
  check('接口存在但不出声时，横幅把它说破',
    vbVS.style.display !== 'none');
  check('排查步骤写在横幅里（媒体音量 / 换浏览器 / 文字转语音）',
    /媒体音量/.test(vbVS.textContent) && /Chrome/.test(vbVS.textContent) && /文字转语音/.test(vbVS.textContent),
    vbVS.textContent.replace(/\s+/g, ' ').slice(0, 56));
  check('横幅里有语音自检入口，且是整行触控目标',
    !!vbVS.querySelector('a[href="voice_check.html"]'));

  /* 横幅本身要被触摸探索认领：否则点它会听到"这里是空白区域"，
     那是我自己造出来的噪音，用户只会更糊涂。 */
  winVS.__spoken.length = 0;
  fakeRect(vbVS, 320, 120, 0, 0);
  touch(winVS, vbVS, 'touchstart', 50, 25);
  check('故障横幅不是触摸死区（摸到会报名字）',
    winVS.__spoken.some((s) => s && s.includes('语音故障提示')),
    winVS.__spoken.filter((s) => s && s !== '«打断»').join(' / ') || '（无播报）');

  // b) 误报必须能自愈
  winVS.__ttsSilent = false;
  winVS.speak('测试一句');
  check('一旦真的出声，故障横幅立即撤销（误报自愈）',
    vbVS.style.display === 'none');

  // c) 我们自己造成的打断不算故障
  winVS.noteVoiceError('interrupted');
  winVS.noteVoiceError('canceled');
  check('cancel / interrupted 不算故障（否则横幅常年挂着，等于没有警告）',
    vbVS.style.display === 'none');
  winVS.noteVoiceError('voice-unavailable');
  check('引擎报 voice-unavailable 时说清是"没有可用的中文语音"',
    vbVS.style.display !== 'none' && /没有可用的中文语音/.test(vbVS.textContent),
    vbVS.textContent.replace(/\s+/g, ' ').slice(0, 40));
  domVS.window.close();

  // d) 根本没有语音接口（微信内置浏览器）
  const domVN = makeDom({ withTTS: false });
  const winVN = domVN.window, docVN = winVN.document;
  await sleep(150);
  touch(winVN, docVN.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1300);
  winVN.verifySpeech();
  const vbVN = docVN.querySelector('#voiceBar');
  check('没有语音接口时，横幅说明"是浏览器缺能力，不是手机坏了"',
    vbVN.style.display !== 'none' && /不能语音播报/.test(vbVN.textContent),
    vbVN.textContent.replace(/\s+/g, ' ').slice(0, 40));
  check('无语音接口时结果区同时给出书面说明（两处都写，防止只看一屏）',
    docVN.querySelector('#result').textContent.includes('不支持语音播报'));
  domVN.window.close();

  // e) 自检页本身：它要能在没有语音的手机上独立工作
  const vcHtml = fs.readFileSync(path.resolve(__dirname, 'voice_check.html'), 'utf8');
  check('自检页同时检验"音频播放"与"语音引擎"两条链路（才能分开音量问题与引擎问题）',
    /'speechSynthesis' in window/.test(vcHtml) && /AudioContext/.test(vcHtml));
  /* 只看 viewport 那一行，不要在整份文件里搜字面量：
     自检页的**注释里**恰好写着"刻意不写 user-scalable=no"，
     全文匹配会把这条注释当违规抓出来 —— 审计规则给错红牌比不给更坏。 */
  const vpMeta = (vcHtml.match(/<meta\s+name="viewport"\s+content="([^"]*)"/i) || [])[1] || '';
  check('自检页的 viewport 不禁用缩放（低视力用户必须能放大）',
    /width=device-width/.test(vpMeta) && !/user-scalable\s*=\s*no/i.test(vpMeta),
    vpMeta);
  check('自检页有可复制的诊断信息与复制入口',
    /id="copybox"/.test(vcHtml) && /id="btnCopy"/.test(vcHtml));
  check('自检页结论用 aria-live（读屏用户不必自己摸索结果）',
    /id="verdict"[^>]*aria-live/.test(vcHtml));

  /* ---------- 17. 访问口令（部署版的后端保护）----------
     为什么前端也要断言：口令是在**前端**加进请求头的。
     前端漏了这一行，后端就会把每一次识别都拒掉 —— 而现象是
     "页面正常、点拍照没反应"，和后端挂了长得一模一样。 */
  console.log('\n--- 访问口令（部署版后端保护）---');

  const domAK = makeDom({ url: 'http://localhost/?k=test-code-123' });
  const winAK = domAK.window, docAK = winAK.document;
  await sleep(150);
  touch(winAK, docAK.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  const btnAK = docAK.querySelector('#btnScene');
  fakeRect(btnAK, 100, 50);
  touch(winAK, btnAK, 'touchstart', ...INSIDE);
  touch(winAK, btnAK, 'touchend', ...INSIDE);
  await sleep(120);
  /* 必须真的喂一张图：走系统相机通道时 pickPhoto 会等 file input 的 onchange，
     没有文件就不会发出任何请求，断言会以"没有 POST"这种错误理由通过。 */
  await finishPendingPhoto(winAK, new winAK.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  await sleep(120);

  const postInit = winAK.__fetchInits
    .filter((x) => x.url.includes('/api/understand') && x.init && x.init.method === 'POST')
    .pop();
  check('链接里带 ?k= 时，识别请求带上访问口令请求头',
    !!(postInit && postInit.init.headers && postInit.init.headers['X-Access-Code'] === 'test-code-123'),
    postInit ? JSON.stringify(postInit.init.headers) : '（没有 POST 请求）');
  check('口令写进 localStorage（从书签/历史打开、地址栏没有 ?k= 时仍可用）',
    winAK.localStorage.getItem('qm_access_v1') === 'test-code-123');
  check('口令不进 URL 查询串（不进浏览器历史、也不会被日志记下）',
    !winAK.__fetches.some((u) => u.includes('k=test-code-123')));
  domAK.window.close();

  const domNK = makeDom({});
  const winNK = domNK.window, docNK = winNK.document;
  await sleep(150);
  check('本地打开（没有口令）时不带这个头，行为与从前完全一致',
    winNK.__fetchInits.every((x) => !x.init || !x.init.headers
      || !('X-Access-Code' in x.init.headers)));
  domNK.window.close();

  // 后端拒绝时，念出来的必须是后端那句人话
  const domRJ = makeDom({
    failPost: { status: 401, detail: '这个链接缺少访问口令（?k=…）。请让分享者把完整链接再发你一次。' },
  });
  const winRJ = domRJ.window, docRJ = winRJ.document;
  await sleep(150);
  touch(winRJ, docRJ.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  const btnRJ = docRJ.querySelector('#btnScene');
  fakeRect(btnRJ, 100, 50);
  touch(winRJ, btnRJ, 'touchstart', ...INSIDE);
  touch(winRJ, btnRJ, 'touchend', ...INSIDE);
  await sleep(120);
  await finishPendingPhoto(winRJ, new winRJ.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
  await sleep(250);
  const rjText = docRJ.querySelector('#result').textContent;
  check('被后端拒绝时，结果区显示后端给的那句人话',
    rjText.includes('缺少访问口令'), rjText.replace(/\s+/g, ' ').slice(0, 46));
  check('4xx 不把状态码念给用户（那是纯噪音，人话才是有用的）',
    !/401/.test(rjText), rjText.replace(/\s+/g, ' ').slice(0, 46));
  check('被后端拒绝时不说成"连接失败"（否则用户会去查 WiFi，方向全错）',
    !/连接失败|检查网络/.test(rjText), rjText.replace(/\s+/g, ' ').slice(0, 46));
  domRJ.window.close();

  /* ================================================================
     18. 结构语义与分享元信息
     对应 2026-10-06 全面评测的两条：SEO 元信息几乎为零、标题层级断裂。
     都是"视觉上完全看不出问题"的缺陷 —— 只有读屏用户和转发链接的人受影响，
     而这两类人恰好一个是最该被照顾的，一个是评委。
     ================================================================ */
  console.log('\n--- 结构语义与分享元信息 ---');
  /* 必须先剥掉 HTML 注释再断言。
     这不是洁癖：上面那段解释"为什么把 <p class="sec-title"> 换成 <h2>"
     的注释里，本身就带着 <p class="sec-title"> 这个字符串 ——
     直接 grep 源码会把它当成"还有旧的 <p> 标签"，于是断言永远不过。
     同类坑之前踩过一次（voice_check.html 的注释里写了 user-scalable=no，
     被全文匹配当成了违规）。**注释里提到标记，源码匹配就会骗人。** */
  const SRCNC = SRC.replace(/<!--[\s\S]*?-->/g, '');

  check('分组小标题用 <h2>（读屏可按 H 键在分组间跳转，原来全是 <p>）',
    (SRCNC.match(/<h2 class="sec-title">/g) || []).length === 3
    && !/<p class="sec-title">/.test(SRCNC),
    'h2=' + (SRCNC.match(/<h2 class="sec-title">/g) || []).length);
  check('正文包在 <main> 里（读屏"跳到主内容"才有效）',
    /<main>/.test(SRCNC) && /<\/main>/.test(SRCNC));
  check('有 meta description（分享出去不是一条秃链接）',
    /<meta name="description" content="[^"]{10,}">/.test(SRCNC));
  check('有 og:title / og:description（微信与大赛平台预览才有摘要）',
    /property="og:title"/.test(SRCNC) && /property="og:description"/.test(SRCNC));
  check('favicon 用内联 SVG（标签页认得出，又不新增外部请求）',
    /rel="icon"[^>]*data:image\/svg\+xml/.test(SRCNC));
  check('标题层级不跳级（有 h1/h2，没有 h3 起跳）',
    /<h1[\s>]/.test(SRCNC) && !/<h3[\s>]/.test(SRCNC));

  /* ================================================================
     19. 按住说话：失败后必须真的能重试
     这是评测里点名的"全站唯一死胡同"：结果区写着"轻触这里可以重试"，
     轻触却毫无反应。修法是让 sendAudio 失败时把重发注册成 pendingRetry。
     断言必须落在"真的又发了一次请求"上 —— 只断言文案里出现"重试"
     是测不到这个 bug 的（修之前那句文案本来就在）。
     ================================================================ */
  console.log('\n--- 按住说话：失败后的重试通道 ---');
  const FAKE_STREAM2 = { getTracks: () => [{ stop() {} }] };

  async function holdToTalk(build) {
    const d = makeDom(build);
    const w = d.window, doc = w.document;
    await sleep(150);
    doc.querySelector('#btnVoice').dispatchEvent(new w.Event('pointerdown', { bubbles: true }));
    await sleep(900);
    w.dispatchEvent(new w.Event('pointerup'));
    await sleep(800);
    return { win: w, doc, spoken: w.__spoken };
  }

  const domAsrFail = await holdToTalk({
    mediaRecorder: true,
    asrText: '我想问路',
    getUserMedia: () => Promise.resolve(FAKE_STREAM2),
    failPost: { status: 503, detail: '服务暂时不可用，请稍后再试。' },
  });
  const asrFailText = domAsrFail.doc.querySelector('#result').textContent;
  check('语音识别失败时，结果区给出后端那句人话',
    asrFailText.includes('暂时不可用'), asrFailText.replace(/\s+/g, ' ').slice(0, 40));

  const nAsrFail = domAsrFail.win.__fetches.filter((u) => u.includes('/api/asr')).length;
  touch(domAsrFail.win, domAsrFail.doc.querySelector('#result'), 'touchend', ...INSIDE);
  await sleep(600);
  const nAsrRetry = domAsrFail.win.__fetches.filter((u) => u.includes('/api/asr')).length;
  check('轻触结果区真的会重发同一段音频（修前提示可重试、轻触却毫无反应）',
    nAsrRetry > nAsrFail, `/api/asr 请求数 ${nAsrFail} → ${nAsrRetry}`);
  domAsrFail.win.close();

  const domNoAsr = await holdToTalk({
    mediaRecorder: true,
    asrText: '',
    getUserMedia: () => Promise.resolve(FAKE_STREAM2),
    asrAvailable: false,
  });
  const noAsrText = domNoAsr.doc.querySelector('#result').textContent;
  check('后端没有听写引擎时，说的是"这里不能听写"而不是"没有听清"',
    noAsrText.includes('没有语音识别引擎') && !/没有听清/.test(noAsrText),
    noAsrText.replace(/\s+/g, ' ').slice(0, 40));
  check('并且明确给出替代出路（输入框 / 拍照按钮）',
    domNoAsr.spoken.some((s) => s && (s.includes('输入框') || s.includes('按钮'))),
    domNoAsr.spoken.filter(Boolean).join(' / '));
  domNoAsr.win.close();

  /* ================================================================
     20. 取景质检：拍完之后、上传之前，先回答"这张能不能用"
     这是 2026-10-07 交互评审的 P0-1，也是与同类产品差距最大的一环。
     明眼用户拍糊了看一眼预览就知道重拍，视障用户**没有任何办法自我纠正**：
     糊图 / 镜头被手指挡住 / 口袋误触都会照常上传，然后 5~8 秒后长出一段
     "荒谬但合理"的结论（全黑图换来一句"暂无明显障碍"）—— 而用户听到的
     是一句通顺的话，没有任何迹象指向照片本身，他会认为软件坏了。

     阈值本身的正确性由 code/frontend/shot_probe.js 在真实源码上把关；
     这里测的是**接线**：拦不拦得住、拦住了说不说人话、算不出来会不会误拦。
     ================================================================ */
  console.log('\n--- 取景质检（拍糊 / 太暗 / 挡镜头）---');

  /* 强制走系统相机通道（mediaDevices=false ⇒ photoMode='capture'），
     因为网页取景那条路要过 video+toBlob，在 jsdom 里没有意义；
     而质检对两条路是同一个函数，用哪条路验接线都成立。 */
  async function shootOnce(build) {
    const d = makeDom(Object.assign({ mediaDevices: false }, build));
    const w = d.window, doc = w.document;
    await sleep(150);
    touch(w, doc.querySelector('#gate'), 'touchend', ...INSIDE);
    await sleep(1200);
    const b = doc.querySelector('#btnScene');
    fakeRect(b, 100, 50);
    touch(w, b, 'touchstart', ...INSIDE);
    touch(w, b, 'touchend', ...INSIDE);
    await sleep(120);
    await finishPendingPhoto(w, new w.File(['x'], 'a.jpg', { type: 'image/jpeg' }));
    await sleep(300);
    return {
      win: w, doc,
      text: doc.querySelector('#result').textContent,
      posts: () => w.__fetches.filter((u) => u.includes('/api/understand')).length,
      spoken: w.__spoken,
    };
  }
  const flat = (s) => s.replace(/\s+/g, ' ').slice(0, 46);

  const okShot = await shootOnce({ shot: 'ok' });
  check('正常照片照常上传（质检是增益，不能变成新的关卡）',
    okShot.posts() > 0, '/api/understand 请求数 ' + okShot.posts());
  okShot.win.close();

  const darkShot = await shootOnce({ shot: 'dark' });
  check('全黑画面**不发请求**（否则 5~8 秒后长出一段基于黑图的"合理"结论）',
    darkShot.posts() === 0, '/api/understand 请求数 ' + darkShot.posts());
  check('并且说清是"太暗"以及该怎么做（不是只说"画面不清楚"）',
    /太暗/.test(darkShot.text) && /再拍/.test(darkShot.text), flat(darkShot.text));
  check('拦截时同时走语音通道（全盲用户看不到结果区那行字）',
    darkShot.spoken.some((s) => s && s.includes('太暗')),
    darkShot.spoken.filter(Boolean).join(' / ') || '（无播报）');
  darkShot.win.close();

  const blankShot = await shootOnce({ shot: 'blank' });
  check('镜头被挡 / 对着白墙（画面里没有内容）被拦下，并提示擦镜头',
    blankShot.posts() === 0 && /镜头/.test(blankShot.text),
    '/api/understand ' + blankShot.posts() + ' 次 ｜ ' + flat(blankShot.text));
  blankShot.win.close();

  const blurShot = await shootOnce({ shot: 'blur' });
  check('明显失焦被拦下，并给出"稳住一秒"这种能马上照做的指令',
    blurShot.posts() === 0 && /糊/.test(blurShot.text) && /稳/.test(blurShot.text),
    '/api/understand ' + blurShot.posts() + ' 次 ｜ ' + flat(blurShot.text));
  blurShot.win.close();

  const brightShot = await shootOnce({ shot: 'bright' });
  check('过曝（对着灯 / 反光）被拦下',
    brightShot.posts() === 0 && /太亮/.test(brightShot.text), flat(brightShot.text));
  brightShot.win.close();

  /* 没有 canvas 能力 / 解不开图时必须**放行**。
     质检算不出来就照旧上传 —— 绝不能让"检不了"变成"用不了"。
     这条同时是给老浏览器和 WebView 兜底：它们在视障用户手里并不少见。 */
  const noCapShot = await shootOnce({});
  check('算不出质检时照旧上传（没有 canvas / 解不开图都不能把功能挡死）',
    noCapShot.posts() > 0, '/api/understand 请求数 ' + noCapShot.posts());
  noCapShot.win.close();

  /* ================================================================
     21. 距离表：补齐它一直没有的无障碍入口（P0-3）
     原先 #dist 只在屏幕上画 <b>，既不在触摸探索里，也不是顺序导航的一项；
     而语音那条路，方位是一句连读的口语 —— 三个方位连着念过去，
     用户容易只记住最后一条。
     「距离不猜 + 时钟方位」是答辩主线，可它的产物得能在非视觉通道上逐条清点。
     ================================================================ */
  console.log('\n--- 距离表：全盲用户能不能听到全部方位 ---');

  const THREE_DIST = {
    speech: '注意，12点方向约 3 步有台阶。向正前方行走。',
    reply: '前方是走廊。【注意】12点方向约 3 步有台阶',
    model: 'test-model', mock: false,
    distances: [
      { bearing: '11点', steps: 6 },
      { bearing: '12点', steps: 3 },
      { bearing: '1点', steps: 9 },
    ],
  };

  const domD = makeDom({ mediaDevices: false, sceneReply: THREE_DIST });
  const winD = domD.window, docD = winD.document;
  await sleep(150);
  touch(winD, docD.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  await shootIn(winD, docD);
  /* 先把第一轮的全部播报存下来再清空 —— 后面几条断言会各自清空 __spoken，
     不清就分不清"哪一句是这一轮说的"。 */
  const firstRound = winD.__spoken.filter(Boolean).slice();

  const distEl = docD.querySelector('#dist');
  check('距离表把三条方位都画出来了（视觉通道本来就是完整的）',
    distEl.querySelectorAll('b').length === 3,
    '共 ' + distEl.querySelectorAll('b').length + ' 条');

  const resLabelD = docD.querySelector('#result').getAttribute('aria-label') || '';
  check('有结果时，结果区的名字说的是"重听"（读屏用户据此知道这一下会发生什么）',
    /重听/.test(resLabelD), resLabelD);

  /* 关键的一条：摸到距离表能听到**全部**方位，而不只是最近那一个。 */
  winD.__spoken.length = 0;
  fakeRect(distEl, 320, 40, 0, 420);
  touch(winD, distEl, 'touchstart', 50, 425);
  await sleep(200);
  const distSpeech = winD.__spoken.filter(Boolean).join(' ｜ ');
  check('摸到距离表会念出全部方位（以前它是个触摸死区，什么都听不到）',
    distSpeech.includes('11点') && distSpeech.includes('12点') && distSpeech.includes('1点'),
    distSpeech || '（无播报）');
  check('并且说明一共有几条（否则用户不知道后面还有没有）',
    /共 3 条/.test(distSpeech), distSpeech || '（无播报）');

  /* 语音直达：用户不必先摸到那个区域，说一句就行 */
  winD.__spoken.length = 0;
  winD.runVoiceRoute(winD.findVoiceRoute('距离表'));
  await sleep(200);
  check('说「距离表」就能听到全部方位（语音直达，不用先在屏幕上找到它）',
    winD.__spoken.filter(Boolean).join(' ').includes('11点'),
    winD.__spoken.filter(Boolean).join(' / ') || '（无播报）');

  /* 结果播完之后只补**一句**提示，轮流说，各不超过两次。
     第一次先说"可以找人"（产品结构里最缺的一环），第二次才轮到"方位可以再听一遍"。 */
  check('第一次结果后补的是"长按执行键可以找人"（这条路以前存在但没人知道）',
    firstRound.some((s) => /长按/.test(s) && /找人/.test(s)),
    firstRound.join(' / '));

  winD.__spoken.length = 0;
  await shootIn(winD, docD);
  /* 播报额度提到 80 字后，方位本身已经在「注意」段里念全了，
     所以这句不能再是"另外还有 N 个方向"——那是**错的**。
     它改口成"没听清可以再说一次距离表"：距离表是结构化复读，逐条可清点。 */
  check('第二次结果后改口提"方位没听清可以说「距离表」"（不能说"还有 N 个方向"）',
    winD.__spoken.some((s) => s && /距离表/.test(s) && /听清|再听/.test(s)),
    winD.__spoken.filter(Boolean).join(' / '));
  check('不再有"还有 N 个方向"这种与事实不符的说法',
    !winD.__spoken.some((s) => s && /还有 \d+ 个方向/.test(s)),
    winD.__spoken.filter(Boolean).join(' / '));
  check('两句不同时说（连起来念会明显拉长一次播报，而用户正在走）',
    !winD.__spoken.some((s) => s && /距离表/.test(s) && /找人/.test(s)),
    winD.__spoken.filter(Boolean).join(' / '));
  domD.window.close();

  /* 空距离表：它仍然是一个"能被摸到、且说得出自己是什么"的区域，
     不能因为没数据就变成"这里是空白"。 */
  const domD0 = makeDom({
    sceneReply: { speech: '走廊里没有障碍', reply: '走廊', model: 'm', distances: [] },
  });
  const winD0 = domD0.window, docD0 = winD0.document;
  await sleep(150);
  const d0 = docD0.querySelector('#dist');
  fakeRect(d0, 320, 40, 0, 420);
  winD0.__spoken.length = 0;
  touch(winD0, d0, 'touchstart', 50, 425);
  await sleep(200);
  check('没有距离数据时，摸到它听到的是"距离表，当前没有数据"，不是"这里是空白"',
    winD0.__spoken.some((s) => s && s.includes('距离表') && s.includes('没有距离数据'))
    && !winD0.__spoken.some((s) => s && s.includes('空白')),
    winD0.__spoken.filter(Boolean).join(' / ') || '（无播报）');
  domD0.window.close();

  /* ================================================================
     22. 结果区的语义不能靠"位置"区分（P1-3）

     同一个位置、同一个手势：成功时 = 重听，失败时 = 重试上一次操作。
     区别只写在 `.retap` 那行视觉小字里 —— 对看不见屏幕的用户就是纯粹的歧义：
     他摸到结果区，不知道这一下会重播上一次结论，还是又发一次请求。
     修法不是砍掉重试（户外信号差时它很有用），而是**让名字说清是哪一种**。
     ================================================================ */
  console.log('\n--- 结果区语义：同一个位置两件事，靠名字说清 ---');

  const domHF = makeDom({
    mediaDevices: false,
    failPost: { status: 503, detail: '服务暂时不可用，请稍后再试。' },
  });
  const winHF = domHF.window, docHF = winHF.document;
  await sleep(150);
  touch(winHF, docHF.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  await shootIn(winHF, docHF);
  const resHF = docHF.querySelector('#result');
  /* 必须先给结果区一个真实尺寸。jsdom 不做布局，getBoundingClientRect 一律全 0，
     触摸探索会因为它"不可见"而跳过 —— 那样 nameOf() 根本不会被调用，
     断言读到的是绑定时那个初始值，测了半天等于没测。
     （元素自己的 touchend 不受影响，所以"重试"会照常触发。这个差异是 10-07 踩到的。） */
  fakeRect(resHF, 320, 60, 0, 300);
  /* 名字要**现算**：同一个元素在失败态下必须换说法。
     写死成"重听"的话，用户听到"重听"、做出来的却是"重试"。 */
  touch(winHF, resHF, 'touchstart', 50, 325);
  const resLabelHF = resHF.getAttribute('aria-label') || '';
  check('失败之后，结果区的名字改成"重试"（同一位置两件事，名字必须说清是哪一件）',
    /重试/.test(resLabelHF) && !/重听/.test(resLabelHF), resLabelHF);
  check('失败提示指向"屏幕中间的结果区"而不是"这里"（"这里"是他手指现在的位置）',
    /屏幕中间的结果区/.test(resHF.textContent), flat(resHF.textContent));
  domHF.window.close();

  /* ================================================================
     23. 结果必须"跟着屏幕"（P0）

     用户点的是**下方**的按钮（拍照 / 读文字 / 下一步），结果出现在上方。
     原来靠 revealEl() 把结果滚回视野 —— 它解决的是"结果出来了没看见"，
     解决不了"看完又滑走了"：用户往下摸下一个功能，结果就留在身后，
     想再看一眼只能再滚回去。

     新契约：刚拍的照片 + 识别结果 + 距离表一起住进吸顶容器 #live，
     页面滚到哪儿它们都停在视口顶端 —— 于是"需要滚动"这件事本身消失了。
     ================================================================ */
  console.log('\n--- 结果跟随屏幕（吸顶常驻） ---');
  const SRC_RV = fs.readFileSync(HTML, 'utf8');

  const liveCss = (SRC_RV.match(/\.live\{[\s\S]*?\n  \}/) || [''])[0];
  check('跟屏区 sticky 吸顶且贴在顶端（top:0）—— 页面滚到哪儿它都还在',
    /position\s*:\s*sticky/.test(liveCss) && /top\s*:\s*0/.test(liveCss),
    liveCss.slice(0, 58).replace(/\s+/g, ' ') || '（没找到 .live 样式）');

  const domRv = makeDom({});
  const wRv = domRv.window, dRv = wRv.document;
  await sleep(150);
  touch(wRv, dRv.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  const resRv = dRv.querySelector('#result');
  const distRv = dRv.querySelector('#dist');
  const shotRv = dRv.querySelector('#shot');
  const liveRv = dRv.querySelector('#live');

  check('识别结果、距离表、刚拍的照片都住在跟屏区里（三者一起跟着屏幕走）',
    !!liveRv && !!resRv && resRv.closest('.live') === liveRv
      && !!distRv && distRv.closest('.live') === liveRv
      && !!shotRv && shotRv.closest('.live') === liveRv,
    [resRv, distRv, shotRv].map((e) => (e ? '#' + e.id : '(缺)')).join(' | '));

  /* 「按住说话」的**位置**在底栏、**身份**仍是普通功能项：
     它在 #dock 里（固定底栏第一行），但不在 <nav> 里 ——
     顺序导航会把 <nav> 内的元素整批跳过（见 navigableTargets），
     为了"看起来在底栏"把它塞进 <nav>，它就会从顺序导航里消失。 */
  const bvRv = dRv.querySelector('#btnVoice');
  check('「按住说话」搬进了固定底栏（不必往下滚两屏才摸得到）',
    !!bvRv && !!dRv.querySelector('#dock') && bvRv.closest('#dock') === dRv.querySelector('#dock'),
    bvRv && bvRv.closest('#dock') ? '在 #dock 内' : '不在底栏里');
  check('但它没被算作"导航控制器"（否则顺序导航整批跳过它，全盲用户再也走不到）',
    !!bvRv && !bvRv.closest('#nav'),
    bvRv && bvRv.closest('#nav') ? '被放进了 #nav —— 会从顺序导航里消失' : '在 #nav 之外');
  check('「按住说话」用描边而非主色实心（底栏里只能有一个实心黄 = 执行）',
    !!bvRv && /sec/.test(bvRv.getAttribute('class') || ''),
    'btnVoice class=' + (bvRv ? bvRv.getAttribute('class') : '(缺)'));

  /* 首屏说明：一整段 100 字长句 → 三条要点。 */
  const guideLi = dRv.querySelectorAll('.guide li');
  check('首屏说明拆成三条要点（原来一整段 5 行，跳行后找不回自己读到哪儿）',
    guideLi.length === 3,
    Array.prototype.map.call(guideLi, (li) => flat(li.textContent).slice(0, 14)).join(' / '));
  const guideCss = (SRC_RV.match(/\.guide li\{[\s\S]*?\n  \}/) || [''])[0];
  check('说明的字号比原来大（13.5px → 15px；它教的是"怎么用"，不该是全页最小的字）',
    /font-size\s*:\s*15px/.test(guideCss), guideCss.slice(0, 46).replace(/\s+/g, ' '));

  /* 跟屏区里的元素恒可见，**不该再触发滚动**：
     用户正站在下方按按钮，屏幕却自己弹回顶部 —— 低视力用户会看晕，
     全盲用户会以为自己按错了。这比"看不见结果"更糟。 */
  wRv.__scrolls.length = 0;
  fakeRect(resRv, 320, 100, 0, -600);
  wRv.setResult('第一段结果');
  await sleep(80);
  check('结果更新时不再滚动页面（它本来就贴在顶端，滚一下反而是打扰）',
    wRv.__scrolls.length === 0, 'scrollIntoView 被调用 ' + wRv.__scrolls.length + ' 次');

  /* 手机首屏的真实情形：页面还没滚动，跟屏区留在说明区底下，
     下半截正被底栏压住 —— 实测"正文只露出一行，其余全在底栏后面"。
     这时必须把它顶到视口顶端让 sticky 生效，而不是指望用户自己去滚。
     （上面那条断言的前提是"跟屏区已经完全露出来"，两条合起来才完整：
       完整可见就别动，被挡住就顶上去。） */
  fakeRect(dRv.querySelector('#dock'), 320, 140, 0, 628);   // 底栏实测 140px
  fakeRect(liveRv, 320, 400, 0, 400);                       // bottom=800 > 768-140-8
  fakeRect(resRv, 320, 100, 0, 420);
  wRv.__scrolls.length = 0;
  wRv.setResult('第二段结果');
  await sleep(80);
  check('跟屏区下半截被底栏挡住时，把它顶到顶端吸住（否则结果永远只露一行）',
    wRv.__scrolls.length === 1 && wRv.__scrolls[0].el === liveRv
      && !!wRv.__scrolls[0].opts && wRv.__scrolls[0].opts.block === 'start',
    '调用 ' + wRv.__scrolls.length + ' 次，目标 ' +
      (wRv.__scrolls[0] && wRv.__scrolls[0].el && wRv.__scrolls[0].el.id));

  /* 底栏高度必须只写一处、而且要是**实测值**：
     页面底部留白与跟屏区上限都从它算出来，写死两个数字早晚各自跑偏。 */
  const rootCss = (SRC_RV.match(/:root\{[\s\S]*?\n  \}/) || [''])[0];
  check('底栏高度是 CSS 变量 --dock-h（页面留白与跟屏区上限共用同一个来源）',
    /--dock-h\s*:\s*\d+px/.test(rootCss), (rootCss.match(/--dock-h[^;]*/) || [''])[0]);
  check('底栏高度由 JS 实测写入（系统字号 / 安全区 / 再加一个键都会让它变）',
    /function syncDockH/.test(SRC_RV) && /--dock-h['"]\s*,\s*h\s*\+\s*['"]px/.test(SRC_RV),
    'syncDockH + setProperty(--dock-h)');
  check('页面底部留白按底栏高度算，不是写死的数字',
    /padding:16px 16px calc\(var\(--dock-h\)/.test(SRC_RV),
    (SRC_RV.match(/padding:16px 16px [^;]*/) || [''])[0]);
  check('跟屏区在还没出结果时不吸顶（首屏不该被一块空占位先吃掉）',
    /\.live\.empty\{position:static\}/.test(SRC_RV)
      && /class="live empty"/.test(SRC_RV),
    /class="live empty"/.test(SRC_RV) ? '初始为空态' : '初始不是空态');

  /* revealEl 的能力本身不能丢：顺序导航把焦点项滚进视野、
     以及将来任何**跟屏区之外**的滚动，都还要靠它。
     用一个跟屏区之外的临时元素，把这几条契约继续钉住。 */
  const tmpRv = dRv.createElement('div');
  tmpRv.setAttribute('tabindex', '-1');
  dRv.body.appendChild(tmpRv);
  fakeRect(tmpRv, 320, 100, 0, -600);
  wRv.__scrolls.length = 0;
  wRv.revealEl(tmpRv);
  await sleep(80);
  check('跟屏区之外的元素仍会被滚进视野（能力没删掉，只是结果不再需要它）',
    wRv.__scrolls.length === 1, '调用 ' + wRv.__scrolls.length + ' 次');
  check('滚动目标是该元素本身，且用 block:center 居中',
    !!wRv.__scrolls[0] && wRv.__scrolls[0].el === tmpRv
      && !!wRv.__scrolls[0].opts && wRv.__scrolls[0].opts.block === 'center',
    JSON.stringify(wRv.__scrolls[0] && wRv.__scrolls[0].opts));
  check('滚动**不抢焦点**（读屏正在念的内容不能被自己的滚动打断）',
    dRv.activeElement !== tmpRv,
    '当前焦点 ' + (dRv.activeElement && (dRv.activeElement.id || dRv.activeElement.tagName)));
  domRv.window.close();

  /* ③ 系统开了"减弱动态效果" → 不做平滑滚动。
     前庭功能敏感的用户开了这个开关之后，平滑滚动会让他不适。 */
  const domRm = makeDom({ reduceMotion: true });
  const wRm = domRm.window, dRm = wRm.document;
  await sleep(150);
  touch(wRm, dRm.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  const tmpRm = dRm.createElement('div');
  dRm.body.appendChild(tmpRm);
  fakeRect(tmpRm, 320, 100, 0, -600);
  wRm.__scrolls.length = 0;
  wRm.revealEl(tmpRm);
  await sleep(80);
  check('系统开了"减弱动态效果"时改为瞬间定位（平滑滚动会让前庭敏感的用户不适）',
    !!wRm.__scrolls[0] && wRm.__scrolls[0].opts && wRm.__scrolls[0].opts.behavior === 'auto',
    wRm.__scrolls[0] ? JSON.stringify(wRm.__scrolls[0].opts) : '（没有滚动）');
  domRm.window.close();

  /* ④ 一次操作里结果区与距离表都要更新 —— 只能滚一次。
     各滚一次就是"跳两下"，比不滚还让人晕。 */
  const domOnce = makeDom({});
  const wOnce = domOnce.window, dOnce = wOnce.document;
  await sleep(150);
  touch(wOnce, dOnce.querySelector('#gate'), 'touchend', ...INSIDE);
  await sleep(1200);
  const tA = dOnce.createElement('div'), tB = dOnce.createElement('div');
  dOnce.body.appendChild(tA);
  dOnce.body.appendChild(tB);
  fakeRect(tA, 320, 100, 0, -800);
  fakeRect(tB, 320, 40, 0, -650);
  wOnce.__scrolls.length = 0;
  wOnce.revealEl(tA);          // 同一轮里的第一个目标
  wOnce.revealEl(tB);          // 第二个目标紧跟其后（结果区 + 距离表就是这样连着来的）
  await sleep(80);
  check('同一轮里两个目标都要滚时只滚一次（各滚一次就是"跳两下"）',
    wOnce.__scrolls.length === 1, '调用 ' + wOnce.__scrolls.length + ' 次');
  check('滚的目标取最后进来的那个（页面位置更靠下，两个一起进视野）',
    !!wOnce.__scrolls[0] && wOnce.__scrolls[0].el === tB,
    wOnce.__scrolls[0] && wOnce.__scrolls[0].el === tB ? '取到了第二个目标' : '（不是第二个目标）');
  domOnce.window.close();

  /* ================================================================
     24. 录音必须转成 WAV 再上传（P0）

     上游的语音识别只收 wav / mp3，而 MediaRecorder 录的是 webm/opus。
     实测 webm 直传就是 400 / code 1214 —— 这才是"按住说话没反应"的根因。
     字节头的正确性由 wav_probe.js 在真实源码上单独把关（jsdom 没有
     AudioContext，转码真身在这里跑不到）；这里管的是**接线**：
     转出来的东西是不是真的被当成 wav 发出去了。
     ================================================================ */
  console.log('\n--- 录音转 WAV 上传 ---');
  const domWv = await holdToTalk({
    mediaRecorder: true, audioContext: true, recMime: 'audio/webm;codecs=opus',
    asrText: '看看周围', getUserMedia: () => Promise.resolve(FAKE_STREAM2),
  });
  const asrInits = domWv.win.__fetchInits.filter((i) => i.url.includes('/api/asr'));
  const lastInit = asrInits[asrInits.length - 1];
  const audioFile = lastInit && lastInit.init && lastInit.init.body
    && lastInit.init.body.get ? lastInit.init.body.get('audio') : null;
  check('上传的是转码后的 WAV（webm 直传会被上游 400 拒收）',
    !!audioFile && audioFile.type === 'audio/wav',
    audioFile ? audioFile.type : '（没拿到音频文件）');
  check('文件名也写成 .wav（上游按真实格式判，扩展名与内容必须一致）',
    !!audioFile && /\.wav$/.test(audioFile.name || ''),
    audioFile ? audioFile.name : '（没拿到文件名）');
  domWv.win.close();

  /* 转码失败（老内核 / 解码器缺席）时必须**安全退回**原格式上传，
     让后端把"格式不支持"翻成人话 —— 在这里抛异常最糟：
     录音已经结束，用户什么反馈都收不到。 */
  const domNoAc = await holdToTalk({
    mediaRecorder: true, recMime: 'audio/webm',
    asrText: '看看周围', getUserMedia: () => Promise.resolve(FAKE_STREAM2),
  });
  const noAcInits = domNoAc.win.__fetchInits.filter((i) => i.url.includes('/api/asr'));
  const noAcFile = noAcInits.length
    ? noAcInits[noAcInits.length - 1].init.body.get('audio') : null;
  check('没有解码器时不崩、改为原样上传（后端会把"格式不支持"翻译成人话）',
    !!noAcFile && !!noAcFile.size,
    noAcFile ? (noAcFile.type + ' / ' + noAcFile.size + 'B') : '（没有发请求）');
  domNoAc.win.close();

  /* ================================================================
     25. 录音最长时长（P0）

     上游硬限制 30 秒，而"按住不放"这个手势很容易被卡住：
     手抖、走神、被路况打断。没有上限的话，用户会对着手机说上两分钟，
     最后收到一句错误提示 —— 而全程他都不知道已经超了。
     ================================================================ */
  console.log('\n--- 录音最长时长 ---');
  const domLim = makeDom({ mediaRecorder: true, getUserMedia: () => Promise.resolve(FAKE_STREAM2) });
  const wLim = domLim.window, dLim = wLim.document;
  await sleep(150);
  const vbLim = dLim.querySelector('#btnVoice');
  fakeRect(vbLim, 320, 64, 0, 700);
  vbLim.dispatchEvent(new wLim.Event('pointerdown', { bubbles: true }));
  await sleep(900);
  check('（前置）录音已经开始', /正在录音/.test(dLim.querySelector('#result').textContent),
    dLim.querySelector('#result').textContent.slice(0, 24));
  const longTimers = wLim.__timers.filter((t) => t.ms >= 20000);
  check('录音设了最长时长 25 秒（上游 30 秒硬上限，超了必被拒收）',
    longTimers.length >= 1 && longTimers[longTimers.length - 1].ms === 25000,
    longTimers.map((t) => t.ms).join(',') || '没有长定时器');
  wLim.__spoken.length = 0;
  longTimers[longTimers.length - 1].fn();                 // 模拟到点
  await sleep(250);
  check('到点自动收尾并说明原因（不能让用户一路说到被拒收）',
    wLim.__spoken.some((s) => s && /25 秒/.test(s)),
    wLim.__spoken.filter(Boolean).join(' / '));
  check('录音按钮复位成"按住说话"（否则用户以为还在录、一直举着手机）',
    /按住说话/.test(vbLim.textContent), vbLim.textContent);
  wLim.dispatchEvent(new wLim.Event('pointerup'));
  domLim.window.close();

  /* ================================================================
     26. 语音指令覆盖面（P0）

     "语音控制"只覆盖一部分功能，等于给一部分功能开了后门。
     而顺序冲突是这套路由最容易错、又最难被发现的地方：
     用户说"下一步"，得到的是"焦点往下移了一格"，SOP 一动不动 ——
     他只会得出"语音不好使"的结论。
     ================================================================ */
  console.log('\n--- 语音指令覆盖面 ---');
  const domVc = makeDom({});
  const wVc = domVc.window, dVc = wVc.document;
  await sleep(150);
  const nameOf = (t) => { const r = wVc.findVoiceRoute(t); return r ? r.name : ''; };

  check('「下一步」命中"已到达，直接下一步"，而不是"下一个功能"（顺序错了功能就悄悄变了）',
    nameOf('下一步') === '已到达，直接下一步', nameOf('下一步'));
  check('「下一个功能」才命中顺序导航', nameOf('下一个功能') === '下一个功能', nameOf('下一个功能'));
  check('「退回上一步」优先于「退出」', nameOf('退回上一步') === '退回上一步', nameOf('退回上一步'));
  check('「读一下这盒药」优先命中药品类型，而不是通用读文字',
    /药品/.test(nameOf('读一下这盒药')), nameOf('读一下这盒药'));
  check('「别念了」命中停止播报（必须排首位，否则会被别的规则抢先执行）',
    nameOf('别念了') === '停止播报', nameOf('别念了'));

  /* 覆盖面：页面上每个主要功能都要能用一句话到达 */
  const CAN_ALL = [
    ['看看周围', '拍照看看周围'],
    ['读一下文字', '拍照读文字'],
    ['读一下这个价签', '读文字，价签'],
    ['我要去医院', '就医引导'],
    ['坐公交', '乘车引导'],
    ['帮我叫个人', '呼叫人工志愿者'],
    ['我想找人', '呼叫人工志愿者'],
    ['距离表', '听全部距离方位'],
    ['重听一遍', '重听上一次播报'],
    ['别念了', '停止播报'],
    ['慢一点', '说得慢一点'],
    ['快一点', '说得快一点'],
    ['上一步', '退回上一步'],
    ['退出', '退出流程'],
    ['上一个功能', '上一个功能'],
    ['下一个功能', '下一个功能'],
  ];
  const missV = CAN_ALL.filter(([say, want]) => nameOf(say) !== want)
    .map(([say, want]) => say + '→' + (nameOf(say) || '没命中') + '（应为 ' + want + '）');
  check('页面上每个主要功能都能用一句话到达（不再有"只能用手点"的功能）',
    missV.length === 0, missV.length ? missV.join('；') : CAN_ALL.length + ' 条指令全部命中');

  /* 停止播报要真的打断，而不是"执行了一个不出声的动作" */
  wVc.__spoken.length = 0;
  wVc.speak('这是一段很长的播报');
  wVc.runVoiceRoute(wVc.findVoiceRoute('别念了'));
  check('说「别念了」会真的中断正在进行的播报',
    wVc.__spoken.includes('«打断»'), wVc.__spoken.filter(Boolean).join(' / '));

  /* "先选类型再执行"必须等类型播报念完：立刻点击的话，btnRead 里的
     speak 会把"已选择药品"当场掐断，用户永远听不到类型到底生效没有。 */
  wVc.__spoken.length = 0;
  wVc.runVoiceRoute(wVc.findVoiceRoute('读一下这盒药'));
  await sleep(400);
  check('语音选读文字类型时，先报"已选择药品"再执行',
    wVc.__spoken.some((s) => s && /已选择/.test(s)),
    wVc.__spoken.filter(Boolean).join(' / '));
  const drugChip = dVc.querySelector('#docChips .chip[data-t="drug"]');
  check('并把类型芯片真的选中了（视觉高亮与 aria-pressed 同步，语音与手点走同一条实现）',
    !!drugChip && drugChip.getAttribute('aria-pressed') === 'true',
    drugChip ? drugChip.outerHTML.slice(0, 60) : '（找不到药品芯片）');
  domVc.window.close();

  const passed = cases.filter((c) => c.pass).length;
  const report = {
    suite: '启明随行 · 前端无障碍交互测试（jsdom）',
    generated_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
    total: cases.length, passed, failed: cases.length - passed,
    pass_rate: Math.round((passed / cases.length) * 100) + '%',
    cases,
  };
  fs.writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
  console.log(`\n===== ${passed}/${cases.length} 通过 =====`);
  console.log(`报告已写入：${OUT}\n`);
  return passed === cases.length ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('测试异常：', e);
  process.exit(1);
});
