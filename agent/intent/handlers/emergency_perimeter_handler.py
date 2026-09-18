import re
import logging
from .base import IntentHandler
from agent.intent.slots import extract_emergency_perimeter_slots, _parse_chinese_number, parse_ordinal_choice, parse_point_day
from agent.intent.classifier import classify_perimeter_sub_type
from memory.session_state import IntentState

logger = logging.getLogger(__name__)

# 连续不相关回复的最大容忍次数
MAX_UNRELATED = 3

# 拒绝类关键词：用户回复这些视为放弃当前任务
DECLINE_KEYWORDS = [
    "不", "不要", "不用", "不查", "不看", "不想", "不必", "算了", "否",
    "拒绝", "别", "取消", "结束", "没", "没有"
]

# 同意类关键词：用户回复这些视为同意继续
AGREE_KEYWORDS = [
    "好", "可以", "行", "要", "查", "看", "展示", "是的", "对", "嗯",
    "ok", "yes", "展示吧", "查一下", "看一下", "行吧", "好吧", "要得", "中"
]

# 子类型（query_type）判定阈值
# 大于等于该分数时，以 embedding 分类器结果覆盖正则结果
SUB_TYPE_THRESHOLD = 0.6


# ============================================================
# 周界态势意图处理器
# 负责：参数抽取、追问相关性判断、生成追问问题
# ============================================================
class EmergencyPerimeterHandler(IntentHandler):
    name = "emergency_perimeter"

    async def extract_slots(self, query: str, is_followup: bool = False) -> dict:
        """
        抽取周界态势参数

        子类型（query_type）判定采用"正则为主、分类器兜底"：
          1. 正则先抽所有字段：query_type 由正则关键词决定
          2. 仅当正则没有命中任何类型（落到 count 兜底）时，
             才用 embedding 分类器判定子类型，分数达标则覆盖

        is_followup：追问轮置 True——不重判子类型（正则落 count 时
        不再交给分类器补判），避免「告警呢」这类短回复被误判成
        其他子类型，覆盖掉已识别的 query_type
        """
        # 1. 正则抽取所有字段
        slots = extract_emergency_perimeter_slots(query)

        # 2. 只有正则落到 count 兜底时才交给分类器补判（追问轮不重判子类型）
        if slots.get("query_type") == "count" and not is_followup:
            try:
                sub_type, score = await classify_perimeter_sub_type(query)
                logger.info(
                    f"[extract_slots] query={query}, regex=count(兜底), "
                    f"classifier sub_type={sub_type}, score={score:.4f}"
                )
                if sub_type and sub_type != "count" and score >= SUB_TYPE_THRESHOLD:
                    slots["query_type"] = sub_type
            except Exception as e:
                logger.warning(f"周界子类型分类失败，使用正则兜底结果: {e}")

        return slots

    def is_related(self, state: IntentState, query: str) -> bool:
        """
        判断用户回复是否与当前周界态势任务相关
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

        # 3. 如果用户只回复了简短内容，且当前还缺参数，大概率是补充
        if len(query) <= 4 and missing:
            return True

        # 4. 如果用户回复了周界态势相关关键词，也判定为相关
        perimeter_keywords = [
            "周界", "防区", "布防", "撤防", "围栏", "围界", "入侵", "翻越",
            "告警", "报警", "抓拍", "指标", "在线", "离线"
        ]
        if any(k in query for k in perimeter_keywords):
            return True

        # 默认判定为不相关
        return False

    async def handle_reply(self, state: IntentState, query: str, llm) -> dict:
        """
        处理用户在追问阶段的回复
        """
        from datetime import datetime, timedelta

        # 如果正在等待用户确认查询类型（支持「第N个」序数指代 / 选项词）
        if state.slots.get("_pending_type_clarify"):
            options = state.slots.get("_type_options") or [
                "key_metrics", "area_overview", "perimeter_alarm_stats", "alarm_overview"
            ]
            resolved = self._resolve_type_choice(query, options)
            if resolved:
                state.slots["query_type"] = resolved
                del state.slots["_pending_type_clarify"]
                state.slots.pop("_type_options", None)
                state.unrelated_count = 0
                state.missing_params = self._get_missing_params(state.slots)
                return {
                    "action": "continue",
                    "state": state,
                    "slots": state.slots
                }
            # 解析失败不在这里死缠：落到底部常规流程，
            # 让「跑题说新需求」仍能走 is_related → 新意图接管

        # 如果正在等待用户确认时间范围
        if state.slots.get("_pending_date_clarify"):
            options = state.slots.get("_date_options", ["近三天", "近一周", "近一个月"])
            q = query.strip().lower()

            # 序数选择：第一个 / 1 / 第2个 → 映射到选项文案，
            # 改写 q 后走下方既有的句式解析（近三天/近一周/近一个月）
            idx = parse_ordinal_choice(q, len(options))
            if idx:
                q = options[idx - 1].lower()

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

            # 澄清选项列的是区间，但用户可能直接答「今天 / 昨天」等具体一天——
            # 先试点日解析，避免被下面的区间解析漏掉误回「没太理解」
            point_day = parse_point_day(q)
            if point_day is not None:
                state.slots["date"] = point_day
                del state.slots["_pending_date_clarify"]
                state.slots.pop("_date_options", None)
                state.unrelated_count = 0
                return {
                    "action": "continue",
                    "state": state,
                    "slots": state.slots
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
        # is_followup=True：追问轮不重判子类型（只允许补填未定的子类型）
        new_slots = await self.extract_slots(query, is_followup=True)
        for k, v in new_slots.items():
            # 只覆盖非空值
            # 特别处理 query_type：不是默认值 count 才补填，
            # 且子类型已定时不被本轮兜底/重判结果覆盖
            if not v or (k == "query_type" and v == "count"):
                continue
            if k == "query_type" and state.slots.get("query_type") not in (None, "", "count"):
                continue
            state.slots[k] = v

        # 重新计算缺失参数
        state.missing_params = self._get_missing_params(state.slots)

        # 类型已明确时清理类型澄清标记
        #（覆盖序数拦截分支之外的路径，如用户直接说选项词被 is_related 放行）
        if state.slots.get("query_type") not in (None, "", "count"):
            state.slots.pop("_pending_type_clarify", None)
            state.slots.pop("_type_options", None)

        return {
            "action": "continue",
            "state": state,
            "slots": state.slots
        }

    def _resolve_type_choice(self, query: str, options: list):
        """
        解析用户对查询类型追问的回复
          1. 序数指代（第一个 / 1 / 第2个）→ 按选项顺序映射
          2. 选项词文本（关键指标/防区一览…）→ 复用现有槽位抽取
        都解析不到返回 None
        """
        # 1) 序数选择
        idx = parse_ordinal_choice(query, len(options))
        if idx:
            return options[idx - 1]

        # 2) 选项词文本：正则/embedding 抽出的子类型在选项列表内即命中
        t = extract_emergency_perimeter_slots(query)["query_type"]
        if t in options:
            return t

        return None

    def _get_missing_params(self, slots: dict) -> list:
        """
        根据当前 slots 判断还缺哪些必填参数
        query_type 落到 count 兜底（子类型未识别）时视为缺失，需要追问
        """
        missing = []

        query_type = slots.get("query_type")
        if not query_type or query_type == "count":
            missing.append("query_type")

        return missing

    def generate_question(self, state: IntentState) -> str:
        """
        根据缺失参数生成追问问题
        同时更新 ask_count 和 last_question
        """
        missing = state.missing_params

        # 按优先级生成问题
        if "query_type" in missing:
            # 选项措辞与 slots.py 正则关键词对齐，回复选项词即可被识别
            question = "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"
        elif "date" in missing:
            question = "请问您想查询哪一天的周界数据？"
        else:
            question = "请问您还需要补充什么信息？"

        # 保存最后问题，用于不相关时重复提醒
        state.last_question = question
        # 追问次数 +1
        state.ask_count += 1

        return question
