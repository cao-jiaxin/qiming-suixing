# -*- coding: utf-8 -*-
"""核心编排 pipeline

四条主链路：
  1. understand_scene : 拍照问答 -> 环境描述（深度 + OCR + VL）  F01–F05
  2. read_text        : 拍照阅文 -> 文字口语化结论                F06–F07
  3. classify_intent  : 语音 -> 意图路由                          F08
  4. request_help     : 呼叫人工                                  F16

延迟治理（设计文档第四章要写）：
  - 图片压缩到 768px 再上传
  - OCR / 深度 并行发起；深度结果先算完再注入提示词
  - 模型返回前先向客户端推送"我看一下，请稍等"的填充语音，消除等待感
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import helpdesk, providers
from .config import config
from .prompts import (
    FREE_SYSTEM,
    FREE_USER_TEMPLATE,
    GUIDE_SYSTEM,
    GUIDE_USER_TEMPLATE,
    INTENT_SYSTEM,
    INTENT_USER_TEMPLATE,
    build_distance_block,
    build_read_system,
    build_read_user,
)

logger = logging.getLogger("qiming")

_executor = ThreadPoolExecutor(max_workers=4)

SECTION_RE = re.compile(r"【(前方情况|注意|建议)】")
STEP_RE = re.compile(r"(\d+)\s*步")
SECTION_KEYS = ("前方情况", "注意", "建议")
_NO_DANGER = ("无明显障碍", "没有明显障碍", "看不清", "距离不明", "未取得")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REQUEST_LOG = _PROJECT_ROOT / "cache" / config.REQUEST_LOG


# ---------------------------------------------------------------- 小工具


def _clip(text: str, limit: int | None = None) -> str:
    """兜底裁剪：控制播报长度，避免 TTS 念太久

    返回长度必须严格 ≤ limit。原先写的是 `text[:limit] + "。"`，
    补的句号会把结果顶到 limit+1 字——"播报不超过额度上限"是本项目写进
    文档与评测的硬约束，在边界上失守等于自打脸。
    """
    limit = limit or config.MAX_REPLY_CHARS
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    room = max(1, limit - 1)                     # 给句末标点留一格
    head = text[:room].rstrip("，、；： ")
    return ((head or text[:1]) + "。")[:limit]


def _strip_labels(text: str) -> str:
    """去掉【标签】，只留要念出来的内容（方块符号不适合语音播报）"""
    return re.sub(r"【[^】]*】", "", text or "").strip()


def _parse_sections(text: str) -> dict:
    """把三段式文本解析成 {前方情况, 注意, 建议}"""
    parts = {"前方情况": "", "注意": "", "建议": ""}
    cur = None
    for line in (text or "").splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            cur = m.group(1)
            parts[cur] = line.strip()[len(m.group(0)):].strip()
        elif cur and line.strip():
            parts[cur] += line.strip()
    return parts


def _nearest_steps(note: str) -> int | None:
    """从「注意」段里取出最近的距离（步）。无可信距离时返回 None。"""
    note = (note or "").strip()
    if not note or any(k in note for k in _NO_DANGER):
        return None
    nums = [int(x) for x in STEP_RE.findall(note)]
    return min(nums) if nums else None


SPEECH_MIN_FAR = 6          # 「前方情况」压缩后短于 6 字就不值得念，直接丢弃
# 三段保底：纯按优先级分配额度时，一句很长的「注意」会把预算吃光 ——
# 实测有过 38 字（"12点方向约6步有障碍，1点方向约6步有障碍，11点方向约15步有障碍"），
# 结果「建议」「前方情况」整段消失：用户听到了方位，反而没听到"该往哪走"。
# 故先给低优先级的段各留一份保底，剩下的才交给「注意」；
# 额度实在不够时自动退回纯优先级（安全段优先的原则不让步）。
SPEECH_FAR_FLOOR = 10       # 「前方情况」保底：用户得知道"这是什么地方"
SPEECH_ADVICE_FLOOR = 10    # 「建议」保底：行动指令必须念到
SPEECH_NOTE_FLOOR = 12      # 「注意」自身的最少额度，低于此不启用保底
_SPEECH_BREAKS = ("，", "；", "、", "：", " ")


def _end(s: str) -> str:
    """保证一段话以句末标点收尾，让 TTS 有正确的停顿"""
    s = (s or "").strip()
    if not s:
        return ""
    return s if s[-1] in "。！？" else s + "。"


def _trim(text: str, budget: int, hard: bool = False) -> str:
    """把一段话压进 budget 个字。

    :param hard: True 时允许在任意字符处截断（用于「建议」这类指令句，
                 断在半截仍然听得懂）。False 时只在标点处收尾，
                 找不到合适的断点就返回空串——**宁可不说，也不说半句话**。
    """
    text = (text or "").strip()
    if not text or budget <= 2:
        return ""
    if len(text) <= budget:
        return text
    room = budget - 1                       # 给句末标点留一格
    if room < 4:
        return ""
    head = text[:room]
    for p in _SPEECH_BREAKS:
        i = head.rfind(p)
        if i >= 0 and len(head[:i].rstrip("，、；： ")) >= SPEECH_MIN_FAR:
            return head[:i].rstrip("，、；： ") + "。"
    if hard:
        return head.rstrip("，、；： ") + "。"
    return ""



def build_speech(text: str, limit: int | None = None) -> dict:
    """三段式文本 -> 一段可直接念出来的话（F14 播报长度治理）

    设计依据（写进设计文档 4.x「多模态交互设计」）：
      屏幕文字与语音播报是两套长度标准。用户是**边走边听**，
      中文 TTS 约 4.5 字/秒 —— 所以播报要按段治理，而不是照读屏幕。
      额度 80 字：保证「前方情况 / 注意 / 建议」三段都念得到。

    三条规则，全部在代码里确定性完成，不交给模型自由发挥：

      1. 紧急重排：若「注意」段含 ≤SPEECH_NEAR_STEPS 步的障碍，把该句提到最前
         并加「注意，」前缀，保证最要命的一句先被听到；
      2. 预算分配：先为「建议」「前方情况」预留保底额度，剩下的按
         注意 > 建议 > 前方情况 分配 —— 三段都听得到，且安全信息不可压缩
         （额度极紧、装不下保底时，自动退回纯优先级）；
      3. 干净断开：压缩一律在标点处收尾，宁可整段不念，
         也绝不念出"走廊，光线尚。"这种半句话——半句话对视障用户是噪音。

    返回 {"speech", "emergency", "dropped"}，便于评测与文档引用中间量。
    """
    limit = limit or config.SPEECH_MAX_CHARS
    parts = _parse_sections(text)
    far, note, advice = parts["前方情况"], parts["注意"], parts["建议"]

    if not any((far, note, advice)):
        # 非三段式输出（如对照组 free 模式）：退回纯裁剪
        return {"speech": _clip(_strip_labels(text), limit),
                "emergency": False, "dropped": ["non-sectioned"]}

    n = _nearest_steps(note)
    emergency = n is not None and n <= config.SPEECH_NEAR_STEPS

    budget = limit
    dropped: list[str] = []

    def take(seg: str, prefix: str = "", hard: bool = False,
             cap: int | None = None) -> str:
        """在剩余额度内收下这一段；放不下就返回空串（不硬塞）

        :param cap: 本段可用的上限（在剩余额度之内再收一道），
                    用来给后面的段留住保底额度。
        """
        nonlocal budget
        seg = (seg or "").strip()
        if not seg or budget <= 3:
            return ""
        room = budget - len(prefix)
        if cap is not None:
            room = min(room, cap)
        if room <= 3:
            return ""
        cand = _trim(seg, room, hard)
        if not cand:
            return ""
        out = _end(prefix + cand)
        budget -= len(out)
        return out

    # 「注意」能用的额度 = 总额度 − 后两段的保底。
    # 只有额度够（>= 注意保底 + 两段保底）才启用，否则退回纯优先级。
    note_cap = None
    if note:
        floors = ((min(SPEECH_FAR_FLOOR, len(far)) if far else 0)
                  + (min(SPEECH_ADVICE_FLOOR, len(advice)) if advice else 0))
        if limit - floors >= SPEECH_NOTE_FLOOR:
            note_cap = limit - floors

    pieces: dict = {}
    # 优先级 1：注意 —— 安全信息，**永不丢弃**，即使只能硬截断也必须念出来
    got = take(note, prefix="注意，" if emergency else "", hard=True, cap=note_cap)
    if got:
        pieces["note"] = got
    elif note:
        dropped.append("note")
    # 优先级 2：建议（行动指令，允许在任意处硬截断——指令句半截仍可理解）
    got = take(advice, hard=True)
    if got:
        pieces["advice"] = got
    elif advice:
        dropped.append("advice")
    # 优先级 3：前方情况（描述性内容，只能标点处断开，否则整段不念）
    got = take(far)
    if got:
        pieces["far"] = got
    elif far:
        dropped.append("far")

    seq = ("note", "far", "advice") if emergency else ("far", "note", "advice")
    speech = "".join(pieces[k] for k in seq if k in pieces)
    if not speech:
        speech = _clip(_strip_labels(text), limit)
    return {"speech": speech, "emergency": emergency, "dropped": dropped}


def _ensure_three_sections(text: str) -> str:
    """确保输出包含三段结构；模型偶尔漏段时补齐，保证下游解析稳定

    判据必须**按标签去重**，不能数标签个数：模型偶尔会输出
    「【前方情况】【注意】【注意】」这种重复段，个数凑够 3 个，
    但「建议」整段缺失，而缺的恰好是用户真正要执行的那一句。
    """
    if set(SECTION_RE.findall(text or "")).issuperset(SECTION_KEYS):
        return text
    parts = _parse_sections(text)
    return (f"【前方情况】{parts['前方情况'] or '环境不够清楚'}\n"
            f"【注意】{parts['注意'] or '暂无明显障碍'}\n"
            f"【建议】{parts['建议'] or '请先停在原地，换个角度再拍一张'}")


def _log_request(record: dict) -> None:
    """可选请求日志：评测脚本靠它复算指标、算延迟分布"""
    if not config.LOG_REQUESTS:
        return
    try:
        _REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _REQUEST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.info("请求日志写入失败：%s", e)


# ---------------------------------------------------------------- 主链路 1：环境理解


def _degraded_scene_text(distances: list) -> str:
    """视觉模型全挂时的降级文案：**只讲本地算得出来的事**。

    为什么不干脆报一句"识别失败"：
    障碍的**方向和步数**根本不依赖大模型 —— 它来自深度估计 + 几何换算，
    全在本机算完（见 providers.depth）。对盲人来说，"12 点方向约 5 步有障碍"
    是这条链路里**最要命的那一句**；而"识别服务连不上"对他没有任何行动价值。
    所以模型挂了也要把方位报出来，同时说清图像描述这次没有。

    段落格式与正常输出保持一致（【前方情况】/【注意】/【建议】），
    这样 build_speech 的紧急重排与预算分配照旧生效 ——
    近处有障碍时，那句"注意"仍会被提到最前面先念。
    """
    ds = [d for d in (distances or []) if d.get("bearing") and d.get("steps")]
    head = "【前方情况】这次没能连上图像描述服务，只能报方位。"
    if not ds:
        return (head + "\n【注意】这次没测到障碍，但这不等于前方安全。"
                       "\n【建议】先停一下，再拍一张试试。")
    near = [d for d in ds if d["steps"] <= config.SPEECH_NEAR_STEPS]
    listed = "，".join(f"{d['bearing']}约{d['steps']}步" for d in ds)
    note = (f"【注意】{listed}，很近，先停下。"
            if near else f"【注意】{listed}，暂时没有很近的障碍。")
    return head + "\n" + note + "\n【建议】放慢脚步，别急着往前走。"


def understand_scene(image_bytes: bytes, query: str = "", mode: str = "constrained") -> dict:
    """拍照 -> 环境理解（F01 播报 / F02 OCR / F03 距离 / F04 拒答 / F05 追问）

    :param mode: constrained（默认，本系统）| free（对照组，无约束通用描述）
                 free 仅用于评测的对照实验（评测项 E14），生产环境不要用。
    """
    t0 = time.time()

    # OCR 与深度并行；VL 依赖深度结果，故放在后面串行
    f_ocr = _executor.submit(providers.ocr.extract, image_bytes)
    f_depth = _executor.submit(providers.depth.estimate, image_bytes)
    ocr_text = f_ocr.result() or ""
    distances = f_depth.result() or []
    t_pre = time.time()

    if mode == "free":
        # 对照组：不给任何约束、不注入距离，考察"通用模型直用"的实际表现
        res = providers.vision.understand(
            image_bytes, FREE_SYSTEM,
            FREE_USER_TEMPLATE.format(query=query or "（无）"),
        )
        text = res["text"]
    else:
        # 距离表结构化注入（F03 的核心动作：模型只能引用，不能估算）
        user_text = GUIDE_USER_TEMPLATE.format(
            distance_block=build_distance_block(distances),
            query=query or "（无）",
        )
        if ocr_text:
            user_text += f"\n\n画面中的 OCR 文字（供参考，不要照念）：\n{ocr_text[:400]}"
        res = providers.vision.understand(image_bytes, GUIDE_SYSTEM, user_text)
        text = _ensure_three_sections(res["text"])
        if res.get("model") == "error":
            # 三级全挂（出口网络不通，或三个模型全被限流）。
            # 距离表是本地算的、仍然有效 —— 换成人能用的说法，别把
            # "模型连不上"当成最终答复丢给用户。
            text = _degraded_scene_text(distances)

    sp = build_speech(text)
    out = {
        "reply": text,
        "speech": sp["speech"],
        "speech_emergency": sp["emergency"],   # 是否触发了紧急重排（障碍句前置）
        "speech_dropped": sp["dropped"],       # 为控长度丢弃了哪几段
        "fillers": config.FILLER_TEXT,
        "model": res.get("model"),
        "fallback": res.get("fallback", False),
        "mock": res.get("mock", False),
        "ocr": ocr_text,
        "distances": distances,
        "mode": mode,
        "latency_ms": int((time.time() - t0) * 1000),
        "preprocess_ms": int((t_pre - t0) * 1000),
    }
    _log_request({"task": "understand", "mode": mode, "query": query,
                  "reply": out["reply"], "speech": out["speech"],
                  "speech_emergency": sp["emergency"], "distances": distances,
                  "model": out["model"], "latency_ms": out["latency_ms"], "ts": time.time()})
    return out


# ---------------------------------------------------------------- 主链路 2：文字阅读


def read_text(image_bytes: bytes, query: str = "", doc_type: str = "auto") -> dict:
    """拍照 -> 阅读文字（F06 药品 / F07 单据·指示牌·价签）

    :param doc_type: drug | sign | price | doc | auto
    """
    t0 = time.time()
    ocr_text = providers.ocr.extract(image_bytes) or ""
    ocr_ok = bool(ocr_text.strip())
    if not ocr_ok:
        # OCR 无果时直接交给多模态模型读图
        ocr_text = "（OCR 未识别到文字，请直接观察图片内容）"

    res = providers.vision.understand(
        image_bytes,
        build_read_system(doc_type),
        build_read_user(doc_type, ocr_text[:800], query),
        # mock 必须给"读文字"的文案，否则无 Key 时会把场景描述当识别结果返回，
        # 用户看到的是"我点的是读文字，怎么给我念走廊"
        mock_text=providers.vision.read_mock(doc_type),
    )
    reply = res["text"].strip()
    out = {
        "reply": reply,
        # 文字阅读场景用户是站定看东西，可容忍更长播报，故用 READ_SPEECH_MAX_CHARS
        "speech": _clip(_strip_labels(reply), config.READ_SPEECH_MAX_CHARS),
        "fillers": config.FILLER_TEXT,
        "ocr": ocr_text,
        "ocr_ok": ocr_ok,
        "doc_type": doc_type,
        "model": res.get("model"),
        "fallback": res.get("fallback", False),
        "mock": res.get("mock", False),
        "latency_ms": int((time.time() - t0) * 1000),
    }
    _log_request({"task": "read", "doc_type": doc_type, "query": query,
                  "reply": reply, "ocr_ok": ocr_ok, "model": out["model"],
                  "latency_ms": out["latency_ms"], "ts": time.time()})
    return out


# ---------------------------------------------------------------- 主链路 3：意图路由


def classify_intent(text: str) -> dict:
    """意图识别：路由到 scene / read / sop_medical / sop_transit / help_human（F08）"""
    from .providers import safe_json

    if not text or not text.strip():
        return {"intent": "scene", "entities": {}}
    res = providers.vision.chat_text(INTENT_SYSTEM, INTENT_USER_TEMPLATE.format(text=text))
    if not res.get("text"):
        # 兜底：模型不可用时退化为关键词路由，保证 MVP 可演示
        return _keyword_intent(text)
    parsed = safe_json(res["text"], {})
    if not isinstance(parsed, dict) or not parsed.get("intent"):
        return _keyword_intent(text)
    parsed.setdefault("entities", {})
    return parsed


# 关键词路由的词表。提成模块级常量的理由见 keyword_intent 的说明。
#
# **顺序即优先级**：先匹配到的先赢。"帮我看看这盒药上写的什么"里同时含
# "看看"（scene）与"药/写的什么"（read），必须让 read 先命中，
# 否则用户想读药盒，收到的却是一段环境描述 —— 这正是实测里反馈过的困惑。
# 所以 read / 医疗 / 乘车 都要排在 scene 之前。
#
# 为什么把 scene 也写进来自动命中：不写的话，"看看周围"这类**最高频**指令
# 会落进"规则没命中"分支，白付一次 LLM 调用（实测 1.7 秒，限流时能到 20 秒）。
# 后端规则与前端 VOICE_ROUTES 保持同一套顺序，两处判断才不会互相矛盾。
_KEYWORD_INTENTS = (
    # 求助的口语说法要比书面说法多得多：最着急的那一刻，人不会说
    # "请求协助"或"呼叫志愿者"，他会说"帮我叫个人""我想找人"。
    # 别只收"叫人"——口语常说"叫**个**人""帮我叫一个"，中间会被一个字断开。
    # 与前端 VOICE_ROUTES 保持同一套说法，两边才不会一个命中一个落空。
    ("help_human", ("人工", "找人帮忙", "志愿者", "叫个人", "帮帮我", "求助",
                    "找人", "叫个", "叫一个", "帮我叫", "帮个忙", "帮找", "求救")),
    ("sop_medical", ("医院", "挂号", "看病", "取药", "缴费", "门诊", "检查")),
    ("sop_transit", ("公交", "地铁", "车站", "坐车", "到站", "站牌")),
    # read 的词表刻意比"读/念"更宽（与前端 VOICE_ROUTES 对齐）：
    # 视障用户很少说"请帮我识别文字"，他说的会是"这盒药写的什么"
    # "这个价签多少钱"——这些都是 read 意图，但一个"读"字都没有。
    ("read", ("读", "念", "写的什么", "说明", "标签", "多少钱", "看看字",
              "药品", "药", "价格", "价签", "牌子", "指示牌", "单据", "票据")),
    ("scene", ("周围", "前面", "前方", "环境", "障碍", "路况",
               "看看", "看一下", "拍", "是什么")),
)


def keyword_intent(text: str) -> dict | None:
    """纯关键词意图路由：**命中返回 dict，没命中返回 None**。

    与 _keyword_intent 的唯一区别就是这一点。后者把"什么都没匹配上"
    也报成 scene，于是调用方无法判断"到底该不该再请一次模型" ——
    而语音链路上这个判断直接决定要不要付出一整次 LLM 调用的时间。
    """
    text = text or ""
    for intent, words in _KEYWORD_INTENTS:
        if any(w in text for w in words):
            entities: dict = {}
            if intent == "help_human":
                entities["reason"] = _guess_help_reason(text)
            return {"intent": intent, "entities": entities}
    return None


def _keyword_intent(text: str) -> dict:
    """无模型时的关键词路由（也是评测时有价值的基线对照组）"""
    return keyword_intent(text) or {"intent": "scene", "entities": {}}


def _guess_help_reason(text: str) -> str:
    """从用户口述里粗略判断求助原因，用于播报不同的安抚话术"""
    text = text or ""
    if any(w in text for w in ("不舒服", "头晕", "难受", "疼", "心慌", "喘")):
        return "unwell"
    if any(w in text for w in ("拦住", "不让进", "进不去", "过不去", "挡着")):
        return "blocked"
    if any(w in text for w in ("找不到", "迷路", "不知道在哪", "走丢")):
        return "lost"
    if any(w in text for w in ("问", "打听")):
        return "query"
    return "other"


# ---------------------------------------------------------------- 主链路 4：呼叫人工


def request_help(text: str = "", reason: str = "", session_id: str = "",
                 location_hint: str = "") -> dict:
    """呼叫人工志愿者（F16）。

    设计取舍：这是**真人接手的通道**，因此不做任何"智能"包装——
    如实告知票号、预计响应时间、可直接拨打的电话，把主动权交回用户。
    """
    if not reason:
        r = classify_intent(text) if text else {"intent": "", "entities": {}}
        reason = ((r.get("entities") or {}).get("reason") or _guess_help_reason(text))
    return helpdesk.request_help(
        session_id=session_id, reason=reason,
        location_hint=location_hint, note=(text or "")[:200],
    )


# ---------------------------------------------------------------- SOP 节点校验


def check_sop_step(image_bytes: bytes, step_title: str, success_signal: str) -> dict:
    """SOP 节点的视觉校验：判断当前画面是否已达成该步骤（F10）"""
    from .prompts import SOP_CHECK_SYSTEM, SOP_CHECK_USER_TEMPLATE
    from .providers import safe_json

    res = providers.vision.understand(
        image_bytes, SOP_CHECK_SYSTEM,
        SOP_CHECK_USER_TEMPLATE.format(step_title=step_title, success_signal=success_signal),
        # 这条链路要的是 JSON。若不单独给 mock，场景文案会被 safe_json 判失败 → reached 恒为 False，
        # 表现为"mock 模式下拍照永远推不动流程"，用户只会以为 SOP 功能坏了。
        mock_text=providers.vision.MOCK_REPLY_SOP,
    )
    parsed = safe_json(res["text"], {"reached": False, "hint": "请把手机抬高一点再拍一张"})
    if not isinstance(parsed, dict):
        parsed = {"reached": False, "hint": "请把手机抬高一点再拍一张"}
    parsed.setdefault("reached", False)
    parsed.setdefault("hint", "")
    # 统一成布尔，防止模型返回 "true" 字符串
    parsed["reached"] = str(parsed["reached"]).lower() in ("true", "1", "yes", "是")
    parsed["model"] = res.get("model")
    parsed["mock"] = res.get("mock", False)
    return parsed
