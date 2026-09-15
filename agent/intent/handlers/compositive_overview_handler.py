import logging
from .base import IntentHandler
from agent.intent.slots import extract_compositive_overview_slots
from agent.intent.classifier import classify_compositive_overview_sub_type
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
MAX_UNRELATED = 3

# 子类型（query_type）判定阈值
# 大于等于该分数时，以 embedding 分类器结果覆盖正则结果
SUB_TYPE_THRESHOLD = 0.6


# ============================================================
# 综合态势总览意图处理器
# 负责：参数抽取、追问相关性判断、生成追问问题
# ============================================================
class CompositiveOverviewHandler(IntentHandler):
    name = "compositive_overview"

    async def extract_slots(self, query: str) -> dict:
        """
        抽取综合态势总览参数

        子类型（query_type）判定采用"正则为主、分类器兜底"（与设备态势对称）：
          1. 正则先抽所有字段：query_type 由正则关键词决定（默认 basic_info）
          2. 正则落到默认 basic_info 时，才用 embedding 分类器补判，
             分数达标且判为 device_health 才覆盖
        """
        # 1. 正则抽取所有字段
        basic_slots = extract_compositive_overview_slots(query)
        if basic_slots.get("query_type") == "device_health":
            return basic_slots

        # 2. 正则落到默认 basic_info 时交给分类器补判
        try:
            sub_type, score = await classify_compositive_overview_sub_type(query)
            logger.info(
                f"[extract_slots] query={query}, regex=basic_info(兜底), "
                f"classifier sub_type={sub_type}, score={score:.4f}"
            )
            if sub_type == "device_health" and score >= SUB_TYPE_THRESHOLD:
                basic_slots["query_type"] = sub_type
        except Exception as e:
            logger.warning(f"综合态势总览子类型分类失败，使用正则兜底结果: {e}")

        return basic_slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前综合态势总览任务相关
        用于追问轮
        """
        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False

        missing = state.missing_params

        # 2. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        if len(query) <= 4 and missing:
            return True

        # 3. 如果用户回复了综合态势总览相关关键词，也判定为相关
        overview_keywords = [
            "综合态势", "总览", "概况", "基本信息", "园区面积", "占地",
            "IoT", "iot", "设备总数", "设备", "健康度", "健康分", "健康",
            "统计", "数据"
        ]
        if any(k in query for k in overview_keywords):
            return True

        # 默认判定为不相关
        return False

    async def handle_reply(self, state: IntentState, query: str, llm) -> dict:
        """
        处理用户在追问阶段的回复
        """
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
            # 特别处理 query_type：如果不是默认值 basic_info，才覆盖
            if v and (k != "query_type" or v != "basic_info"):
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
        综合态势总览目前不强制需要额外参数
        """
        missing = []
        return missing

    def generate_question(self, state: IntentState) -> str:
        """
        总览两个子类型均无必填参数，正常流程不会触发追问；
        保留该方法与其它域 handler 对称，作为安全网
        """
        missing = state.missing_params

        if "date" in missing:
            question = "请问您想查询哪一天的总览数据？"
        else:
            question = "请问您还需要补充什么信息？"

        # 保存最后问题，用于不相关时重复提醒
        state.last_question = question
        # 追问次数 +1
        state.ask_count += 1

        return question