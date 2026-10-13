# -*- coding: utf-8 -*-
"""提交前密钥自检 —— 回答一个问题：我现在打包/提交，会不会把 API Key 交出去？

用法：
    python check_secret.py          扫描并给出结论
    python check_secret.py --root D:\\某个待打包的目录   扫别的目录

为什么单独做这个脚本：
    大赛要求提交"完整源码"。源码目录里躺着一个 .env，里面是真实的 API Key。
    只要有人把整个文件夹压成 zip 交上去，Key 就等于公开了——而这件事
    **没有任何报错、没有任何提示**，等发现时已被人刷走额度。
    "我记得别放进去"是不够的，必须有一条可执行的检查。

设计原则（本脚本自己绝不泄露）：
    · 命中只打印**掩码**（前 3 + 后 3，中间打星），绝不明文输出
    · 不联网、不上传任何东西
    · .env / .env.example 属于"应该在的地方"，不计入风险，只提示存在
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SEP = "=" * 62

# 只认"长得像 Key"的字符串，不认任何业务文本。
# 智谱 Key 是 32 位十六进制 + 英文句点 + 一段 secret（id.secret 格式）。
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("智谱 id.secret 格式",
     re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,}")),
    ("sk- 前缀风格 Key",
     re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    # 必须是"某个厂商的 API Key 变量**直接等于**一个长串"。
    # 早期版本写成 (?:...|API)_?(?:API_)?KEY\s*= 会匹配到 `VL_API_KEY = os.getenv(...)`
    # 这类**变量名本身**，把正常代码刷成一片红色告警——狼来了之后真告警就没人看了。
    ("环境变量被填了值",
     re.compile(r"(?:ZHIPU|DASHSCOPE|OPENAI|MOONSHOT)_API_KEY\s*=\s*[\"']?([A-Za-z0-9._\-]{20,})")),
    ("请求头里的 Bearer",
     re.compile(r"Bearer\s+[A-Za-z0-9._\-]{24,}")),
]

# .env 是放 Key 的**唯一合法位置**，不计为风险，但它本身绝不能提交
ALLOWED_NAMES = {".env"}
# .env.example 是空模板，可以提交
TEMPLATE_NAMES = {".env.example"}

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy",
             "models", "venv", ".venv", ".pytest_cache"}
SKIP_SUFFIX = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3",
               ".onnx", ".zip", ".pdf", ".ico", ".pyc", ".woff", ".woff2", ".ttf"}
MAX_BYTES = 3_000_000


def mask(s: str) -> str:
    """掩码：只留首尾各 3 位，够你认出是哪一把 Key，不够别人拿去用。"""
    s = s.strip()
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


def scan(root: Path) -> tuple[list[tuple[str, int, str, str]], list[str]]:
    """返回（风险条目，发现 .env 的位置）。"""
    risks: list[tuple[str, int, str, str]] = []
    env_files: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        rel = str(p.relative_to(root))
        if p.name in TEMPLATE_NAMES:      # 空模板，既不算风险也不算敏感
            continue
        if p.name in ALLOWED_NAMES:
            env_files.append(rel)
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for name, pat in PATTERNS:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                risks.append((rel, line, name, mask(m.group(0))))
    return risks, env_files


def gitignore_ok(root: Path) -> tuple[bool, str]:
    """项目有没有把 .env 挡在门外。"""
    gi = root / ".gitignore"
    if not gi.exists():
        return False, "项目根没有 .gitignore —— 一旦 git init 或整目录打包，.env 会被带走"
    body = gi.read_text(encoding="utf-8", errors="ignore")
    lines = [l.strip() for l in body.splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if ".env" in lines or "*.env" in lines or ".env.*" in lines:
        return True, f"{gi.name} 已忽略 .env"
    return False, ".gitignore 存在，但没有忽略 .env"


def main() -> int:
    ap = argparse.ArgumentParser(description="启明随行 · 提交前密钥自检")
    ap.add_argument("--root", default=None, help="要扫描的目录，默认项目根")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    root = Path(args.root).resolve() if args.root else here.parent.parent

    print(SEP)
    print("启明随行 · 提交前密钥自检")
    print(SEP)
    print(f"扫描目录：{root}")

    risks, env_files = scan(root)

    print(f"\n[1] 放 Key 的合规位置：{env_files or '未发现 .env（当前是示例模式）'}")
    for f in env_files:
        print(f"    - {f}   ← 这个文件**不得**随源码提交/打包")

    print("\n[2] .env 之外的疑似 Key 明文：")
    if not risks:
        print("    未发现 —— 源码、文档、测试报告里都没有 Key。")
    else:
        for rel, line, name, masked in risks:
            print(f"    !! [{name}] {rel}:{line}  ->  {masked}")

    ok, why = gitignore_ok(root)
    print(f"\n[3] 提交防护：{'OK' if ok else '不足'} —— {why}")

    print(SEP)
    if risks or not ok:
        print("结论：现在还不能安全地打包/提交。请按上面处理后再发。")
        if risks:
            print("  处理办法：把命中的 Key 删掉（.env 里那份才是唯一合法副本），")
            print("            然后到 https://open.bigmodel.cn 控制台**删除该 Key 重新生成**")
            print("            —— 已经出现过的 Key 应视为已泄露，不要抱侥幸。")
        if not ok:
            print("  处理办法：确认项目根的 .gitignore 存在且包含 .env（本仓库已内置）。")
        print("\n交源码前的推荐动作：")
        print("  1. python check_secret.py            （本脚本）")
        print("  2. 只打包 code/ 与 docs/ 等目录，**不要**把 .env 拖进去")
        print("  3. 压缩包解压后第一时间再跑一次本脚本确认")
        return 1

    print("结论：可以提交 / 打包（前提是不要把 .env 本身放进去）。")
    print("      演示视频与截图里请勿露出 check_model.py 的整屏输出；")
    print("      该脚本对 Key 已做掩码（只显示首尾各 2 位），但截图前仍建议核对一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
