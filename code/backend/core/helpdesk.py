# -*- coding: utf-8 -*-
"""人工协助通道（F16 呼叫人工志愿者）

为什么一个"AI 智能体"作品还必须留人工通道？
—— 因为视障者遇到的情况里，总有一小部分任何模型都处理不了：
    保安拦下不让进、电梯故障、医生问的问题涉及隐私、突发身体不适。
    一个没有人工兜底的盲用系统，是不负责任的。
    这一条写进答辩，是"社会价值"分最实的落点。

实现刻意保持极简：一个工单文件 + 一个队列号，不做数据库。
理由：这是演示与真实试用期的过渡方案，过度设计只会拖慢交期。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from .config import config

logger = logging.getLogger("qiming")

# 日志写在项目 cache/ 下（backend/core/helpdesk.py → 上溯三级到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LOG_PATH = _PROJECT_ROOT / "cache" / config.HELPDESK_LOG

_TICKETS: dict = {}

# 求助原因 → 播报话术（由大模型 classify_intent 给的 intent/entities 决定）
REASON_SPEECH = {
    "lost": "已经通知志愿者来找你，请你在原地站好不要走动。",
    "blocked": "已经通知工作人员协助，请稍等，不要自己试图通过。",
    "unwell": "已经联系工作人员，如果你感觉很不舒服，请先坐下来或者扶住墙。",
    "query": "已经把你的问题转给志愿者，马上就有人回应你。",
    "other": "已经发出求助，请你在原地稍等，志愿者马上就到。",
}


def _append_log(record: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:                       # 日志失败绝不能影响求救
        logger.warning("求助日志写入失败：%s", e)


def request_help(session_id: str = "", reason: str = "other",
                 location_hint: str = "", note: str = "") -> dict:
    """发起人工求助。

    :param reason: lost / blocked / unwell / query / other
    :param location_hint: 用户口述的位置（如"门诊三楼电梯口"）
    :return: 含票号、排队号、播报文本的 dict
    """
    reason = reason if reason in REASON_SPEECH else "other"
    ticket_id = uuid.uuid4().hex[:10]
    queue_no = (len(_TICKETS) % 99) + 1
    record = {
        "ticket_id": ticket_id,
        "session_id": session_id or "-",
        "reason": reason,
        "location_hint": location_hint,
        "note": note,
        "queue_no": queue_no,
        "helper_name": config.HELPDESK_NAME,
        "helper_phone": config.HELPDESK_PHONE,
        "sla_minutes": config.HELPDESK_SLA_MINUTES,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "waiting",
    }
    _TICKETS[ticket_id] = record
    _append_log(record)
    logger.info("人工求助已登记：%s（%s）", ticket_id, reason)

    loc = f"你的位置我记下了：{location_hint}。" if location_hint else ""
    speech = (
        f"已为你呼叫{config.HELPDESK_NAME}，你的求助号是 {queue_no} 号。"
        f"{loc}{REASON_SPEECH[reason]}"
        f"预计 {config.HELPDESK_SLA_MINUTES} 分钟内有人联系你。"
        f"如果需要，可以直接拨打 {config.HELPDESK_PHONE}。"
    )
    return {**record, "speech": speech}


def get_ticket(ticket_id: str) -> dict | None:
    return _TICKETS.get(ticket_id)


def list_tickets(limit: int = 20) -> list[dict]:
    items = sorted(_TICKETS.values(), key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


def finish_ticket(ticket_id: str, note: str = "") -> dict | None:
    t = _TICKETS.get(ticket_id)
    if t is None:
        return None
    t["status"] = "finished"
    t["note"] = note or t.get("note", "")
    t["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _append_log({**t, "event": "finish"})
    return t
