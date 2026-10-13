# -*- coding: utf-8 -*-
"""场景 SOP（标准作业流程）状态机

为什么用状态机而不是让大模型自由生成流程？
——就医流程一旦被模型"编造"，会真实误导用户。
这里把流程写死为数据结构，大模型只负责两件小事：
  1) 在固定节点上理解用户意图；
  2) 判断是否已达成当前节点（由画面证据决定）。
这既保证了安全性，也构成区别于普通问答机器人的核心设计。

新增流程只需在 FLOWS 里加一个 dict，不需要改任何逻辑代码。
"""

from typing import Dict, Optional

FLOWS = {
    "sop_medical": {
        "title": "医院就诊引导",
        "steps": [
            {
                "id": "enter",
                "title": "进入门诊大厅",
                "guide": "请面向大厅正前方站好，我来帮你找挂号的地方。",
                "success_signal": "门诊大厅，能看到挂号窗口或自助机的指示牌",
            },
            {
                "id": "register",
                "title": "挂号/取号",
                "guide": "请举起手机对准前方，我们找一下挂号窗口或自助挂号机。",
                "success_signal": "挂号窗口或自助挂号机出现在画面中",
            },
            {
                "id": "queue",
                "title": "候诊区等待叫号",
                "guide": "请坐到候诊区，留意叫号屏或广播。需要我帮你看屏幕上的号码吗？",
                "success_signal": "候诊座椅区或叫号显示屏",
            },
            {
                "id": "clinic",
                "title": "前往诊室",
                "guide": "叫到号后，请把挂号单或手机屏幕上的诊室号拍给我，我帮你确认。",
                "success_signal": "带有房间号的门牌",
            },
            {
                "id": "pay",
                "title": "缴费",
                "guide": "就诊结束后，我们找缴费窗口或自助缴费机。请对准前方拍照。",
                "success_signal": "收费窗口或自助缴费机",
            },
            {
                "id": "pharmacy",
                "title": "取药",
                "guide": "请对准前方，我帮你找药房窗口，并确认你的取药窗口号。",
                "success_signal": "药房窗口或取药叫号屏",
            },
        ],
    },
    "sop_transit": {
        "title": "公共交通出行引导",
        "steps": [
            {
                "id": "station",
                "title": "找到站牌/入口",
                "guide": "请面向道路正前方，我帮你确认公交站牌的位置。",
                "success_signal": "公交站牌或地铁站指示牌",
            },
            {
                "id": "line",
                "title": "确认线路",
                "guide": "请把站牌拍清楚，我读给你听有哪些线路。",
                "success_signal": "站牌上的线路号码清晰可见",
            },
            {
                "id": "board",
                "title": "候车与上车",
                "guide": "请在站台等候，听到车辆进站声告诉我，我帮你确认是不是你要坐的车。",
                "success_signal": "公交车停靠且车身线路号可见",
            },
        ],
    },
}


class SopSession:
    """一次 SOP 会话。可在内存 / Redis 中存放，这里用 dict 做最小实现。"""

    def __init__(self, flow_id: str):
        if flow_id not in FLOWS:
            raise KeyError(f"未知流程：{flow_id}")
        self.flow_id = flow_id
        self.flow = FLOWS[flow_id]
        self.index = 0
        self.history: list = []

    @property
    def current(self):
        """当前步骤；已结束时返回 None（调用方必须先看 finished）"""
        if self.finished:
            return None
        return self.flow["steps"][self.index]

    @property
    def finished(self) -> bool:
        return self.index >= len(self.flow["steps"])

    @property
    def total_steps(self) -> int:
        return len(self.flow["steps"])

    def progress(self) -> int:
        """当前是第几步（1 基，用于前端进度显示）"""
        return min(self.index + 1, self.total_steps)

    def next_prompt(self, reached: bool, hint: str = "") -> dict:
        """推进状态机。reached=True 才前进，否则停留在当前节点重给引导。

        返回给前端的 speech 即为本次要播报的全部内容。
        """
        if self.finished:
            return {
                "finished": True,
                "step": None,
                "index": self.total_steps,
                "total_steps": self.total_steps,
                "speech": f"{self.flow['title']}已经完成了。需要的话，可以开始新的引导。",
            }

        if reached:
            self.history.append({"step": self.current["id"], "result": "reached"})
            self.index += 1
            if self.finished:
                return {
                    "finished": True,
                    "step": None,
                    "index": self.total_steps,
                    "total_steps": self.total_steps,
                    "speech": f"{self.flow['title']}已完成，祝你顺利。需要的话我可以继续帮你。",
                }
            step = self.current
            return {
                "finished": False,
                "step": {"id": step["id"], "title": step["title"]},
                "index": self.progress(),
                "total_steps": self.total_steps,
                "speech": f"上一步完成。接下来：{step['title']}。{step['guide']}",
            }

        # 未达成：停留在当前节点，把视觉校验的提示播出去
        step = self.current
        tail = f"。{hint}" if hint else f"。{step['guide']}"
        return {
            "finished": False,
            "step": {"id": step["id"], "title": step["title"]},
            "index": self.progress(),
            "total_steps": self.total_steps,
            "speech": f"还没有到{step['title']}的位置{tail}",
        }

    def step_back(self) -> dict:
        """退回上一步（F11b，无障碍专用的纠错通道）。

        为什么必须有：明眼用户靠进度条就能看出"这一步我是不是真的到了"，
        视障用户只能听一句描述，再自己判断 —— 把走廊尽头当服务台、
        把站牌背面当站牌、把取号机当成挂号窗口，都是很常见的误判。
        而 index 原实现只能 +1，误判一次的唯一出路是"退出流程"（DELETE 会话），
        代价是从第 1 步重走整条流程；对正站在马路边、手上有行李的人来说不可接受。

        语义边界（三条，缺一条就会出现"退不回去"或"退过头"）：
        · 已经在第 1 步 → 停在原地，明确告知"已经是第一步了"，不报错（报错会让人以为坏了）；
        · 流程已结束（index 越界）→ 先退回最后一步，让"误判最后一步"也能挽回；
        · 退回后 finished 一定为 False，否则前端会继续把卡片当作"已结束"藏起来。

        history 里对应记录也一并弹出，保证 history 与 index 始终一致 ——
        否则会话结束后导出的路径会把用户**没走过**的那一步算进去。
        """
        moved = self.index > 0
        if self.finished:
            # 越界（已结束）→ 落到最后一步，让"误判最后一步"也能挽回
            moved = True
            self.index = self.total_steps - 1
        elif moved:
            self.index -= 1

        # history 与 index 必须同步：一条 history 记录对应"完成过一步"。
        # 只退 index 不退 history，会话结束时导出的路径会把用户没走过的一步算进去。
        if moved and self.history:
            self.history.pop()

        step = self.current
        n, total = self.progress(), self.total_steps
        lead = f"已退回第 {n} 步" if moved else "已经是第一步了"
        return {
            "finished": False,
            "back": True,
            "moved": moved,
            "step": {"id": step["id"], "title": step["title"]},
            "index": n,
            "total_steps": total,
            "speech": f"{lead}，共 {total} 步：{step['title']}。{step['guide']}",
        }


# 简易会话存储（生产环境替换为 Redis）
_SESSIONS: dict = {}


def start_session(session_id: str, flow_id: str) -> SopSession:
    s = SopSession(flow_id)
    _SESSIONS[session_id] = s
    return s


def get_session(session_id: str) -> Optional[SopSession]:
    return _SESSIONS.get(session_id)


def drop_session(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)
