"""模块边界守卫：让 loop 与 graph 不会再"长回去"。

背景：要保 loop、删 legacy，靠的不是删除当天的小心，而是**从现在起不许新增耦合**。
完整方案见 `docs/loop-graph-解耦方案.md`（P0）。

三条硬规则（现状 0 违规，新增一条就红）：

1. `agent/loop/**` 不许 import `graph` / `langchain*` / `langgraph`
   —— 新链路只依赖 `openai` + `mcp`，一旦它又摸到旧图，删 graph 时就删不动了；
2. `agent/loop/**` 不许 import `agent.intent.handlers`
   —— 那 13 个 Handler 是旧链路专用；loop 只允许用 `agent.intent.classifier` / `.slots`；
3. `graph/**` 不许 import `agent.loop`
   —— 旧图反向依赖新链路会让"删 loop"变成"删不了"（场景通道的 `scene_resolve` 就是先例）。

一条祖父基线（只许变少，不许变多）：

4. `app.py` 里对 `graph.*` / `langchain*` 的 import —— 这些将来随 legacy 一起消失，
   现在先冻结成白名单：**新增一条失败**，**清理一条也失败**（逼基线同步收缩）。

实现用 `ast` 而不是字符串匹配：注释、docstring 里出现 "graph" 不算违规。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# 扫描时跳过的目录：缓存与产物目录里不会有真实 import
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".tmp_stubs",
        "uploads",
        "exports",
        "reports",
    }
)

# 旧链路的三种"气味"：graph 包、langchain 家族、langgraph。
# langchain_core 与 langchain 是两个顶层包，必须分开列，否则前者漏判。
LEGACY_PREFIXES: tuple[str, ...] = ("graph", "langchain", "langchain_core", "langgraph")

# --------------------------------------------------------------------------- #
# 三条硬规则：(扫描目录, 禁止依赖, 违规时的解释)
# --------------------------------------------------------------------------- #
HARD_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "agent/loop",
        LEGACY_PREFIXES,
        "loop 不许依赖旧链路或旧框架：它只应依赖 openai + mcp",
    ),
    (
        "agent/loop",
        ("agent.intent.handlers",),
        "loop 只允许用 agent.intent.classifier / .slots，不许碰旧链路的 Handler",
    ),
    (
        "graph",
        ("agent.loop",),
        "旧图不许反向依赖 loop，否则删 loop 会连带删掉场景功能",
    ),
)

# --------------------------------------------------------------------------- #
# app.py 的祖父基线：2026-11-12 现状快照，只许变少
# --------------------------------------------------------------------------- #
APP_GRAPH_IMPORTS = frozenset(
    {
        # 知识库通道返回的是 langchain 消息，_safe_serialize 要用 BaseMessage：
        # 这一条删 legacy 之后仍然会保留
        "langchain_core.messages",
        # 各专用通道（保留）
        "graph.knowledge_qa_langgraph",
        "graph.gen_doc_pipeline",
        "graph.asr_pipeline",
        "graph.scene_control_langgraph",
        # 兜底链路：删 legacy 时一起消失
        "graph.reactive_pipeline",
        # 以下 13 张是 legacy 业务图，删 legacy 时整批消失
        "graph.person_status_langgraph",
        "graph.security_status_langgraph",
        "graph.canteen_status_langgraph",
        "graph.vehicle_status_langgraph",
        "graph.information_status_langgraph",
        "graph.energy_status_langgraph",
        "graph.meeting_status_langgraph",
        "graph.emergency_fire_langgraph",
        "graph.emergency_perimeter_langgraph",
        "graph.device_status_langgraph",
        "graph.compositive_overview_langgraph",
        "graph.device_query_langgraph",
        "graph.twins_inspection_langgraph",
    }
)


# --------------------------------------------------------------------------- #
# 扫描工具
# --------------------------------------------------------------------------- #
def _iter_py(rel_dir: str):
    root = REPO / rel_dir
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        if _SKIP_DIRS.intersection(path.parts):
            continue
        yield path


def imported_modules(path: Path) -> dict[str, int]:
    """返回 {被 import 的模块名: 首次出现的行号}。

    函数体内的延迟 import 也算（`app.py` 里 `from agent.loop.rollout import ...`
    就写在 handler 内部），否则最容易被绕过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.setdefault(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import（from .x import y）不跨模块边界
                continue
            if node.module:
                found.setdefault(node.module, node.lineno)
    return found


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    """按点切分做前缀匹配：`graphql_client` 不能被 `graph` 误伤。"""
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def scan(rel_dir: str, forbidden: tuple[str, ...]) -> list[str]:
    """扫出违规，返回 `文件:行 — import 模块` 形式的问题清单。"""
    problems: list[str] = []
    for path in _iter_py(rel_dir):
        for module, line in imported_modules(path).items():
            if _matches(module, forbidden):
                problems.append(f"{path.relative_to(REPO)}:{line} — import {module}")
    return sorted(problems)


def _app_py_legacy_imports() -> set[str]:
    return {
        module
        for module in imported_modules(REPO / "app.py")
        if _matches(module, LEGACY_PREFIXES)
    }


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel_dir,forbidden,why",
    HARD_RULES,
    ids=[f"{d}:{p[0]}" for d, p, _ in HARD_RULES],
)
def test_hard_boundary_rules(rel_dir: str, forbidden: tuple[str, ...], why: str) -> None:
    problems = scan(rel_dir, forbidden)
    assert not problems, (
        f"{why}\n"
        f"违规 {len(problems)} 处（详见 docs/loop-graph-解耦方案.md 第 5 节）：\n  "
        + "\n  ".join(problems)
    )


def test_app_py_legacy_imports_do_not_grow() -> None:
    """app.py 对旧链路的 import 只许变少，不许变多。"""
    added = sorted(_app_py_legacy_imports() - APP_GRAPH_IMPORTS)
    assert not added, (
        "app.py 新增了对旧链路的依赖，解耦会因此倒退：\n  "
        + "\n  ".join(added)
        + "\n新通道请写成 agent/channels/*，不要在 app.py 里直接 import graph。"
    )


def test_app_py_baseline_is_not_stale() -> None:
    """基线里的条目被清理后必须同步删掉，否则基线会掩盖真实状态。"""
    stale = sorted(APP_GRAPH_IMPORTS - _app_py_legacy_imports())
    assert not stale, (
        "这些 import 已经不存在了，请从本文件的 APP_GRAPH_IMPORTS 里删掉对应条目：\n  "
        + "\n  ".join(stale)
    )


def test_detector_catches_new_violation(tmp_path: Path) -> None:
    """守卫自身要有自测：新写的依赖必须被认出来（含函数内延迟 import）。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import os\n"
        "def lazy():\n"
        "    from graph.person_status_langgraph import build_person_status_graph\n"
        "    return build_person_status_graph\n",
        encoding="utf-8",
    )

    modules = imported_modules(sample)

    assert modules["graph.person_status_langgraph"] == 3   # 行号要对得上
    assert _matches("graph.person_status_langgraph", ("graph",)) is True
    assert _matches("graphql_client", ("graph",)) is False      # 前缀要按点切
    assert _matches("langchain_core.messages", ("langchain",)) is False
    assert _matches("langchain_core.messages", LEGACY_PREFIXES) is True
