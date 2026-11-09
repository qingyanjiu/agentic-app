"""工具输出治理。

MCP 返回体经常很大（设备台账、告警列表），直接塞进上下文会撑爆窗口，
也会让模型在噪声里找不到重点。这里统一做三件事：

1. 超过行数/字节上限就截断；
2. 完整内容落盘，并把路径写进提示，模型需要细节时可以再读；
3. 截断方向可选：head（结构化列表，保留开头）或 tail（日志/命令输出，保留结尾）。

阈值和 pi 的 truncate 保持一致：2000 行 / 50KB。
"""
from __future__ import annotations

import os
import tempfile
import time

MAX_LINES = 2000
MAX_BYTES = 50 * 1024

_DEFAULT_SPILL_DIR = os.path.join(tempfile.gettempdir(), "agentic-loop-spill")


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _spill(text: str, label: str, spill_dir: str | None) -> str:
    directory = spill_dir or _DEFAULT_SPILL_DIR
    os.makedirs(directory, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    path = os.path.join(directory, f"{safe_label}-{int(time.time() * 1000)}.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def govern(
    text: str | None,
    *,
    label: str = "tool",
    mode: str = "head",
    spill_dir: str | None = None,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> str:
    """把工具输出压到可进上下文的体量。

    :param mode: head 保留开头（列表/结构化数据），tail 保留结尾（日志/命令输出）
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    total_lines = _count_lines(text)
    total_bytes = len(text.encode("utf-8"))

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return text

    lines = text.split("\n")
    if mode == "tail":
        kept = lines[-max_lines:]
    else:
        kept = lines[:max_lines]

    body = "\n".join(kept)
    # 行数没超但字节超了，按字节再截一次，避免多字节字符被切断
    if len(body.encode("utf-8")) > max_bytes:
        encoded = body.encode("utf-8")[:max_bytes]
        body = encoded.decode("utf-8", errors="ignore")

    path = _spill(text, label, spill_dir)
    direction = "前" if mode == "head" else "后"
    notice = (
        f"\n\n[输出已截断：原始 {total_lines} 行 / {total_bytes} 字节，"
        f"这里保留{direction} {_count_lines(body)} 行；完整内容已落盘：{path}]"
    )
    return body + notice
