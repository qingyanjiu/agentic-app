"""设备楼层/关键词过滤的单元测试（纯函数）。"""
import asyncio
import json

from agent.loop.domains.device_query import (
    _name_fallback,
    _row_matches_floor,
    _row_matches_keyword,
    build_domain,
    clear_device_cache,
    normalize_floor,
)


def test_normalize_floor_chinese_and_digits():
    assert normalize_floor("3楼") == "3F"
    assert normalize_floor("三层") == "3F"
    assert normalize_floor("3F") == "3F"
    assert normalize_floor("11楼") == "11F"
    assert normalize_floor("11层") == "11F"


def test_normalize_floor_negative():
    """数据里负层是 -1F，不是 B1F。"""
    assert normalize_floor("负一层") == "-1F"
    assert normalize_floor("地下一层") == "-1F"
    assert normalize_floor("-1楼") == "-1F"
    assert normalize_floor("B1") == "-1F"
    assert normalize_floor("-1F") == "-1F"


def test_normalize_floor_empty_or_unknown():
    assert normalize_floor("") is None
    assert normalize_floor(None) is None
    assert normalize_floor("园区") is None


def test_floor_match_does_not_confuse_1f_with_11f():
    row = {"name": "11F-走廊MJ", "spaceName": None}
    assert _row_matches_floor(row, "11F") is True
    assert _row_matches_floor(row, "1F") is False


def test_floor_match_prefers_space_name():
    row = {"name": "某设备", "spaceName": "园区/科研办公楼/3F"}
    assert _row_matches_floor(row, "3F") is True
    assert _row_matches_floor(row, "4F") is False


def test_negative_floor_from_name_when_space_empty():
    """144/579 行 spaceName 为空，必须能靠 name 判断。"""
    row = {"name": "-1F-电梯厅MJ", "spaceName": None}
    assert _row_matches_floor(row, "-1F") is True


def test_keyword_matches_rawname_and_code():
    row = {"name": "3F-主楼梯MJ", "rawName": "3F-主楼梯MJ", "code": "DEFAULT-0170"}
    assert _row_matches_keyword(row, "主楼梯") is True
    assert _row_matches_keyword(row, "0170") is True
    assert _row_matches_keyword(row, "不存在") is False


def test_format_rows_header_is_user_facing():
    """清单会原样展示给用户，header 不能带"status 不是在线状态"这类给模型看的注解。"""
    from agent.loop.domains.device_query import _format_rows

    rows = [
        {
            "rawName": "3F-主楼梯MJ",
            "code": "MJ-001",
            "id": 7,
            "spaceName": "园区/科研办公楼/3F",
            "status": 1,
            "syncSource": "0",
        }
    ]
    text = _format_rows(rows, total=579, floor="3F", keyword="主楼梯")
    lines = text.splitlines()

    assert "status" not in lines[0]
    assert "1 台设备（园区共 579 台）" in lines[0]
    assert "楼层 3F" in lines[0]
    assert "关键词「主楼梯」" in lines[0]
    assert lines[1].startswith("- 3F-主楼梯MJ")
    assert "状态 启用" in lines[1]


# ------------------------------------------------------------
# 名称兜底：「查看 监控5F客梯厅」要能定位到这台设备
# ------------------------------------------------------------
def test_name_fallback_extracts_typed_device_name():
    assert _name_fallback("查看 监控5F客梯厅") == "5F客梯厅"
    assert _name_fallback("查看 监控 5F客梯厅") == "5F客梯厅"
    assert _name_fallback("看看门禁3F大厅") == "3F大厅"


def test_name_fallback_keeps_dashed_floor_name():
    assert _name_fallback("查看 监控5F-主楼梯MJ") == "5F-主楼梯MJ"


def test_name_fallback_ignores_generic_words_and_pure_queries():
    """「查看监控」「5楼有哪些监控」不能被当成设备名。"""
    assert _name_fallback("查看监控") is None
    assert _name_fallback("5楼有哪些监控") is None
    assert _name_fallback("园区有多少监控") is None
    assert _name_fallback("查看监控设备") is None


# ------------------------------------------------------------
# 设备清单缓存
# ------------------------------------------------------------
class _FakeToolbox:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def call(self, server, tool, args):
        self.calls += 1
        return json.dumps(self.rows, ensure_ascii=False)


def _camera(name, code):
    """一条模拟的设备行，故意带上缓存要裁掉的多余字段。"""
    return {
        "id": f"id-{code}", "name": name, "rawName": name, "code": code,
        "spaceName": "园区/科研办公楼/5F", "status": "1", "syncSource": "3",
        "price": "9999", "rawId": "raw", "manufacture": "hik",
    }


def _list_tool(box):
    domain = build_domain(box)
    return next(t for t in domain.tools if t.name == "device_query__list")


def test_device_list_caches_and_slims_rows(monkeypatch):
    import agent.loop.domains.device_query as dq

    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()

    box = _FakeToolbox([_camera("5F客梯厅", "jk_zl_5f_0006")])
    tool = _list_tool(box)

    first = asyncio.run(tool.execute({"query": "查看 监控5F客梯厅"}))
    asyncio.run(tool.execute({"query": "查看 监控5F客梯厅"}))

    assert box.calls == 1                      # 第二次命中缓存，没有再拉
    assert "5F客梯厅" in first.text
    assert "1 台设备" in first.text

    cached = dq._CACHE["list:3"][1][0]         # 只保留精简字段
    assert cached["code"] == "jk_zl_5f_0006"
    assert "price" not in cached and "manufacture" not in cached


def test_device_list_refetches_once_then_says_not_found(monkeypatch):
    import agent.loop.domains.device_query as dq

    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()

    box = _FakeToolbox([_camera("别处的摄像头", "jk_zl_5f_0001")])
    tool = _list_tool(box)

    out = asyncio.run(tool.execute({"query": "查看 监控5F客梯厅"}))

    assert box.calls == 2                      # 未命中 → 强制重拉一次
    assert "没有找到" in out.text and "5F客梯厅" in out.text


# ---------------------------------------------------------------- 方位词归一
def test_loosen_keyword_strips_orientation_suffix():
    import agent.loop.domains.device_query as dq

    assert dq._loosen_keyword("7F西侧") == "7F西"
    assert dq._loosen_keyword("7F西面") == "7F西"
    assert dq._loosen_keyword("东侧") == "东"
    assert dq._loosen_keyword("7F西通道") is None      # 没方位后缀，不用放宽
    assert dq._loosen_keyword("") is None


def test_direction_word_loosens_to_candidates(monkeypatch):
    """「打开7F西侧监控」：数据里叫「7F西通道西向东」，严格匹配 0 台，放宽后给候选。"""
    import agent.loop.domains.device_query as dq

    def camera_7f(name, code):
        return {
            "id": f"id-{code}", "name": name, "rawName": name, "code": code,
            "spaceName": "园区/科研办公楼/7F", "status": "1", "syncSource": "3",
        }

    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()

    box = _FakeToolbox([
        camera_7f("7F西通道西向东", "jk_zl_7f_0002"),
        camera_7f("7F西通道东向西", "jk_zl_7f_0004"),
        camera_7f("7F中通道西向东", "jk_zl_7f_0003"),
    ])
    tool = _list_tool(box)

    out = asyncio.run(tool.execute({"query": "打开7F西侧监控"}))

    assert "没有找到" not in out.text
    assert "2 台设备" in out.text                  # 只放宽到「7F西」，不带中通道
    assert "jk_zl_7f_0002" in out.text and "jk_zl_7f_0004" in out.text
    assert out.entities is not None
    assert [item.id for item in out.entities.items] == ["jk_zl_7f_0002", "jk_zl_7f_0004"]


# ---------------------------------------------------------------- 名称抽取清洗
def test_clean_candidate_strips_verbs_and_trailing_type_word():
    import agent.loop.domains.device_query as dq

    assert dq._clean_candidate("打开7F西通道西向东监控") == "7F西通道西向东"
    assert dq._clean_candidate("7F西通道西向东监控") == "7F西通道西向东"
    assert dq._clean_candidate("监控") is None
    assert dq._clean_candidate("") is None
    assert dq._clean_candidate("设备") is None


def test_name_fallback_ignores_previous_turn_text(monkeypatch):
    """追问轮拼出来的两句串：类型词后面的整句不能被当成设备名。"""
    import agent.loop.domains.device_query as dq

    merged = "打开7F西侧的监控 打开7F西通道西向东监控"
    assert dq._name_fallback(merged) == "7F西通道西向东"


def test_name_in_text_finds_full_name_with_spaces():
    """「打开 7F西通道西向东」：没有类型词、没有横线，靠台帐里的全名反查。"""
    import agent.loop.domains.device_query as dq

    rows = [
        {"name": "7F西通道西向东", "rawName": "7F西通道西向东", "code": "jk_zl_7f_0002"},
        {"name": "7F西通道", "rawName": "7F西通道", "code": "short"},
    ]
    assert dq._name_in_text("打开 7F西通道西向东", rows) == "7F西通道西向东"  # 取最长的
    assert dq._name_in_text("打开监控", rows) is None


def test_pure_floor_is_not_a_device_name():
    """「打开7F监控」抽出来的 7F 是范围，不是设备名。"""
    import agent.loop.domains.device_query as dq

    assert dq._name_fallback("打开7F监控") is None
    assert dq._name_fallback("打开7楼的监控") is None
    assert dq._name_fallback("打开负一层的监控") is None
    assert dq._name_fallback("打开7F西通道监控") == "7F西通道"


def test_question_words_are_not_device_names():
    """「道闸设备有哪些」曾把「设备有哪些」当设备名 → 0 命中 → 误报"没找到"。"""
    import agent.loop.domains.device_query as dq

    assert dq._name_fallback("道闸设备有哪些") is None
    assert dq._name_fallback("门禁设备有哪些") is None
    assert dq._name_fallback("监控有几个") is None
    # 真名字照旧
    assert dq._name_fallback("查看 监控5F客梯厅") == "5F客梯厅"


def test_gate_list_query_returns_both_devices(monkeypatch):
    import agent.loop.domains.device_query as dq

    def gate(name, code):
        return {
            "id": f"id-{code}", "name": name, "rawName": name, "code": code,
            "spaceName": "园区/南门", "status": "1", "syncSource": "1",
        }

    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()

    box = _FakeToolbox([
        gate("南门道闸进口", "dz_park_0001"),
        gate("南门道闸出口", "dz_park_0002"),
    ])
    tool = _list_tool(box)

    out = asyncio.run(tool.execute({"query": "道闸设备有哪些"}))

    assert "没有找到" not in out.text
    assert out.text.splitlines()[0] == "2 台设备："
    assert out.entities is not None
    # 前端靠 text.startsWith(title) 去重，这段必须成立
    assert out.text.strip().startswith(out.entities.to_dict()["title"])
    assert [i.id for i in out.entities.items] == ["dz_park_0001", "dz_park_0002"]


def test_place_word_filters_by_space_name(monkeypatch):
    """「大门口有哪些门禁」：地点词不是楼层，但能匹配 spaceName，别列出全部门禁。"""
    import agent.loop.domains.device_query as dq

    def row(name, code, space):
        return {"id": f"id-{code}", "name": name, "rawName": name, "code": code,
                "spaceName": space, "status": "1", "syncSource": "0"}

    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()

    box = _FakeToolbox([
        row("南门门禁1", "mj_nm_1", "园区/南门/大门口"),
        row("南门门禁2", "mj_nm_2", "园区/南门/大门口"),
        row("5F门禁1", "mj_5f_1", "园区/科研办公楼/5F"),
        row("5F门禁2", "mj_5f_2", "园区/科研办公楼/5F"),
    ])
    tool = _list_tool(box)

    out = asyncio.run(tool.execute({"query": "大门口有哪些门禁"}))

    assert "关键词「大门口」" in out.text.splitlines()[0]
    assert [i.id for i in out.entities.items] == ["mj_nm_1", "mj_nm_2"]


def test_keyword_matches_space_name():
    import agent.loop.domains.device_query as dq

    row = {"name": "南门门禁1", "rawName": "南门门禁1", "code": "mj_nm_1",
           "spaceName": "园区/南门/大门口"}
    assert dq._row_matches_keyword(row, "大门口") is True
    assert dq._row_matches_keyword(row, "南门") is True
    assert dq._row_matches_keyword(row, "5F") is False
