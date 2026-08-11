import re
from .base import IntentHandler
from agent.intent.slots import extract_person_status_slots
from memory.session_state import IntentState

# 连续不相关回复的最大容忍次数
# 超过则放弃当前任务
MAX_UNRELATED = 3


# ============================================================
# 人员态势意图处理器
# 负责：参数抽取、追问相关性判断、生成追问问题
# ============================================================
class PersonStatusHandler(IntentHandler):
    name = "person_status"
    
    def extract_slots(self, query: str) -> dict:
        """
        抽取人员态势参数
        直接复用 slots.py 中的逻辑
        """
        return extract_person_status_slots(query)
    
    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前人员态势任务相关
        用于追问轮
        """
        # 0. 如果正在等待用户确认是否查询今日数据，任何简短回复都视为相关
        if state.slots.get("_pending_confirm"):
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
        # 如果正在等待确认是否查询今日数据
        pending = state.slots.get("_pending_confirm")
        if pending:
            # 用户明确拒绝
            if any(k in query for k in ["不要", "不用", "不查", "不看", "算了", "否"]):
                return {
                    "action": "give_up",
                    "reason": "用户拒绝查询今日数据",
                    "answer": "好的，已取消查询。请问您还有其他问题吗？"
                }

            # 其他回复（可以/好/是的/查一下/直接回车）都视为确认
            state.slots["_confirm_proceed"] = True
            del state.slots["_pending_confirm"]
            state.unrelated_count = 0
            return {
                "action": "continue",
                "state": state,
                "slots": state.slots
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
        new_slots = self.extract_slots(query)
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