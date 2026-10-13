# -*- coding: utf-8 -*-
"""外部模型 Provider 封装

设计原则：
1. 每个 Provider 都有 mock 分支，无 API Key 时整个系统仍可端到端联调。
2. 主模型失败自动降级到备用模型（答辩现场的关键保险）。
3. 所有对外请求统一走 OpenAI 兼容协议，方便在厂商之间切换。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tempfile

import requests
from PIL import Image, ImageOps

from .config import (
    asr_backend,
    config,
    key_usable,
    mock_reason,
    vision_backends_available,
)

logger = logging.getLogger("qiming")

# 模型对象缓存。PaddleOCR / FunASR 的构造是**秒级到十几秒**的模型加载，
# 早期版本在每个请求里 new 一次，等于给每次识别都白加一段延迟，
# 而且没有任何报错，看上去就像"网络慢"。这里做进程内单例。
_MODEL_CACHE: dict = {}


def _cached(key: str, factory):
    """惰性单例。构造失败缓存 None，避免每个请求都重复重试一次重加载。"""
    if key not in _MODEL_CACHE:
        try:
            _MODEL_CACHE[key] = factory()
            logger.info("已加载可选组件：%s", key)
        except Exception as e:  # noqa: BLE001
            logger.info("可选组件不可用（%s）：%s", key, e)
            _MODEL_CACHE[key] = None
    return _MODEL_CACHE[key]


def _to_rgb_array(image_bytes: bytes):
    """bytes -> RGB ndarray。

    两个供应商都**不吃原始 bytes**：PaddleOCR 的输入只接受文件路径或 ndarray，
    FunASR 同理。先统一解码成 ndarray，顺便按 EXIF 把图摆正
    （手机竖拍的照片像素是躺倒的，方向只记在 EXIF 里）。
    """
    try:
        import numpy as np

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        return np.asarray(img, dtype="uint8")
    except Exception as e:  # noqa: BLE001
        logger.info("图像解码失败，跳过：%s", e)
        return None


# ---------------------------------------------------------------- 工具


def compress_image(raw: bytes, max_side: int | None = None, quality: int | None = None) -> bytes:
    """压缩图片：把最长边压到 max_side，是降低端到端延迟最有效的单点手段"""
    if not raw:
        return raw
    max_side = max_side or config.IMAGE_MAX_SIDE
    quality = quality or config.IMAGE_QUALITY
    try:
        img = Image.open(io.BytesIO(raw))
        # 必须先按 EXIF 摆正再重新编码。手机竖拍的照片像素本身是躺倒的，
        # 方向只记在 EXIF 里，而 PIL 保存 JPEG 时默认不写 EXIF——
        # 不摆正就等于把用户的照片转了 90° 交给模型，模型给出的
        # "左边/右边/正前方"随之全错，而我们的时钟方位正是据此生成的。
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return raw
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_data_url(image_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()


# 思维链标签。<think> / <thinking> / <reasoning> 都见过，答案段是 <answer>。
_THINK_BLOCK_RE = re.compile(r"<\s*(think|thinking|reasoning)\s*>.*?<\s*/\s*\1\s*>",
                             re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<\s*(think|thinking|reasoning)\s*>", re.I)
_ANSWER_RE = re.compile(r"<\s*answer\s*>(.*?)<\s*/\s*answer\s*>", re.S | re.I)
_REASON_TAG_RE = re.compile(r"</?\s*(?:think|thinking|reasoning|answer)\s*/?>", re.I)


def strip_reasoning(text: str) -> str:
    """剥离思维链，只留正式答案。

    为什么必须做（2026-09-26 真机抓到）：降级链第②级 GLM-4.1V-Thinking-Flash
    返回的是
        <think>用户需要回答形状和颜色…图是圆形，红色。</think><answer>图中是一个红色的圆形。</answer>
    本项目是**语音播报**产品：不清洗就等于把模型的自言自语念给视障用户听，
    而他站在路边等着过马路。三段式解析（【前方情况】/【注意】/【建议】）
    也会被 think 文本挤掉。

    第三种情况最阴：`max_tokens` 截断在思考中途，**只有开标签没有闭标签**。
    此时任何"配对删除"都无效，必须从开标签处一路截到结尾——
    宁可这一轮没答案（上层会提示重拍），也不能念半截思维链。
    """
    if not text:
        return text
    answers = _ANSWER_RE.findall(text)
    if answers:
        text = "\n".join(a.strip() for a in answers if a.strip())
    else:
        text = _THINK_BLOCK_RE.sub("", text)
        m = _THINK_OPEN_RE.search(text)
        if m:
            text = text[:m.start()]
    return _REASON_TAG_RE.sub("", text).strip()


def _extract_content(data: dict) -> str:
    """从 OpenAI 兼容响应里取出正文，并对常见异常结构给出可读的原因。

    上游返回 content=null（内容被过滤）或 content 是分片数组时，
    直接 `.strip()` 会抛 AttributeError，被上层当成"模型服务挂了"而降级，
    真实原因被掩盖，排查时会一直往网络/Key 上找。
    """
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"模型返回结构异常：{str(data)[:200]}") from e
    if isinstance(content, list):          # 少数实现返回分片数组
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if content is None or not str(content).strip():
        # content 为空有两种截然不同的原因，报错必须分开，否则会把人带偏：
        #   ① 内容被安全过滤 → 该改提示词
        #   ② 会思考的模型把 max_tokens 全用在思维链上（finish_reason=length），
        #      正式答案一个字都没写 → 该调大 VL_MAX_TOKENS
        # 实测 2026-09-26：GLM-4.6V-Flash 与 GLM-4.1V-Thinking-Flash 都属第②种，
        # 却被旧文案报成"可能被内容过滤拦截"，排查方向整个是错的。
        msg = choice.get("message") or {}
        reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "")
        finish = str(choice.get("finish_reason") or "")
        if reasoning or finish == "length":
            raise RuntimeError(
                f"模型把输出额度用在了思维链上，正式答案没写完"
                f"（finish_reason={finish or '未知'}，思维链 {len(str(reasoning))} 字）。"
                f"→ 调大 VL_MAX_TOKENS（当前 {config.VL_MAX_TOKENS}）")
        raise RuntimeError(f"模型返回空内容（可能被内容过滤拦截）：{str(data)[:200]}")
    # 出口唯一：所有链路（场景理解 / 读文字 / SOP 校验）都经此返回，
    # 因此思维链清洗放在这里，而不是让每个调用方各自记得处理。
    cleaned = strip_reasoning(str(content))
    if not cleaned:
        # 只吐出思维链、没有正式答案（典型是被 max_tokens 截断在思考中途）。
        # 这里必须抛错而不是返回空串：抛错才会触发降级链去试下一个模型，
        # 返回空串则会让上层把"这一级没答上来"当成"模型说没有内容"。
        raise RuntimeError(
            f"模型只返回了思维链、没有正式答案（可能被 max_tokens 截断）："
            f"{str(content)[:200]}")
    return cleaned


def _request_chat(base_url: str, api_key: str, model: str, messages: list,
                  temperature: float, max_tokens: int, timeout: int) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return _extract_content(r.json())


def _chat_vision(base_url: str, api_key: str, model: str, system: str,
                 user_text: str, image_bytes: bytes, timeout: int,
                 max_tokens: int | None = None) -> str:
    """调用 OpenAI 兼容的多模态对话接口

    max_tokens 必须由调用方按**该模型的最大输出**给出：会思考的模型
    （GLM-4.6V / GLM-4.1V-Thinking）先写一段思维链，额度给小了会把答案饿死；
    而 GLM-4V-Flash 最大输出只有 1K，给大了直接 400。
    """
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": to_data_url(image_bytes)}},
                {"type": "text", "text": user_text},
            ],
        },
    ]
    return _request_chat(base_url, api_key, model, messages, 0.2,
                         max_tokens or config.VL_MAX_TOKENS, timeout)


def _chat_text(base_url: str, api_key: str, model: str, system: str,
               user_text: str, timeout: int) -> str:
    """纯文本对话（意图识别等无需图片的场景，避免为空图报错）"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
    return _request_chat(base_url, api_key, model, messages, 0.0, 200, timeout)


def safe_json(text: str, default: dict) -> dict:
    """大模型输出的 JSON 经常裹 ```json 代码块，这里做容错解析"""
    if not text:
        return default
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    logger.warning("JSON 解析失败，回退默认值：%s", text[:120])
    return default


# ---------------------------------------------------------------- 视觉理解


class VisionProvider:
    """多模态视觉理解。三级降级，全部免费、全部走智谱同一个 Key：

        GLM-4.6V-Flash  →  GLM-4.1V-Thinking-Flash  →  GLM-4V-Flash

    为什么是三级而不是一级：免费模型的并发常被压到 1~5，答辩现场连续拍照
    很容易撞 429。三级里前两级是同平台不同模型，第三级是最轻的模型，
    相当于"换个名字再试一次"，多数限流撑到第三次就过去了，用户全程无感。
    若另外配了阿里百炼 Key，备用通道会自动切到百炼（见 config）。

    mock 必须**分任务**给文案。曾经只有一句 MOCK_REPLY（场景三段式），
    而 understand() 是场景理解 / 文字阅读 / SOP 校验三条链路共用的入口，
    于是"拍照读文字"在无 Key 时返回了"这是室内走廊"——终端用户看到的是
    "我点的是读文字，怎么给我念走廊"，而代码层面零报错、测试全绿。
    """

    # 场景理解（F01）：三段式，供 /api/understand 与前端解析共用
    MOCK_REPLY_SCENE = ("【前方情况】这是室内走廊，地面平坦。\n"
                        "【注意】暂无明显障碍。\n"
                        "【建议】可以放心向前直行。")

    # 文字阅读（F06/F07）：按 doc_type 给对应形态的示例，绝不能复用场景文案
    MOCK_REPLY_READ = {
        "auto": "这是一张药品包装盒，写着布洛芬缓释胶囊，0.3克，有效期到2027年3月，一次一粒，一天两次。",
        "drug": "布洛芬缓释胶囊，规格0.3克，有效期到2027年3月，未过期。一次一粒，一天两次，饭后服用。",
        "sign": "蓝色指示牌：三楼，心内科，向前直行。右侧另有卫生间。",
        "price": "商品名有机纯牛奶，价格19.9元，规格1升，保质期6个月。",
        "doc": "这是一张门诊收费单据，金额128.5元，缴费成功，日期2026年9月25日，单号尾号3172。",
    }

    # SOP 节点校验（F10）：这条链路要的是 JSON，喂场景文案会被 safe_json 判失败，
    # 表现为"mock 模式下拍照片永远推不动流程"，用户只会以为 SOP 坏了。
    MOCK_REPLY_SOP = '{"reached": true, "hint": ""}'

    # 向后兼容旧引用
    MOCK_REPLY = MOCK_REPLY_SCENE

    @staticmethod
    def read_mock(doc_type: str = "auto") -> str:
        return VisionProvider.MOCK_REPLY_READ.get(
            doc_type, VisionProvider.MOCK_REPLY_READ["auto"])

    def understand(self, image_bytes: bytes, system: str, user_text: str,
                   mock_text: str | None = None) -> dict:
        """mock_text：本次任务在无模型时应该"假装"返回什么。不传则退化为场景文案。

        是否 mock 由 vision_backends_available() 决定——它同时看主力与备用
        两个 Key 的**可用性**（空值、中文、占位符都算不可用）。
        以前这里只看 MOCK_MODE 与主 Key，导致"只配了备用 Key"的用户静默退回 mock。
        """
        img = compress_image(image_bytes)
        if config.MOCK_MODE or not vision_backends_available():
            logger.info("VisionProvider: MOCK 模式（%s）", mock_reason() or "未配置识别模型")
            return {"text": mock_text or self.MOCK_REPLY, "model": "mock",
                    "fallback": False, "mock": True}

        errors: list[str] = []

        # 主模型（Key 不可用则跳过，不要白等一次超时）
        if key_usable(config.VL_API_KEY):
            try:
                text = _chat_vision(config.VL_BASE_URL, config.VL_API_KEY,
                                    config.VL_MODEL_NAME, system, user_text, img,
                                    config.VL_TIMEOUT)
                return {"text": text, "model": config.VL_MODEL_NAME,
                        "fallback": False, "mock": False}
            except Exception as e:  # noqa: BLE001
                logger.error("主视觉模型失败：%s，尝试降级", e)
                errors.append(f"{config.VL_MODEL_NAME}: {e}")

        # 备用模型
        if key_usable(config.VL_FALLBACK_API_KEY):
            try:
                text = _chat_vision(config.VL_FALLBACK_BASE_URL, config.VL_FALLBACK_API_KEY,
                                    config.VL_FALLBACK_MODEL_NAME, system, user_text, img,
                                    config.VL_TIMEOUT,
                                    max_tokens=config.VL_MAX_TOKENS_THINKING)
                return {"text": text, "model": config.VL_FALLBACK_MODEL_NAME,
                        "fallback": True, "mock": False}
            except Exception as e:  # noqa: BLE001
                logger.error("备用视觉模型也失败：%s", e)
                errors.append(f"{config.VL_FALLBACK_MODEL_NAME}: {e}")

        # 第三级兜底：与主力同一个 Key、同一个平台，只换成最轻的免费模型。
        # 这是专门给"免费额度限流"准备的——同一账号换个模型名往往就能过。
        if key_usable(config.VL_API_KEY):
            try:
                text = _chat_vision(config.VL_BASE_URL, config.VL_API_KEY,
                                    config.VL_LAST_MODEL_NAME, system, user_text, img,
                                    config.VL_TIMEOUT,
                                    max_tokens=config.VL_MAX_TOKENS_LAST)
                return {"text": text, "model": config.VL_LAST_MODEL_NAME,
                        "fallback": True, "mock": False}
            except Exception as e:  # noqa: BLE001
                logger.error("兜底视觉模型也失败：%s", e)
                errors.append(f"{config.VL_LAST_MODEL_NAME}: {e}")

        # 走到这里说明"配了 Key 但调用全失败"——这与 mock 是两回事，
        # 必须如实报错，不能悄悄退回示例文案（那才是最难查的故障）。
        return {"text": "抱歉，现在识别不了，请再说一次或换个角度拍一张。",
                "model": "error", "fallback": True, "mock": False,
                "error": " | ".join(errors)}

    def chat_text(self, system: str, user_text: str) -> dict:
        """纯文本推理（意图识别等无需图片的场景）

        刻意优先用**免费文本模型** GLM-4.7-Flash 而不是视觉模型：
        同一平台、同一个 Key，但文本模型更快、占用的是文本配额，
        把宝贵的视觉额度留给真正需要看图的那一路。
        """
        if config.MOCK_MODE or not vision_backends_available():
            return {"text": "", "model": "mock", "fallback": False}
        for base, key, model in (
            (config.VL_BASE_URL, config.VL_API_KEY, config.LLM_MODEL_NAME),
            (config.VL_BASE_URL, config.VL_API_KEY, config.VL_MODEL_NAME),
            (config.VL_FALLBACK_BASE_URL, config.VL_FALLBACK_API_KEY, config.VL_FALLBACK_MODEL_NAME),
        ):
            try:
                if not key_usable(key):     # 空值 / 占位符直接跳过，不浪费一次超时
                    continue
                text = _chat_text(base, key, model, system, user_text, config.VL_TIMEOUT)
                return {"text": text, "model": model,
                        "fallback": model != config.LLM_MODEL_NAME}
            except Exception as e:  # noqa: BLE001
                logger.error("文本模型调用失败（%s）：%s", model, e)
        return {"text": "", "model": "error", "fallback": True}


# ---------------------------------------------------------------- OCR


def _join_ocr(result) -> str:
    """把 PaddleOCR 的返回拍平成多行文本，兼容 2.x 与 3.x 两种结构。

    2.x: [[ [box, (text, score)], ... ]]        —— 逐行的 [框, (文本, 置信度)]
    3.x: [OCRResult]，dict-like，文本在 rec_texts 里
    """
    lines: list[str] = []
    for page in result or []:
        texts = None
        try:
            texts = page["rec_texts"]           # 3.x
        except (TypeError, KeyError, IndexError):
            texts = None
        if texts:
            lines.extend(str(t) for t in texts if t)
            continue
        for line in page or []:                 # 2.x
            if line and len(line) >= 2 and line[1]:
                lines.append(str(line[1][0]))
    return "\n".join(lines)


class OCRProvider:
    """文字提取。生产环境用 PaddleOCR PP-OCRv5，本地未安装时自动跳过。"""

    def _engine(self):
        def build():
            from paddleocr import PaddleOCR  # 可选依赖

            # PaddleOCR 3.x 用 use_textline_orientation 取代了 2.x 的 use_angle_cls，
            # 并且删掉了 show_log。若只按某一版的参数写，另一版会直接抛异常，
            # 而异常被下面的 except 吞掉后表现为"装了 OCR 却永远返回空字符串"——
            # 这是最难查的一类失效，所以按版本逐个试。
            for kwargs in ({"lang": "ch", "use_textline_orientation": True},
                           {"lang": "ch", "use_angle_cls": True, "show_log": False},
                           {"lang": "ch", "use_angle_cls": True},
                           {"lang": "ch"}):
                try:
                    return PaddleOCR(**kwargs)
                except (TypeError, ValueError):
                    continue
            return PaddleOCR(lang="ch")

        return _cached("ocr", build)

    def extract(self, image_bytes: bytes) -> str:
        if config.MOCK_MODE or not image_bytes:
            return ""
        engine = self._engine()
        if engine is None:
            return ""
        arr = _to_rgb_array(image_bytes)
        if arr is None:
            return ""
        try:
            # 3.x 的主入口是 predict()，2.x 只有 ocr()。
            # 两者的 input 都只接受 路径 / ndarray，**不接受 bytes**：
            # 早期直接把 bytes 传进去，会被当作文件路径处理而抛异常，
            # 被 except 吞掉后返回 ""，于是生产环境 OCR 从未真正生效过。
            if hasattr(engine, "predict"):
                result = engine.predict(arr)
            else:
                result = engine.ocr(arr)
            return _join_ocr(result)
        except Exception as e:  # noqa: BLE001
            logger.info("OCR 调用失败：%s", e)
            return ""


# ---------------------------------------------------------------- 深度估计


class DepthProvider:
    """距离估计（F03）。

    实现在 `core/depth.py`：ONNX（Depth-Anything-V2-Small）优先，
    不可用时退化为几何启发式后端（零附加依赖，CPU 毫秒级）。

    关键点（设计文档第四章要写）：**距离绝不让大模型猜**，
    必须由本模块先算好，再结构化注入提示词。
    """

    def estimate(self, image_bytes: bytes) -> list:
        if config.MOCK_MODE or not image_bytes:
            return []
        from . import depth as depth_mod          # 局部导入，避免循环依赖
        try:
            return depth_mod.estimate(image_bytes)
        except Exception as e:
            logger.info("深度估计失败，本帧不报距离：%s", e)
            return []

    def prompt_table(self, distances: list) -> str:
        from . import depth as depth_mod
        return depth_mod.to_prompt_table(distances)


# ---------------------------------------------------------------- 语音


class ASRError(RuntimeError):
    """语音识别的**可解释失败**。

    为什么要单独一个异常类型，而不是照旧返回空字符串：
    "没听清"与"格式不对"与"引擎不可用"对用户的含义完全不同 ——
    前者应该重说一遍，后两者重说一百遍也没用。
    原来的实现把所有失败都压成空字符串，前端只能统一说"没有听清"，
    于是用户在一个必然失败的路径上反复重试，而他没有任何办法看出真正的原因。
    """


def _audio_format(data: bytes) -> str:
    """按魔数认音频格式，返回 'wav' / 'mp3' / ''。

    只认魔数，不信 Content-Type、也不信文件扩展名 —— 客户端这两样都可以随便写。
    而且上游智谱也是按真实格式判的：实测把 WAV 字节改名成 `a.webm` 上传，
    直接返回 400 / code 1214「transcriptions不支持当前文件格式」。
    """
    if len(data) < 12:
        return ""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":                       # 带 ID3 标签的 MP3
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:   # MP3 帧同步字
        return "mp3"
    return ""


def _wav_seconds(data: bytes) -> float:
    """从 WAV 头算时长（秒）。解析不出来返回 0，表示"算不出"而非"非法"。"""
    try:
        import wave
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


# 智谱错误码 -> 用户能听懂的一句话。只映射会导致"重试有用/没用"反转的那些，
# 其余统一兜底并带上错误码（便于对着日志排查，又不泄漏 Key）。
_ASR_ERROR_HINTS = {
    "1214": "这段音频的格式不被支持，只支持 wav 和 mp3",
    "1305": "语音识别服务当前繁忙，请稍后再说一次",
    "1113": "语音识别额度已用完",
    "1000": "语音识别的凭证无效",
    "1001": "语音识别的凭证无效",
    "1002": "语音识别的凭证已过期",
    "1003": "语音识别的凭证无效",
    "1004": "语音识别鉴权失败",
}


class ASRProvider:
    """语音识别。两条通道，按 config.asr_backend() 的判据二选一。

    ① zhipu  智谱 GLM-ASR-2512（云端）—— 配了智谱 Key 就能用，零本地部署
    ② funasr 本地 Paraformer        —— 零调用成本，需装 torch + 下载模型

    为什么默认走云端：本地这条路在本项目的**任何真实运行环境**里都不成立
    （评委机器不会现装 torch，部署沙箱更不会）。而 /api/asr 是视障用户
    最主要的输入通道，让它在实际环境里空转，等于"按住说话"这个功能不存在。
    """

    # ---------------- 通道判定 ----------------

    def backend(self) -> str:
        """实际生效的通道。判据与 /api/meta 的声明共用 config.asr_backend()，
        避免出现"清单上说能识别、按下去却没反应"这种自相矛盾。"""
        return asr_backend()

    def available(self) -> bool:
        """后端是否真的具备听写能力。

        只看"通道是否配好"，**不**真的去 build 本地引擎：build 会现场下载并
        加载模型（几百 MB、几十秒），放在 /api/asr 里会让第一次请求直接超时。
        有这个标志，前端才能在"没听清"和"这台设备根本不能听写"之间分清 ——
        前者让用户重说，后者让用户改用按钮，说错方向等于把用户坑在原地。
        """
        return bool(self.backend())

    # ---------------- 本地通道 ----------------

    def _engine(self):
        def build():
            from funasr import AutoModel  # 可选依赖
            return AutoModel(model=config.ASR_MODEL_NAME)

        return _cached("asr", build)

    def _local(self, audio_bytes: bytes) -> str:
        engine = self._engine()
        if engine is None:
            return ""
        tmp = None
        try:
            # FunASR 的 input 接受 文件路径 / ndarray / 采样点列表，
            # **不接受原始 bytes**（和 OCR 是同一类错误）。
            # 落一个临时文件是最省事、也不引入额外音频解码依赖的做法。
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            res = engine.generate(input=tmp)
            return (res[0].get("text") if res else "") or ""
        except Exception as e:  # noqa: BLE001
            logger.info("本地 ASR 调用失败：%s", e)
            return ""
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---------------- 云端通道 ----------------

    def _cloud(self, audio_bytes: bytes, fmt: str) -> str:
        """调智谱 GLM-ASR-2512。

        字段名按官方文档：multipart 的 `file` 必填，`model` 必填，
        另有可选的 `hotwords`（热词，≤100 个）。
        """
        key = config.VL_API_KEY
        data = {"model": config.ASR_CLOUD_MODEL}
        if config.ASR_HOTWORDS:
            # 热词是提升指令词命中率最直接的旋钮：本项目用户说的就是
            # 那几个固定动作，而同音误听会让功能**完全不触发**
            # （"读一下"→"度一下"之后本地路由与后端意图分类双双落空，
            # 用户只会得到"没听懂"，他不可能猜到是自己被听错了）。
            data["hotwords"] = json.dumps(config.ASR_HOTWORDS[:100], ensure_ascii=False)
        try:
            r = requests.post(
                config.ASR_API_URL,
                headers={"Authorization": "Bearer " + key},
                files={"file": ("audio." + fmt, audio_bytes, "audio/" + fmt)},
                data=data,
                timeout=config.ASR_TIMEOUT,
            )
        except requests.Timeout:
            raise ASRError("语音识别超时，请再说一次") from None
        except Exception as e:  # noqa: BLE001
            logger.info("云端 ASR 请求异常：%s", e)
            raise ASRError("语音识别暂时不可用，请稍后再试") from None

        if r.status_code != 200:
            code, msg = "", ""
            try:
                err = (r.json() or {}).get("error") or {}
                code, msg = str(err.get("code") or ""), str(err.get("message") or "")
            except Exception:  # noqa: BLE001
                msg = r.text[:120]
            logger.info("云端 ASR 失败：HTTP %s code=%s msg=%s", r.status_code, code, msg)
            hint = _ASR_ERROR_HINTS.get(code)
            if hint:
                raise ASRError(hint)
            raise ASRError(f"语音识别失败（错误码 {code or r.status_code}）") from None

        try:
            return (r.json() or {}).get("text") or ""
        except Exception:  # noqa: BLE001
            raise ASRError("语音识别的返回值无法解析") from None

    # ---------------- 对外入口 ----------------

    def transcribe(self, audio_bytes: bytes) -> str:
        """识别一段音频。失败时抛 ASRError（可解释），成功但没说话则返回 ""。

        格式与时长在**本地先拦**，不把必然失败的请求发给上游：
        一次白跑要等好几秒，用户等到的还是"没听清"，归因完全错。
        """
        if config.MOCK_MODE or not audio_bytes:
            return ""
        backend = self.backend()
        if not backend:
            return ""

        fmt = _audio_format(audio_bytes)
        if backend == "zhipu":
            if not fmt:
                # 最常见的成因：前端把 MediaRecorder 的 webm/opus 直接传上来了。
                # 上游对 webm 一律 400（实测 code 1214），提前拦下来能把
                # "换个浏览器试试"这种无用建议换成一句准确的原因。
                raise ASRError("录音格式不被支持（需要 wav 或 mp3），请换用 Chrome 或 Safari 打开")
            if len(audio_bytes) > config.ASR_MAX_BYTES:
                raise ASRError("录音太长了，请说短一点")
            if fmt == "wav":
                secs = _wav_seconds(audio_bytes)
                # 上游硬限制 30 秒；这里留 0.5 秒余量。
                if secs > config.ASR_MAX_SECONDS + 0.5:
                    raise ASRError("录音超过 30 秒，请说短一点")
            return self._cloud(audio_bytes, fmt)

        return self._local(audio_bytes)


class TTSProvider:
    """语音合成。生产环境用 CosyVoice2 或腾讯云 / 阿里云 TTS。"""

    def synthesize(self, text: str) -> bytes:
        # 返回空字节时，前端自动降级到浏览器内置 speechSynthesis
        logger.info("TTS 待接入，本次由前端 Web Speech API 播报：%s", text[:30])
        return b""


vision = VisionProvider()
ocr = OCRProvider()
depth = DepthProvider()
asr = ASRProvider()
tts = TTSProvider()
