# -*- coding: utf-8 -*-
"""单目深度 → 步数 → 时钟方位  （功能 F03）

这是本项目**唯一一个必须由我们自己算、不能交给大模型的部分**。

为什么不能问大模型"前面那个东西多远"？
—— 多模态模型没有任何绝对尺度参照，它给出的"3 米"完全是无根据的猜测。
    视障者会按这个数字迈步，猜错是要摔跤的。

本模块的职责：
    输入一张现场照片，输出一张「哪个时钟方向、大约几步之内有东西」的距离表，
    再由 pipeline 把这张表**结构化注入**提示词。
    提示词里明确写着：距离只能引用这张表，禁止自行估算。

两级后端：
    A) ONNX 后端（推荐，生产用）
       加载 Depth-Anything-V2-Small，得到相对深度图；用"地面接触点几何"
       给相对深度标定尺度，得到每个时钟方向的步数。
       模型文件放到 models/depth_anything_v2_small.onnx 即自动启用。

    B) 几何启发式后端（兜底，零依赖，CPU 上毫秒级）
       手机大致水平握持、离地约 1.2 米，由「地平线 - 像素行」的俯角
       反算地面距离；再用图像梯度能量在画面下半部找障碍候选。
       精度不如 A，但保证任何环境下都能给出"步数"，
       不会出现"这个功能今天演示不了"的尴尬。

注意：MOCK_MODE 下返回空表，提示词会自动进入"不报距离"的分支。
"""
from __future__ import annotations

import io
import logging
import math
import os
from typing import Optional

from .config import config

logger = logging.getLogger("qiming")

# 时钟方位全集（正前为 12 点，顺时针）。这是**提示词里允许模型使用的**方位集合，
# 不是本模块的输出集合——见下方 COLS 的说明。
CLOCK_HOURS = [12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

# 手机水平握持时，水平视场角约 70°，每小时 = 30°
CLOCK_DEG_PER_HOUR = 30.0

# 画面水平切分列数。
#
# 为什么用 `COLS` 而不是直接拿 CLOCK_HOURS 当列名（这是一个曾经写错、
# 且错得很危险的地方，务必看懂再改）：
#   列只是**内部分辨率**，方位的名字必须由列中心的真实偏角换算，
#   即 bearing_from_x((c+0.5)/COLS)。早先的写法是 `CLOCK_HOURS[c]`，
#   等价于宣布"画面最左一列 = 12 点（正前方）"，于是障碍明明在用户
#   左前方 30°，播报却说"12 点方向有障碍"——用户会朝正前方迈步撞上去。
#
# 另一个必须接受的事实：水平视场角只有约 70°，而时钟每小时 30°，
#   所以**本模块能覆盖的只有 11 点 / 12 点 / 1 点三个方位**。
#   左后方、正后方、正左方的信息不在视野内，只能靠大模型看图描述补，
#   深度表不得假装知道。COLS 取 12 是为了让步数分辨率足够细，
#   多个列自然落到同一方位，最后由 estimate() 按方位取最近的一个。
COLS = 12

_MODEL_CACHE: dict = {}


# ------------------------------------------------------------------ 工具


def _deg2rad(d: float) -> float:
    return d * math.pi / 180.0


def bearing_from_x(x_norm: float, hfov_deg: float | None = None) -> str:
    """把归一化横坐标（0=最左, 1=最右）映射为时钟方位。

    正前方 = 12 点；3 点 = 正右；9 点 = 正左。
    用时钟而不是"左右"，是因为视障者转头后"左右"会反，时钟方位不会。
    """
    hfov = hfov_deg or config.DEPTH_HFOV_DEG
    angle = (x_norm - 0.5) * hfov          # 负=左，正=右
    offset = int(round(angle / CLOCK_DEG_PER_HOUR))
    hour = (12 + offset - 1) % 12 + 1      # 映射到 1..12
    return f"{hour}点"


def column_bearing(col_index: int, cols: int = COLS) -> str:
    """第 col_index 列（0=画面最左）对应的时钟方位。

    **所有"列 → 方位"的换算必须走这里**，不要在别处写 `CLOCK_HOURS[i]`：
    列与时钟方位不是一一对应的（视场角 70° 只覆盖 3 个方位），
    一一对应的写法会把左前方的障碍报成正前方。
    """
    return bearing_from_x((col_index + 0.5) / max(1, cols))


def _steps_from_depth_ratio(ratio: float) -> int:
    """把"相对深度比例"映射为步数。

    视障者不用"米"，用"步"。普通人一步约 0.6 米，
    我们取 config.STEP_LENGTH_M（默认 0.6）。
    """
    steps = int(round(ratio))
    return max(1, min(config.DEPTH_MAX_STEPS, steps))


def steps_from_ground_row(y_norm: float, horizon_ratio: float | None = None) -> Optional[int]:
    """几何法：由像素行位置反算地面距离（单位：步）。

    模型：手机离地 H 米，大致水平前伸；画面地平线在 horizon_ratio 处。
    像素行的俯角 θ = (y - horizon) * VFOV
    地面距离 d = H / tan(θ)

    y 越靠下（越大）→ θ 越大 → d 越小 → 越近。这与人的直觉一致。
    """
    horizon = horizon_ratio if horizon_ratio is not None else config.DEPTH_HORIZON_RATIO
    vfov = _deg2rad(config.DEPTH_VFOV_DEG)
    theta = (y_norm - horizon) * vfov
    if theta <= _deg2rad(1.0):
        return None                        # 在地平线以上或极其接近 → 视为无穷远
    tan_t = math.tan(theta)
    if tan_t <= 1e-6:
        return None
    dist_m = config.DEPTH_CAM_HEIGHT_M / tan_t
    if dist_m > config.DEPTH_MAX_STEPS * config.STEP_LENGTH_M:
        return None                        # 太远，没有提示价值
    return max(1, int(round(dist_m / config.STEP_LENGTH_M)))


# ------------------------------------------------------------------ 后端 A：ONNX


def _load_onnx():
    """加载 Depth-Anything-V2-Small ONNX。不可用时返回 None，自动走启发式后端。"""
    if "onnx" in _MODEL_CACHE:
        return _MODEL_CACHE["onnx"]
    path = config.DEPTH_ONNX_PATH
    session = None
    if path and os.path.exists(path):
        try:
            import onnxruntime as ort          # 可选依赖
            session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            logger.info("Depth-Anything ONNX 已加载：%s", path)
        except Exception as e:
            logger.warning("ONNX 深度模型加载失败，改用几何启发式后端：%s", e)
    else:
        logger.info("未找到 %s，使用几何启发式后端（功能可用，精度较粗）", path or "深度模型")
    _MODEL_CACHE["onnx"] = session
    return session


def _infer_depth_map(img_array, session):
    """跑 ONNX 得到相对深度图（H×W，值越大越远）"""
    import numpy as np

    h, w = img_array.shape[:2]
    size = config.DEPTH_INPUT_SIZE
    # 简单最近邻缩放，避免引入 cv2 依赖
    ys = (np.arange(size) * h / size).astype(int).clip(0, h - 1)
    xs = (np.arange(size) * w / size).astype(int).clip(0, w - 1)
    small = img_array[ys][:, xs].astype("float32") / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype="float32")
    std = np.array([0.229, 0.224, 0.225], dtype="float32")
    x = (small - mean) / std
    x = x.transpose(2, 0, 1)[None, ...].astype("float32")
    out = session.run(None, {session.get_inputs()[0].name: x})[0]
    depth = np.squeeze(out)
    if depth.ndim == 3:
        depth = depth[0]
    return depth


def _estimate_with_onnx(img_array, session) -> list[dict]:
    """ONNX 后端：相对深度 → 用地面几何标定尺度 → 每方位最近的障碍距离。

    标定思路（写进设计文档）：Depth-Anything 只输出**相对**深度，没有绝对尺度。
    我们用画面下部的"地面"像素做锚点：地面像素的几何距离可由镜头俯角算出，
    据此拟合 depth→米 的线性系数 a：dist ≈ a / depth（深度值近似反比于距离）。
    这不是精确标定，但在 1~15 步量程内足够用。
    """
    import numpy as np

    depth = _infer_depth_map(img_array, session)
    dh, dw = depth.shape
    horizon = int(config.DEPTH_HORIZON_RATIO * dh)

    # ---- 用地面带标定尺度：取画面最下部 15% 的行，认为那里是 2~4 步内的地面
    ground_band = depth[int(dh * 0.85):, :]
    if ground_band.size == 0:
        return []
    anchor_depth = float(np.median(ground_band))
    if anchor_depth <= 1e-6:
        return []
    anchor_steps = 3.0
    # 深度值近似反比于距离：dist = a / depth  →  a = dist * depth
    a = anchor_steps * anchor_depth

    results: list[dict] = []
    cols = COLS
    band_top = int(dh * 0.35)
    for i in range(cols):
        x0, x1 = int(dw * i / cols), int(dw * (i + 1) / cols)
        col = depth[band_top:, x0:x1]
        if col.size == 0:
            continue
        # 该列最近的表面 = 深度值最小处
        flat_idx = int(np.argmin(col))
        near = float(col.reshape(-1)[flat_idx])
        if near <= 1e-6:
            continue
        steps = a / near
        if steps > config.DEPTH_MAX_STEPS:
            continue
        row = band_top + flat_idx // max(1, col.shape[1])
        results.append({
            "bearing": column_bearing(i, cols),
            "steps": max(1, int(round(steps))),
            "kind": "surface",
            "confidence": 0.85,
            "source": "depth-anything-onnx",
            "y_norm": round(min(0.99, row / dh), 3),
        })
    return results


# ------------------------------------------------------------------ 后端 B：几何启发式


def _estimate_heuristic(img_array) -> list[dict]:
    """几何启发式后端。

    两步：
      1. 在画面下半部（地平线以下）按 12 列 × 4 带做分块，算梯度能量，
         能量高的块 = 有纹理/有结构的物体（台阶、箱子、柱子、垃圾桶…）。
      2. 每列取"最靠下"的高能量块，其下边缘就是该物体与地面的接触点，
         代入地面距离公式得到步数。

    局限（必须如实写进文档）：
      - 无法区分障碍物与地面纹理（地砖缝隙、反光）
      - 无法识别悬空障碍（头顶横杆）—— 这类靠大模型描述补
      - 相机俯仰角变化会影响精度
    因此启发式结果只作为**演示兜底**，正式评测必须用 ONNX 后端。
    """
    import numpy as np

    h, w = img_array.shape[:2]
    horizon = int(config.DEPTH_HORIZON_RATIO * h)

    # 灰度 + 梯度能量
    gray = img_array[..., :3].astype("float32") @ np.array([0.299, 0.587, 0.114], dtype="float32")
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    energy = gx + gy

    band = energy[horizon:, :]
    if band.size == 0:
        return []

    cols = COLS
    rows = 8                                  # 带数越多，步数分辨率越高（8 带约每带 3~4 步）
    bh, bw = band.shape[0] / rows, band.shape[1] / cols

    tile = np.zeros((rows, cols), dtype="float32")
    for r in range(rows):
        for c in range(cols):
            y0, y1 = int(r * bh), int((r + 1) * bh)
            x0, x1 = int(c * bw), int((c + 1) * bw)
            blk = band[y0:max(y0 + 1, y1), x0:max(x0 + 1, x1)]
            if blk.size:
                tile[r, c] = float(blk.mean())

    # 自适应阈值：取全体块的 P75，避免整幅图都很"乱"时全部命中
    p75 = float(np.percentile(tile, 75))
    if p75 <= 1e-3:
        return []                          # 画面几乎没有结构（如纯色墙），不报距离
    # 再叠一个绝对下限。只靠 P75 会有一个隐蔽后果：JPEG 压缩噪声本身就能越过
    # 相对阈值，于是一面白墙也"处处有障碍"——对视障用户这不是信息，是噪音，
    # 会让人不敢走动。这个下限是假阳性与漏报之间的取舍点，改动需记 ab_log。
    thresh = max(p75, config.DEPTH_MIN_ENERGY)

    results: list[dict] = []
    for c in range(cols):
        col_vals = tile[:, c]
        hits = np.where(col_vals >= thresh)[0]
        if hits.size == 0:
            continue
        r = int(hits.max())                # 最靠下的一带 = 最近
        y_contact = horizon + (r + 1) * bh
        y_norm = min(0.995, y_contact / h)
        steps = steps_from_ground_row(y_norm)
        if steps is None:
            continue
        results.append({
            "bearing": column_bearing(c, cols),
            "steps": steps,
            "kind": "obstacle",
            "confidence": round(min(0.75, float(col_vals[r]) / (thresh * 3 + 1e-6)), 2),
            "source": "geometry-heuristic",
            "y_norm": round(y_norm, 3),
        })
    return results


# ------------------------------------------------------------------ 对外接口


def estimate(image_bytes: bytes) -> list[dict]:
    """统一入口。返回 [{"bearing","steps","kind","confidence","source"}]，按步数升序。

    输出方位数量取决于相机视场角，通常只有 11 点 / 12 点 / 1 点这三个
    （见 COLS 处的说明），不是 12 个都在。

    永远不抛异常：任何失败都退化为返回空表，提示词会自动改为"不报距离"。
    """
    if not image_bytes:
        return []
    try:
        import numpy as np
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(image_bytes))
        # 必须先按 EXIF 摆正再算几何。手机竖拍的照片像素是躺倒的、方向只记在
        # EXIF 里；若直接拿去算"哪一列是哪个时钟方位"，等于把方位整体转 90°。
        img = ImageOps.exif_transpose(img).convert("RGB")
        # 按**最长边**缩放。原先按宽度判断，竖图（1080×1920）的宽度是短边，
        # 会被放过，实际仍在 1920 像素的高度上逐块算梯度，白耗 CPU。
        long_side = max(img.width, img.height)
        if long_side > config.DEPTH_WORK_SIDE:
            ratio = config.DEPTH_WORK_SIDE / long_side
            img = img.resize((max(1, int(img.width * ratio)),
                              max(1, int(img.height * ratio))), Image.LANCZOS)
        arr = np.asarray(img, dtype="uint8")
    except Exception as e:
        logger.info("深度估计跳过（图像不可解析）：%s", e)
        return []

    out: list[dict] = []
    session = _load_onnx()
    if session is not None:
        try:
            out = _estimate_with_onnx(arr, session)
        except Exception as e:
            logger.warning("ONNX 深度推理失败，回退几何启发式：%s", e)
            out = []
    if not out:
        try:
            out = _estimate_heuristic(arr)
        except Exception as e:
            logger.info("启发式深度估计失败（numpy 缺失？）：%s", e)
            return []

    # 同一方位只保留最近的一个（视障者只需要知道"这条线上多远有东西"）。
    # 这一步是必需的：多个列会映射到同一个时钟方位，必须合并成一个。
    best: dict = {}
    for r in out:
        cur = best.get(r["bearing"])
        if cur is None or r["steps"] < cur["steps"]:
            best[r["bearing"]] = r
    ordered = sorted(best.values(), key=lambda x: x["steps"])
    return ordered[: config.DEPTH_MAX_ITEMS]


def to_prompt_table(distances: list[dict]) -> str:
    """把距离表转成注入提示词的文本块。

    这是抑制距离幻觉的**核心动作**：提示词里会写明
    "距离只能引用本表，不得自行估算"。
    """
    if not distances:
        return "（本次没有取得可靠距离数据，请勿报出任何米数或步数）"
    lines = []
    for d in distances:
        kind = "障碍" if d.get("kind") == "obstacle" else "物体"
        lines.append(f"- {d['bearing']}方向，约 {d['steps']} 步处有{kind}")
    return "已知距离数据（只能引用，不得修改或估算）：\n" + "\n".join(lines)


def summary(distances: list[dict]) -> str:
    """一行摘要，用于日志与评测记录"""
    if not distances:
        return "无距离数据"
    return " / ".join(f"{d['bearing']}{d['steps']}步" for d in distances)
