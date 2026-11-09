"""工具参数校验与会话压缩。

两件事都不调模型，纯本地计算：

- validate_args：按工具声明的 JSON Schema 做轻量校验（必填、类型、多余字段）。
  不引第三方依赖，覆盖我们实际用到的 schema 子集（object + string/number/boolean/array）。
- compress_tool_messages：工具结果总量超预算时，从最早的长结果开始替换成占位，
  保留开头若干字符。目的是防止一次多轮工具把上下文撑爆，而不是做语义摘要。
"""
from __future__ import annotations

DEFAULT_CONTEXT_BUDGET = 60_000   # 字符数，约 20k token 量级
_COMPRESS_THRESHOLD = 2_000       # 单条超过这个长度才考虑压缩
_KEEP_HEAD = 300                  # 压缩后保留的开头字符数

_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _type_ok(expected: str, value: object) -> bool:
    py_type = _TYPE_MAP.get(expected)
    if py_type is None:
        return True
    if expected in ("number", "integer") and isinstance(value, bool):
        return False   # bool 是 int 的子类，单独排除
    return isinstance(value, py_type)


def validate_args(schema: dict | None, args: dict) -> list[str]:
    """返回错误列表；空列表表示通过。"""
    if not schema:
        return []
    errors: list[str] = []
    props: dict = schema.get("properties") or {}
    required: list = schema.get("required") or []

    for key in required:
        if key not in args or args[key] in (None, ""):
            errors.append(f"缺少必填参数 {key}")

    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                errors.append(f"不认识的参数 {key}")
            continue
        expected = spec.get("type")
        if expected and not _type_ok(expected, value):
            errors.append(f"参数 {key} 应为 {expected}")

    return errors


def compress_tool_messages(
    messages: list[dict],
    budget_chars: int = DEFAULT_CONTEXT_BUDGET,
) -> int:
    """工具结果总量超预算时压缩最早的长结果，返回被压缩的条数。

    只改 role == "tool" 的消息，且保留开头，避免模型完全失去线索。
    """
    tool_indexes = [
        i for i, m in enumerate(messages) if m.get("role") == "tool" and isinstance(m.get("content"), str)
    ]
    total = sum(len(messages[i]["content"]) for i in tool_indexes)
    if total <= budget_chars:
        return 0

    compressed = 0
    for i in tool_indexes:
        if total <= budget_chars:
            break
        content = messages[i]["content"]
        if len(content) <= _COMPRESS_THRESHOLD:
            continue
        head = content[:_KEEP_HEAD]
        placeholder = f"[工具结果已压缩：原始 {len(content)} 字符，保留开头如下]\n{head}…"
        total -= len(content) - len(placeholder)
        messages[i] = {**messages[i], "content": placeholder}
        compressed += 1
    return compressed
