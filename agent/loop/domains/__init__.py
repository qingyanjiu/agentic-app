"""内置业务域装配。

新增业务域只需三步：
1. 在本目录加一个模块，导出 `build_domain(toolbox) -> DomainSpec`；
2. 如果要做 dry-run，再导出 `preview_plan(query) -> dict`；
3. 在下面的 _MODULES / PREVIEWERS 里登记。

查询类业务域（11 个）直接基于 `_generic.QueryDomain` 声明，只有映射表不同。
"""
from __future__ import annotations

from ..registry import DomainSpec
from ..runtime import McpToolbox
from . import (
    canteen_status,
    compositive_overview,
    device_query,
    device_status,
    emergency_fire,
    emergency_perimeter,
    energy_status,
    information_status,
    meeting_status,
    person_status,
    security_status,
    twins_inspection,
    vehicle_status,
)

# 13 个业务域（顺序即 system prompt 里的工具概览顺序）
_MODULES = (
    person_status,
    security_status,
    canteen_status,
    vehicle_status,
    information_status,
    energy_status,
    meeting_status,
    emergency_fire,
    emergency_perimeter,
    device_status,
    device_query,
    compositive_overview,
    twins_inspection,
)


def build_default_domains(toolbox: McpToolbox | None = None) -> list[DomainSpec]:
    box = toolbox or McpToolbox()
    return [module.build_domain(box) for module in _MODULES]


# dry-run 用的"不调 MCP、不调 LLM"的参数预览器
PREVIEWERS = {module.CONFIG.name if hasattr(module, "CONFIG") else module.__name__.rsplit(".", 1)[-1]: module.preview_plan for module in _MODULES}
