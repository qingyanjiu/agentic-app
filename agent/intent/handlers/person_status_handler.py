import re
import logging
from .base import IntentHandler
from agent.intent.slots import extract_person_status_slots, _parse_chinese_number
from agent.intent.classifier import classify_sub_type
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
# 超过则放弃当前任务
MAX_UNRELATED = 3

# 子类型（query_type）判定阈值
# 大于等于该分数时，以 embedding 分类器结果覆盖正则结果
SUB_TYPE_THRESHOLD = 0.6

# 确认类关键词：用户回复这些视为同意继续
AGREE_KEYWORDS = [
    "好", "可以", "行", "要", "查", "看", "展示", "是的", "对", "嗯",
    "ok", "yes", "展示吧", "查一下", "看一下", "行吧", "好吧", "要得", "中"
]

# 拒绝类关键词：用户回复这些视为放弃当前任务
DECLINE_KEYWORDS = [
    "不", "不要", "不用", "不查", "不看", "不想", "不必", "算了", "否",
    "拒绝", "别", "取消", "结束", "没", "没有"
]


# ============================================================
# 人员态势意图处理器
# 负责：参数抽取、追问相关性判断、生成追问问题
# ============================================================
class PersonStatusHandler(IntentHandler):
    name = "person_status"
    
    async def extract_slots(self, query: str) -> dict:
        """
        抽取人员态势参数
        子类型（query_type）判定采用"正则为主、分类器救兜底"：
          1. 正则先抽所有字段：query_type 由正则关键词决定（保留领域排序，
             如"省公司进了多少人"命中 enter 而不是 structure）
          2. 仅当正则没有命中任何类型（落到 count 兜底）时，
             才用 embedding 分类器判定子类型，分数达标则覆盖
        人名/时间等其他字段始终由正则抽取
        """
        # 1. 正则抽取所有字段（query_type + 人名/时间等）
        slots = extract_person_status_slots(query)

        # 2. 只有正则落到 count 兜底时才交给分类器补判
        if slots.get("query_type") == "count":
            try:
                sub_type, score = await classify_sub_type(query)
                logger.info(
                    f"[extract_slots] query={query}, regex=count(兜底), "
                    f"classifier sub_type={sub_type}, score={score:.4f}"
                )
                if sub_type and sub_type != "count" and score >= SUB_TYPE_THRESHOLD:
                    slots["query_type"] = sub_type
            except Exception as e:
                logger.warning(f"子类型分类失败，使用正则兜底结果: {e}")

        return slots
    
    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前人员态势任务相关
        用于追问轮
        """
        # 0. 如果正在等待用户确认是否查询今日数据或确认时间范围，都视为相关
        if state.slots.get("_pending_confirm") or state.slots.get("_pending_date_clarify"):
            return True

        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False
        
        missing = state.missing_params
        
        # 2. 如果缺人名，用户回复了简短中文词，大概率是补充人名
        if "person_name" in missing:
            if re.search(r'[\u4e00-\u9fa5]{2,4}', query) and len(query) <= 8:
                return True
        
        # 3. 如果缺时间，用户回复了时间词，判定为相关
        if "date" in missing:
            if any(k in query for k in ["今天", "昨天", "明天", "本周", "本月", "上周", "上月"]):
                return True
        
        # 4. 如果缺区域，用户回复了区域词，判定为相关
        if "area" in missing:
            if any(k in query for k in ["A栋", "B栋", "主楼", "食堂", "停车场", "大门口"]):
                return True
        
        # 5. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        if len(query) <= 4 and missing:
            return True

        # 6. 如果用户回复了人员态势相关关键词，也判定为相关
        person_status_keywords = [
            "实时", "进入", "离开", "人数", "流动", "结构", "分布",
            "中通服", "省公司", "规划设计院", "异常", "轨迹", "位置"
        ]
        if any(k in query for k in person_status_keywords):
            return True
        # 默认判定为不相关
        return False
    
    async def handle_reply(self, state: IntentState, query: str, llm) -> dict:
        """
        处理用户在追问阶段的回复
        """
        from datetime import datetime, timedelta

        # 如果正在等待用户确认时间范围
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

        # 如果正在等待确认是否查询今日数据
        pending = state.slots.get("_pending_confirm")
        if pending:
            # 用户明确拒绝
            if any(k in query for k in DECLINE_KEYWORDS):
                return {
                    "action": "give_up",
                    "reason": "用户拒绝查询今日数据",
                    "answer": "好的，已取消查询。请问您还有其他问题吗？"
                }

            # 用户明确同意
            if any(k in query for k in AGREE_KEYWORDS):
                state.slots["_confirm_proceed"] = True
                del state.slots["_pending_confirm"]
                state.unrelated_count = 0
                return {
                    "action": "continue",
                    "state": state,
                    "slots": state.slots
                }

            # 其他模糊回复，再次提醒
            return {
                "action": "re_ask",
                "state": state,
                "answer": f"没太理解您的意思。{state.last_question}"
            }
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
            # 特别处理 query_type：如果不是默认值 location，才覆盖
            if v and (k != "query_type" or v != "location"):
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
        人员态势中，位置和轨迹查询必须有人名
        """
        missing = []
        query_type = slots.get("query_type")
        
        if query_type in ["location", "trace"] and not slots.get("person_name"):
            missing.append("person_name")
        
        return missing
    
    def generate_question(self, state: IntentState) -> str:
        """
        根据缺失参数生成追问问题
        同时更新 ask_count 和 last_question
        """
        missing = state.missing_params
        
        # 按优先级生成问题
        if "person_name" in missing:
            question = "请问您要查询哪位人员？"
        elif "date" in missing:
            question = "请问您想查询哪一天？"
        elif "area" in missing:
            question = "请问您想查询哪个区域？"
        else:
            question = "请问您还需要补充什么信息？"
        
        # 保存最后问题，用于不相关时重复提醒
        state.last_question = question
        # 追问次数 +1
        state.ask_count += 1
        
        return question