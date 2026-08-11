import asyncio
import logging
import traceback
from datetime import date, datetime

from memory.session_state import session_state, IntentState
from agent.intent.handlers.person_status_handler import PersonStatusHandler

# 从 MCP 配置加载真实的人员态势工具
from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# Python query_type 到 Java MCP 工具名的映射
# Java 侧车目前只实现了今日态势和人员流动两个工具
_JAVA_TOOL_MAP = {
    "realtime": "person_status:getTodayPersonnelAffairs",
    "enter": "person_status:getTodayPersonnelAffairs",
    "leave": "person_status:getTodayPersonnelAffairs",
    "count": "person_status:getTodayPersonnelAffairs",
    "flow": "person_status:getTodayPersonnelFlow",
}


def _is_today(date_slots: dict) -> bool:
    """
    判断用户查询的时间范围是否为今天
    人员态势后端目前只支持查询今日数据
    """
    start_time = date_slots.get("start_time")
    if not start_time:
        return True

    try:
        start_dt = datetime.fromisoformat(start_time)
        return start_dt.date() == date.today()
    except Exception:
        # 解析失败时默认按今天处理，避免误拦截
        return True


def _extract_text(result) -> str:
    """
    从 MCP 工具返回结构中提取文本内容
    兼容：
      - 普通字符串
      - LangChain 的 [{type: 'text', text: '...'}] 列表
    """
    if isinstance(result, str):
        return result

    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("text") or str(first)

    return str(result)


def _format_person_status_result(query_type: str, raw_text: str) -> str:
    """
    根据 query_type 从 Java 返回的今日态势文本中提取用户关心的字段

    Java 返回格式示例：
      "实时在园人数：0人；今日进入人数：0人；今日离开人数：0人。"
    """
    import re

    # 1. 人员流动趋势：直接返回原始数据
    if query_type == "flow":
        return raw_text

    # 2. 实时/总人数：提取"实时在园人数"
    if query_type in ("realtime", "count"):
        m = re.search(r"实时在园人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"实时在园人数：{m.group(1)}人"
        return raw_text

    # 3. 进入人数
    if query_type == "enter":
        m = re.search(r"今日进入人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"今日进入人数：{m.group(1)}人"
        return raw_text

    # 4. 离开人数
    if query_type == "leave":
        m = re.search(r"今日离开人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"今日离开人数：{m.group(1)}人"
        return raw_text

    # 兜底：原样返回
    return raw_text


def _query_type_label(query_type: str) -> str:
    """根据 query_type 返回中文名称，用于生成确认问题"""
    return {
        "realtime": "实时人数",
        "enter": "进入人数",
        "leave": "离开人数",
        "count": "园区总人数",
        "flow": "人员流动趋势",
    }.get(query_type, "数据")


class PersonStatusPipeline:
    _tools = None

    def __init__(self, llm, tools):
        self.llm = llm
        # 以工具名作为 key，方便按 Java 侧车的工具名查找
        self.tools = {t.name: t for t in tools}

    @classmethod
    async def create(cls, llm, user_id=None, session_id=None):
        """
        工厂方法：首次调用时从 MCP 加载人员态势工具
        使用类变量 _tools 缓存，避免每个请求都重新 fork 子进程
        """
        # 首次加载时从 MCP 配置读取工具
        if cls._tools is None:
            try:
                # 15 秒超时保护：防止 MCP 子进程启动异常导致服务卡住
                tools = await asyncio.wait_for(
                    get_mcp_tools("mcp_client/mcp_server_config.yaml"),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.error("加载 MCP 人员态势工具超时")
                cls._tools = []
            except Exception as e:
                logger.error(f"加载 MCP 人员态势工具失败: {e}", exc_info=True)
                cls._tools = []
            else:
                # 只保留需要调用的人员态势工具
                java_tool_names = set(_JAVA_TOOL_MAP.values())
                cls._tools = [t for t in tools if t.name in java_tool_names]
                if not cls._tools:
                    logger.warning(
                        f"未找到任何人员态势 MCP 工具，期望 {java_tool_names}"
                    )

        return cls(llm, cls._tools)

    def _get_missing_params(self, slots: dict) -> list:
        missing = []
        query_type = slots.get("query_type")
        if query_type in ["location", "trace"] and not slots.get("person_name"):
            missing.append("person_name")
        return missing

    async def astream_run(self, state: IntentState, user_id: str, session_id: str):
        """
        流式执行人员态势 pipeline
        如果参数缺失则追问，参数完整则调用工具
        """
        handler = PersonStatusHandler()

        # ============================================================
        # 1. 检查缺失参数
        # 根据当前 slots 判断还缺哪些必填参数
        # ============================================================
        state.missing_params = self._get_missing_params(state.slots)

        # ============================================================
        # 2. 如果还缺参数，进入追问逻辑
        # ============================================================
        if state.missing_params:
            # ============================================================
            # 2.1 追问次数上限控制
            # 防止用户一直不补充参数导致无限追问
            # 最多追问 3 次，超过则放弃当前任务并清空会话状态
            # ============================================================
            if state.ask_count >= 3:
                # 清空当前会话状态，避免后续消息继续进入追问分支
                session_state.clear(user_id, session_id)

                yield {
                    "event": "custom",
                    "data": {
                        "type": "answer",
                        "content": "追问次数过多，我先不继续了。请问您还有其他问题吗？"
                    }
                }
                return  # 结束本次处理

            # ============================================================
            # 2.2 生成追问问题
            # 根据缺失参数类型生成相应的追问
            # 例如缺人名就问"请问您要查询哪位人员？"
            # ============================================================
            question = handler.generate_question(state)

            # ============================================================
            # 2.3 流式返回追问问题
            # 前端收到后应该展示追问并等待用户回复
            # ============================================================
            yield {
                "event": "custom",
                "data": {
                    "type": "ask",
                    "question": question,
                    "missing_params": state.missing_params,
                    "ask_count": state.ask_count
                }
            }
            return  # 追问后直接返回，等待用户补充参数

        # ============================================================
        # 3. 参数完整，准备调用工具
        # ============================================================
        query_type = state.slots.get("query_type")
        date_slots = state.slots.get("date", {})

        # 4. 根据 query_type 映射到 Java 侧车暴露的 MCP 工具
        java_tool_name = _JAVA_TOOL_MAP.get(query_type)
        if not java_tool_name:
            logger.warning(f"query_type={query_type} 暂不被 Java MCP 后端支持")
            yield {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": "该查询类型当前 MCP 后端暂不支持，请稍后再试。"
                }
            }
            state.done = True
            yield {"event": "custom", "data": {"type": "done"}}
            return

        tool = self.tools.get(java_tool_name)
        if not tool:
            yield {
                "event": "custom",
                "data": {"type": "error", "content": f"{java_tool_name} 工具未加载"}
            }
            return

        # 5. 非今天查询时，先询问用户是否查看今日数据
        # Java 后端目前只支持今日数据，避免直接返回今天数据造成误解
        if query_type in ("realtime", "enter", "leave", "count"):
            if not _is_today(date_slots) and not state.slots.get("_confirm_proceed"):
                state.slots["_pending_confirm"] = True
                question = (
                    "当前平台后端仅支持查询今日数据，"
                    f"是否为您展示今日{_query_type_label(query_type)}？"
                )
                state.last_question = question
                yield {
                    "event": "custom",
                    "data": {
                        "type": "ask",
                        "question": question,
                        "missing_params": [],
                        "ask_count": state.ask_count
                    }
                }
                return

        # 如果用户已确认，清理标记后继续调用工具
        if state.slots.get("_confirm_proceed"):
            del state.slots["_confirm_proceed"]

        # 6. 按 Java 工具签名组装参数
        # getTodayPersonnelAffairs 无参数；getTodayPersonnelFlow 需要 type 参数
        if query_type == "flow":
            # 默认查询当日流动趋势；如需按周/月可在 slots 里扩展 date.type
            tool_args = {"type": "day"}
        else:
            tool_args = {}

        # 7. 调用工具
        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")
        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            # 8. 提取并格式化结果
            raw_text = _extract_text(result)
            answer_text = _format_person_status_result(query_type, raw_text)

            # 9. 流式返回结果
            yield {
                "event": "custom",
                "data": {"type": "answer", "content": answer_text}
            }
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"调用人员态势工具失败: {e}\n{tb}")
            yield {
                "event": "custom",
                "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"}
            }

        # 10. 标记完成
        state.done = True
        yield {"event": "custom", "data": {"type": "done"}}