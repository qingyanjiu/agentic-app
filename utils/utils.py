# 获取dify配置   
import json
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)

def get_config(config_file_path = 'global_config.json') -> dict:
    config = {}
    with open(config_file_path, 'r') as f:
        config = json.loads(f.read())
    return config


def serialize_messages(messages: list[BaseMessage]) -> list[dict]:
    result = []

    for msg in messages or []:
        if isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "ai"
        elif isinstance(msg, SystemMessage):
            role = "system"
        elif isinstance(msg, ToolMessage):
            role = "tool"
        else:
            role = "unknown"

        item = {
            "role": role,
            "content": msg.content,
        }

        # ToolMessage 可选保存 tool_call_id
        if isinstance(msg, ToolMessage):
            item["tool_call_id"] = getattr(msg, "tool_call_id", None)

        # AIMessage 可选保存 tool_calls
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = tool_calls

        result.append(item)

    return result

def deserialize_messages(saved_messages: list[dict]) -> list:
    result = []

    for msg in saved_messages or []:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "user":
            result.append(HumanMessage(content=content))

        elif role == "ai":
            # 如果你保存了 tool_calls，也恢复回去
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                result.append(AIMessage(content=content, tool_calls=tool_calls))
            else:
                result.append(AIMessage(content=content))

        elif role == "system":
            result.append(SystemMessage(content=content))

        elif role == "tool":
            result.append(
                ToolMessage(
                    content=content,
                    tool_call_id=msg.get("tool_call_id", "")
                )
            )

    return result