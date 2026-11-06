# -*- coding: utf-8 -*-
"""
场景控制 LangGraph 测试（不连 sidecar、不连真实 LLM）。

覆盖：
  1. LLM 规划 → 选工具 → 带 X-Session-Id 调用 → 回答只出现"已下发"
  2. 缺必填参数 → ask 事件追问，不发动作
  3. llm=None → 规则兜底（楼层/视角/监控）
  4. 工具调用异常 → error + done，且不谎报成功
  5. 破坏性动作（防区/巡检任务）额外提示"会改变现场状态"
"""
from conftest import FakeLLM, answers_of, asks_of, events_of, run
from graph.scene_control_langgraph import build_scene_control_graph


# sidecar /mcp/scene 的工具定义（字段口径与 tools/list 返回一致）
ENV_WEATHER_ENUM = [
    "晴天", "多云", "阴天", "小雨", "中雨", "大雨", "暴雨",
    "雷阵雨", "雾", "小雪", "中雪", "大雪", "暴雪", "雨夹雪",
]

SCENE_TOOLS = [
    {
        "name": "scene_switch_floor",
        "description": "切换楼层，-1=B1，11/99=整栋",
        "inputSchema": {
            "type": "object",
            "properties": {
                "floor": {"type": "integer", "description": "楼层"},
                "focus": {"type": "boolean", "description": "是否聚焦"},
            },
            "required": ["floor"],
        },
    },
    {
        "name": "scene_reset_view",
        "description": "重置视角",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "scene_show_camera",
        "description": "显示指定摄像头画面",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "摄像头点位标识"},
                "floor": {"type": "integer", "description": "楼层"},
            },
            "required": ["tag"],
        },
    },
    {
        "name": "scene_set_area_state",
        "description": "防区布防/撤防（破坏性）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "state": {"type": "string", "enum": ["布防", "撤防"]},
            },
            "required": ["tag", "state"],
        },
    },
    {
        "name": "scene_set_env",
        "description": "设置场景环境（时间/天气）",
        "inputSchema": {
            "type": "object",
            "properties": {
                # 与 sidecar 实际契约一致：24 小时制时间字符串
                "env_time": {"type": "string", "description": "场景时间，格式 HH:MM:SS"},
                # 与 sidecar tools/list 一致：天气只接受这些枚举值
                "env_weather": {"type": "string", "enum": ENV_WEATHER_ENUM},
            },
        },
    },
    {
        "name": "get_scene_state",
        "description": "查询场景当前状态",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class FakeSceneClient:
    def __init__(self, action_result=None, action_error=None, state_delay=0.0):
        self.state_text = '{"floor": 1, "pois": []}'
        self.state_delay = state_delay
        self.action_result = action_result or (
            '已下发动作 scene_switch_floor（floor=3, focus=True）。\n'
            "注意：这只代表动作帧已送到场景页面，UE 不回执。\n"
            '回复用户时不要说"已完成"，只能说"已下发"。'
        )
        self.action_error = action_error
        self.calls = []
        self.list_tools_calls = 0

    async def list_tools(self):
        self.list_tools_calls += 1
        return SCENE_TOOLS

    async def get_scene_state(self, x_session_id=None):
        self.calls.append(("get_scene_state", {}, x_session_id))
        if self.state_delay:
            import asyncio
            await asyncio.sleep(self.state_delay)
        return self.state_text

    async def call_tool(self, name, arguments, x_session_id=None):
        self.calls.append((name, arguments, x_session_id))
        if self.action_error is not None:
            raise self.action_error
        return self.action_result


def _input(query, scene_session_id="scene-abc"):
    return {
        "slots": {},
        "missing_params": [],
        "ask_count": 0,
        "unrelated_count": 0,
        "last_question": "",
        "original_query": query,
        "answer": None,
        "error": None,
        "done": False,
        "events": [],
        "scene_session_id": scene_session_id,
    }


def _action_calls(client):
    """过滤掉规划前的 get_scene_state 快照调用"""
    return [c for c in client.calls if c[0] != "get_scene_state"]


def test_llm_plan_calls_tool_with_scene_session():
    client = FakeSceneClient()
    # 带 tag 的监控指令不走规则快通道，验证 LLM 规划链路
    llm = FakeLLM('{"tool": "scene_show_camera", "arguments": {"tag": "CY-HIK-JK-001"}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("看下 CY-HIK-JK-001 的监控")))

    # 动作帧带上了前端声明的场景会话
    assert _action_calls(client) == [
        ("scene_show_camera", {"tag": "CY-HIK-JK-001"}, "scene-abc")
    ]

    answer = answers_of(final)[0]
    assert "已下发" in answer
    # 内部提示不能透给用户
    assert "回复用户时" not in answer
    assert "已完成" not in answer
    assert len(events_of(final, "done")) == 1

    # 规划提示词只带工具清单；不再为规划预取场景快照
    prompt = llm.prompts[0]
    assert "scene_switch_floor" in prompt
    assert "scene_show_camera" in prompt
    assert "当前场景状态" not in prompt
    assert all(c[0] != "get_scene_state" for c in client.calls)


def test_llm_need_clarify_asks_without_action():
    client = FakeSceneClient()
    llm = FakeLLM('{"need_clarify": true, "question": "请告诉我摄像头点位的 tag"}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("看下监控")))

    assert asks_of(final)[0]["question"] == "请告诉我摄像头点位的 tag"
    assert _action_calls(client) == []          # 没下发任何动作
    assert events_of(final, "done") == []       # 追问轮不结束回合


def test_schema_required_args_enforced_even_if_llm_omits():
    """LLM 给了工具但漏了必填 tag：不能盲调，改追问"""
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_show_camera", "arguments": {"floor": 3}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("看下三楼的监控")))

    assert _action_calls(client) == []
    assert "点位/对象标识" in asks_of(final)[0]["question"]


def test_rule_fallback_switch_floor():
    client = FakeSceneClient()
    graph = build_scene_control_graph(client, llm=None)

    final = run(graph.ainvoke(_input("切换到3楼")))

    assert _action_calls(client) == [("scene_switch_floor", {"floor": 3, "focus": True}, "scene-abc")]
    assert "已下发" in answers_of(final)[0]


def test_rule_fallback_reset_view():
    client = FakeSceneClient(action_result="已下发动作 scene_reset_view（无参数）。")
    graph = build_scene_control_graph(client, llm=None)

    run(graph.ainvoke(_input("把视角复位")))

    assert _action_calls(client)[0][0] == "scene_reset_view"


# ------------------------------------------------------------
# 规则快通道：确定性指令不调 LLM、不读场景快照
# ------------------------------------------------------------
def test_fast_path_skips_llm():
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_reset_view", "arguments": {}}')
    graph = build_scene_control_graph(client, llm=llm)

    run(graph.ainvoke(_input("切到3楼")))

    assert llm.prompts == []                      # 没调 LLM
    assert all(c[0] != "get_scene_state" for c in client.calls)  # 没读快照
    assert _action_calls(client) == [
        ("scene_switch_floor", {"floor": 3, "focus": True}, "scene-abc")
    ]


def test_fast_path_disabled_for_compound_command():
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_switch_floor", "arguments": {"floor": 3}}')
    graph = build_scene_control_graph(client, llm=llm)

    run(graph.ainvoke(_input("切到3楼，再重置视角")))

    assert llm.prompts                       # 复合句仍交给 LLM
    assert all(c[0] != "get_scene_state" for c in client.calls)


def test_fast_path_disabled_on_followup():
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_switch_floor", "arguments": {"floor": 3}}')
    graph = build_scene_control_graph(client, llm=llm)

    data = _input("3楼")
    data["ask_count"] = 1
    data["last_question"] = "请告诉我楼层"
    run(graph.ainvoke(data))

    assert llm.prompts                       # 追问轮不能拿拼接串走规则
    assert all(c[0] != "get_scene_state" for c in client.calls)


def test_status_query_calls_get_scene_state_once():
    """只有“查状态”这一种意图才调用 get_scene_state，且只调一次。"""
    client = FakeSceneClient()
    graph = build_scene_control_graph(client, llm=None)

    run(graph.ainvoke(_input("现在场景在第几层")))

    state_calls = [c for c in client.calls if c[0] == "get_scene_state"]
    assert len(state_calls) == 1


def test_rule_fallback_missing_tag_asks():
    client = FakeSceneClient()
    graph = build_scene_control_graph(client, llm=None)

    final = run(graph.ainvoke(_input("看下监控")))

    assert _action_calls(client) == []
    assert "tag" in asks_of(final)[0]["question"]


def test_rule_fallback_unknown_query_asks():
    client = FakeSceneClient()
    graph = build_scene_control_graph(client, llm=None)

    final = run(graph.ainvoke(_input("帮我处理一下")))

    assert asks_of(final)  # 兜底也是追问，不能瞎选工具


def test_tool_error_reports_error_not_success():
    client = FakeSceneClient(action_error=RuntimeError("scene action timeout"))
    llm = FakeLLM('{"tool": "scene_reset_view", "arguments": {}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("重置视角")))

    assert answers_of(final) == []
    errors = events_of(final, "error")
    assert errors and "timeout" in errors[0]["content"]
    assert len(events_of(final, "done")) == 1


def test_destructive_tool_adds_state_change_warning():
    client = FakeSceneClient(action_result="已下发动作 scene_set_area_state（tag=A-01, state=撤防）。")
    llm = FakeLLM('{"tool": "scene_set_area_state", "arguments": {"tag": "A-01", "state": "撤防"}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("把A-01防区撤防")))

    answer = answers_of(final)[0]
    assert "已下发" in answer
    assert "会改变现场状态" in answer


def test_ask_count_limit_gives_up():
    client = FakeSceneClient()
    llm = FakeLLM('{"need_clarify": true, "question": "请补充点位 tag"}')
    graph = build_scene_control_graph(client, llm=llm)

    data = _input("看下监控")
    data["ask_count"] = 3
    final = run(graph.ainvoke(data))

    assert asks_of(final) == []
    assert "追问次数过多" in answers_of(final)[0]
    assert len(events_of(final, "done")) == 1


# ------------------------------------------------------------
# scene_set_env 枚举参数：sidecar 只认固定取值，不能直传“雨/雪/晴”
# ------------------------------------------------------------
def _env_args(client):
    calls = _action_calls(client)
    assert [c[0] for c in calls] == ["scene_set_env"], calls
    return calls[0][1]


def test_rule_fallback_weather_maps_to_enum_values():
    cases = (
        ("下雨", "小雨"),
        ("下雪", "小雪"),
        ("晴天", "晴天"),
        ("来场暴雨", "暴雨"),
        ("打雷下雨", "雷阵雨"),
        ("下点雨夹雪", "雨夹雪"),
        ("改成多云", "多云"),
    )
    for query, expected in cases:
        client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
        graph = build_scene_control_graph(client, llm=None)

        run(graph.ainvoke(_input(query)))

        args = _env_args(client)
        assert args["env_weather"] == expected, f"{query} 实际下发: {args}"
        assert args["env_weather"] in ENV_WEATHER_ENUM


def test_rule_fallback_env_time_values():
    cases = (
        ("改成傍晚", "18:00:00"),
        ("改成晚上", "20:00:00"),
        ("改成白天", "12:00:00"),
        ("改成下午一点", "13:00:00"),
        ("时间切到早上9点", "09:00:00"),
        ("时间切到12:00:00", "12:00:00"),
        ("时间切到下午一点半", "13:30:00"),
        ("时间切到晚上7点15分", "19:15:00"),
        ("时间切到晚上十二点", "00:00:00"),
    )
    for query, expected in cases:
        client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
        graph = build_scene_control_graph(client, llm=None)

        run(graph.ainvoke(_input(query)))

        assert _env_args(client)["env_time"] == expected, query


def test_llm_weather_argument_normalized_to_enum():
    """LLM 给出“雨”这类口语值，下发前要收敛成“小雨”"""
    client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
    llm = FakeLLM('{"tool": "scene_set_env", "arguments": {"env_weather": "雨"}}')
    graph = build_scene_control_graph(client, llm=llm)

    run(graph.ainvoke(_input("下雨吧，谢谢")))

    assert _env_args(client)["env_weather"] == "小雨"


def test_llm_unrecognized_weather_asks_instead_of_calling():
    """枚举外的值（冰雹）不能透传，改追问"""
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_set_env", "arguments": {"env_weather": "冰雹"}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("下冰雹，谢谢")))

    assert _action_calls(client) == []
    question = asks_of(final)[0]["question"]
    assert "小雨" in question and "冰雹" not in question


def test_llm_env_time_normalized_to_clock():
    cases = (
        ("改成晚上", "晚上", "20:00:00"),
        ("改成下午一点", "下午一点", "13:00:00"),
        ("改成中午", "中午", "12:00:00"),
        ("改成12:00:00", "12:00:00", "12:00:00"),
    )
    for query, llm_time, expected in cases:
        client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
        llm = FakeLLM(
            '{"tool": "scene_set_env", "arguments": {"env_time": "%s"}}' % llm_time
        )
        graph = build_scene_control_graph(client, llm=llm)

        # 加逗号让这句话成为复合句，禁用规则快通道，确保覆盖 LLM 归一化分支
        run(graph.ainvoke(_input(query + "，谢谢")))

        assert _env_args(client)["env_time"] == expected, query


def test_query_time_wins_over_llm_time():
    """用户原话是“下午一点”时，不能被 LLM 的“中午”覆盖"""
    client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
    llm = FakeLLM('{"tool": "scene_set_env", "arguments": {"env_time": "12:00:00"}}')
    graph = build_scene_control_graph(client, llm=llm)

    run(graph.ainvoke(_input("改成下午一点，谢谢")))

    assert _env_args(client)["env_time"] == "13:00:00"


def test_weather_only_does_not_add_env_time():
    client = FakeSceneClient(action_result="已下发动作 scene_set_env。")
    llm = FakeLLM('{"tool": "scene_set_env", "arguments": {"env_weather": "晴天"}}')
    graph = build_scene_control_graph(client, llm=llm)

    run(graph.ainvoke(_input("改成晴天，谢谢")))

    args = _env_args(client)
    assert args == {"env_weather": "晴天"}


def test_llm_invalid_time_asks_instead_of_calling():
    client = FakeSceneClient()
    llm = FakeLLM('{"tool": "scene_set_env", "arguments": {"env_time": "25点"}}')
    graph = build_scene_control_graph(client, llm=llm)

    final = run(graph.ainvoke(_input("改个时间")))

    assert _action_calls(client) == []
    question = asks_of(final)[0]["question"]
    assert "下午一点" in question
