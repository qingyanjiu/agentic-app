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
