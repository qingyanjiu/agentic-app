import logging
from .base import IntentHandler
from agent.intent.slots import extract_device_status_slots, extract_device_type
from agent.intent.classifier import classify_device_sub_type
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
MAX_UNRELATED = 3

# 子类型（query_type）判定阈值
# 大于等于该分数时，以 embedding 分类器结果覆盖正则结果
SUB_TYPE_THRESHOLD = 0.6

# 统计口径信号词：追问"哪类设备"时，回复里带这些词说明用户重说了完整问法
# （"门禁设备在线率"），不能当成对设备类型问题的回答
_TYPE_ANSWER_NOISE = (
    "在线", "离线", "率", "趋势", "占比", "统计", "健康", "排名", "分布", "数量", "走势",
)


# ============================================================
# 设备态势意图处理器
# 负责：参数抽取、追问相关性判断、生成追问问题
#
# 兜底反问（与消防/周界同一约定）：
#   用户只说"查下设备"这类笼统问法时，正则落 count 兜底、分类器也判不出子类型，
#   此时不再走 call_tool 的"暂不支持"守卫，而是反问"哪类设备"；
#   用户选定后按**台账口径**调 device_query:listDeviceOnly 列出该类设备
#   （设备域只有"设备列表/设备详情"两个台账工具，走资产库口径的 syncSource）。
# ============================================================
class DeviceStatusHandler(IntentHandler):
    name = "device_status"

    async def extract_slots(self, query: str) -> dict:
        """
        抽取设备态势参数

        子类型（query_type）判定采用"正则为主、分类器兜底"：
          1. 正则先抽所有字段：query_type 由正则关键词决定
          2. 仅当正则没有命中任何类型（落到 count 兜底）时，
             才用 embedding 分类器判定子类型，分数达标则覆盖
        """
        # 1. 正则抽取所有字段
        slots = extract_device_status_slots(query)

        # 2. 只有正则落到 count 兜底时才交给分类器补判
        if slots.get("query_type") == "count":
            try:
                sub_type, score = await classify_device_sub_type(query)
                logger.info(
                    f"[extract_slots] query={query}, regex=count(兜底), "
                    f"classifier sub_type={sub_type}, score={score:.4f}"
                )
                if sub_type and sub_type != "count" and score >= SUB_TYPE_THRESHOLD:
                    slots["query_type"] = sub_type
            except Exception as e:
                logger.warning(f"设备子类型分类失败，使用正则兜底结果: {e}")

        return slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前设备态势任务相关
        用于追问轮
        """
        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False

        missing = state.missing_params

        # 2. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        if len(query) <= 4 and missing:
            return True

        # 3. 如果用户回复了设备态势相关关键词，也判定为相关
        device_keywords = [
            "设备", "分类", "占比", "健康", "在线", "离线", "维修", "维保",
            "报修", "区域", "安防", "监控", "广播", "门禁", "趋势", "统计",
            "数量", "故障",
            # 兜底反问的选项词（资产库口径的八类设备）
            "道闸", "梯控", "电梯", "入侵报警", "报警", "水表", "电表",
        ]
        if any(k in query for k in device_keywords):
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

        # 上一轮问的是"哪类设备"：只按设备类型解析这一句回复。
        # 不能交给下面的常规合并路径——"门禁"在设备态势正则里会命中
        # mj_online（门禁在线率），用户其实只是在回答设备类型。
        # 但回复里带"在线率/趋势/占比"这类统计口径词时，说明用户重说了一个
        # 完整问法（如"门禁设备在线率"），那种情况仍走常规路径
        if "device_type" in (state.missing_params or []) and not any(
            k in query for k in _TYPE_ANSWER_NOISE
        ):
            device_type = extract_device_type(query)
            if device_type:
                state.slots["device_type"] = device_type
                # 类型选定了才敢定口径：笼统问法一律按台账列表查
                if state.slots.get("query_type") in (None, "", "count"):
                    state.slots["query_type"] = "device_list"
                state.missing_params = self._get_missing_params(state.slots)
                return {
                    "action": "continue",
                    "state": state,
                    "slots": state.slots
                }

        # 从用户最新回复中抽取参数，补充到已有 slots
        new_slots = await self.extract_slots(query)
        for k, v in new_slots.items():
            # 只覆盖非空值
            # 特别处理 query_type：如果不是默认值 count，才覆盖
            if v and (k != "query_type" or v != "count"):
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

        设备态势其余子类型不强制需要额外参数（月度趋势的 date 为可选）；
        只有 query_type 落到 count 兜底（分不清哪类设备数据）时，
        视为缺 device_type，走反问（与 graph 的 check_missing_params 对称）。
        类型已经明确的（device_type 有值）不用再问，按台账列表查即可
        """
        missing = []
        if slots.get("query_type") in (None, "", "count") and not slots.get("device_type"):
            missing.append("device_type")
        return missing

    def generate_question(self, state: IntentState) -> str:
        """
        根据缺失参数生成追问问题
        同时更新 ask_count 和 last_question
        """
        missing = state.missing_params

        # 按优先级生成问题
        if "device_type" in missing:
            question = "请问您想查询哪类设备？门禁、道闸、梯控、监控、入侵报警、广播、水表，还是电表？"
        elif "date" in missing:
            question = "请问您想查询哪个月份的设备数据？"
        else:
            question = "请问您还需要补充什么信息？"

        # 保存最后问题，用于不相关时重复提醒
        state.last_question = question
        # 追问次数 +1
        state.ask_count += 1

        return question
