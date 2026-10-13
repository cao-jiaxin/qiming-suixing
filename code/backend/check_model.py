# -*- coding: utf-8 -*-
"""启明随行 · 模型自检 / 一键配置真实模型

用法（在 code/backend 目录下，或任意目录均可）：
    1) 看现在的状态          python check_model.py
    2) 填入免费 Key          python check_model.py --set-key
    3) 只写不测（脚本用）    python check_model.py --set-key "sk-xxxx" --no-test

为什么需要这个脚本：
    无 Key 时系统会**静默**退回内置示例（接口 200、不报错、耗时正常），
    界面上的提示又只有一句话。用户只能靠"结果看起来不对劲"来怀疑，
    而排查方向往往错在网络或前端上。本脚本把"到底谁在回答"这件事
    变成一条明确结论 + 一次真实的最小视觉调用。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，直接 print 中文会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

ENV_PATH = BACKEND_DIR / ".env"
ENV_EXAMPLE = BACKEND_DIR / ".env.example"

SEP = "=" * 62


def _mask(key: str) -> str:
    """只显示长度与首尾各 2 位，避免 Key 泄露到截图/日志里"""
    k = (key or "").strip()
    if not k:
        return "（未填写）"
    if len(k) <= 6:
        return f"（长度 {len(k)}，过短）"
    return f"{k[:2]}****{k[-2:]}（长度 {len(k)}）"


def write_key_to_env(key: str) -> None:
    """把 Key 写进 code/backend/.env，并确保 MOCK_MODE=false。

    没有 .env 就从 .env.example 复制一份：模板里已经把所有模型名与
    几何参数写好了，用户不需要理解任何一项。
    """
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
    elif ENV_EXAMPLE.exists():
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        print(f"（未找到 .env，已从 {ENV_EXAMPLE.name} 复制模板）")
    else:
        text = ""

    lines = text.splitlines()
    out: list[str] = []
    key_written = mock_written = False
    for line in lines:
        s = line.strip()
        if s.startswith("ZHIPU_API_KEY="):
            out.append(f"ZHIPU_API_KEY={key}")
            key_written = True
        elif s.startswith("MOCK_MODE="):
            out.append("MOCK_MODE=false      # 演示模式开关，false = 用真实模型")
            mock_written = True
        else:
            out.append(line)
    if not key_written:
        out.append("")
        out.append("# -------- 视觉理解（主力：智谱 GLM-4.6V-Flash，官方免费模型）--------")
        out.append(f"ZHIPU_API_KEY={key}")
    if not mock_written:
        out.append("MOCK_MODE=false      # 演示模式开关，false = 用真实模型")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已写入：{ENV_PATH}")


def build_test_image() -> bytes:
    """生成一张确定性的测试图：白底 + 红色实心圆。

    刻意不用文字图：渲染中文需要额外字体文件，缺字体时会静默画成方框，
    反而把"模型看不见"和"字体没装"混在一起。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 320), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 60, 260, 260), fill=(220, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- 错误翻译

# 智谱把"限流"细分成两个业务错误码，**处理方式相反**，但在 HTTP 层
# 常常都表现为 429 / 5xx。只看 HTTP 状态码会把两者混为一谈，
# 于是用户跑去改 Key、改网络，而正确动作其实是"等一下"或"换下一级模型"。
# 来源：https://docs.bigmodel.cn/cn/api/rate-limit
BIZ_CODE_TABLE = {
    "1302": ("触发**你自己账号**的速率限制（并发数超过账户上限）",
             "把请求改成串行即可；本项目单用户串行，正常使用碰不到。"),
    "1305": ("**平台服务过载**：该模型当前整体访问量过大，"
             "官方明确说明**与单一账户的调用行为无关**",
             "稍后重试、拉长重试间隔、降级到其他模型。"
             "免费模型并发普遍只有 1，晚间高峰最容易撞上。"),
}

_HTTP_TABLE = {
    400: "请求参数被拒。多半是模型名与该平台不匹配（检查 VL_MODEL_NAME）。",
    401: "Key 无效或复制不完整。请到控制台重新复制一次（注意别漏掉尾部字符）。",
    403: "Key 没有该模型的权限，或账号未完成实名认证。",
    404: "模型不存在或接口地址写错了。检查 VL_MODEL_NAME 与 ZHIPU_BASE_URL。",
    429: "触发限流或并发上限。免费模型并发很小（常常只有 1~5）——"
         "本项目会自动降级到下一个免费模型再试。",
    500: "上游服务异常，与你的配置无关，稍后重试。",
    502: "上游网关异常，稍后重试。",
    503: "上游正在扩容/维护，稍后重试。",
    504: "上游网关超时，稍后重试。",
}


def parse_biz_code(text: str) -> tuple[str, str]:
    """从响应体里取出业务错误码与 message。

    为什么需要它：智谱的限流/过载**有时以 HTTP 200 + {"error": {...}} 返回**。
    只判断 status_code 会把"失败"当成"成功"，接着去解析正文，
    抛出的是"返回结构异常"——真实原因（平台过载）被掩盖成"代码问题"，
    用户按代码问题排查，永远查不到。
    """
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return "", ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = str(err.get("code") or err.get("error_code") or "").strip()
        msg = str(err.get("message") or err.get("msg") or "").strip()
        return code, msg
    if isinstance(err, str) and err.strip():
        return "", err.strip()
    return "", ""


def explain_failure(res: dict) -> tuple[str, bool]:
    """把一次失败翻译成"用户该做什么"。

    返回 (说明文字, 是否应立即停止降级)。
    401/403 换模型没有任何意义（问题在 Key 与权限），必须停下并说清；
    其余情况（限流、过载、超时、模型名写错）都值得换下一级再试。
    """
    if res["kind"] == "timeout":
        return (f"超过 {res['ms'] / 1000:.0f} 秒无响应。\n"
                "        → 检查网络；校园网/公司网若限制外网，换手机热点再试。", False)
    if res["kind"] == "conn":
        return (f"连不上 {res['raw'][:120]}\n"
                "        → 网络不通或被安全软件拦截，与 Key 无关。", False)

    code = res.get("code") or ""
    if code in BIZ_CODE_TABLE:
        what, how = BIZ_CODE_TABLE[code]
        return (f"业务错误码 {code}｜{what}\n"
                f"        → {how}", False)

    status = res.get("status")
    note = _HTTP_TABLE.get(status, "未知错误。")
    detail = f"HTTP {status}｜{note}" if status else note
    if code:
        detail += f"\n        业务错误码 {code}（未收录，请对照官方错误码文档）：" \
                  f"{res.get('msg', '')[:160]}"
    else:
        detail += f"\n        上游原文：{(res.get('raw') or '')[:240]}"
    stop = status in (401, 403)
    if stop:
        detail += "\n        → 这是 Key/权限问题，换模型也没用，先按上面处理。"
    return detail, stop


def call_vision_once(cfg, base_url: str, api_key: str, model: str,
                     image_bytes: bytes) -> dict:
    """一次最小视觉调用。**不抛异常**，失败也返回结构化结果。"""
    import requests
    from core.providers import _extract_content, to_data_url

    t0 = time.time()

    def _elapsed_ms() -> int:
        return int((time.time() - t0) * 1000)

    try:
        r = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system",
                     "content": "你是一个视觉助手，回答不超过一句话。"},
                    {"role": "user",
                     "content": [
                         {"type": "image_url",
                          "image_url": {"url": to_data_url(image_bytes)}},
                         {"type": "text",
                          "text": "图中是什么形状、什么颜色？只回答一句话。"},
                     ]},
                ],
                "temperature": 0.2,
                "max_tokens": 60,
            },
            # 注意：这里只把 Key 放进请求头，绝不打印、绝不写文件
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=cfg.VL_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return {"ok": False, "ms": _elapsed_ms(), "kind": "timeout",
                "status": None, "code": "", "msg": "", "raw": ""}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "ms": _elapsed_ms(), "kind": "conn", "status": None,
                "code": "", "msg": "", "raw": f"{base_url}（{e}）"}

    ms = _elapsed_ms()
    code, msg = parse_biz_code(r.text)
    if r.status_code != 200 or code:
        return {"ok": False, "ms": ms, "kind": "upstream",
                "status": r.status_code, "code": code, "msg": msg, "raw": r.text}
    try:
        text = _extract_content(r.json())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": ms, "kind": "format", "status": 200,
                "code": "", "msg": str(e), "raw": r.text}
    return {"ok": True, "ms": ms, "kind": "", "status": 200,
            "code": "", "msg": "", "raw": "", "text": text}


def build_chain(cfg) -> list[tuple[str, str, str, str]]:
    """按 core/providers 的**真实降级顺序**列出可用目标。

    顺序必须与产品运行时一致，否则自检结论不能代表产品行为：
        ① 主力 → ② 一级降级 → ③ 限流兜底（同一个 Key，只换模型名）
    """
    from core.config import key_usable

    raw = [
        ("① 主力", cfg.VL_BASE_URL, cfg.VL_API_KEY, cfg.VL_MODEL_NAME),
        ("② 一级降级", cfg.VL_FALLBACK_BASE_URL, cfg.VL_FALLBACK_API_KEY,
         cfg.VL_FALLBACK_MODEL_NAME),
        ("③ 限流兜底", cfg.VL_BASE_URL, cfg.VL_API_KEY, cfg.VL_LAST_MODEL_NAME),
    ]
    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, base, key, model in raw:
        if not key_usable(key):          # 没配的通道直接跳过，不白等一次超时
            continue
        sig = (base.rstrip("/"), model)
        if sig in seen:                  # 同一平台同一模型不重复试
            continue
        seen.add(sig)
        out.append((label, base, key, model))
    return out


def run_real_test(cfg, no_fallback: bool = False) -> bool:
    """沿**真实降级链**逐级试，直到某一级成功，返回真实链路是否可用。

    为什么必须连测三级：免费模型在晚间高峰撞 1305（平台服务过载）是常态。
    旧版只测主力那一个模型，于是"链路本来是好的、只是主力被平台限流"
    会被显示成"失败"，把用户引向"是不是我 Key 配错了"这个错误方向。
    连测之后的结论能直接对应产品实际行为：主力不通 → 自动换模型 → 通了。
    """
    chain = build_chain(cfg)
    if no_fallback:
        chain = chain[:1]

    print(f"正在做真实调用测试（逐级尝试，共 {len(chain)} 级）：")
    img = build_test_image()
    failures: list[tuple[str, str, dict]] = []

    for idx, (label, base, key, model) in enumerate(chain, 1):
        print(f"\n  [{idx}/{len(chain)}] {label}：{model}")
        print(f"        接口 {base.rstrip('/')}/chat/completions")
        res = call_vision_once(cfg, base, key, model, img)
        if res["ok"]:
            text = res["text"]
            print(f"        结果：成功（{res['ms']} ms）")
            print(f"        模型回答：{text}")
            ok_shape, ok_color = "圆" in text, "红" in text
            if ok_shape and ok_color:
                print("        判定：模型确实看见了红色圆形，视觉链路工作正常。")
            else:
                print("        判定：调用成功，但回答与测试图（红色圆形）不符 ——")
                print("              若反复如此，说明该模型名可能被路由到了别的模型。")

            print(SEP)
            if idx == 1:
                print("结论：真实模型已生效（主力模型就通了）。")
                print("      重启服务后，网页顶部的「演示模式」横幅会消失。")
            else:
                print(f"结论：主力模型这次没通（{failures[0][0]}），"
                      f"但**降级链工作正常** ——")
                print(f"      第 {idx} 级 {model} 成功，真实链路可用，无需改任何配置。")
                print("      这是免费模型在高峰期的正常状态；产品运行时会自动这样切换，")
                print("      用户侧只表现为响应慢一点。想更稳可稍后重跑本脚本，"
                      "或另配一条独立备用通道。")
            return True

        detail, stop = explain_failure(res)
        print(f"        结果：失败（{res['ms']} ms）")
        for line in detail.splitlines():
            print("        " + line)
        failures.append((f"{model}", detail.splitlines()[0], res))
        if stop:
            print(SEP)
            print("结论：这是 Key / 权限问题，换下一级模型也没有意义，已停止降级测试。")
            print("      按上面的提示处理后重跑本脚本。")
            return False
        if idx < len(chain):
            print("        → 继续试下一级……")
            # 官方建议"拉长重试间隔，避免立即高频重试"：给平台一点喘息时间，
            # 也避免连续三次失败被判定为攻击性调用。
            time.sleep(1.5)

    print(SEP)
    print(f"结论：{len(chain)} 级全部失败 —— 真实链路当前不可用。")
    codes = {r.get("code") for _, _, r in failures if r.get("code")}
    kinds = {r["kind"] for _, _, r in failures}
    if codes & {"1305", "1302"} or 429 in {r.get("status") for _, _, r in failures}:
        print("      全部是限流/过载类错误（平台侧），**不是你的 Key 或代码问题**。")
        print("      等待 5~10 分钟再重跑一次，不要在短时间内连续重试。")
    elif "timeout" in kinds or "conn" in kinds:
        print("      像是网络不通：检查是否能访问 open.bigmodel.cn，"
              "必要时换手机热点试一次。")
    else:
        print("      请按上面每一级的提示逐条处理；如需最简验证，"
              "可先修第 ① 级的问题再重跑。")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="启明随行 · 模型自检")
    ap.add_argument("--set-key", nargs="?", const="__PROMPT__", default=None,
                    metavar="KEY",
                    help="把 API Key 写入 code/backend/.env 并关闭 MOCK_MODE；"
                         "不带值时会交互式询问（输入不回显）")
    ap.add_argument("--no-test", action="store_true", help="只写配置，不做联网测试")
    ap.add_argument("--no-fallback", action="store_true",
                    help="只测主力模型，不沿降级链继续试（默认会逐级试完）")
    args = ap.parse_args()

    if args.set_key is not None:
        key = args.set_key
        if key == "__PROMPT__":
            import getpass
            print("请粘贴你的 BigModel API Key（免费领取："
                  "https://open.bigmodel.cn  →  右上角 → API Keys）。")
            print("本项目用的三个视觉模型（GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash / "
                  "GLM-4V-Flash）")
            print("都是官方标注的长期免费模型，一个 Key 就能跑通三级降级链。")
            print("粘贴后按回车。输入过程不会回显，属正常现象。")
            key = getpass.getpass("API Key: ")
        key = (key or "").strip().strip('"').strip("'")
        if not key:
            print("未输入内容，已取消。")
            return 1
        write_key_to_env(key)

    print(SEP)
    print("启明随行 · 模型状态自检")
    print(SEP)

    # 必须放在写完 .env 之后导入：config 在导入期就把 .env 读进类属性了
    from core.config import (
        config, key_hint, mock_active, mock_reason, vision_backends_available,
    )

    env_exists = ENV_PATH.exists()
    print(f".env 文件        : {'存在 ' + str(ENV_PATH) if env_exists else '不存在（这本身就会导致走示例）'}")
    print(f"环境变量 MOCK_MODE: {os.getenv('MOCK_MODE') or '未设置'}")
    print(f"当前是否示例模式  : {'是 ← 所有识别返回内置示例' if mock_active() else '否 ← 正在调用真实模型'}")
    reason = mock_reason()
    if reason:
        print(f"原因              : {reason}")
    print("视觉模型（三级降级，均为智谱官方免费模型，同一个 Key 即可）:")
    print(f"  ① {config.VL_MODEL_NAME} / {config.VL_MODEL_VERSION}   　← 主力")
    print(f"  ② {config.VL_FALLBACK_MODEL_NAME} / {config.VL_FALLBACK_MODEL_VERSION}"
          f"   [{config.VL_FALLBACK_PROVIDER}]")
    print(f"  ③ {config.VL_LAST_MODEL_NAME} / {config.VL_LAST_MODEL_VERSION}   　← 限流兜底")
    print(f"文本模型          : {config.LLM_MODEL_NAME} / {config.LLM_MODEL_VERSION}（免费）")
    print(f"接口地址          : {config.VL_BASE_URL}")
    print(f"主力 API Key      : {_mask(config.VL_API_KEY)}")
    _same_key = (bool(config.VL_API_KEY)
                 and config.VL_FALLBACK_API_KEY == config.VL_API_KEY)
    print(f"备用 API Key      : {_mask(config.VL_FALLBACK_API_KEY)}"
          + ("（与主 Key 相同：第二条降级链共用同一个 Key，属正常）"
             if _same_key else ""))

    if config.MOCK_MODE:
        print(SEP)
        print("被 MOCK_MODE=true 挡住了：即使 Key 正确也不会调用模型。")
        print("解决：把 .env 里的 MOCK_MODE 改成 false（或运行 --set-key 自动改），再重启服务。")
        return 1

    if not vision_backends_available():
        print(SEP)
        print("没有可用的视觉模型 Key，所以全部识别都返回内置示例。")
        print("解决（约 1 分钟，中文界面、手机号注册、免费）：")
        print("  1. 打开 https://open.bigmodel.cn  手机号注册并实名（免费）")
        print("  2. 右上角 → API Keys → 新建一个，复制")
        print("     （GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash / GLM-4V-Flash "
              "三个视觉模型都是官方免费模型，一个 Key 全都能用）")
        print("  3. 回到本目录运行： python check_model.py --set-key")
        print("  4. 重启服务（双击 start_lan.cmd）")
        print(f"提示：{key_hint()}")
        return 1

    print(SEP)
    if args.no_test:
        print("模型 Key 已就绪，服务将调用真实模型（本次按要求跳过联网测试）。")
        return 0

    ok = run_real_test(config, no_fallback=args.no_fallback)
    print(SEP)
    if ok:
        print("下一步：重启服务（双击 start_lan.cmd），"
              "网页顶部的黄色「演示模式」横幅会消失。")
        return 0
    print("下一步：按上面的提示处理。限流/过载类错误等 5~10 分钟再跑一次即可，"
          "不必改任何配置。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
