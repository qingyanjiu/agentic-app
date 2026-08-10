import logging
from memory.session_state import session_state, IntentState
from agent.intent.handlers.person_status_handler import PersonStatusHandler

# 临时注释掉 MCP 加载
# from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# 临时 mock 工具
# 用于先跑通人员态势流程，后面再替换为真实 MCP 工具
# ============================================================
class MockPersonStatusTool:
    name = "query_person_status"

    async def ainvoke(self, params: dict) -> str:
        query_type = params.get("query_type")
        person_name = params.get("person_name")

        # 位置/轨迹：需要人名
        if query_type == "location":
            return f"{person_name} 当前在 A栋 3 楼会议室"
        elif query_type == "trace":
            return f"{person_name} 轨迹：A栋 → 食堂 → 停车场"

        # 人数统计/今日态势
        elif query_type == "count":
            return "园区当前共有 226 人"
        elif query_type == "realtime":
            return "实时人数 226 人"
        elif query_type == "enter":
            return "今日进入人数 226 人"
        elif query_type == "leave":
            return "今日离开人数 226 人"

        # 异常人员
        elif query_type == "abnormal":
            return "今日异常人员 0 人"

        # 人员流动趋势
        elif query_type == "flow":
            return (
                "人员流动趋势（单位：人）：\\n"
                "08:00  进入 15 人，离开 7 人\\n"
                "09:00  进入 27 人，离开 14 人\\n"
                "10:00  进入 21 人，离开 16 人\\n"
                "11:00  进入 38 人，离开 25 人\\n"
                "12:00  进入 29 人，离开 13 人\\n"
                "13:00  进入 46 人，离开 18 人\\n"
                "14:00  进入 34 人，离开 29 人"
            )

        # 人员结构分布
        elif query_type == "structure":
            return (
                "人员结构分布：\\n"
                "中通服和信科技：192 人\\n"
                "省公司：192 人\\n"
                "规划设计院：38 人\\n"
                "其他：38 人"
            )

        return "未知查询类型"

class PersonStatusPipeline:
    _tools = None
    
    def __init__(self, llm, tools):
        self.llm = llm
        self.tools = {t.name: t for t in tools}
    
    @classmethod
    async def create(cls, llm, user_id=None, session_id=None):
        if cls._tools is None:
            # 临时用 mock 工具
            cls._tools = [MockPersonStatusTool()]
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
        person_name = state.slots.get("person_name")
        date = state.slots.get("date", {})
        area = state.slots.get("area", "")
        
        # 4. 查找工具
        tool = self.tools.get("query_person_status")
        if not tool:
            yield {
                "event": "custom",
                "data": {"type": "error", "content": "人员态势工具未加载"}
            }
            return
        
        # 5. 调用工具
        try:
            result = await tool.ainvoke({
                "query_type": query_type,
                "person_name": person_name,
                "date_start": date.get("start_time"),
                "date_end": date.get("end_time"),
                "area": area
            })
            
            # 6. 流式返回结果
            yield {
                "event": "custom",
                "data": {"type": "answer", "content": result}
            }
        except Exception as e:
            logger.error(f"调用人员态势工具失败: {e}", exc_info=True)
            yield {
                "event": "custom",
                "data": {"type": "error", "content": f"查询失败: {str(e)}"}
            }
        
        # 7. 标记完成
        state.done = True
        yield {"event": "custom", "data": {"type": "done"}}