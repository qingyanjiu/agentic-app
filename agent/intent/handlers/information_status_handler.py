import re
import logging
from .base import IntentHandler
from agent.intent.slots import extract_information_status_slots, _parse_chinese_number
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
MAX_UNRELATED = 3

# 拒绝类关键词：用户回复这些视为放弃当前任务
DECLINE_KEYWORDS = [
    "不", "不要", "不用", "不查", "不看", "不想", "不必", "算了", "否",
    "拒绝", "别", "取消", "结束", "没", "没有"
]


# ============================================================
# 信息发布意图处理器
# 负责：参数抽取、追问相关性判断、处理追问回复
# 子类型（event_type）：info_view / broadcast_view / task_trend /
#                      program_count / info_equip / broadcast_equip
# MCP 调用传 {startTime, endTime}
# ============================================================
class InformationStatusHandler(IntentHandler):
    name = "information_status"

    async def extract_slots(self, query: str, is_followup: bool = False) -> dict:
        """
        抽取信息发布参数

        is_followup：追问轮置 True——只抽时间等填空字段，
        不重判子类型（短回复易落回默认值或误命中关键词，污染原查询）
        """
        slots = extract_information_status_slots(query)
        if is_followup:
            # 追问轮子类型只能不变：丢弃本轮重判结果，避免覆盖原查询
            slots.pop("event_type", None)
        return slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前信息发布任务相关
        用于追问轮
        """
        # 0. 如果正在等待用户确认时间范围，都视为相关
        if state.slots.get("_pending_date_clarify"):
            return True

        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False

        missing = state.missing_params

        # 2. 如果缺时间，用户回复了时间词，判定为相关
        if "date" in missing:
            if any(k in query for k in ["今天", "昨天", "明天", "本周", "本月", "上周", "上月", "近", "天"]):
                return True

        # 3. 如果用户回复了信息发布相关关键词，也判定为相关
        information_keywords = [
            "信息发布", "广播", "发布", "节目", "任务", "设备", "终端",
            "信息屏", "发布屏", "广播设备", "发布设备", "在线", "离线",
            "统计", "趋势", "一览", "占比", "状态"
        ]
        if any(k in query for k in information_keywords):
            return True

        # 4. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        if len(query) <= 4 and missing:
            return True

        # 默认判定为不相关
        return False

    async def handle_reply(self, state: IntentState, query: str, llm) -> dict:
        """
        处理用户在追问阶段的回复
        """
        from datetime import datetime, timedelta

        # ============================================================
        # 情况 A：正在等待用户确认时间范围
        # 解析"近三天 / 近4天 / 昨天 / 一周"等，生成 {start_time, end_time}
        # ============================================================
        if state.slots.get("_pending_date_clarify"):
            options = state.slots.get("_date_options", ["近三天", "近一周", "近一个月"])
            q = query.strip().lower()
            now = datetime.now()
            start = None
            label = None

            # 用户明确拒绝
            if any(k in q for k in DECLINE_KEYWORDS):
                return {
                    "action": "give_up",
                    "reason": "用户拒绝确认时间范围",
                    "answer": "好的，已取消查询。请问您还有其他问题吗？"
                }

            # 先尝试通用解析“近N天 / 前N天 / N天 / 最近N天”
            m = re.search(r"(近|最近|前|过去)?(\d+|[一二两三四五六七八九十]+)(?:个)?天", q)
            if m:
                n_str = m.group(2)
                n = _parse_chinese_number(n_str)
                days_back = max(1, n - 1)
                start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
                label = f"近{n}天"
            elif "三天" in q:
                start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
                label = "近三天"
            elif "一周" in q or "星期" in q or "周" in q:
                start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
                label = "近一周"
            elif "月" in q:
                start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
                label = "近一个月"

            if start is None:
                # 没听懂，再次询问
                return {
                    "action": "re_ask",
                    "state": state,
                    "answer": f"没太理解，请说具体天数，例如“近两天”、“近四天”，或选择：{'、'.join(options)}"
                }

            # 组装 MCP 需要的 {startTime, endTime} 对应的 date 结构
            state.slots["date"] = {
                "time_type": "span",
                "start_time": start.isoformat(),
                "end_time": now.isoformat(),
                "raw": label
            }
            del state.slots["_pending_date_clarify"]
            state.slots.pop("_date_options", None)
            state.unrelated_count = 0
            return {
                "action": "continue",
                "state": state,
                "slots": state.slots
            }

        # ============================================================
        # 情况 B：没有待确认项，判断用户回复是否与当前任务相关
        # ============================================================
        is_related = self.is_related(state, query)

        if not is_related:
            # 不相关次数 +1
            state.unrelated_count += 1

            # 超过最大容忍次数，放弃当前任务
            if state.unrelated_count >= MAX_UNRELATED:
                return {
                    "action": "give_up",
                    "reason": "连续不相关超过阈值",
                    "answer": "看起来您可能不想继续查询了，请问您还有其他问题吗？"
                }

            # 未超过阈值，再次提醒用户
            return {
                "action": "re_ask",
                "state": state,
                "answer": f"您可能没理解我的问题。{state.last_question} 或者您可以直接说新的需求。"
            }

        # 用户回复相关，重置不相关计数
        state.unrelated_count = 0

        # 从用户最新回复中抽取参数，补充到已有 slots
        # is_followup=True：追问轮只补填空字段，不重判子类型
        new_slots = await self.extract_slots(query, is_followup=True)
        for k, v in new_slots.items():
            # 只覆盖非空值
            if v:
                state.slots[k] = v

        # 重新计算缺失参数
        state.missing_params = self._get_missing_params(state.slots)

        return {
            "action": "continue",
            "state": state,
            "slots": state.slots
        }

    def _get_missing_params(self, slots: dict) -> list:
        """
        根据当前 slots 判断还缺哪些必填参数
        信息发布查询必须有时间范围（MCP 传 {startTime, endTime}）
        """
        missing = []
        if not slots.get("date"):
            missing.append("date")
        return missing
