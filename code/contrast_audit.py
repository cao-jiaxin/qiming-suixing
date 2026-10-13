# -*- coding: utf-8 -*-
"""
对比度审计 —— 给「启明随行」的配色做 WCAG 实测体检。

为什么要写这个脚本，而不是凭感觉调色：
本项目的第二读者是**低视力用户**，他们是真的在看屏幕的。配色好看不好看是次要的，
"看不看得清"是硬指标。而"我觉得这个绿色挺柔和"恰恰是低视力用户最常踩的坑——
柔和的低饱和色，对比度往往就是不够。

判据（WCAG 2.1 / GB-T 37668-2019「可感知性」）：
  正文文本（< 18.66px 粗体 / < 24px 常规）  ≥ 4.5:1
  大号文本（≥ 18.66px 粗体 或 ≥ 24px 常规） ≥ 3:1
  非文本元素（图标、边框、焦点环、状态色块） ≥ 3:1

跑法：python contrast_audit.py            # 控制台表格
      python contrast_audit.py --json     # 同时写 results/contrast_audit.json
不需要任何第三方依赖，纯标准库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


# ---------------------------------------------------------------- 基础换算
def _channel(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def ratio(fg: str, bg: str) -> float:
    """WCAG 对比度，1.0 ~ 21.0。返回保留两位小数。"""
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return round((l1 + 0.05) / (l2 + 0.05), 2)


# 三档判据，不能只用一个数：
#   text-small 正文小字        ≥ 4.5:1（AAA 7:1）
#   text-large 大号文本        ≥ 3.0:1（AAA 4.5:1）
#   non-text   描边/焦点环/状态色块等非文本元素 ≥ 3.0:1
# 把 non-text 也按 4.5 判，会冤枉本来就合规的边框色——
# 审计脚本给出错误的红牌，比不给红牌更坏。
THRESHOLD = {
    "text-small": (4.5, 7.0),
    "text-large": (3.0, 4.5),
    "non-text":   (3.0, 4.5),
}
LABEL = {"text-small": "小字", "text-large": "大字", "non-text": "非文本"}


def verdict(r: float, kind: str = "text-small") -> str:
    need, aaa = THRESHOLD[kind]
    if r >= aaa:
        return "AAA"
    if r >= need:
        return "AA"
    return "不达标"


def check(fg: str, bg: str, what: str, kind: str = "text-small") -> dict:
    r = ratio(fg, bg)
    v = verdict(r, kind)
    return {"what": what, "fg": fg, "bg": bg, "ratio": r,
            "grade": v, "pass": v != "不达标", "kind": kind}


# ---------------------------------------------------------------- 审计清单
#
# ⚠ 这里有两套清单，别把标签看错（2026-10-06 修过一次标签错误）：
#
#   SUPERSEDED —— 2026-09-25 视觉重构**之前**的旧配色。它已经不在
#                index.html 里了（只出现在"为什么把它换掉"的注释里）。
#                保留它纯粹是留一份改造前后的对照证据，**通过与否都不影响结论**。
#                原来这一节被标成"现状（code/frontend/index.html）"，
#                于是脚本一边说"现状 4 项不达标"，一边上线的是另一套全达标的配色 ——
#                提交材料里同时出现两条互相矛盾的信息。
#
#   SHIPPED   —— 现在真正跑在 index.html 与 docs/UI设计方案_20260926.html 里的配色，
#                也是退出码唯一依据的那一套。
SUPERSEDED = [
    check("#8A9A7B", "#FFFFFF", "主按钮 白字 / 鼠尾草绿底（拍照看看周围）", "text-large"),
    check("#5D6A6B", "#FFFFFF", "次按钮 文字 / 白底（拍照读文字）", "text-large"),
    check("#FFFFFF", "#A9736A", "警告按钮 白字 / 砖红底（呼叫人工志愿者）", "text-large"),
    check("#FFFFFF", "#8A9A7B", "选中的文字类型 chip 白字 / 绿底"),
    check("#2E3538", "#F7F6F3", "正文 深灰 / 暖灰底"),
    check("#5D6A6B", "#F7F6F3", "次要文字 / 暖灰底"),
    check("#8D9899", "#F7F6F3", "提示与元信息（.hint / .sec-title / .meta）"),
    check("#8D9899", "#FFFFFF", "结果区占位文字（.ph）"),
    check("#8D9899", "#F6F4EF", "折叠标题（调试信息）"),
    check("#7A5C10", "#FFF8E6", "演示模式横幅 正文"),
    check("#6A4E08", "#FFF8E6", "演示模式横幅 标题（粗体）", "text-large"),
    check("#5B6B50", "#EEF2EA", "距离表格 步数标签"),
    check("#8E5B50", "#FBEEEA", "距离表格 近距离警示（≤4 步）"),
    check("#1D6B4E", "#F7F6F3", "触摸探索焦点环（3px 描边）", "non-text"),
    check("#A9736A", "#FFFFFF", "二次确认虚线描边", "non-text"),
    check("#FFFFFF", "#2E3A2C", "启动闸门 白字 / 墨绿底"),
    check("#8FBF9F", "#2E3A2C", "启动闸门 提示框描边", "non-text"),
    check("#B9C3B4", "#2E3A2C", "启动闸门 说明小字"),
]

# 现行配色：与 code/frontend/index.html 的 :root 以及
# docs/UI设计方案_20260926.html 逐条一致（盲道黄 #F2C230 方案）
SHIPPED = [
    check("#151A1E", "#F2C230", "主按钮 近黑字 / 盲道黄底（拍照看看周围）", "text-large"),
    check("#151A1E", "#F2C230", "选中的文字类型 chip 近黑字 / 盲道黄底"),
    check("#1C252B", "#FFFFFF", "次按钮 深墨字 / 白底", "text-large"),
    check("#FFFFFF", "#B23F26", "警告按钮 白字 / 警示砖红底（呼叫人工志愿者）", "text-large"),
    check("#10161C", "#F2F4F3", "正文 近黑 / 冷灰白底"),
    check("#3F4A53", "#F2F4F3", "次要文字 / 冷灰白底"),
    check("#5E6A73", "#F2F4F3", "提示与元信息（.hint / .sec-title / .meta）"),
    check("#5E6A73", "#FFFFFF", "结果区占位文字（.ph）"),
    check("#5E6A73", "#F2F4F3", "折叠标题（调试信息）"),
    # 首屏使用说明（.guide）：它教的是"怎么用"，按正文标准要求，
    # 不按"角落里的小字"放宽 —— 这正是把字号从 13.5px 提到 15px 的理由。
    check("#3F4A53", "#FFFFFF", "使用说明 正文（.guide）"),
    check("#10161C", "#FFFFFF", "使用说明 加粗动作词（.guide b）"),
    check("#3F4A53", "#FFFFFF", "底栏「按住说话」描边键（.btn.sec）"),
    # 照片回执（#shot）的 1px 描边刻意**不列**：它与 .card 用的是同一个 --line，
    # 属于装饰性分隔，不承担"让用户识别出这是一个可操作控件"的职责，
    # 因此不在 WCAG 1.4.11（非文本对比度管的是组件边界与状态）的范围内。
    # 列进来会得到一条 1.39:1 的假红牌 —— 而审计脚本给出错误的红牌，
    # 比不给红牌更坏：它会让真正该修的那几条淹没在噪音里。
    check("#6B4A05", "#FFF3D4", "演示模式横幅 正文"),
    check("#4F3603", "#FFF3D4", "演示模式横幅 标题（粗体）", "text-large"),
    check("#2F4A3C", "#E4EFE8", "距离表格 步数标签"),
    check("#8A2F1C", "#FBE7E2", "距离表格 近距离警示（≤4 步）"),
    check("#0E6B57", "#F2F4F3", "触摸探索焦点环（3px 描边）", "non-text"),
    check("#B23F26", "#FFFFFF", "二次确认圆点描边", "non-text"),
    check("#F5F7F6", "#101820", "启动闸门 浅字 / 夜空靛蓝底"),
    check("#7FCBA8", "#101820", "启动闸门 提示框描边", "non-text"),
    check("#A9B4B8", "#101820", "启动闸门 说明小字"),
    # --- 深色模式（新增，本方案补的第二套主题）---
    check("#E9EEEF", "#11171B", "深色-正文 浅字 / 夜底"),
    check("#A6B1B6", "#11171B", "深色-元信息"),
    check("#BFC9CC", "#19222A", "深色-使用说明 正文（.guide）"),
    check("#151A1E", "#F2C230", "深色-主按钮（黄底黑字，深浅两套共用）", "text-large"),
    check("#5FD3A6", "#11171B", "深色-焦点环", "non-text"),
]


def render(title: str, rows: list[dict]) -> None:
    print(f"\n=== {title} ===")
    bad = []
    for r in rows:
        mark = "  " if r["pass"] else "!!"
        print(f"{mark} {r['ratio']:>5.2f}:1  {r['grade']:<5} [{LABEL[r['kind']]:<3}] "
              f"{r['fg']} on {r['bg']}  {r['what']}")
        if not r["pass"]:
            bad.append(r)
    print(f"   —— 共 {len(rows)} 项，不达标 {len(bad)} 项")
    return bad


def main() -> int:
    old_bad = render("旧配色（2026-09-25 起已废弃，仅作对照）", SUPERSEDED)
    bad = render("现行配色（code/frontend/index.html）", SHIPPED)

    if "--json" in sys.argv:
        RESULTS.mkdir(parents=True, exist_ok=True)
        out = RESULTS / "contrast_audit.json"
        out.write_text(json.dumps({
            "标准": "WCAG 2.1 AA（正文 4.5:1 / 大字 3:1 / 非文本 3:1）",
            "现行配色": SHIPPED, "旧配色_仅对照": SUPERSEDED,
            "现行不达标数": len(bad), "旧配色不达标数": len(old_bad),
            "结论": "以「现行配色」为准；旧配色清单不参与判定，只是改造前后的对照证据",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out}")

    # 只看**现行**配色：旧配色清单是对照证据，不该影响退出码
    # （原来它被误标成"现状"，差点让人以为线上配色不合规）。
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
