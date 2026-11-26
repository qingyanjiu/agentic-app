"""报告图表渲染：把结构化数据画成 PNG，供 Word 嵌入。

设计约定：
    - 本模块只消费 collector 已经整理好的结构化序列，不取数、不编数；
    - 数据为空/不完整时返回 None，由调用方跳过插图；
    - matplotlib 延迟导入：未安装或环境缺字体时不阻塞服务启动。
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 中文字体候选：优先宋体系（思源宋体/宋体），再退到黑体/雅黑
_FONT_CANDIDATES = [
    "Noto Serif CJK SC",
    "Noto Serif CJK TC",
    "Noto Serif CJK HK",
    "Noto Serif CJK JP",
    "Source Han Serif SC",
    "SimSun",
    "宋体",
    "Noto Sans CJK SC",
    "Microsoft YaHei",
    "SimHei",
    "WenQuanYi Micro Hei",
    "PingFang SC",
    "Arial Unicode MS",
]

# 多序列配色（与报告整体风格保持一致，克制、可区分）
_PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6"]

_plt = None


def _get_pyplot():
    """延迟加载 matplotlib，并配置中文字体。"""
    global _plt
    if _plt is not None:
        return _plt
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        # 允许通过环境变量指定字体文件（容器里没装字体包时兜底）
        font_path = os.environ.get("CHART_FONT_PATH")
        if font_path and os.path.exists(font_path):
            try:
                font_manager.fontManager.addfont(font_path)
                family = font_manager.FontProperties(fname=font_path).get_name()
                if family and family not in _FONT_CANDIDATES:
                    _FONT_CANDIDATES.insert(0, family)
            except Exception as exc:  # noqa: BLE001 - 字体只是增强，失败可继续
                logger.warning("加载自定义图表字体失败：%s", exc)

        plt.rcParams["font.sans-serif"] = _FONT_CANDIDATES + ["sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False
        _plt = plt
        return _plt
    except Exception as exc:  # noqa: BLE001 - 依赖缺失时跳过图表，不影响报告主体
        logger.warning("matplotlib 不可用，报告将跳过图表：%s", exc)
        return None


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _normalize(data: Any) -> Tuple[List[str], List[Dict[str, Any]]]:
    """把图表配置归一成 (x 轴标签, [{name, data}, ...])。

    支持两种写法：
        {"x": [...], "series": [{"name": "...", "data": [...]}]}
        {"x": [...], "name": "...", "data": [...]}   # 单序列简写
    """
    if not isinstance(data, dict):
        return [], []

    x = [str(v) for v in (data.get("x") or [])]
    if not x:
        return [], []

    raw_series = data.get("series")
    if not raw_series and data.get("data") is not None:
        raw_series = [{"name": data.get("name", "数值"), "data": data.get("data")}]

    series: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_series or []):
        if not isinstance(item, dict):
            continue
        values = item.get("data") or []
        if len(values) != len(x):
            logger.warning("折线图序列长度与 x 轴不一致，已跳过：%s", item.get("name"))
            continue
        ys: List[float] = []
        for value in values:
            number = _to_float(value)
            ys.append(float("nan") if number is None else number)
        if all(math.isnan(v) for v in ys):
            continue
        series.append(
            {
                "name": str(item.get("name") or "系列%d" % (idx + 1)),
                "data": ys,
            }
        )
    return x, series


def render_line_chart(
    data: Any,
    *,
    title: str,
    out_path: str,
    x_label: str = "",
    y_label: str = "",
    width: float = 6.4,
    height: float = 3.6,
) -> Optional[str]:
    """渲染折线图并保存为 PNG，成功返回文件路径，数据为空/失败返回 None。"""
    plt = _get_pyplot()
    if plt is None or not out_path:
        return None

    x, series = _normalize(data)
    if len(x) < 2 or not series:
        logger.info("折线图数据不足，跳过：%s", title)
        return None

    fig, ax = None, None
    try:
        fig, ax = plt.subplots(figsize=(width, height), dpi=160)
        for idx, item in enumerate(series):
            ax.plot(
                range(len(x)),
                item["data"],
                color=_PALETTE[idx % len(_PALETTE)],
                linewidth=1.6,
                marker="o" if len(x) <= 24 else None,
                markersize=3,
                label=item["name"],
            )

        ax.set_title(title, fontsize=11, pad=8)
        if x_label:
            ax.set_xlabel(x_label, fontsize=9)
        if y_label:
            ax.set_ylabel(y_label, fontsize=9)
        ax.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.tick_params(axis="both", labelsize=8)
        try:
            ax.ticklabel_format(axis="y", style="plain")
        except Exception:  # noqa: BLE001 - 某些后端不支持，忽略即可
            pass

        # x 轴标签过密时按步长抽样，保留首尾
        step = max(1, (len(x) + 9) // 10)
        ticks = list(range(0, len(x), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels(
            [x[i] for i in ticks],
            rotation=30 if len(x) > 8 else 0,
            ha="right" if len(x) > 8 else "center",
        )
        if len(series) > 1:
            ax.legend(fontsize=8, frameon=False)

        fig.tight_layout()
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fig.savefig(out_path, bbox_inches="tight")
        return out_path
    except Exception as exc:  # noqa: BLE001 - 单张图失败不影响整份报告
        logger.warning("折线图渲染失败 %s：%s", title, exc)
        return None
    finally:
        if fig is not None:
            plt.close(fig)
