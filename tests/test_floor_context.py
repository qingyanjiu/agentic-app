"""「这层 / 当前层」→ 具体楼层（UE-FAIL-006）。"""
from agent.floor_context import (
    floor_label,
    parse_scene_floor,
    payload_floor,
    replace_floor_context,
)


def test_floor_label_normalizes_common_shapes():
    assert floor_label("3F") == "3"
    assert floor_label("3") == "3"
    assert floor_label("-1") == "B1"
    assert floor_label("B1") == "B1"
    assert floor_label(99) == "整栋楼"
    assert floor_label(11) == "整栋楼"
    assert floor_label(None) is None


def test_payload_floor_supports_common_keys():
    assert payload_floor({"floor": 3}) == "3"
    assert payload_floor({"currentFloor": "3F"}) == "3"
    assert payload_floor({"floorNum": -1}) == "B1"
    assert payload_floor({"current_floor": 7}) == "7"
    assert payload_floor({}) is None


def test_parse_scene_floor_handles_dict_and_json_text():
    assert parse_scene_floor({"floor": 7}) == "7"
    assert parse_scene_floor('{"floor": 7, "pois": []}') == "7"
    assert parse_scene_floor('{"data": {"currentFloor": 3}}') == "3"
    assert parse_scene_floor("not-json") is None
    assert parse_scene_floor("floor: 7") == "7"     # 宽松兜底


def test_replace_floor_context():
    assert replace_floor_context("这层楼安防数据", "7") == "7楼安防数据"
    assert replace_floor_context("当前层有多少设备", "3") == "3楼有多少设备"
    assert replace_floor_context("本层门禁", "B1") == "B1楼门禁"
    assert replace_floor_context("这层楼安防数据", "整栋楼") == "整栋楼安防数据"
    assert replace_floor_context("这层楼安防数据", None) == "这层楼安防数据"
