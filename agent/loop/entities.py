"""面向 UI 的结构化实体（先给设备用，后续人员/车辆/空间复用）。

## 为什么要有这一层

清单类回答现在是纯文本：`- 5F客梯厅｜编号 jk_zl_5f_0006｜位置 …｜状态 启用`。
前端要把它变成可点按钮，只能靠正则猜"这段文字是不是设备列表"——
模型措辞、列数、字段顺序每次都变，正则永远追不完（前端为此写了 500 行仍会漏）。

解法是把"这是什么东西"的判定搬回后端：**产出数据的工具最懂自己的字段**，
它在返回文本的同时再给一份结构化列表，经 `list` 事件直达前端，全程不经过模型。
文本仍然保留（要进 history 供下一轮追问），前端拿到 `list` 后以结构化数据为准。

## 协议

WebSocket `{"event": "custom", "data": {…}}`，`type` 为 `list`：

    {"type": "list", "kind": "device", "title": "14 台设备，楼层 5F",
     "items": [{"kind": "device", "id": "jk_zl_5f_0006", "label": "5F客梯厅",
                "attrs": {"code": "jk_zl_5f_0006", "type": "监控",
                          "space": "园区/科研办公楼/5F", "status": "启用"}}],
     "total": 268, "truncated": false, "note": ""}

- `kind`：业务类型，可扩展（`device` / `person` / `vehicle` / `space`…）。
  前端维护 `kind → 组件` 的映射，后端加类型不用改协议。
- `id`：点击后回传给后端的标识。设备就是点位编号（= 场景下发的 tag）。
- `attrs`：`kind` 私有的附加字段，前端不认识也可以忽略。
- `total`：过滤前的总量（例如园区共 268 台），用于"其余 N 台未展示"。
- `truncated`：`items` 是否被截断。

## 行内引用（第二阶段，暂未启用）

模型自由回答里提到某一台设备时，可写成 markdown 链接
`[5F客梯厅](entity:device/jk_zl_5f_0006)`，前端渲染器认 `entity:` scheme 即可
转成同一个组件。`Entity.to_link()` 就是这个写法。暂不启用是因为点不动无所谓，
而"一块列表整片漏掉"才是痛点；等 `list` 事件稳定后再打开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 业务类型：新增类型在这里登记，前端同步加一条渲染映射
KIND_DEVICE = "device"

# 行内引用 scheme（见模块注释「第二阶段」）
LINK_SCHEME = "entity:"


@dataclass(slots=True)
class Entity:
    """列表里的一项，对应前端一个可点按钮。"""

    kind: str
    id: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"kind": self.kind, "id": self.id, "label": self.label}
        if self.attrs:
            data["attrs"] = dict(self.attrs)
        return data

    def to_link(self) -> str:
        """行内引用写法 `[label](entity:kind/id)`（第二阶段协议）。"""
        label = self.label.replace("[", "\\[").replace("]", "\\]")
        return f"[{label}]({LINK_SCHEME}{self.kind}/{self.id})"


@dataclass(slots=True)
class EntityList:
    """一块结构化列表，对应前端一张卡片。"""

    kind: str
    title: str
    items: list[Entity] = field(default_factory=list)
    total: int | None = None
    truncated: bool = False
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.items)

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "kind": self.kind,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
        }
        if self.total is not None:
            data["total"] = self.total
        if self.truncated:
            data["truncated"] = True
        if self.note:
            data["note"] = self.note
        return data
