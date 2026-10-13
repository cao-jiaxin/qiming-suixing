# -*- coding: utf-8 -*-
"""端到端冒烟测试：无需真实 API Key，验证全部接口在 MOCK 模式下可跑通。

用法：
    cd code/backend
    set MOCK_MODE=true            (PowerShell: $env:MOCK_MODE="true")
    python smoke_test.py

产出：
    ../../results/smoke_test_result.json  —— 可直接截图放进设计文档与答辩 PPT

为什么要这个脚本而不是手动 curl？
    ——答辩评委常问"你们怎么验证系统可用"。一个可复现、有时间戳的自动化测试报告，
      比一句"我们测过能跑"有力得多。

覆盖范围（对应 results/功能清单.csv）：
    F01–F05   环境理解（三段式 / 距离估计 / 拒答）
    F03       距离估计（含启发式后端真实产出验证）
    F06/F07   文字阅读（5 种 doc_type）
    F08       意图路由
    F09–F12   SOP 流程（就医 6 步 / 乘车 3 步 / 进度查询 / 退出重来）
    F13       无障碍交互（播报策略元信息暴露）
    F14       播报长度治理（紧急重排 / 安全段保全 / 上限约束）
    F16       呼叫人工
    F17/F18   主备降级信息、元信息
    F19       本脚本自身
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MOCK_MODE", "true")

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from core import providers  # noqa: E402
from core.config import config, mock_active  # noqa: E402
from core.pipeline import build_speech  # noqa: E402
from main import app  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "results" / "smoke_test_result.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

SPEECH_MAX = config.SPEECH_MAX_CHARS


def _raises(fn) -> bool:
    """执行 fn，抛出异常即返回 True。用于断言"这里必须抛错"的护栏。"""
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


def wav_bytes(seconds: float, rate: int = 16000) -> bytes:
    """造一段静音 WAV，用于测格式识别与时长解析（不需要真实语音）。"""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def fake_image() -> bytes:
    """造一张纯色 JPEG，避免依赖外部素材"""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), (120, 150, 180)).save(buf, "jpeg")
        return buf.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xdb" + b"\x00" * 512 + b"\xff\xd9"


def striped_image() -> bytes:
    """造一张带横向条纹的图，用于验证深度启发式后端**真的能产出**距离候选。

    纯色图没有梯度能量，启发式后端会（正确地）判定"画面没有结构"并返回空表；
    条纹图模拟地面纹理/台阶边缘，应当能产出距离项——这正是 F03 的可验证点。
    """
    try:
        from PIL import Image
        img = Image.new("RGB", (640, 480), (150, 150, 150))
        px = img.load()
        for y in range(250, 480, 14):        # 画面下半部画横条
            for x in range(640):
                px[x, y] = (55, 55, 55)
        buf = io.BytesIO()
        img.save(buf, "jpeg")
        return buf.getvalue()
    except Exception:
        return fake_image()


def main() -> int:
    client = TestClient(app)
    img = fake_image()
    striped = striped_image()
    cases: list[dict] = []

    def run(name: str, method: str, url: str, expect=None, expect_fail: bool = False, **kw):
        """expect: 断言函数，返回 True 视为通过
           expect_fail: 该用例期望失败（用于验证错误分支确实被触发）
        """
        t0 = time.time()
        try:
            r = getattr(client, method)(url, **kw)
            ok = 200 <= r.status_code < 300
            body = (r.json() if "application/json" in r.headers.get("content-type", "")
                    else r.text[:120])
            note = ""
            if expect is not None and ok:
                try:
                    good = bool(expect(body))
                except Exception as e:                      # noqa: BLE001
                    good, note = False, f"断言异常 {e}"
                if not good:
                    ok, note = False, note or "断言未通过"
            if expect_fail:
                if r.status_code == 200:
                    ok, note = False, f"本应返回错误，却返回了 200（{body}）"
                else:
                    ok, note = True, f"已正确返回 {r.status_code}"
            cases.append({
                "case": name, "method": method.upper(), "url": url,
                "status": r.status_code, "pass": ok, "note": note,
                "latency_ms": int((time.time() - t0) * 1000),
                "resp": body,
            })
        except Exception as e:  # noqa: BLE001
            cases.append({"case": name, "method": method.upper(), "url": url,
                          "status": 0, "pass": False, "note": f"EXCEPTION {e}",
                          "latency_ms": int((time.time() - t0) * 1000), "resp": None})
        return cases[-1]

    def add(name: str, passed: bool, note: str = "", resp=None, url: str = ""):
        cases.append({"case": name, "method": "DIRECT", "url": url or name,
                      "status": 200 if passed else 0, "pass": passed,
                      "note": note, "latency_ms": 0, "resp": resp})

    # ---- F17 / F18 元信息 ----
    run("服务元信息（模型清单+深度参数+播报策略）", "get", "/api/meta",
        expect=lambda b: "models" in b and "depth_params" in b
        and "speech_policy" in b
        and b["models"]["vision"]["fallback"]["name"])

    # ---- F01–F05 环境理解 ----
    run("环境理解-三段式且屏幕文字≤60字", "post", "/api/understand",
        expect=lambda b: b.get("speech") and "【" not in b["speech"]
        and len(b["speech"]) <= SPEECH_MAX,
        files={"image": ("scene.jpg", img, "image/jpeg")}, data={"query": "我现在在哪"})
    run("环境理解-播报长度受 SPEECH_MAX_CHARS 约束且带重排标记", "post", "/api/understand",
        expect=lambda b: isinstance(b.get("speech_emergency"), bool)
        and isinstance(b.get("speech_dropped"), list),
        files={"image": ("scene.jpg", img, "image/jpeg")})
    run("环境理解-对照组模式(free)", "post", "/api/understand",
        expect=lambda b: b.get("mode") == "free",
        files={"image": ("scene.jpg", img, "image/jpeg")}, data={"mode": "free"})

    # ---- 视觉模型全挂时的降级：必须仍然给出本地方位 ----
    # 来自真机反馈「总是超时、识别不出来」。实测托管沙箱连不上智谱时，
    # 三级降级全失败、后端又被网关 60s 掐成 504，用户一个字节都拿不到。
    # 但障碍的**方向和步数是本地算出来的**（深度估计 + 几何换算），与大模型无关 ——
    # 所以模型挂了也必须把方位报出来；那句"识别失败"对视障用户没有任何行动价值。
    _real_understand = providers.vision.understand
    try:
        providers.vision.understand = lambda *a, **k: {
            "text": "抱歉，现在识别不了，请再说一次或换个角度拍一张。",
            "model": "error", "fallback": True, "mock": False,
            "error": "GLM-4.6V-Flash: 429 | Thinking: timeout | 4V: timeout"}
        run("降级-视觉模型全挂时仍报本地方位（而不是回一句识别失败）", "post", "/api/understand",
            expect=lambda b: b.get("model") == "error"
            and b.get("mock") is False
            and "方位" in b.get("reply", "")
            and "识别不了" not in b.get("speech", "")
            and isinstance(b.get("distances"), list),
            files={"image": ("scene.jpg", img, "image/jpeg")})
    finally:
        providers.vision.understand = _real_understand

    # ---- 上传边界：空文件与超大文件必须被挡住 ----
    # 空 bytes 会一路传到模型调用处才失败，日志里看到的是一堆"模型调用异常"，
    # 而不是"用户没传图"，归因成本很高；超大文件则会被完整读进内存，
    # 在并发下直接吃掉 worker 的内存。
    run("上传-空图片应被拒绝（400）", "post", "/api/understand", expect_fail=True,
        files={"image": ("empty.jpg", b"", "image/jpeg")})
    run("上传-超大图片应被拒绝（413）", "post", "/api/understand", expect_fail=True,
        files={"image": ("big.jpg", b"\x00" * (13 * 1024 * 1024), "image/jpeg")})

    # ---- F03 距离估计 ----
    run("距离估计-接口可用", "post", "/api/depth",
        expect=lambda b: "distances" in b and "params" in b,
        files={"image": ("scene.jpg", img, "image/jpeg")})

    # 直连模块验证启发式后端。
    #
    # 这里**不能**再用 importlib.reload(core.config) 去临时改 MOCK_MODE：
    # 那会把模块内的 config 换成新对象，而本文件顶部 `from core.config import config`
    # 以及 providers / pipeline 里早已绑定的仍是旧对象。后果是之后所有
    # "改 config → 断言" 的用例**静默失效**（值写进了旧对象，被调用的是新对象），
    # 现象是断言永远不过、且看不出原因。这个坑本轮真实踩到过。
    #
    # 另外 depth.estimate() 现在已无 MOCK_MODE 短路（无 Key 时本就该给出真实距离估计，
    # 否则演示与评测都会缺项），所以当初 reload 的理由也已不存在。
    try:
        from core import depth as depth_mod

        ds = depth_mod.estimate(striped)
        add("距离估计-F03 启发式后端真实产出步数", bool(ds),
            "" if ds else "条纹图未产出距离项，请检查 core/depth.py 与 DEPTH_* 参数",
            ds, "core.depth.estimate")
        if ds:
            add("距离估计-方位为时钟点位且步数为正",
                all(str(d["bearing"]).endswith("点") and d["steps"] >= 1 for d in ds),
                str(ds[:3]), ds, "core.depth.estimate")
    except Exception as e:  # noqa: BLE001
        add("距离估计-F03 启发式后端真实产出步数", False, f"EXCEPTION {e}")

    # ---- F03 方位映射（回归用例） ----
    # 这里曾经真实写错，而且错得很危险：早期把画面从左到右的 12 列
    # 直接命名成 12 点、1 点、2 点……等价于宣布"画面最左列 = 正前方"。
    # 结果是障碍明明在用户左前方 30°，播报却说"12 点方向有障碍"，
    # 用户朝正前方迈步撞上去。方位是安全信息，必须有断言守着。
    try:
        from core import depth as _dep

        add("距离-方位映射：画面中央 = 12 点（正前方）",
            _dep.bearing_from_x(0.5) == "12点", f"x=0.5 → {_dep.bearing_from_x(0.5)}")
        add("距离-方位映射：画面右侧 = 1 点（右前方）",
            _dep.bearing_from_x(0.9) == "1点", f"x=0.9 → {_dep.bearing_from_x(0.9)}")
        add("距离-方位映射：画面左侧 = 11 点（左前方）",
            _dep.bearing_from_x(0.1) == "11点", f"x=0.1 → {_dep.bearing_from_x(0.1)}")
        _cols = (_dep.column_bearing(1), _dep.column_bearing(6), _dep.column_bearing(10))
        add("距离-方位映射：列序由左至右依次为 11 / 12 / 1 点",
            _cols == ("11点", "12点", "1点"), " / ".join(_cols))
        add("距离-方位映射：最左列不得被标成正前方（防回归）",
            _dep.column_bearing(0) != "12点", f"列0 → {_dep.column_bearing(0)}")
    except Exception as e:  # noqa: BLE001
        add("距离-方位映射", False, f"EXCEPTION {e}")

    # ---- F03 假阳性控制：没有结构的画面不应报出障碍 ----
    # 只看 P75 相对阈值时，JPEG 压缩噪声本身就能越过阈值，
    # 于是一面白墙也会被报成"处处有障碍"。这对视障用户不是信息而是噪音，
    # 会让人不敢走路，所以必须有一条绝对下限的断言守着。
    try:
        import numpy as _np
        from PIL import Image as _PILImage

        from core import depth as _dep2

        _buf = io.BytesIO()
        _PILImage.new("RGB", (640, 480), (128, 128, 128)).save(_buf, "JPEG", quality=95)
        _flat = _dep2.estimate(_buf.getvalue())
        add("距离-纯色画面不报障碍", _flat == [], str(_flat)[:120])

        _rng = _np.random.default_rng(7)
        _noisy = _np.clip(128 + _rng.integers(-1, 2, (480, 640, 3)), 0, 255).astype("uint8")
        _buf2 = io.BytesIO()
        _PILImage.fromarray(_noisy).save(_buf2, "JPEG", quality=100)
        _nres = _dep2.estimate(_buf2.getvalue())
        add("距离-只有镜头噪点的画面不报障碍（绝对能量下限生效）",
            _nres == [], str(_nres)[:120])
    except Exception as e:  # noqa: BLE001
        add("距离-假阳性控制", False, f"EXCEPTION {e}")

    # ---- 图像预处理：EXIF 方向（回归用例） ----
    # 手机竖拍的照片像素本身是躺倒的，方向只记在 EXIF 里；而重新编码成 JPEG
    # 时 PIL 默认不写 EXIF。不主动摆正，等于把用户的照片转 90° 交给模型，
    # 模型给出的"左边/右边/正前方"随之全错。
    try:
        from PIL import Image as _PILImage2

        from core.providers import compress_image

        _src = _PILImage2.new("RGB", (200, 100), (10, 20, 30))
        _exif = _PILImage2.Exif()
        _exif[274] = 6                      # Orientation=6：需顺时针转 90° 才是正的
        _buf3 = io.BytesIO()
        _src.save(_buf3, "JPEG", exif=_exif)
        _out = compress_image(_buf3.getvalue(), max_side=1000)
        _got = _PILImage2.open(io.BytesIO(_out))
        add("图像-按 EXIF 摆正手机竖拍照片（不再躺倒）",
            _got.height > _got.width,
            f"输入 200×100（EXIF=6）→ 输出 {_got.width}×{_got.height}")
    except Exception as e:  # noqa: BLE001
        add("图像-EXIF 方向", False, f"EXCEPTION {e}")

    # ---- F14 播报长度治理（build_speech 直接单测，不进网络层） ----
    _cases = [
        # (用例名, 三段式输入, 期望：紧急重排 / 必须保留的关键词)
        ("播报-正常场景不触发重排且不超上限",
         "【前方情况】走廊，光线尚可，两侧有扶手\n【注意】暂无明显障碍\n【建议】沿 12 点方向直行约 20 步",
         False, "12 点方向"),
        ("播报-近距离障碍触发紧急重排且障碍句前置",
         "【前方情况】这是一条比较长的医院走廊，地面铺着浅色地砖，两侧墙上挂着不少宣传画\n"
         "【注意】12 点方向约 2 步处有台阶\n【建议】请先停下脚步，用脚尖试探一下台阶高度再走",
         True, "12 点方向约 2 步"),
        ("播报-远距离障碍不触发重排",
         "【前方情况】宽敞的门诊大厅，右侧是挂号窗口排队区域，左侧有几个自助挂号机，人流较多\n"
         "【注意】9 点方向约 8 步处有立式指示牌\n【建议】建议向 3 点方向移动两步避开人流",
         False, "立式指示牌"),
        ("播报-三段全超长时安全段仍必须念出",
         "【前方情况】" + "这是一条宽敞明亮而且人流量比较大的门诊大厅走廊两侧还有不少宣传展板" * 2 + "\n"
         "【注意】左手边两点钟方向附近大概三步左右的距离上横放着一张金属材质的候诊长椅\n"
         "【建议】请立刻停下脚步不要再往前走并且向三点钟方向平移大约两步避开椅子",
         False, "两点钟方向"),
        ("播报-非三段式输出退回纯裁剪",
         "这是一张医院走廊的照片，画面里有几扇门和一条长椅，光线一般。",
         False, ""),
    ]
    for name, txt, want_emg, must_keep in _cases:
        res = build_speech(txt)
        ok = (len(res["speech"]) <= SPEECH_MAX
              and res["emergency"] == want_emg
              and "note" not in res["dropped"]          # 安全段永不丢弃
              and (must_keep in res["speech"] if must_keep else True))
        add(name, ok,
            f"字数 {len(res['speech'])}/{SPEECH_MAX} 重排 {res['emergency']} "
            f"丢弃 {res['dropped'] or '无'} → {res['speech']}",
            res, "core.pipeline.build_speech")

    # 用户反馈：识别结果的三段（前方情况 / 注意 / 建议）必须**都念出来**。
    # 原先额度 30 字，一句很长的「注意」（实测 38 字的三个方位）就把预算吃光，
    # 用户只听到方位，既听不到"这是什么地方"，也听不到"该往哪走"。
    _three = ("【前方情况】桌面上有笔记本电脑和杂物\n"
              "【注意】12点方向约6步有障碍，1点方向约6步有障碍，11点方向约15步有障碍\n"
              "【建议】向右前方绕行")
    _r3 = build_speech(_three)
    add("播报-三段齐全（长「注意」不吃掉「建议」与「前方情况」）",
        all(k in _r3["speech"] for k in ("笔记本电脑", "15步", "绕行"))
        and not _r3["dropped"] and len(_r3["speech"]) <= SPEECH_MAX,
        f"字数 {len(_r3['speech'])}/{SPEECH_MAX} 丢弃 {_r3['dropped'] or '无'} → {_r3['speech']}",
        _r3, "core.pipeline.build_speech")

    # 边界：额度小到装不下保底时，必须退回纯优先级 —— 安全段优先的原则不让步。
    _tight = build_speech(_three, limit=20)
    add("播报-额度极紧时退回纯优先级（安全段优先不让步）",
        "步" in _tight["speech"] and len(_tight["speech"]) <= 20,
        f"字数 {len(_tight['speech'])}/20 丢弃 {_tight['dropped'] or '无'} → {_tight['speech']}",
        _tight, "core.pipeline.build_speech")

    # ---- 文本治理：边界与缺段（回归用例） ----
    from core.pipeline import _clip, _ensure_three_sections

    _clipped = _clip("这是一段很长的测试文本，" * 8, 20)
    add("文本-裁剪结果严格不超过上限（补的句号也不能超）",
        len(_clipped) <= 20, f"{len(_clipped)} 字 → {_clipped}")

    _dup = "【前方情况】走廊\n【注意】暂无明显障碍\n【注意】12 点方向约 2 步有台阶"
    _fixed = _ensure_three_sections(_dup)
    add("文本-模型输出重复段时仍补齐缺失的「建议」段",
        _fixed.count("【建议】") == 1 and _fixed.count("【前方情况】") == 1,
        _fixed.replace("\n", " ｜ "))

    # ---- F06/F07 文字阅读（5 种 doc_type） ----
    for dt in ("auto", "drug", "sign", "price", "doc"):
        run(f"文字阅读-{dt}", "post", "/api/read",
            expect=lambda b, dt=dt: b.get("doc_type") == dt and b.get("speech"),
            files={"image": ("text.jpg", img, "image/jpeg")},
            data={"query": "这写的什么", "doc_type": dt})

    # ---- 回归护栏：mock 必须分任务，读文字不得吐出场景文案 ----
    # 曾经只有一句共用的 MOCK_REPLY（场景三段式），而 understand() 是
    # 场景理解 / 文字阅读 / SOP 校验三条链路共用的入口，于是无 Key 时
    # "拍照读文字"返回了"这是室内走廊"—— 接口 200、零报错、测试全绿。
    for dt in ("auto", "drug", "sign", "price", "doc"):
        r = run(f"护栏-文字阅读({dt})不得返回场景描述", "post", "/api/read",
                expect=lambda b: ("【前方情况】" not in (b.get("reply") or "")
                                  and "走廊" not in (b.get("reply") or "")
                                  and b.get("mock") is True),
                files={"image": ("text.jpg", img, "image/jpeg")},
                data={"query": "这写的什么", "doc_type": dt})
        reply = (r["resp"] or {}).get("reply", "") if isinstance(r["resp"], dict) else ""
        add(f"护栏-文字阅读({dt})确实给出了该类型的示例内容", bool(reply.strip()),
            f"{reply[:40]}…")

    sc = run("护栏-场景理解仍返回三段式（未被读文字文案污染）", "post", "/api/understand",
             expect=lambda b: "【前方情况】" in (b.get("reply") or "")
             and b.get("mock") is True,
             files={"image": ("scene.jpg", img, "image/jpeg")}, data={"query": ""})
    _read_auto = providers.vision.read_mock("auto")
    add("护栏-场景理解与文字阅读使用不同 mock 文案",
        _read_auto != providers.vision.MOCK_REPLY_SCENE
        and "【前方情况】" not in _read_auto
        and _read_auto not in ((sc["resp"] or {}).get("reply", "")),
        f"场景『{providers.vision.MOCK_REPLY_SCENE[:14]}…』"
        f"阅读『{_read_auto[:14]}…』")

    # mock 状态必须如实上报：原来 /api/meta 直读环境变量，
    # 没建 .env（无 Key）时仍报 mock_mode=false，用户据此以为在跑真模型。
    mm = run("护栏-meta 如实上报实际 mock 状态", "get", "/api/meta",
             expect=lambda b: (b["models"]["mock_mode"] is mock_active()
                               and b["models"].get("mock_reason") is not None),
             )
    add("护栏-未配置 Key 时 mock_reason 说清原因",
        bool((mm["resp"] or {}).get("models", {}).get("mock_reason")),
        ((mm["resp"] or {}).get("models", {}).get("mock_reason") or "（空）"))

    # ---- 真实模型配置链路的护栏 ----
    # 用户实测报障："我怎么样才能用真实的模型"。根因不是功能缺失，而是
    # ①start_lan.cmd 写死了 MOCK_MODE=true（双击启动 = 强制演示模式）；
    # ②没建 .env 时静默退化为示例，界面上只有一句原因。
    # 下面几条守住"配置能被正确识别 + 状态能被用户自助查到"。
    from core.config import (  # noqa: PLC0415
        key_usable, mock_reason, vision_backends_available,
    )

    add("护栏-占位符 Key 不得被当成真实 Key",
        not key_usable("") and not key_usable("   ") and not key_usable("你的key")
        and not key_usable("sk-xxx") and not key_usable("123")
        and key_usable("abcdef1234567890abcdef1234567890"),
        "空值/中文/短串一律判为不可用，只有长 ASCII 串算可用")

    # config 是进程级单例，被 providers / pipeline / main 各自 import 绑定。
    # 一旦有人 reload 它，本地变量与模块全局就会指向两个对象，
    # 之后所有"改配置再断言"的用例都会静默失效。这条断言把该风险钉死。
    import core.config as _cc  # noqa: PLC0415

    add("护栏-config 单例未被替换（reload 会使断言静默失效）",
        config is _cc.config, f"{id(config)} vs {id(_cc.config)}")

    # 只配了备用 Key（DASHSCOPE）时也必须算"接入了真实模型"。
    # 曾经 mock 判定只看 ZHIPU_API_KEY，导致配了备用 Key 的用户静默退回 mock，
    # 备用 Key 形同虚设——用户明明配了却还在看示例，且没有任何提示。
    _saved = (config.VL_API_KEY, config.VL_FALLBACK_API_KEY, config.MOCK_MODE)
    try:
        config.MOCK_MODE = False          # 排除显式开关，只看 Key
        config.VL_API_KEY = ""
        config.VL_FALLBACK_API_KEY = "abcdef1234567890abcdef1234567890"
        _fb_backends = list(vision_backends_available())
        _fb_reason = mock_reason()
        only_fallback_ok = (_fb_backends == ["fallback"] and not _fb_reason)
        # 备用通道默认复用主 Key 时，不得被算成"两条独立后端"
        # （否则主 Key 一失效就是整条链同时失效，界面却显示两个后端可用）
        config.VL_API_KEY = "abcdef1234567890abcdef1234567890"
        config.VL_FALLBACK_API_KEY = config.VL_API_KEY
        _dedup_ok = vision_backends_available() == ["zhipu"]
        config.VL_API_KEY = ""
        config.VL_FALLBACK_API_KEY = ""
        _none_reason = mock_reason()
        both_missing_ok = bool(_none_reason) and not vision_backends_available()
    finally:
        (config.VL_API_KEY, config.VL_FALLBACK_API_KEY,
         config.MOCK_MODE) = _saved

    add("护栏-只配备用 Key 也判为接入真实模型（不再静默退回 mock）",
        only_fallback_ok,
        f"backends={_fb_backends} reason={_fb_reason!r}")
    add("护栏-两个 Key 都不可用时判为 mock 且必须给出原因",
        both_missing_ok, f"reason={_none_reason!r}")
    add("护栏-备用 Key 与主 Key 相同时只算一条后端（不虚报冗余）",
        _dedup_ok, "主备同一个 Key 时 backends 应为 ['zhipu']")

    _meta_models = (mm["resp"] or {}).get("models", {})
    add("护栏-meta 暴露 key_hint 与 vision_backends（界面据此指导用户）",
        isinstance(_meta_models.get("key_hint"), str)
        and isinstance(_meta_models.get("vision_backends"), list),
        f"key_hint={_meta_models.get('key_hint')!r} "
        f"backends={_meta_models.get('vision_backends')!r}")
    _real_key = config.VL_API_KEY or config.VL_FALLBACK_API_KEY
    add("护栏-meta 不得回传 Key 本身或其片段",
        (not _real_key)
        or (_real_key not in json.dumps(_meta_models, ensure_ascii=False)),
        "配了真实 Key 时校验它没有出现在 /api/meta 响应里")
    add("护栏-模型自检脚本存在（用户自助排查的唯一入口）",
        (Path(__file__).parent / "check_model.py").exists(),
        "code/backend/check_model.py")

    # ---- 思维链清洗（2026-09-26 真机抓到）----
    # 降级链第②级 GLM-4.1V-Thinking-Flash 会返回
    #   <think>…推理过程…</think><answer>正式答案</answer>
    # 本项目是语音播报产品：不剥离就是把模型的自言自语念给站在路边的视障用户听。
    # 第三种情况最阴——被 max_tokens 截断时只有开标签没有闭标签，
    # "配对删除"完全失效，必须从开标签截到结尾。
    from core.providers import _extract_content, strip_reasoning  # noqa: PLC0415

    add("护栏-思维链标签必须剥离（<think>+<answer> 结构）",
        strip_reasoning("<think>图是圆形，红色。</think><answer>图中是一个红色的圆形。</answer>")
        == "图中是一个红色的圆形。",
        "只保留 <answer> 内容")

    add("护栏-思维链无 answer 段时整块删除",
        strip_reasoning("<think>先看颜色再看形状。</think>前面有个台阶。").strip()
        == "前面有个台阶。",
        "配对删除 <think>…</think>，保留其余文本")

    add("护栏-思维链被截断（只有开标签）时从开标签截到结尾",
        strip_reasoning("<think>用户现在需要回答形状和颜色，图是圆形，红") == "",
        "宁可这一轮没答案触发降级，也不能念半截思维链")

    add("护栏-思维链行不通时必须抛错以触发降级（不能返回空串）",
        _raises(lambda: _extract_content(
            {"choices": [{"message": {"content": "<think>只有思考没有答案"}}]})),
        "空串会被上层误读成'模型说没有内容'，抛错才会去试下一个模型")

    add("护栏-strip_reasoning 不误伤正常文本",
        strip_reasoning("【前方情况】这是走廊。\n【注意】无明显障碍。")
        == "【前方情况】这是走廊。\n【注意】无明显障碍。",
        "正常三段式文案必须原样通过")

    # ---- 输出额度分档（2026-09-26 实测得出，不能随手改）----
    # 额度给大了：延迟翻倍（512→5.0s，1024→11.2s），对视障"边走边听"是硬伤；
    # 额度给小了：思考型模型把额度全用在思维链上，正式答案写不完 → 那一级白等。
    add("护栏-兜底模型输出额度不得超过其上限 1K（GLM-4V-Flash 最大输出）",
        config.VL_MAX_TOKENS_LAST <= 1024,
        f"VL_MAX_TOKENS_LAST={config.VL_MAX_TOKENS_LAST}，给大了上游直接 400")
    add("护栏-一级降级额度不得小于主力（思考型模型需要更多额度）",
        config.VL_MAX_TOKENS_THINKING >= config.VL_MAX_TOKENS,
        f"主力={config.VL_MAX_TOKENS} 一级降级={config.VL_MAX_TOKENS_THINKING}")
    add("护栏-输出额度必须为正数（0 会让上游报参数错误）",
        config.VL_MAX_TOKENS > 0 and config.VL_MAX_TOKENS_THINKING > 0
        and config.VL_MAX_TOKENS_LAST > 0,
        f"{config.VL_MAX_TOKENS}/{config.VL_MAX_TOKENS_THINKING}/{config.VL_MAX_TOKENS_LAST}")

    # ---- F08 意图路由 ----
    for text, want in (("帮我读一下这个药盒", "read"),
                       ("我要去医院挂号", "sop_medical"),
                       ("这趟公交到哪", "sop_transit"),
                       ("帮我叫个人来", "help_human"),
                       ("前面有什么", "scene")):
        run(f"意图路由-{want}", "post", "/api/route",
            expect=lambda b, want=want: b.get("intent") == want, data={"text": text})

    # ---- F09/F10/F11/F12 SOP ----
    run("SOP 流程列表（含就医与乘车）", "get", "/api/sop/flows",
        expect=lambda b: any(f["id"] == "sop_medical" for f in b["flows"])
        and any(f["id"] == "sop_transit" for f in b["flows"]))

    s = run("就医引导-开始（6步，index=1）", "post", "/api/sop/start",
            expect=lambda b: b.get("total_steps") == 6 and b.get("index") == 1,
            data={"flow_id": "sop_medical"})
    sid = (s["resp"] or {}).get("session_id", "") if isinstance(s["resp"], dict) else ""

    if sid:
        run("就医引导-拍照校验推进", "post", "/api/sop/step",
            expect=lambda b: "index" in b and "total_steps" in b,
            files={"image": ("step.jpg", img, "image/jpeg")}, data={"session_id": sid})
        # mock 下拍照片必须真的能推进。曾经 mock 返回的是场景文案，
        # check_sop_step 用 safe_json 解析必然失败 → reached 恒为 False，
        # 表现为"mock 模式下拍照永远推不动流程"，用户只会以为 SOP 坏了。
        run("护栏-SOP 拍照校验在 mock 下能推进（reached=true）", "post", "/api/sop/step",
            expect=lambda b: b.get("reached") is True,
            files={"image": ("step.jpg", img, "image/jpeg")}, data={"session_id": sid})
        run("就医引导-手动确认兜底通道", "post", "/api/sop/step",
            expect=lambda b: b.get("reached") is True,
            data={"session_id": sid, "manual_reached": "1"})
        run("就医引导-查询进度", "get", f"/api/sop/{sid}",
            expect=lambda b: b.get("total_steps") == 6)
        run("就医引导-退出/重来(F12)", "delete", f"/api/sop/{sid}",
            expect=lambda b: b.get("ok") is True)
        run("SOP-会话已删除后查询应 404", "get", f"/api/sop/{sid}", expect_fail=True)

    t = run("乘车引导-开始（3步）", "post", "/api/sop/start",
            expect=lambda b: b.get("total_steps") == 3,
            data={"flow_id": "sop_transit"})
    tsid = (t["resp"] or {}).get("session_id", "") if isinstance(t["resp"], dict) else ""
    if tsid:
        run("乘车引导-第1次推进", "post", "/api/sop/step",
            data={"session_id": tsid, "manual_reached": "1"})
        run("乘车引导-第2次推进", "post", "/api/sop/step",
            data={"session_id": tsid, "manual_reached": "1"})
        # F11b 退回上一步。为什么这条必须进冒烟：
        # 视障用户判断"到了没有"只能听一句描述，误判概率远高于明眼用户，
        # 而 SopSession.index 原实现只能 +1 —— 误判一次的唯一出路是退出流程
        # （DELETE 会话）从第 1 步重走。这不是体验问题，是"救不回来"的问题。
        run("乘车引导-退回上一步（判错了必须能挽回，否则只能退出重走）", "post", "/api/sop/back",
            expect=lambda b: b.get("back") is True and b.get("moved") is True
            and b.get("finished") is False and b.get("index") == 2,
            data={"session_id": tsid})
        run("乘车引导-退回后可以重新推进（退回去不是终点）", "post", "/api/sop/step",
            data={"session_id": tsid, "manual_reached": "1"})
        run("乘车引导-第3次推进应 finished", "post", "/api/sop/step",
            expect=lambda b: b.get("finished") is True,
            data={"session_id": tsid, "manual_reached": "1"})
        run("乘车引导-已完成后再推进不报错", "post", "/api/sop/step",
            expect=lambda b: b.get("finished") is True,
            data={"session_id": tsid, "manual_reached": "1"})
        run("乘车引导-已完成后退回上一步（误判最后一步是最常见的一种）", "post", "/api/sop/back",
            expect=lambda b: b.get("finished") is False and b.get("moved") is True,
            data={"session_id": tsid})

    g = run("就医引导-开始（退回边界用例用）", "post", "/api/sop/start",
            data={"flow_id": "sop_medical"})
    gsid = (g["resp"] or {}).get("session_id", "") if isinstance(g["resp"], dict) else ""
    if gsid:
        # 已经在第 1 步时退回：必须"停在原地 + 明确说明"，不能报错。
        # 报错的话用户会以为应用坏了，然后去做更危险的补救（退出重走）。
        run("护栏-第 1 步退回不报错、停在原地（返回 moved=false）", "post", "/api/sop/back",
            expect=lambda b: b.get("moved") is False and b.get("index") == 1
            and b.get("finished") is False and "第一步" in b.get("speech", ""),
            data={"session_id": gsid})
        run("护栏-原地退回后总步数不变（不能把 total 也一起退掉）", "get", f"/api/sop/{gsid}",
            expect=lambda b: b.get("total_steps") == 6)
        run("护栏-原地退回后 history 为空（没走过的一步不能被算进路径）", "get", f"/api/sop/{gsid}",
            expect=lambda b: b.get("history") == [])

    run("SOP-退回未知会话应返回 404", "post", "/api/sop/back", expect_fail=True,
        data={"session_id": "no-such"})

    run("SOP-未知会话应返回 404", "post", "/api/sop/step", expect_fail=True,
        data={"session_id": "no-such", "manual_reached": "1"})

    # ---- F16 呼叫人工 ----
    h = run("呼叫人工-发起求助（含票号与电话）", "post", "/api/help",
            expect=lambda b: b.get("ticket_id") and b.get("speech")
            and b.get("queue_no") and b.get("helper_phone"),
            data={"text": "我在医院门口找不到路，帮我叫个人", "location_hint": "门诊楼门口"})
    tid = (h["resp"] or {}).get("ticket_id", "") if isinstance(h["resp"], dict) else ""
    if tid:
        run("呼叫人工-查询工单", "get", f"/api/help/{tid}",
            expect=lambda b: b.get("status") == "waiting")
        run("呼叫人工-结束工单", "post", f"/api/help/{tid}/finish", data={"note": "志愿者已到"})
        run("呼叫人工-工单列表", "get", "/api/help",
            expect=lambda b: isinstance(b.get("items"), list))
    run("呼叫人工-原因分类为 unwell（身体不适）", "post", "/api/help",
        expect=lambda b: b.get("reason") == "unwell", data={"text": "我有点头晕，站不住了"})
    run("呼叫人工-不存在工单应 404", "get", "/api/help/nosuchticket", expect_fail=True)

    # ---- 语音识别占位 ----
    run("语音识别（占位，无 audio 后端时返回空）", "post", "/api/asr",
        files={"audio": ("a.webm", b"\x1a\x45\xdf\xa3" * 8, "audio/webm")})

    # ---- 公网访问保护（RATE_LIMIT_PER_MIN / MAX_CONCURRENCY / DAILY_MAX_CALLS）----
    # 这三道闸是为"链接已经发给别人"之后准备的。测试要盯住两件事：
    #   ① 默认全关时**完全无感** —— 本地开发和这份测试都不该被限流干扰；
    #   ② 打开时确实拦得住，而且拒绝语是**人话**（前端会把 detail 直接念给用户听）。
    import main as qm_main  # noqa: PLC0415

    def _gate_restore():
        """配置是单例，直接改实例属性再改回来。
        绝不 importlib.reload(core.config)：那会把模块里的 config 换成新对象，
        而各文件顶部 `from core.config import config` 绑定的仍是旧对象，
        之后所有"改配置再断言"的用例都会静默失效（踩过）。"""
        config.RATE_LIMIT_PER_MIN = 0
        config.MAX_CONCURRENCY = 0
        config.DAILY_MAX_CALLS = 0
        qm_main._rate.clear()
        qm_main._day[0], qm_main._day[1] = None, 0
        qm_main._concurrency = 0

    try:
        _gate_restore()

        codes = [client.get("/api/meta").status_code for _ in range(12)]
        add("默认不限流：连续 12 次请求全部放行（本地开发不受干扰）",
            all(c == 200 for c in codes), f"状态码 {set(codes)}")

        config.RATE_LIMIT_PER_MIN = 3
        qm_main._rate.clear()
        seq = [client.get("/api/meta").status_code for _ in range(5)]
        add("单 IP 限流：超过每分钟上限后返回 429",
            seq[:3] == [200, 200, 200] and seq[3] == 429,
            f"前 5 次状态码 {seq}")
        last = client.get("/api/meta")
        detail = ""
        try:
            detail = last.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        add("限流拒绝语是人话（前端会把 detail 原样念给用户）",
            last.status_code == 429 and "频繁" in detail and len(detail) <= 30,
            f"{last.status_code} {detail}")
        add("限流不影响静态页面（否则用户连\"为什么打不开\"都看不到）",
            client.get("/").status_code == 200)

        config.RATE_LIMIT_PER_MIN = 0
        config.MAX_CONCURRENCY = 1
        qm_main._concurrency = 1                      # 手工把名额占满
        r_conc = client.get("/api/meta")
        add("并发上限：名额占满时返回 429 而不是排队堆积",
            r_conc.status_code == 429, f"{r_conc.status_code}")

        config.MAX_CONCURRENCY = 0
        config.DAILY_MAX_CALLS = 1
        qm_main._day[0], qm_main._day[1] = time.strftime("%Y-%m-%d"), 999
        r_day = client.get("/api/meta")
        day_detail = ""
        try:
            day_detail = r_day.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        add("每日累计上限：用尽后拒绝，且说明\"明天再来\"",
            r_day.status_code == 429 and "明天" in day_detail,
            f"{r_day.status_code} {day_detail}")
    finally:
        _gate_restore()

    # 并发计数必须在异常/拒绝路径上也归还。只增不减的话，
    # 几次之后就再也没有任何请求能通过——而且是静默的。
    add("并发计数归零（名额泄漏会让全站永久拒绝服务）",
        qm_main._concurrency == 0, f"当前 {qm_main._concurrency}")
    add("保护默认全关（env 未设时三项均为 0，生产行为由部署环境显式决定）",
        config.RATE_LIMIT_PER_MIN == 0 and config.MAX_CONCURRENCY == 0
        and config.DAILY_MAX_CALLS == 0)

    # ---- 访问口令（部署版的最后一道闸）----
    # 限流依赖"能认出同一个客户端"，而这个前提在托管环境里不成立
    # （实测某平台边缘网关下，按 IP 限流连打 70 次都没触发）。
    # 口令不依赖任何身份识别，是唯一一条换 IP、换节点都绕不过的规则。
    img_kw = dict(files={"image": ("t.jpg", img, "image/jpeg")})
    try:
        config.ACCESS_CODE = "unit-test-code"

        r_no = client.post("/api/understand", **img_kw)
        no_detail = ""
        try:
            no_detail = r_no.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        add("缺口令：识别接口返回 401", r_no.status_code == 401, f"{r_no.status_code}")
        add("缺口令的提示是人话且足够短（要能当一句话念完）",
            "口令" in no_detail and len(no_detail) <= 32, no_detail)

        r_wrong = client.post("/api/understand",
                              headers={"X-Access-Code": "wrong"}, **img_kw)
        add("口令错误同样被拒（不是「带了就放行」）",
            r_wrong.status_code == 401, f"{r_wrong.status_code}")

        r_head = client.post("/api/understand",
                             headers={"X-Access-Code": "unit-test-code"}, **img_kw)
        add("口令走请求头：放行", r_head.status_code == 200, f"{r_head.status_code}")

        r_query = client.post("/api/understand?k=unit-test-code", **img_kw)
        add("口令走查询参数：也放行（方便 curl 手工验证）",
            r_query.status_code == 200, f"{r_query.status_code}")

        add("元信息接口不校验口令（页面靠它决定要不要挂演示横幅）",
            client.get("/api/meta").status_code == 200)
        add("静态页面不校验口令（否则用户连「为什么打不开」都看不到）",
            client.get("/").status_code == 200)
        add("元信息只报「要不要口令」，不回传口令本身或片段",
            "unit-test-code" not in json.dumps(
                client.get("/api/meta").json(), ensure_ascii=False))
    finally:
        config.ACCESS_CODE = ""

    add("口令默认为空 = 不校验（本地开发与这份测试零影响）",
        config.ACCESS_CODE == "")

    # ================================================================
    # 回归护栏：2026-10-06 全面评测查出的缺陷
    #
    # 下面每一条都对应一个**真实存在过**、且大多"未鉴权即可触发"的问题。
    # 之所以逐条钉成断言，是因为它们都属于"不报错、只是行为不对"的类型：
    # 不写测试，下次重构一定会再犯。
    # ================================================================

    # ① 非 ASCII 口令。compare_digest 对含非 ASCII 的 str 会抛 TypeError，
    #    修之前任何人 `?k=中文` 就能把这件事变成 500。
    try:
        config.ACCESS_CODE = "unit-test-code"
        r_cn_q = client.post("/api/route", params={"k": "中文口令"}, data={"text": "测试"})
        add("非 ASCII 口令（查询串）只返回 401，不再 500",
            r_cn_q.status_code == 401, f"{r_cn_q.status_code}")
        # 请求头里的非 ASCII：HTTP 头合法上是 ASCII，但服务端是按 latin-1
        # 解码头字节的，所以线上确实有人能塞进"非 ASCII 的 str"。
        # 这里直接用 bytes 绕过 httpx 的 ASCII 校验，模拟那条真实路径。
        r_cn_h = client.post("/api/route",
                             headers={b"X-Access-Code": "中文口令".encode("utf-8")},
                             data={"text": "测试"})
        add("非 ASCII 口令（请求头原始字节）同样只返回 401",
            r_cn_h.status_code == 401, f"{r_cn_h.status_code}")
        r_ok = client.post("/api/route", headers={"X-Access-Code": "unit-test-code"},
                           data={"text": "测试"})
        add("修完编码问题后，正确口令仍然放行（没有误伤）",
            r_ok.status_code == 200, f"{r_ok.status_code}")
    finally:
        config.ACCESS_CODE = ""

    # ② 未知 flow_id。交给 sop.start_session 抛 KeyError 的话，
    #    用户看到的是 500 + "Internal Server Error"，既不知道错在哪也不知正确取值。
    r_flow = client.post("/api/sop/start", data={"flow_id": "not_a_flow"})
    flow_detail = ""
    try:
        flow_detail = r_flow.json().get("detail", "")
    except Exception:  # noqa: BLE001
        pass
    add("未知 flow_id 返回 400 并列出可选流程（而不是 500）",
        r_flow.status_code == 400 and "sop_medical" in flow_detail,
        f"{r_flow.status_code} {flow_detail}")

    # ③ 免口令的 /api/meta 不该播报服务器内部路径。
    meta_body = client.get("/api/meta").json()
    add("元信息不泄漏 onnx_path（免口令接口不暴露目录结构）",
        "onnx_path" not in json.dumps(meta_body, ensure_ascii=False))

    # ④ 模型清单必须区分"声明了"与"真的在跑"。
    _md = meta_body.get("models", {})
    add("模型清单如实标注 available（可选模型不再只报名字与版本）",
        all("available" in (_md.get(k) or {}) for k in ("asr", "tts", "ocr", "depth")))
    add("未实现的 TTS 被标为不可用，并说明由谁顶上",
        (_md.get("tts") or {}).get("available") is False
        and bool((_md.get("tts") or {}).get("note")),
        str((_md.get("tts") or {}).get("note"))[:60])

    # ⑤ 上传只认魔数：不信 Content-Type，也不信扩展名。
    r_bad = client.post("/api/depth",
                        files={"image": ("x.png", b"this is definitely not an image", "image/png")})
    add("纯文本伪装成 image/png 被 400 挡住（不再白花模型额度）",
        r_bad.status_code == 400, f"{r_bad.status_code}")
    r_wav = client.post("/api/depth",
                        files={"image": ("x.png", b"RIFF\x00\x00\x00\x00WAVEfmt ", "image/png")})
    add("音频容器伪装成 png 同样被挡（仅靠扩展名判断会漏掉）",
        r_wav.status_code == 400, f"{r_wav.status_code}")
    r_real = client.post("/api/depth", files={"image": ("scene.jpg", img, "image/jpeg")})
    add("校验没有误伤真实照片", r_real.status_code == 200, f"{r_real.status_code}")

    # ⑥ 文本入参长度上限：query 会被拼进提示词，无上限就是成本与注入的双重风险。
    r_long = client.post("/api/route", data={"text": "长" * 500})
    add("超长文本入参被 422 拒绝（防提示注入与成本失控）",
        r_long.status_code == 422, f"{r_long.status_code}")

    # ⑦ 安全响应头。刻意**不设** X-Frame-Options：链接会被投到大赛平台上，
    #    评委可能在 iframe 里预览，一帧被拒 = 页面完全打不开。
    h_api = client.get("/api/meta").headers
    add("响应带 X-Content-Type-Options（防 MIME 嗅探）",
        h_api.get("x-content-type-options") == "nosniff",
        str(h_api.get("x-content-type-options")))
    add("响应带 Referrer-Policy（不把 URL 里的口令漏给第三方）",
        bool(h_api.get("referrer-policy")), str(h_api.get("referrer-policy")))
    add("接口响应不缓存（no-store）",
        h_api.get("cache-control") == "no-store", str(h_api.get("cache-control")))
    add("静态页面可缓存但必须回源校验（no-cache，改了刷新就能见）",
        client.get("/").headers.get("cache-control") == "no-cache",
        str(client.get("/").headers.get("cache-control")))
    add("刻意不设 X-Frame-Options（否则大赛平台 iframe 预览会打不开）",
        client.get("/").headers.get("x-frame-options") is None)

    # ⑧ 首屏 138KB，必须压。
    r_gz = client.get("/", headers={"Accept-Encoding": "gzip"})
    add("静态页面在支持 gzip 的客户端下被压缩",
        r_gz.headers.get("content-encoding") == "gzip",
        str(r_gz.headers.get("content-encoding")))

    # ⑨ ASR 如实报告可用性：让前端能分清"没听清"与"根本不能听写"。
    r_asr = client.post("/api/asr",
                        files={"audio": ("a.webm", b"\x1a\x45\xdf\xa3" * 8, "audio/webm")})
    add("ASR 返回 asr_available，前端据此改口播而不是让用户反复重说",
        "asr_available" in (r_asr.json() or {}), str(r_asr.json())[:70])

    # ================================================================
    # 语音识别（F08）：把"按住说话"从空转变成真的能用
    #
    # 原来 /api/asr 只认本地 FunASR，而 funasr 从没装上过 —— 也就是说
    # 它在任何真实环境里都恒定返回空文本，前端只能说"没有听清"。
    # 这是视障用户最主要的输入通道，空转等于这个功能不存在。
    # 现在接上云端 GLM-ASR-2512（复用同一把智谱 Key）。
    # 这一节全部打桩，不消耗任何额度、也不依赖网络。
    # ================================================================
    print("\n--- 语音识别：格式、时长、通道与错误翻译 ---")
    from core.providers import ASRError, _audio_format, _wav_seconds

    # 格式只认魔数。上游也是按真实格式判的：实测把 WAV 字节改名成 a.webm
    # 上传，直接 400 / code 1214「transcriptions不支持当前文件格式」。
    add("ASR-魔数认得 WAV", _audio_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav")
    add("ASR-魔数认得带 ID3 标签的 MP3", _audio_format(b"ID3\x03\x00" + b"\x00" * 9) == "mp3")
    add("ASR-魔数认得 MP3 帧同步字", _audio_format(b"\xff\xfb\x90" + b"\x00" * 9) == "mp3")
    add("ASR-魔数不认 webm/opus（录音识别不出来的根因就在这里）",
        _audio_format(b"\x1a\x45\xdf\xa3" + b"\x00" * 9) == "")
    add("ASR-魔数不认任意二进制", _audio_format(b"\x00" * 16) == "")

    _w1 = wav_bytes(1.0)
    add("ASR-WAV 时长解析正确", abs(_wav_seconds(_w1) - 1.0) < 0.05, f"{_wav_seconds(_w1):.2f}s")
    add("ASR-非 WAV 时长解析返回 0（算不出 ≠ 非法，不能因此拒绝）",
        _wav_seconds(b"garbage-not-wav") == 0.0)

    _saved_asr = (config.MOCK_MODE, config.ASR_PROVIDER, config.VL_API_KEY)
    try:
        config.MOCK_MODE = False
        config.ASR_PROVIDER = "zhipu"
        config.VL_API_KEY = "x" * 32          # 只测"有 Key"，不碰真 Key
        add("ASR-配了 Key 时实际生效通道是 zhipu（与 /api/meta 声明同源）",
            providers.asr.backend() == "zhipu", providers.asr.backend())

        _err = ""
        try:
            providers.asr.transcribe(b"\x1a\x45\xdf\xa3" + b"\x00" * 4000)
        except ASRError as e:
            _err = str(e)
        add("ASR-非 wav/mp3 在本地就抛出可解释错误（不发必然失败的请求）",
            "格式" in _err, _err[:36])

        _err2 = ""
        try:
            providers.asr.transcribe(wav_bytes(31.0))
        except ASRError as e:
            _err2 = str(e)
        add("ASR-超过 30 秒的录音提前拦下并说明原因", "30 秒" in _err2, _err2[:36])

        # 上游请求字段名写错就是 400。这里逐项核对，避免"代码看着对、发出去不对"。
        _calls = {}
        _real_post = providers.requests.post

        class _RespOK:
            status_code = 200
            def json(self):      # noqa: D401
                return {"text": "看看周围"}

        def _fake_post(url, headers=None, files=None, data=None, timeout=None, **kw):
            _calls.update({"url": url, "files": files, "data": data,
                           "headers": headers, "timeout": timeout})
            return _RespOK()

        providers.requests.post = _fake_post
        try:
            _txt = providers.asr.transcribe(wav_bytes(2.0))
            add("ASR-走云端通道并拿回识别文本", _txt == "看看周围", repr(_txt))
            _files = _calls.get("files") or {}
            _fname, _fdata, _fmime = (_files.get("file") or ("", b"", ""))
            add("ASR-multipart 字段名是 file（官方必填字段，写错就是 400）",
                "file" in _files, ",".join(_files.keys()))
            add("ASR-文件名带 .wav 扩展名（上游按它判文件类型）",
                str(_fname).endswith(".wav"), str(_fname))
            add("ASR-上传的是原始音频字节，没有被 base64 或截断",
                len(_fdata) == len(wav_bytes(2.0)), f"{len(_fdata)}B")
            add("ASR-请求体带 model", (_calls.get("data") or {}).get("model") == config.ASR_CLOUD_MODEL,
                str((_calls.get("data") or {}).get("model")))
            add("ASR-请求体带热词 hotwords（提升指令词命中率）",
                "hotwords" in (_calls.get("data") or {}))
            add("ASR-授权头用 Bearer（Key 不进 URL、不进请求体）",
                str((_calls.get("headers") or {}).get("Authorization", "")).startswith("Bearer ")
                and "x" * 8 not in str(_calls.get("url")),
                "Bearer ***")
            add("ASR-超时被显式设置（否则用户会一直举着手机等）",
                isinstance(_calls.get("timeout"), int), str(_calls.get("timeout")))
        finally:
            providers.requests.post = _real_post

        class _Resp400:
            status_code = 400
            def json(self):      # noqa: D401
                return {"error": {"code": "1214",
                                  "message": "transcriptions不支持当前文件格式"}}

        providers.requests.post = lambda *a, **k: _Resp400()
        try:
            _err3 = ""
            try:
                providers.asr.transcribe(wav_bytes(2.0))
            except ASRError as e:
                _err3 = str(e)
            add("ASR-上游 1214 被翻成人话（不把英文原文与错误码丢给用户）",
                "格式" in _err3 and "1214" not in _err3, _err3[:40])
        finally:
            providers.requests.post = _real_post
    finally:
        (config.MOCK_MODE, config.ASR_PROVIDER, config.VL_API_KEY) = _saved_asr

    # ---- 意图识别的预算：规则优先，模型只做兜底 ----
    # 实测（2026-10-07）：不加这一层时，一次 /api/asr 能因为意图分类里的
    # LLM 调用（先 429、再卡满 20 秒超时）跑到 30.5 秒 ——
    # 而识别文本 2.3 秒就回来了。加上后稳定在 0.5~4 秒。
    print("\n--- 语音意图：规则优先 + 给模型死预算 ---")
    import asyncio

    from core.pipeline import keyword_intent
    from main import _intent_with_budget

    add("意图-「看看周围」由规则命中 scene",
        (keyword_intent("看看周围") or {}).get("intent") == "scene")
    add("意图-「读一下这个价签」优先判为 read 而不是 scene",
        (keyword_intent("读一下这个价签") or {}).get("intent") == "read")
    add("意图-求助口语「帮我叫个人」能被规则命中",
        (keyword_intent("帮我叫个人") or {}).get("intent") == "help_human")
    add("意图-规则没命中时返回 None（这才是「要不要再请模型」的判据）",
        keyword_intent("今天天气不错呀") is None)

    from core import pipeline as _pl
    _real_ci = _pl.classify_intent
    _n = {"c": 0}

    def _count_ci(text):
        _n["c"] += 1
        return {"intent": "scene", "entities": {}}

    try:
        _pl.classify_intent = _count_ci
        asyncio.run(_intent_with_budget("看看周围"))
        add("意图-规则命中时完全不调用模型（这一条就是 30.5 秒 → 0.5 秒的来源）",
            _n["c"] == 0, f"模型被调用 {_n['c']} 次")
        _n["c"] = 0
        _r2 = asyncio.run(_intent_with_budget("今天天气不错呀"))
        add("意图-规则没命中时才请模型", _n["c"] == 1, f"{_n['c']} 次")
        add("意图-模型返回结果被采用", _r2.get("intent") == "scene", str(_r2.get("intent")))

        # 模型卡住时必须在预算内返回：识别文本已经到了，用户不该陪着等模型。
        def _slow_ci(text):
            time.sleep(4.5)
            return {"intent": "read", "entities": {}}

        _pl.classify_intent = _slow_ci
        _t0 = time.perf_counter()
        _r3 = asyncio.run(_intent_with_budget("今天天气不错呀"))
        _dt = time.perf_counter() - _t0
        add("意图-模型卡住时不拖死语音链路（超预算退回确定性结果）",
            _dt < config.INTENT_BUDGET_S + 1.5 and _r3.get("intent") == "scene",
            f"{_dt:.1f}s / 预算 {config.INTENT_BUDGET_S}s")
    finally:
        _pl.classify_intent = _real_ci

    # ---- 接口层的三条结局 ----
    print("\n--- /api/asr 的三条结局必须分清 ---")
    _rs = client.post("/api/asr", files={"audio": ("a.wav", wav_bytes(1.0), "audio/wav")})
    add("接口-识别不可用时如实报 asr_available=false（前端据此改口播，而不是说「没听清」）",
        _rs.status_code == 200 and _rs.json().get("asr_available") is False,
        f"{_rs.status_code} / asr_available={_rs.json().get('asr_available')}")

    _saved2 = (config.MOCK_MODE, config.ASR_PROVIDER, config.VL_API_KEY)
    _real_cloud = providers.asr._cloud
    try:
        config.MOCK_MODE = False
        config.ASR_PROVIDER = "zhipu"
        config.VL_API_KEY = "x" * 32
        providers.asr._cloud = lambda data, fmt: "看看周围"

        _rok = client.post("/api/asr", files={"audio": ("a.wav", wav_bytes(1.0), "audio/wav")})
        _jok = _rok.json()
        add("接口-正常 WAV 走通并返回识别文本", _rok.status_code == 200 and _jok.get("text") == "看看周围",
            f"{_rok.status_code} / {_jok.get('text')!r}")
        add("接口-如实报出实际生效的后端通道", _jok.get("backend") == "zhipu",
            str(_jok.get("backend")))
        add("接口-返回识别耗时（响应速度是可视、可核对的一项）",
            isinstance(_jok.get("elapsed_ms"), int), f"{_jok.get('elapsed_ms')}ms")

        _rbad = client.post("/api/asr", files={"audio": ("a.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 4000,
                                                         "audio/webm")})
        add("接口-格式不对时给 400 + 一句人话（而不是 200 + 空文本让用户反复重说）",
            _rbad.status_code == 400 and "格式" in (_rbad.json().get("detail") or ""),
            f"{_rbad.status_code} / {(_rbad.json().get('detail') or '')[:26]}")

        # 拒收的响应里绝不能带 Key 或内部路径
        _blob = json.dumps(_rbad.json(), ensure_ascii=False)
        add("接口-错误响应不泄漏 Key 或服务器路径",
            "x" * 8 not in _blob and "/" not in _blob.replace("/api", ""),
            _blob[:40])
    finally:
        providers.asr._cloud = _real_cloud
        (config.MOCK_MODE, config.ASR_PROVIDER, config.VL_API_KEY) = _saved2

    _meta = client.get("/api/meta").json()
    _asr_m = (_meta.get("models") or {}).get("asr") or {}
    add("元信息-ASR 声明含 name/version/backend/available（声明必须与实际情况一致）",
        all(k in _asr_m for k in ("name", "version", "backend", "available")),
        json.dumps(_asr_m, ensure_ascii=False)[:52])
    add("元信息-ASR 声明的名称是真实会调用的那个模型",
        str(_asr_m.get("name") or "").startswith(("GLM-ASR", "paraformer")),
        str(_asr_m.get("name")))

    passed = sum(1 for c in cases if c["pass"])
    report = {
        "project": "启明随行 · 视障无障碍智能体",
        "mode": "MOCK_MODE=true（无需真实 API Key）",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(cases), "passed": passed,
        "failed": len(cases) - passed,
        "pass_rate": f"{passed / len(cases) * 100:.0f}%",
        "cases": cases,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 冒烟测试 {'全部通过' if passed == len(cases) else str(passed) + '/' + str(len(cases)) + ' 通过'} ===")
    for c in cases:
        mark = "PASS" if c["pass"] else "FAIL"
        extra = f"   ← {c['note']}" if c["note"] else ""
        print(f"  [{mark}] {c['case']:<34} {c['method']:<6} {c['url']:<26} "
              f"{c['status']} {c['latency_ms']}ms{extra}")
    print(f"\n报告已写入：{OUT}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
