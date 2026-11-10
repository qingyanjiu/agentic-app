"""设备楼层/关键词过滤的单元测试（纯函数）。"""
from agent.loop.domains.device_query import (
    _row_matches_floor,
    _row_matches_keyword,
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
