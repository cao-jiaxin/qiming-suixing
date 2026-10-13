# -*- coding: utf-8 -*-
"""全局配置：模型名称与版本号集中声明

大赛硬性要求：必须在提交文档中标明所用 AI 大模型名称及版本号、
AI 辅助编程的模型及 AI 编写代码所占比例。本文件即该声明的唯一数据源。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# code/backend/ 目录。所有"相对于后端根"的路径都从这里派生，
# 不使用当前工作目录，避免"换个目录启动就找不到文件"。
BACKEND_DIR = Path(__file__).resolve().parent.parent

# 显式指定 .env 的位置，不依赖启动时的工作目录。
# 原先写的是裸 load_dotenv()（从 CWD 向上搜索），只有恰好 cd 到 code/backend
# 启动时才读得到；从别处启动会静默丢掉 MOCK_MODE 与全部 API Key，
# 现象是"明明填了 Key 却在走 mock"，而且不报任何错，极难排查。
load_dotenv(BACKEND_DIR / ".env")


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


class Config:
    # ----------------  Serving  ----------------
    # 默认只监听本机。默认 0.0.0.0 的隐患：随手起一个开发实例
    # 就等于对整个局域网（校园网/咖啡厅 WiFi）开门，
    # 而开发实例上通常既没限流也没口令。
    # 要对局域网开放请显式设 HOST=0.0.0.0 —— start_lan.cmd 已经这么做了。
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    # 关闭后所有外部模型调用走本地 mock，便于无网 / 无额度时联调前端
    MOCK_MODE = _b("MOCK_MODE", False)

    # ----------------  视觉理解（多模态大模型）  ----------------
    # 三级降级链全部落在智谱同一个免费 Key 上，领一个 Key 就有三层保险：
    #   ① GLM-4.6V-Flash        128K 上下文 / 32K 输出，视觉推理，能力最强
    #   ② GLM-4.1V-Thinking-Flash  64K / 16K，复杂场景多步分析
    #   ③ GLM-4V-Flash          16K / 1K，最轻最快，限流时的最后防线
    # 三者均为智谱官方模型页标注的「免费模型」（长期免费，不是新用户赠额）。
    VL_PROVIDER = os.getenv("VL_PROVIDER", "zhipu")          # zhipu | bailian
    VL_MODEL_NAME = os.getenv("VL_MODEL_NAME", "GLM-4.6V-Flash")
    # ⚠ 版本号会被写进设计文档的「AI 模型声明」章节（大赛硬性要求）。
    # 提交前务必到官方模型文档页核对一次，不要沿用这里的示例值。
    VL_MODEL_VERSION = os.getenv("VL_MODEL_VERSION", "glm-4.6v-flash")
    VL_API_KEY = os.getenv("ZHIPU_API_KEY", "")
    VL_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    # 单次模型调用的等待上限。**这个数字是倒推出来的，不是随手取的**：
    # 降级链最多试 3 个模型，3 × 8 = 24s，再加上本地预处理（实测 1.7s），
    # 端到端要落在 30s 以内 —— 那是前端 fetch 的超时线，也是托管网关
    # （实测 60s 返回 504）之前必须回来的时刻。
    # 原来写 20s：3 × 20 = 60s 正好等于网关上限，于是模型一旦连不上，
    # 后端每次都被网关掐断成 504，用户只等到一句"超时"，什么都拿不到。
    # 而正常路径实测 5.0s（主力）/ 6~11s（思考型），8s 都覆盖得到。
    VL_TIMEOUT = int(os.getenv("VL_TIMEOUT", "8"))

    # 输出 token 上限。**必须区分档位**，否则会出静默故障：
    # GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash 都是"会思考"的模型，回答前先写一段
    # 思维链。原来统一给 300，思维链一写就把额度吃光（finish_reason=length），
    # 正式答案一个字都没写 → content 为空 → 被当成"内容被过滤"降级。
    # 实测（2026-09-26）：300 时降级链第二级**必然失败**，这条路形同虚设。
    # 而最后兜底的 GLM-4V-Flash 最大输出只有 1K，给大了会直接 400。
    #
    # 实测（2026-09-26，同一张图同一提示词，端到端延迟）：
    #     VL_MAX_TOKENS=512  → 主力 GLM-4.6V-Flash 成功，**5.0 秒**
    #     VL_MAX_TOKENS=1024 → 主力成功但**11.2 秒**（额度越大，它思考得越久）
    # 本项目是"边走边听"的语音产品，10 秒以上的等待会让信息到达时用户已走过头，
    # 所以主力压到 512（三段式正文 ≤60 字，512 足够）。
    # 但**思考型模型需要更多**：GLM-4.1V-Thinking-Flash 在 512 下实测被截断
    # （只有思维链、没有答案），所以一级降级单独给大额度。
    VL_MAX_TOKENS = int(os.getenv("VL_MAX_TOKENS", "512"))                    # 主力
    VL_MAX_TOKENS_THINKING = int(os.getenv("VL_MAX_TOKENS_THINKING", "1024"))  # 一级降级（思考型）
    VL_MAX_TOKENS_LAST = int(os.getenv("VL_MAX_TOKENS_LAST", "512"))          # 最轻档（上限 1K，勿调大）

    # 第三级兜底：与主力**同一个 Key、同一个平台**，只换更轻的免费模型名。
    VL_LAST_MODEL_NAME = os.getenv("VL_LAST_MODEL_NAME", "GLM-4V-Flash")
    VL_LAST_MODEL_VERSION = os.getenv("VL_LAST_MODEL_VERSION", "glm-4v-flash")

    # 备用视觉模型（主模型限流 / 超配额时自动降级）。
    # 默认复用智谱的第二个免费视觉模型（同一个 Key 即可，无需再注册第二家）；
    # 只有当你另外申请了阿里百炼的 DASHSCOPE_API_KEY，才会切到百炼通道。
    _USE_BAILIAN = bool(os.getenv("DASHSCOPE_API_KEY"))
    VL_FALLBACK_PROVIDER = os.getenv("VL_FALLBACK_PROVIDER",
                                     "bailian" if _USE_BAILIAN else "zhipu")
    VL_FALLBACK_MODEL_NAME = os.getenv(
        "VL_FALLBACK_MODEL_NAME",
        "qwen3-vl-flash" if _USE_BAILIAN else "GLM-4.1V-Thinking-Flash")
    VL_FALLBACK_MODEL_VERSION = os.getenv(
        "VL_FALLBACK_MODEL_VERSION",
        "2026-03" if _USE_BAILIAN else "glm-4.1v-thinking-flash")
    VL_FALLBACK_API_KEY = (os.getenv("DASHSCOPE_API_KEY")
                           or os.getenv("ZHIPU_API_KEY", ""))
    VL_FALLBACK_BASE_URL = (
        os.getenv("DASHSCOPE_BASE_URL",
                  "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if _USE_BAILIAN
        else os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    )

    # ----------------  文本推理（智能体编排 / 意图识别）  ----------------
    LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "GLM-4.7-Flash")
    LLM_MODEL_VERSION = os.getenv("LLM_MODEL_VERSION", "glm-4.7-flash-2026-06")

    # ----------------  语音识别 ASR / 语音合成 TTS  ----------------
    # ASR 有两条通道，按"实际能用哪个"自动选（判据集中在 asr_backend()）：
    #   ① zhipu  智谱 GLM-ASR-2512 云端，复用同一把智谱 Key，零本地部署
    #   ② funasr 本地 Paraformer，零调用成本，但要装 torch + 下载数百 MB 模型
    # 默认 auto：配了 Key 走云端，没配再退回本地。
    #
    # 为什么把云端通道放进默认路径：本地 FunASR 在评委机器与部署沙箱上
    # 都不可能现装（依赖 torch，模型要联网下载）。原来的代码只认 funasr，
    # 于是 /api/asr 在真实环境里恒定返回空文本 —— 而它是视障用户最主要的
    # 输入通道，"按住说话说完什么也没有"等于这个功能不存在。
    ASR_PROVIDER = os.getenv("ASR_PROVIDER", "auto").strip().lower()   # auto|zhipu|funasr
    ASR_CLOUD_MODEL = os.getenv("ASR_CLOUD_MODEL", "glm-asr-2512")
    ASR_API_URL = os.getenv(
        "ASR_API_URL",
        "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions")
    # 官方限制：单文件 ≤25MB、时长 ≤30 秒。这里留出余量，超限提前拦下来
    # 并给用户人话，而不是把 400 从上游原样透出来。
    ASR_MAX_SECONDS = float(os.getenv("ASR_MAX_SECONDS", "30"))
    ASR_MAX_BYTES = int(os.getenv("ASR_MAX_BYTES", str(20 * 1024 * 1024)))
    # 单次识别的超时。用户是按着手机等结果，等太久他会以为坏了并重说一遍，
    # 于是识别结果里出现一串重复内容 —— 不如早失败早提示。
    # 语音识别超时。智谱 ASR 正常时是几百毫秒级（实测 3.92s 音频 → 597ms），
    # 原来给 30s 意味着"连不上时也要干等半分钟"。压到 12s：
    # 正常路径绰绰有余，异常路径快速失败 —— 用户还能来得及再按一次重说。
    ASR_TIMEOUT = int(os.getenv("ASR_TIMEOUT", "12"))
    # 语音链路上"意图分类"能花的时间预算（秒）。
    # 意图只是锦上添花：识别出的文本才是必须尽快到用户手上的东西。
    # 实测免费文本模型限流时会先 429、再卡满 20 秒 —— 识别本身 2 秒就回来了，
    # 用户却要陪着等 20 秒才看到动作发生。所以这里给一个死预算，
    # 超预算就退回关键词规则（确定性、微秒级），绝不让整条语音链路被拖住。
    INTENT_BUDGET_S = float(os.getenv("INTENT_BUDGET_S", "3.5"))
    # 热词表：提升指令词命中率最直接的旋钮。
    # 视障用户说的就是那几个固定动作，而同音误听会让功能**完全不触发**
    # （"读一下"→"度一下"之后，本地关键词路由与后端意图分类都会落空，
    # 用户得到的是"没听懂"，他不可能猜到是自己发音被听错了）。
    ASR_HOTWORDS = [w.strip() for w in os.getenv(
        "ASR_HOTWORDS",
        "看看周围,读一下文字,就医引导,乘车引导,距离表,下一步,退出,"
        "呼叫志愿者,重听一遍,挂号,取药,站台,站牌,障碍物,红绿灯",
    ).split(",") if w.strip()]
    # 本地 FunASR 通道的模型声明（保留这条路的能力，不代表实际生效，
    # 实际生效通道由 asr_backend() 决定，见 model_manifest 的 asr 段）
    ASR_MODEL_NAME = os.getenv("ASR_MODEL_NAME", "paraformer-zh")
    ASR_MODEL_VERSION = os.getenv("ASR_MODEL_VERSION", "FunASR v1.1")
    TTS_MODEL_NAME = os.getenv("TTS_MODEL_NAME", "CosyVoice2-0.5B")
    TTS_MODEL_VERSION = os.getenv("TTS_MODEL_VERSION", "CosyVoice2 v2.0")

    # ----------------  文档 / 图像处理  ----------------
    OCR_MODEL_NAME = os.getenv("OCR_MODEL_NAME", "PP-OCRv5")
    OCR_MODEL_VERSION = os.getenv("OCR_MODEL_VERSION", "PaddleOCR 3.x")
    DEPTH_MODEL_NAME = os.getenv("DEPTH_MODEL_NAME", "Depth-Anything-V2-Small")
    DEPTH_MODEL_VERSION = os.getenv("DEPTH_MODEL_VERSION", "vits-2024")

    # ----------------  深度 → 步数 的几何参数（F03 距离播报）  ----------------
    # 相机与几何假设：手机大致水平前伸，离地 DEPTH_CAM_HEIGHT_M。
    # 这些参数决定了"步数"的绝对尺度，改动即影响距离准确率，
    # 因此每次调整都必须在 results/ab_log.md 里记一笔。
    DEPTH_CAM_HEIGHT_M = float(os.getenv("DEPTH_CAM_HEIGHT_M", "1.20"))   # 持机高度
    DEPTH_VFOV_DEG = float(os.getenv("DEPTH_VFOV_DEG", "55"))             # 垂直视场角
    DEPTH_HFOV_DEG = float(os.getenv("DEPTH_HFOV_DEG", "70"))             # 水平视场角（时钟方位用）
    DEPTH_HORIZON_RATIO = float(os.getenv("DEPTH_HORIZON_RATIO", "0.45"))  # 地平线所在行（0=顶）
    STEP_LENGTH_M = float(os.getenv("STEP_LENGTH_M", "0.60"))             # 一步约多少米
    DEPTH_MAX_STEPS = int(os.getenv("DEPTH_MAX_STEPS", "30"))             # 超过即视为空旷
    DEPTH_MAX_ITEMS = int(os.getenv("DEPTH_MAX_ITEMS", "3"))              # 最多报几个方向
    DEPTH_WORK_SIDE = int(os.getenv("DEPTH_WORK_SIDE", "640"))            # 深度计算分辨率
    DEPTH_INPUT_SIZE = int(os.getenv("DEPTH_INPUT_SIZE", "518"))          # ONNX 输入边长
    # 启发式后端的绝对能量下限（灰度梯度均值）。只靠 P75 相对阈值时，
    # JPEG 压缩噪声也会越过阈值，导致"一面白墙也处处报障碍"。
    # 调高 = 更保守（少误报、可能漏报），调低 = 更敏感。改动需记 ab_log。
    DEPTH_MIN_ENERGY = float(os.getenv("DEPTH_MIN_ENERGY", "6.0"))
    DEPTH_ONNX_PATH = os.getenv("DEPTH_ONNX_PATH") or str(
        BACKEND_DIR / "models" / "depth_anything_v2_small.onnx"
    )

    # ----------------  图像处理参数（延迟优化关键）  ----------------
    IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "768"))   # 压到 768px 可显著降低往返时延
    IMAGE_QUALITY = int(os.getenv("IMAGE_QUALITY", "80"))

    # ----------------  播报长度策略（屏幕文字与语音播报分开限长）  ----------------
    # 依据：中文 TTS 约 4–5 字/秒，60 字要念 12–15 秒。
    # 视障用户是"边走边听"，语音必须比屏幕文字更短，否则一句话没听完人已经走到障碍物上。
    MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "60"))        # 屏幕文字上限
    SPEECH_MAX_CHARS = int(os.getenv("SPEECH_MAX_CHARS", "80"))      # 环境播报上限（三段式全念）
    READ_SPEECH_MAX_CHARS = int(os.getenv("READ_SPEECH_MAX_CHARS", "50"))  # 文字阅读上限（站定听）
    SPEECH_NEAR_STEPS = int(os.getenv("SPEECH_NEAR_STEPS", "3"))     # 几步内视为"近距离危险"

    # ----------------  视觉兜底文案（等待真结果时先播放，消除等待感）  ----------------
    FILLER_TEXT = os.getenv("FILLER_TEXT", "我看一下，请稍等")

    # ----------------  公网访问保护（部署时才打开，默认全关）  ----------------
    # 这三个数是给"链接已经发出去"之后的场景准备的。默认 0 = 不限制，
    # 因为本地开发和自动化测试都不该被限流干扰：
    #   RATE_LIMIT_PER_MIN  单个 IP 每分钟最多几次请求
    #   MAX_CONCURRENCY     全局同时在处理的请求数（免费模型并发常被压到 1~5）
    #   DAILY_MAX_CALLS     全局每天累计调用上限（换 IP 也绕不过的那一道）
    #   ACCESS_CODE         访问口令，空 = 不校验
    # 只拦 /api/*，静态页面永远放行 —— 否则用户连"为什么打不开"都看不到。
    RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "0"))
    MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "0"))
    DAILY_MAX_CALLS = int(os.getenv("DAILY_MAX_CALLS", "0"))
    # 为什么有了三道限流还要一个口令：限流依赖"能认出同一个客户端"，
    # 而这个前提在托管环境里**不成立** —— 实测某平台边缘网关下，
    # 按 IP 的限流连打 70 次都没触发（每个请求都被算成新客户）。
    # 口令不依赖任何身份识别：没有口令就是不给调，换 IP、换节点都一样。
    # 口令随链接一起发（?k=xxxx），使用者不需要输入任何东西；
    # 但按大赛要求，设了口令就必须在文档显著位置给出（见 文件说明.md）。
    ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()

    # CORS 允许的来源（逗号分隔）。默认 "*" 方便本地与局域网联调；
    # 公网部署时应收敛为实际域名 —— 前端是同源调用（API=''），
    # 收紧它不会影响页面本身，只会挡住"任意站点跨域读接口"。
    ALLOW_ORIGINS = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]

    # ----------------  呼叫人工（F16）  ----------------
    # 演示期接模拟号码；若能联系到医院社工部/志愿者协会，把真实号码填进 .env 即可。
    HELPDESK_NAME = os.getenv("HELPDESK_NAME", "启明随行志愿帮帮团")
    HELPDESK_PHONE = os.getenv("HELPDESK_PHONE", "400-000-0000")
    HELPDESK_SLA_MINUTES = int(os.getenv("HELPDESK_SLA_MINUTES", "5"))
    HELPDESK_LOG = os.getenv("HELPDESK_LOG", "help_requests.jsonl")

    # ----------------  运行日志（评测用，记录每次请求的距离表，便于复算指标）  ----------------
    REQUEST_LOG = os.getenv("REQUEST_LOG", "requests.jsonl")
    LOG_REQUESTS = _b("LOG_REQUESTS", False)


config = Config()

# 真实 Key 一定是不含中文的 ASCII 长串。把"看起来像占位符"的值一并当成未配置，
# 否则用户把 .env.example 的模板原样留下（或填了个 "你的key"），
# 系统会从"演示模式"变成"模型调用失败"——比原来更难懂。
_PLACEHOLDER_VALUES = {
    "", "none", "null", "todo", "changeme",
    "your-api-key", "your_api_key", "yourkey", "api-key", "apikey",
    "sk-xxx", "xxx", "填入你的key", "在此填入", "你的key",
}


def key_usable(key: str | None) -> bool:
    """判断一个 API Key 是否**可用**（非空、非占位符）。

    独立成函数是为了让 mock 判定与 Provider 调用走同一个标准。
    两边标准一旦不一致，就会出现"判定说不用 mock、调用时 Key 是空的"这种双向故障。
    """
    k = (key or "").strip().strip('"').strip("'")
    if not k:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in k):   # 真实 Key 不含中文字符
        return False
    if len(k) < 16:                                    # 真实 Key 长度远超此值
        return False
    return k.lower() not in _PLACEHOLDER_VALUES


def vision_backends_available() -> list[str]:
    """当前**真正可用**的视觉后端列表。

    以前 mock 判定只看 ZHIPU_API_KEY，于是"只配了备用 Key（DASHSCOPE）"的用户
    会被静默判为 mock —— 备用 Key 形同虚设，用户明明配了却还是在看假答案。
    """
    out = []
    if key_usable(config.VL_API_KEY):
        out.append("zhipu")
    # 备用通道默认复用同一个智谱 Key（只是换个免费模型名），此时它不算
    # 一条**独立**的后端——否则"只配了主 Key"也会被误报成有两个后端可用，
    # 而实际主 Key 一失效就全链路同时失效。
    fb = config.VL_FALLBACK_API_KEY
    if key_usable(fb) and fb.strip() != (config.VL_API_KEY or "").strip():
        out.append("fallback")
    return out


def mock_reason() -> str:
    """返回当前**为什么**会走 mock 的可读原因（可能多条）；没走 mock 则返回空串。

    这个函数存在的唯一理由是防止"静默降级"：以前只有 MOCK_MODE 一个开关，
    用户没建 .env（ZHIPU_API_KEY 为空）时全部接口都在返回假数据，
    而 /api/meta 里 mock_mode 仍是 false，界面上只有一行小字"模型 mock"，
    结果就是"我明明点的是读文字，怎么给我念走廊"这类无从查起的现象。

    两条原因都列出：很多人是"开了 MOCK_MODE 调试"+"压根没配 Key"同时存在，
    只报一条会让人以为改掉那一条就好了。
    """
    reasons = []
    if config.MOCK_MODE:
        reasons.append("MOCK_MODE=true（显式开启了演示模式）")
    if not vision_backends_available():
        if config.VL_API_KEY:
            reasons.append("ZHIPU_API_KEY 不像一个真实 Key（疑似占位符或复制不完整）")
        else:
            reasons.append("未配置 ZHIPU_API_KEY，没有可用的视觉模型")
    return "；".join(reasons)


def mock_active() -> bool:
    """实际是否走 mock（= 显式开关 或 没有任何可用的视觉模型 Key）"""
    return bool(mock_reason())


def key_hint() -> str:
    """给界面/自检脚本用的一句话指引（不含敏感信息）"""
    if mock_active():
        return "运行 python check_model.py --set-key 填入免费 Key（约 1 分钟）"
    return ""


def _module_available(name: str) -> bool:
    """某个可选依赖是否真的装上了。"""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def asr_backend() -> str:
    """当前**实际生效**的 ASR 通道：'zhipu' / 'funasr' / ''（不可用）。

    为什么要单独抽成一个函数：判据会同时被两处使用 —— `/api/meta` 的模型声明
    与 `providers.ASRProvider` 的真实调用。两处各写一份迟早会分家，
    而分家的后果是"清单上说能识别、按下去却没反应"，这种不一致比能力弱更伤信誉。
    """
    want = config.ASR_PROVIDER
    has_cloud = key_usable(config.VL_API_KEY)
    has_local = _module_available("funasr")
    if want == "zhipu":
        return "zhipu" if has_cloud else ""
    if want == "funasr":
        return "funasr" if has_local else ""
    if has_cloud:
        return "zhipu"
    return "funasr" if has_local else ""


def asr_component() -> dict:
    """ASR 的模型声明：**按实际生效的通道**报，而不是按设计意图报。

    这里原来是写死的 `paraformer-zh / FunASR v1.1` —— 那是本地部署方案，
    而运行时 funasr 从没装上过，实际生效的一直是空转。
    大赛硬性要求声明"真实使用的模型名称与版本"，声明与实际不一致
    比"能力弱"严重得多：它是诚信问题。
    """
    backend = asr_backend()
    if backend == "zhipu":
        return {"name": "GLM-ASR-2512", "version": config.ASR_CLOUD_MODEL,
                "backend": "zhipu（云端）", "available": True, "note": ""}
    if backend == "funasr":
        return {"name": config.ASR_MODEL_NAME, "version": config.ASR_MODEL_VERSION,
                "backend": "funasr（本地）", "available": True, "note": ""}
    return {"name": "GLM-ASR-2512", "version": config.ASR_CLOUD_MODEL,
            "backend": "", "available": False,
            "note": "未配置智谱 Key 且未安装 FunASR，语音识别当前不可用"}


def _component(name: str, version: str, module: str | None, missing_note: str) -> dict:
    """一条"声明了什么 + 是否真的在跑"的模型记录。

    为什么要把这两件事分开报：清单上写满 7 个模型、实际只有视觉那一个在执行，
    是一种很容易自我说服、却经不起一次追问的状态。available=false 确实是自曝其短，
    但它同时给出 note 说明"现在由什么顶上"，比含糊其辞可信得多。
    """
    ok = bool(module) and _module_available(module)
    return {"name": name, "version": version, "available": ok,
            "note": "" if ok else missing_note}


def model_manifest() -> dict:
    """返回全部使用到的模型清单，供 /api/meta 与文档自动化引用"""
    return {
        # 语义已修正为"实际是否 mock"：原来直读环境变量，缺 Key 时会谎报 false
        "mock_mode": mock_active(),
        "mock_requested": config.MOCK_MODE,   # 环境变量里显式写死的那个值
        "mock_reason": mock_reason(),         # 为空串表示真实调用
        "key_hint": key_hint(),               # 如何切换为真实模型（无敏感信息）
        "vision_backends": vision_backends_available(),   # 实际可用的后端
        "vision": {
            "primary": {
                "provider": config.VL_PROVIDER,
                "name": config.VL_MODEL_NAME,
                "version": config.VL_MODEL_VERSION,
                # 只报"有没有配上可用的 Key"，绝不回传 Key 本身或其片段
                "key_configured": key_usable(config.VL_API_KEY),
            },
            "fallback": {
                "provider": config.VL_FALLBACK_PROVIDER,
                "name": config.VL_FALLBACK_MODEL_NAME,
                "version": config.VL_FALLBACK_MODEL_VERSION,
                "key_configured": key_usable(config.VL_FALLBACK_API_KEY),
            },
        },
        "llm": {"name": config.LLM_MODEL_NAME, "version": config.LLM_MODEL_VERSION},
        # 这四个刻意多报一个 available。只写"名称 + 版本号"会让人以为它们都在跑，
        # 而实际上缺可选依赖时 ASR / OCR / TTS / ONNX 全都不会真正执行。
        #
        # ASR 的声明由 asr_component() 现算：它已经接上云端通道，
        # 写死的名字会与"实际生效的模型"不一致（见该函数的说明）。
        "asr": asr_component(),
        "tts": _component(config.TTS_MODEL_NAME, config.TTS_MODEL_VERSION, None,
                          "后端未提供合成端点，播报由前端 Web Speech API 承担"),
        "ocr": _component(config.OCR_MODEL_NAME, config.OCR_MODEL_VERSION, "paddleocr",
                          "未安装 PaddleOCR，文字识别当前由多模态模型直读"),
        "depth": _component(config.DEPTH_MODEL_NAME, config.DEPTH_MODEL_VERSION,
                            "onnxruntime" if os.path.exists(config.DEPTH_ONNX_PATH) else None,
                            "ONNX 模型文件未随包提供，当前走几何启发式后端"),
    }


def access_guard() -> dict:
    """公网访问保护的实际生效值。对外暴露是刻意的：
    只有"能查到"才谈得上"验证过"——否则部署时是否真的打开了限流，
    谁也说不清楚，只能靠"我记得设了"。0 表示该项未启用。"""
    return {
        "rate_limit_per_min": config.RATE_LIMIT_PER_MIN,
        "max_concurrency": config.MAX_CONCURRENCY,
        "daily_max_calls": config.DAILY_MAX_CALLS,
        # 只报"要不要口令"，绝不回传口令本身或其片段
        "access_code_required": bool(config.ACCESS_CODE),
        "enabled": bool(
            config.RATE_LIMIT_PER_MIN or config.MAX_CONCURRENCY
            or config.DAILY_MAX_CALLS or config.ACCESS_CODE
        ),
    }


def depth_params() -> dict:
    """深度→步数的几何参数。写进设计文档第四章「距离可信机制」，改动需 ab_log 留痕。"""
    return {
        "cam_height_m": config.DEPTH_CAM_HEIGHT_M,
        "vfov_deg": config.DEPTH_VFOV_DEG,
        "hfov_deg": config.DEPTH_HFOV_DEG,
        "horizon_ratio": config.DEPTH_HORIZON_RATIO,
        "step_length_m": config.STEP_LENGTH_M,
        "max_steps": config.DEPTH_MAX_STEPS,
        "work_side": config.DEPTH_WORK_SIDE,
        "min_energy": config.DEPTH_MIN_ENERGY,
        # 只报"有没有"，不回传路径：/api/meta 是免口令接口，
        # 把服务器上的目录结构播给任何访客没有任何好处。
        "onnx_available": os.path.exists(config.DEPTH_ONNX_PATH),
    }


def speech_policy() -> dict:
    """播报长度策略。写进设计文档第四章「多模态交互设计」。

    核心论点：屏幕文字与语音播报是两套长度标准 —— 屏幕可反复扫读，
    语音只念一遍。因此环境播报上限 80 字，并做两件事：
    ① 分段保底：先给「建议」「前方情况」各留一份额度，剩下的才归「注意」，
       否则一句很长的「注意」会把后两段挤掉（用户只听到方位、听不到该往哪走）；
    ② 紧急重排：检测到近距离障碍时把障碍句提到最前，
       保证最关键的一句永远先被听到。
    """
    return {
        "screen_max_chars": config.MAX_REPLY_CHARS,
        "speech_max_chars": config.SPEECH_MAX_CHARS,
        "read_speech_max_chars": config.READ_SPEECH_MAX_CHARS,
        "near_steps_threshold": config.SPEECH_NEAR_STEPS,
        "zh_chars_per_second": 4.5,
        "est_seconds": round(config.SPEECH_MAX_CHARS / 4.5, 1),
    }

