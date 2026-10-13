# -*- coding: utf-8 -*-
"""「启明随行」服务端入口

启动：
    pip install -r requirements.txt
    cp .env.example .env      # 填入 ZHIPU_API_KEY
    uvicorn main:app --reload --port 8000

无 Key 也能跑：在 .env 里设置 MOCK_MODE=true，所有模型调用走本地 mock，
方便先把手持端和交互流程调通。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core import pipeline, providers, sop
from core.config import (
    config,
    access_guard,
    depth_params,
    mock_active,
    model_manifest,
    speech_policy,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("qiming")

app = FastAPI(
    title="启明随行 API",
    description="面向视障人士的多模态环境理解与场景代办智能体",
    version="0.2.0",
)

# ================================================================
# 公网访问保护
#
# 为什么必须有：链接一旦发出去，谁拿到谁就能调用这个后端，而后端背后
# 是有限的免费额度与很低的并发上限。不加保护的后果不是抽象的"可能被滥用"，
# 而是**演示当天被陌生流量挤到 429** —— 恰恰是最不能出问题的时候。
#
# 三道闸全部由环境变量控制，默认全关（0 = 不限制）：
#   RATE_LIMIT_PER_MIN  单 IP 每分钟上限
#   MAX_CONCURRENCY     全局并发上限
#   DAILY_MAX_CALLS     全局每日累计上限（换 IP 也绕不过）
#
# 三个刻意的选择：
#   ① 只拦 /api/*，静态页面永远放行。否则用户看到的是一片打不开的空白，
#      连"为什么"都无从得知。
#   ② 拒绝时返回 **429 + 一句人话**，不用 403。前端会把 detail 原样显示并念出来，
#      "请求太频繁" 是用户能自己解决的，"没权限" 不是。
#   ③ 这段中间件注册在 CORS **之前**：中间件的执行顺序与注册顺序相反，
#      所以 CORS 会包在外面，连限流响应也带上跨域头，前端才读得到 detail。
# ================================================================
_rate: dict[str, list] = {}      # ip -> [窗口起点, 窗口内计数]
_concurrency = 0                 # 正在处理中的 /api 请求数
_day = [None, 0]                 # [日期, 当日累计调用数]

# 计费端点（会真正调用模型/ASR/深度的那些）。其余 /api（meta、sop/flows 等）
# 只受"是否已达上限"的约束，不再增加当日计数。
METERED_PATHS = {"/api/understand", "/api/read", "/api/depth", "/api/asr"}


def _too_many(msg: str) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": msg})


def _client_ip(request) -> str:
    """取真实客户端 IP。部署环境通常还有一层反向代理，
    此时 request.client 是代理的地址，所有用户会被算成同一个 IP——
    限流会退化成"一个人就能锁死全站"。所以优先读 X-Forwarded-For 的第一段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def access_gate(request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    global _concurrency
    now = time.time()

    # 访问口令：不依赖客户端身份，因此不受"IP 认不出来 / 进程实例不稳定"影响。
    # 刻意放行 GET /api/meta —— 前端加载时靠它决定要不要挂"演示模式"横幅，
    # 而且它不含任何模型调用；把它也拦住会让页面在最外层就变成一片空白，
    # 用户连"需要口令"这句话都看不到。
    if config.ACCESS_CODE and request.url.path != "/api/meta":
        got = request.headers.get("x-access-code") or request.query_params.get("k") or ""
        # 定长比较，不用 ==：字符串比较在第一个不同的字符处就返回，
        # 逐位试探可以还原出口令。
        #
        # 必须两侧都转成 bytes：compare_digest 对**含非 ASCII 的 str** 会抛
        # TypeError（"comparing strings with non-ASCII characters is not supported"）。
        # 而口令来自 URL 与请求头，任何人都能随手发一个中文进来 ——
        # 实测 `?k=中文口令` 会把这里变成 500。转 bytes 后比较语义不变，
        # 且不再有编码限制；外层再兜一层 try，保证"口令不对"永远只是 401。
        try:
            ok = secrets.compare_digest(got.encode("utf-8"),
                                        config.ACCESS_CODE.encode("utf-8"))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return JSONResponse(status_code=401, content={
                # 这句话会原样进播报，所以必须短（中文 TTS 约 4.5 字/秒）
                "detail": "缺少访问口令。请让分享者把带 ?k= 的完整链接再发一次。"
            })

    if config.DAILY_MAX_CALLS:
        today = time.strftime("%Y-%m-%d")
        if _day[0] != today:
            _day[0], _day[1] = today, 0
        if _day[1] >= config.DAILY_MAX_CALLS:
            return _too_many("今天的演示额度已经用完了，明天再来试。")

    if config.RATE_LIMIT_PER_MIN:
        ip = _client_ip(request)
        rec = _rate.get(ip)
        if rec is None or now - rec[0] >= 60:
            _rate[ip] = [now, 1]
        else:
            rec[1] += 1
            if rec[1] > config.RATE_LIMIT_PER_MIN:
                return _too_many("请求太频繁了，请等一分钟再试。")
        # 顺手清理过期记录：一个只增不减的字典就是一条内存泄漏，
        # 而内存泄漏同样属于"跑着跑着就不出声了"的静默故障。
        if len(_rate) > 5000:
            for k in [k for k, v in _rate.items() if now - v[0] >= 120]:
                _rate.pop(k, None)

    if config.MAX_CONCURRENCY and _concurrency >= config.MAX_CONCURRENCY:
        return _too_many("现在同时使用的人有点多，请稍后再试。")

    _concurrency += 1
    try:
        resp = await call_next(request)
    finally:
        # 必须放在 finally：请求中途抛异常时若忘记归还名额，
        # 并发计数会只增不减，几次之后整个服务就永久拒绝所有请求。
        _concurrency -= 1

    # 只有真正会打模型的端点才计入每日额度。把 /api/meta 也算进去的话，
    # 前端每次加载都会白白消耗额度（页面靠它决定要不要挂演示横幅），
    # 演示还没开始，额度就先少了一截。
    if config.DAILY_MAX_CALLS and request.url.path in METERED_PATHS:
        _day[1] += 1
    return resp


# 允许来源由 ALLOW_ORIGINS 决定（默认 "*" 方便联调，公网部署收敛为实际域名）。
# 前端是同源调用（API=''），所以收紧它不影响页面本身。
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# 安全响应头 + 压缩
#
# 注册在 CORS **之后** ⇒ 成为最外层，连 CORS / 限流的响应也带上这些头。
#
# 刻意**不加** X-Frame-Options / CSP 的 frame-ancestors：
# 这个作品的链接会被投到大赛平台上，评委那边可能在 iframe 里预览。
# 一帧被拒 = 页面完全打不开 = 作品判为"不可访问"，这个代价
# 远大于理论上的点击劫持风险（页面里没有敏感操作，唯一的"副作用"
# 是给志愿者派一张工单）。
# ================================================================
@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # 只在 HTTPS 下有效（浏览器按规范忽略 HTTP 收到的 HSTS），所以可以无条件发。
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # 接口一律不缓存；页面用 no-cache = 允许缓存但每次必须回源校验，
    # 既保证"改了代码刷新就能看到"，又让重复访问走 304（几乎不传字节）。
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    else:
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


# 单文件 H5 有 138 KB，不压缩在手机弱网下很吃亏。
# 放最外层，让静态页面与接口响应都受益。
app.add_middleware(GZipMiddleware, minimum_size=1024)

BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

# 上传大小上限。没有上限时一个超大文件会被完整读进内存，直接吃掉 worker 的内存。
# 12MB 远高于压缩后（≤768px）的实拍照片，正常使用不可能触到。
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

# 文本入参上限。没有上限时 query 会被原样拼进提示词：
# 既能把 token 成本与响应时间推到不可预期，也是一条明晃晃的提示注入通道。
MAX_QUERY_CHARS = 200
MAX_NOTE_CHARS = 200
MAX_ID_CHARS = 64

# 只认魔数，不信 Content-Type 也不信扩展名（两者都由客户端随便写）。
# 不校验的后果不是"报错"，而是把二进制垃圾 base64 后直发模型：
# 白花额度（降级链还会跑三次），拿回来一句"我看不清"，
# 而日志里完全看不出"用户传错了东西"。
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",         # JPEG
    b"\x89PNG\r\n\x1a\n",    # PNG
    b"GIF87a", b"GIF89a",    # GIF
    b"BM",                   # BMP
)


def _looks_like_image(data: bytes) -> bool:
    head = data[:16]
    if head.startswith(_IMAGE_MAGIC):
        return True
    # WEBP 是 RIFF 容器：RIFF....WEBP
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _ensure_image(data: bytes, field: str = "image") -> None:
    if not _looks_like_image(data):
        raise HTTPException(status_code=400, detail=f"{field} 不是有效的图片文件")


async def _read_upload(f: UploadFile, field: str = "image") -> bytes:
    """分块读取上传文件，边读边判大小。

    **不能**先 `await f.read()` 再判长度：那样会把整个文件先读进内存，
    传一个 500MB 的文件就会先吃掉等量内存才被拒绝 —— 而全局并发上限只有 2，
    一个超大文件足以把整个实例打爆。这里改成 64KB 一块地读，
    一超限立刻 413 停止读取，内存占用封顶在 limit + 一块。

    空文件也要挡：空 bytes 会一路传到模型调用处才失败，日志里看到的是一堆
    "模型调用异常"，而不是"用户没传图"，归因成本很高。
    """
    buf = bytearray()
    while True:
        chunk = await f.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{field} 超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限",
            )
    if not buf:
        raise HTTPException(status_code=400, detail=f"{field} 为空")
    return bytes(buf)


# ---------------------------------------------------------------- 元信息


@app.get("/api/meta")
def meta():
    """返回模型清单、深度参数与播报策略。设计文档的「工具平台介绍」章节直接引用此接口的返回。"""
    return JSONResponse({
        "service": "启明随行",
        "version": "0.2.0",
        "models": model_manifest(),
        "depth_params": depth_params(),
        "speech_policy": speech_policy(),
        "access_guard": access_guard(),
        "features": FEATURES,
    })


# 功能清单（与 results/功能清单.csv 同源，供前端动态渲染按钮与文档核对）
FEATURES = [
    {"id": "F01", "name": "拍照播报环境"},
    {"id": "F02", "name": "OCR 辅助读招牌"},
    {"id": "F03", "name": "距离播报（步数）"},
    {"id": "F04", "name": "拒绝编造"},
    {"id": "F05", "name": "追问"},
    {"id": "F06", "name": "药品说明朗读"},
    {"id": "F07", "name": "单据/指示牌/价签阅读"},
    {"id": "F08", "name": "语音意图路由"},
    {"id": "F09", "name": "就医引导（6步）"},
    {"id": "F10", "name": "SOP 节点达成校验"},
    {"id": "F11", "name": "乘车引导（3步）"},
    {"id": "F12", "name": "流程中途退出/重来"},
    {"id": "F13", "name": "超大按钮+大字号"},
    {"id": "F14", "name": "播报可打断"},
    {"id": "F15", "name": "填充语音"},
    {"id": "F16", "name": "呼叫人工志愿者"},
    {"id": "F17", "name": "主备模型自动降级"},
    {"id": "F18", "name": "MOCK 模式可演示"},
]


# ---------------------------------------------------------------- 主链路


@app.post("/api/understand")
async def understand(image: UploadFile = File(..., description="现场照片"),
                     query: str = Form("", max_length=MAX_QUERY_CHARS,
                                       description="用户语音转写文本"),
                     mode: str = Form("constrained", max_length=16,
                                      description="constrained|free（free 为评测对照组）")):
    """场景理解：拍照 -> 环境描述（含时钟方位与步数距离）

    **必须走线程池**：pipeline 内部是同步阻塞的（requests 直连模型，单次超时 8s，
    失败还要再试备用模型），直接在 async 路由里调用会占住事件循环，
    期间这台服务器上的**所有**请求都无法被处理（含静态前端）。
    单用户手动点两下看不出来，评测脚本一串行并发就会全卡住。
    """
    data = await _read_upload(image)
    _ensure_image(data)
    return JSONResponse(await run_in_threadpool(pipeline.understand_scene, data, query, mode))


@app.post("/api/read")
async def read(image: UploadFile = File(..., description="文字照片"),
               query: str = Form("", max_length=MAX_QUERY_CHARS),
               doc_type: str = Form("auto", max_length=16,
                                    description="drug|sign|price|doc|auto")):
    """文字阅读：拍药品 / 指示牌 / 价签 / 单据 -> 口语化结论（F06/F07）"""
    data = await _read_upload(image)
    _ensure_image(data)
    return JSONResponse(await run_in_threadpool(pipeline.read_text, data, query, doc_type))


@app.post("/api/depth")
async def depth_only(image: UploadFile = File(..., description="现场照片")):
    """只跑距离估计（F03），不调大模型。

    用途：① 答辩现场单独演示"距离不是模型猜的"；
          ② 评测时把深度误差与文案误差分开归因。
    """
    data = await _read_upload(image)
    _ensure_image(data)
    from core import depth as depth_mod
    ds = await run_in_threadpool(depth_mod.estimate, data)
    return JSONResponse({
        "distances": ds,
        "summary": depth_mod.summary(ds),
        "params": depth_params(),
    })


async def _intent_with_budget(text: str) -> dict:
    """语音链路的意图分类：**规则优先 + 给模型一个死预算**。

    为什么不能直接用 pipeline.classify_intent：它内部是一次 LLM 调用，
    超时按 8 秒算（三级降级最坏 24 秒，仍在托管网关 60 秒上限之内），
    而且免费模型限流时会先 429 再卡满超时。
    实测一次 `/api/asr` 因此跑了 **30.5 秒** —— 识别文本其实 2.3 秒就回来了。
    对"举着手机等回答"的视障用户，这 28 秒的差别就是"能用"与"像坏了"。

    规则能命中的指令（本项目绝大多数）在这里零延迟返回；只有规则没命中
    才给它一个短预算；超时/异常一律退回规则结果，绝不让语音链路被拖住。
    """
    hit = pipeline.keyword_intent(text)
    if hit:
        return hit
    try:
        return await asyncio.wait_for(
            run_in_threadpool(pipeline.classify_intent, text),
            timeout=config.INTENT_BUDGET_S,
        )
    except Exception:  # noqa: BLE001  （超时或异常都退回同一个确定性结果）
        return {"intent": "scene", "entities": {}}


@app.post("/api/asr")
async def asr(audio: UploadFile = File(...)):
    """语音转文字 + 意图路由（F08）

    三条结局必须分清，否则用户会在必然失败的路径上反复重试：
      · 识别成功但用户没说话   -> 200 + text 为空 + asr_available=true，前端说"没有听清"
      · 这段音频本身有问题     -> 400 + 一句人话（格式不被支持 / 太长 / 超时 / 额度）
      · 后端没有听写能力       -> 200 + asr_available=false，前端改口播"请用按钮"

    第 2 类原来会被压成空串混进第 1 类：用户听到"没有听清"就重说一遍，
    而真正的原因可能是这段录音的格式上游根本不认（前端没转码就会这样），
    再说一百遍也不会成功。
    """
    data = await _read_upload(audio, "audio")
    # 注意这里用模块级 mock_active()，不是 config.mock_active()。
    # 原来那行写的是 config.mock_active()（不存在的方法），却一直没暴露：
    # `and` 左边 available() 恒为 False 时直接短路，右边那句**从来没执行过**。
    # ASR 接上云端通道后 available() 变成 True，它立刻抛 AttributeError。
    # 这类"被短路掩盖的死代码"只有让条件真的成立才会现形。
    ok = providers.asr.available() and not mock_active()
    t0 = time.perf_counter()
    try:
        text = await run_in_threadpool(providers.asr.transcribe, data)
    except providers.ASRError as e:
        return JSONResponse(
            status_code=400,
            content={"detail": str(e), "asr_available": ok,
                     "backend": providers.asr.backend()},
        )
    intent = await _intent_with_budget(text)
    return JSONResponse({
        "text": text,
        "intent": intent,
        "asr_available": ok,
        "backend": providers.asr.backend(),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    })


@app.post("/api/route")
async def route(text: str = Form(..., max_length=MAX_QUERY_CHARS)):
    """纯文本意图路由（给不想传音频的场景用）"""
    return JSONResponse(await run_in_threadpool(pipeline.classify_intent, text))


# ---------------------------------------------------------------- 呼叫人工（F16）


@app.post("/api/help")
async def help_request(text: str = Form("", max_length=MAX_QUERY_CHARS,
                                        description="用户口述内容"),
                       reason: str = Form("", max_length=16,
                                          description="lost|blocked|unwell|query|other"),
                       session_id: str = Form("", max_length=MAX_ID_CHARS),
                       location_hint: str = Form("", max_length=100,
                                                 description="用户说的位置")):
    """呼叫人工志愿者。真人通道，不做智能包装，如实给出票号与电话。"""
    return JSONResponse(await run_in_threadpool(
        pipeline.request_help, text, reason, session_id, location_hint))


@app.get("/api/help/{ticket_id}")
async def help_status(ticket_id: str):
    from core import helpdesk
    t = helpdesk.get_ticket(ticket_id)
    if t is None:
        return JSONResponse({"error": "工单不存在"}, status_code=404)
    return JSONResponse(t)


@app.post("/api/help/{ticket_id}/finish")
async def help_finish(ticket_id: str, note: str = Form("", max_length=MAX_NOTE_CHARS)):
    from core import helpdesk
    t = helpdesk.finish_ticket(ticket_id, note)
    if t is None:
        return JSONResponse({"error": "工单不存在"}, status_code=404)
    return JSONResponse(t)


@app.get("/api/help")
async def help_list(limit: int = 20):
    """志愿者端/答辩演示用的求助列表"""
    from core import helpdesk
    return JSONResponse({"items": helpdesk.list_tickets(limit)})


# ---------------------------------------------------------------- SOP 智能体


@app.get("/api/sop/flows")
async def sop_flows():
    """列出全部可用流程（前端据此渲染入口：就医 / 乘车）"""
    return JSONResponse({
        "flows": [
            {"id": fid, "title": f["title"], "steps": len(f["steps"])}
            for fid, f in sop.FLOWS.items()
        ]
    })


@app.post("/api/sop/start")
async def sop_start(flow_id: str = Form("sop_medical", max_length=32),
                    session_id: str = Form("", max_length=MAX_ID_CHARS)):
    # 入口就挡：未知 flow_id 直接 400 并列出可选值。交给 sop.start_session
    # 抛 KeyError 的话，用户看到的是 500 + "Internal Server Error" ——
    # 既不知道错在哪，也不知道正确的取值是什么。
    if flow_id not in sop.FLOWS:
        return JSONResponse(status_code=400, content={
            "detail": f"没有这个流程：{flow_id}。可选：" + "、".join(sop.FLOWS),
        })
    sid = session_id or uuid.uuid4().hex[:12]
    s = sop.start_session(sid, flow_id)
    return JSONResponse({
        "session_id": sid,
        "flow_id": flow_id,
        "flow_title": s.flow["title"],
        "total_steps": len(s.flow["steps"]),
        "index": 1,
        "step": {"id": s.current["id"], "title": s.current["title"]},
        "speech": f"开始{s.flow['title']}。第一步：{s.current['title']}。{s.current['guide']}",
    })


@app.post("/api/sop/step")
async def sop_step(session_id: str = Form(..., max_length=MAX_ID_CHARS),
                   image: UploadFile | None = File(None),
                   manual_reached: str = Form("", description="跳过拍照校验，直接判定为达成")):
    """推进 SOP。支持两种模式：
       1) 传 image：由视觉校验是否达成；
       2) 传 manual_reached=1：用户/志愿者手动确认（无障碍兜底通道）。
    """
    s = sop.get_session(session_id)
    if s is None:
        return JSONResponse({"error": "会话不存在，请先调用 /api/sop/start"}, status_code=404)

    if s.finished:
        return JSONResponse(s.next_prompt(True))

    if manual_reached in ("1", "true", "yes"):
        reached, hint = True, ""
    elif image is not None:
        data = await _read_upload(image)
        _ensure_image(data)
        r = await run_in_threadpool(
            pipeline.check_sop_step, data, s.current["title"], s.current["success_signal"])
        reached, hint = bool(r.get("reached")), r.get("hint", "")
    else:
        return JSONResponse({"error": "需要提供 image 或 manual_reached"}, status_code=400)

    out = s.next_prompt(reached, hint)
    out["reached"] = reached
    return JSONResponse(out)


@app.post("/api/sop/back")
async def sop_back(session_id: str = Form(..., max_length=MAX_ID_CHARS)):
    """退回上一步（F11b）。

    独立的动作，不走 /api/sop/step —— 后者带 image/manual_reached 参数，
    语义是"校验是否到达"。把"我想退回去"塞进同一个端点，会让
    manual_reached=1（前进）与 back=1（后退）两个相反意图共用一个入口，
    前端任何一处参数写错就会静默反向执行，而用户完全听不出来。
    """
    s = sop.get_session(session_id)
    if s is None:
        return JSONResponse({"error": "会话不存在，请先调用 /api/sop/start"}, status_code=404)
    return JSONResponse(s.step_back())


@app.get("/api/sop/{session_id}")
async def sop_state(session_id: str):
    """查询当前进度（换设备/重连后用）"""
    s = sop.get_session(session_id)
    if s is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse({
        "session_id": session_id,
        "flow_id": s.flow_id,
        "flow_title": s.flow["title"],
        "finished": s.finished,
        "index": min(s.index + 1, len(s.flow["steps"])),
        "total_steps": len(s.flow["steps"]),
        "step": None if s.finished else {"id": s.current["id"], "title": s.current["title"]},
        "history": s.history,
    })


@app.delete("/api/sop/{session_id}")
async def sop_drop(session_id: str):
    """中途退出 / 重新开始（F12）"""
    sop.drop_session(session_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- 静态前端


try:
    # 用绝对路径，不用 "../frontend"。相对路径是相对**进程的工作目录**，
    # 只有恰好 cd 到 code/backend 启动才成立；换成从项目根
    # `uvicorn code.backend.main:app` 启动就会静默挂载失败（只留一行 warning），
    # 表现为"后端起来了但打开网页是 404"。
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
except Exception as e:  # 前端目录不存在时不阻塞后端启动
    logger.warning("静态前端未挂载（%s）：%s", FRONTEND_DIR, e)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
