import logging
from .base import IntentHandler
from agent.intent.slots import extract_device_query_slots
from agent.intent.classifier import classify_device_query_sub_type
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
MAX_UNRELATED = 3

# 子类型（query_type）判定阈值
# 大于等于该分数时，以 embedding 分类器结果覆盖正则结果
SUB_TYPE_THRESHOLD = 0.6

# 明确的"列表口径"措辞：出现这些词说明用户要的是清单，
# 此时正则为纲，不再让子类型分类器改判成详情
# （"查下广播设备清单"这类问法在语料里与详例句式接近，实测会被判成 device_detail 0.75）
LIST_WORDING = ("清单", "列表", "有哪些", "哪些", "列出", "所有", "全部", "都有", "台账")


# ============================================================
# 设备查询意图处理器
# 与"设备态势"（device_status，统计口径）区分：这里是设备台账口径，
# 查设备列表 / 查某台设备的详情
#
# 口径是**资产库**（平台纳管的设备资产，Java 侧 /mcp/devicequery 端点的
# device_query:listDevice / device_query:getDeviceDetail）：
#   - 设备类型码是 syncSource：0门禁 1道闸 2梯控 3监控 4入侵报警 5广播 6水表 7电表
#   - status 是启用/停用（0停用 1启用 2维修 3报废），不是在线/离线
#   - 列表接口筛选参数全可选，因此只有"详情"缺设备名称/编号时才追问
#
# 负责：参数抽取、追问相关性判断、生成追问问题
# ============================================================
class DeviceQueryHandler(IntentHandler):
    name = "device_query"

    async def extract_slots(self, query: str, is_followup: bool = False) -> dict:
        """
        抽取设备查询参数

        子类型（query_type）判定采用"正则为主、分类器兜底"（与综合态势总览对称）：
          1. 正则先抽所有字段：query_type 由正则关键词决定（默认 device_list）
          2. 正则落到默认 device_list 时，才用 embedding 分类器补判，
             分数达标且判为 device_detail 才覆盖
          3. 用户明确说了"清单/列表/有哪些"等列表措辞时（LIST_WORDING），
             不再让分类器改判——否则"查下广播设备清单"会被判成详情

        is_followup：追问轮置 True——只抽设备名称/编码等填空字段，
        不重判子类型（用户在详情追问里只回"MH-001"，短回复会被兜底口径冲掉）
        """
        # 1. 正则抽取所有字段
        slots = extract_device_query_slots(query)

        # 2. 正则落到默认 device_list 时交给分类器补判
        #    （追问轮短回复易被误判，且补调分类器的成本无谓）
        if (
            slots.get("query_type") != "device_detail"
            and not is_followup
            and not any(k in query for k in LIST_WORDING)
        ):
            try:
                sub_type, score = await classify_device_query_sub_type(query)
                logger.info(
                    f"[extract_slots] query={query}, regex=device_list(兜底), "
                    f"classifier sub_type={sub_type}, score={score:.4f}"
                )
                if sub_type == "device_detail" and score >= SUB_TYPE_THRESHOLD:
                    slots["query_type"] = sub_type
            except Exception as e:
                logger.warning(f"设备查询子类型分类失败，使用正则兜底结果: {e}")

        if is_followup:
            # 追问轮子类型只能不变：丢弃本轮重判结果，避免覆盖原查询
            slots.pop("query_type", None)

        return slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前设备查询任务相关
        用于追问轮（详情查询缺设备名称/编码时）
        """
        # 1. 如果用户明确说放弃，直接判为不相关
        if any(k in query for k in ["算了", "不查了", "取消", "不问了", "结束"]):
            return False

        missing = state.missing_params

        # 2. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        #    例如追问"哪类设备"时回复"监控"、追问"哪台设备"时回复"MJ-003"
        if len(query) <= 20 and missing:
            return True

        # 3. 如果用户回复了设备查询相关关键词，也判定为相关
        #    设备类型只收资产库查得到的八类（0门禁 1道闸 2梯控 3监控 4入侵报警
        #    5广播 6水表 7电表，见 slots.py 的 DEVICE_TYPE_DICT）
        device_keywords = [
            "设备", "列表", "清单", "台账", "详情", "详细", "档案", "信息",
            "编码", "编号", "名称", "型号", "参数", "规格",
            "门禁", "闸机", "道闸", "车闸", "梯控", "电梯",
            "监控", "摄像头", "摄像机", "枪机", "球机", "半球",
            "入侵报警", "周界报警", "报警设备", "红外报警",
            "广播", "音箱", "喇叭", "水表", "电表",
            "楼", "层", "栋", "园区", "食堂", "停车场", "北门", "南门",
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

        # 从用户最新回复中抽取参数，补充到已有 slots
        # is_followup=True：追问轮只补填空字段，不重判子类型
        new_slots = await self.extract_slots(query, is_followup=True)
        for k, v in new_slots.items():
            # 只覆盖非空值
            # 特别处理 query_type：如果不是默认值 device_list，才覆盖
            # （用户在详情追问里补充"MH-001"，不能被兜底的列表口径冲掉）
            if v and (k != "query_type" or v != "device_list"):
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

        资产库列表接口的筛选参数全是可选的（不传设备类型就是全部类型），
        所以列表查询没有必填项。设备类型/区域/楼栋/楼层都只是可选筛选条件。
        只有详情必须能定位到某一台设备（getDeviceDetail 只认内部 id，
        得先拿名称或编号去列表里把它搜出来），否则缺 device_keyword。
        """
        missing = []

        if slots.get("query_type") == "device_detail" and not slots.get("device_keyword"):
            missing.append("device_keyword")

        return missing

    def generate_question(self, state: IntentState) -> str:
        """
        根据缺失参数生成追问问题
        同时更新 ask_count 和 last_question
        """
        missing = state.missing_params

        # 只有详情会缺参数：缺的是"查哪一台设备"
        if "device_keyword" in missing:
            question = "请问您想查看哪台设备的详情？可以说设备名称或设备编号。"
        else:
            question = "请问您还需要补充什么信息？"

        # 保存最后问题，用于不相关时重复提醒
        state.last_question = question
        # 追问次数 +1
        state.ask_count += 1

        return question
