import re
import logging
from .base import IntentHandler
from agent.intent.slots import extract_security_status_slots, _parse_chinese_number
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
# 超过则放弃当前任务
MAX_UNRELATED = 3

# 拒绝类关键词：用户回复这些视为放弃当前任务
DECLINE_KEYWORDS = [
    "不", "不要", "不用", "不查", "不看", "不想", "不必", "算了", "否",
    "拒绝", "别", "取消", "结束", "没", "没有"
]


# ============================================================
# 安防态势意图处理器
# 当前支持：alarm_list / alarm_detail / patrol / device /
#           security_index / ai_alert / inspection_trend / ai_inspection
# 负责：参数抽取、追问相关性判断、处理追问回复
# ============================================================
class SecurityStatusHandler(IntentHandler):
    name = "security_status"

    async def extract_slots(self, query: str) -> dict:
        """
        抽取安防态势参数

        当前支持 alarm_list（告警列表）、alarm_detail（告警详情）、
        patrol（巡查/巡逻任务）、device（安防设备状态）。
        后续扩展其他子类型时，再在这里加 embedding 分类器兜底。
        """
        slots = extract_security_status_slots(query)
        return slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前安防态势任务相关
        用于追问轮
        """
        # 0. 如果正在等待用户确认时间范围，都视为相关
        if state.slots.get("_pending_date_clarify"):
            return True

        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False

        missing = state.missing_params

        # 2. 如果缺告警 ID，用户回复了序号或数字，判定为相关
        if "alarm_id" in missing:
            if re.search(r"第\s*\d+\s*条|告警\s*\d+|告警ID\s*\d+|id\s*\d+|\d+", query):
                return True

        # 3. 如果缺时间，用户回复了时间词，判定为相关
        if "date" in missing:
            if any(k in query for k in ["今天", "昨天", "明天", "本周", "本月", "上周", "上月", "近", "天"]):
                return True

        # 4. 如果用户回复了安防相关关键词，也判定为相关
        security_keywords = [
            "告警", "报警", "安防", "未处理", "查看", "展示", "拉一下",
            "巡查", "巡逻", "巡更", "巡检", "设备", "摄像头", "门禁", "离线",
            "安全指数", "安全状况", "AI告警", "智能告警", "告警分布",
            "巡查趋势", "智能巡检", "AI巡查"
        ]
        if any(k in query for k in security_keywords):
            return True

        # 5. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
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
        # 情况 A：正在等待用户确认时间范围（上一轮被 vague_date 问过）
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
        new_slots = await self.extract_slots(query)
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
        - alarm_list / patrol / device 必须有时间范围（MCP 传 {startTime, endTime}）
        - alarm_detail 必须有 alarm_id
        """
        missing = []
        event_type = slots.get("event_type", "alarm_list")

        if event_type == "alarm_detail":
            if not slots.get("alarm_id"):
                missing.append("alarm_id")
        else:
            if not slots.get("date"):
                missing.append("date")

        return missing
