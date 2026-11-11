"""Loop Agent 命令行试跑入口。

用法：

    # 1) 只看工具声明（不连 MCP、不调模型）
    python -m agent.loop.cli --list-tools

    # 2) 只看参数解析结果（不连 MCP、不调模型）——验证"参数抽取下沉到工具"是否生效
    python -m agent.loop.cli --dry-run "3楼的门禁设备都列一下"
    python -m agent.loop.cli --dry-run "MH-001 这台设备什么情况"

    # 3) 真跑一轮（需要 LLM 与 Java sidecar 可达）
    LLM_API_KEY=sk-xxx python -m agent.loop.cli "现在园区有多少人"

环境变量：

    LLM_BASE_URL  默认 https://api.deepseek.com
    LLM_API_KEY   未设置时回退 OPENAI_API_KEY
    LLM_MODEL     默认 deepseek-chat
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dataclasses import asdict

from openai import AsyncOpenAI

from . import (
    ChatPipeline,
    Composer,
    Executor,
    LoopAgent,
    Router,
    Session,
    build_registry,
)
from .domains import PREVIEWERS


def _resolve_client() -> tuple[AsyncOpenAI, str]:
    base_url = os.getenv("LLM_BASE_URL") or "https://api.deepseek.com"
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or "deepseek-chat"
    if not api_key:
        raise SystemExit(
            "缺少模型凭证：请设置 LLM_API_KEY（或 OPENAI_API_KEY）"
        )
    return AsyncOpenAI(base_url=base_url, api_key=api_key), model


async def _main(args: argparse.Namespace) -> int:
    if args.list_tools:
        registry = build_registry()
        print("=== 业务域概览 ===")
        print(registry.describe())
        print("\n=== OpenAI tool 定义 ===")
        for tool in registry.tools:
            print(json.dumps(tool.to_openai(), ensure_ascii=False, indent=2))
        return 0

    if args.discover:
        from .discover import PILOT_SERVERS, discover_domains
        from .domains import build_default_domains
        from .runtime import McpToolbox

        print(f"试点 server：{', '.join(PILOT_SERVERS)}")
        box = McpToolbox()
        explicit = build_default_domains(box)
        known = {pair for domain in explicit for pair in domain.mcp_tools}
        try:
            domains = await discover_domains(box, known, PILOT_SERVERS)
        finally:
            try:
                await box.close()
            except BaseException:  # noqa: BLE001
                # mcp SDK 关会话时可能抛 BaseExceptionGroup，清理路径上忽略
                pass

        for domain in domains:
            print(f"\n[{domain.name}] 新增 {len(domain.tools)} 个")
            for tool in domain.tools:
                props = list(tool.parameters.get("properties") or {})
                print(f"  - {tool.name}  参数={props}")
        print(f"\n合计新增 {sum(len(d.tools) for d in domains)} 个工具")
        return 0

    if args.dry_run:
        if not args.prompt:
            raise SystemExit("--dry-run 需要提供一句用户问题")
        print(f"输入：{args.prompt}")
        for name, preview in PREVIEWERS.items():
            print(f"\n[{name}]")
            print(json.dumps(preview(args.prompt), ensure_ascii=False, indent=2))
        return 0

    if args.route:
        if not args.prompt:
            raise SystemExit("--route 需要提供一句用户问题")
        plan = await Router().route(args.prompt)
        print(json.dumps(asdict(plan) if plan else None, ensure_ascii=False, indent=2, default=str))
        print("\n（None 表示分类器给不出高置信结果，会交给 L2 loop）")
        return 0

    if args.chat:
        if not args.prompt:
            raise SystemExit("--chat 需要提供一句用户问题")
        client, model = _resolve_client()
        registry = build_registry()
        pipeline = ChatPipeline(
            router=Router(),
            executor=Executor(registry),
            composer=Composer(client, model),
            loop_agent=LoopAgent(
                client=client, model=model, registry=registry, max_steps=args.max_steps
            ),
        )
        async def on_delta(text: str) -> None:
            print(text, end="", flush=True)

        result = await pipeline.handle(args.prompt, Session(), on_delta=on_delta)
        print()
        print("\n=== 最终回答 ===")
        print(result.text)
        print("\n=== 链路摘要 ===")
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if not args.prompt:
        raise SystemExit("请提供一句用户问题，或加 --list-tools / --dry-run / --route / --chat")

    client, model = _resolve_client()
    registry = build_registry()

    streamed = {"any": False}

    def on_event(event) -> None:
        # 流式增量逐字输出，避免每块都打一行 JSON
        if event.type == "assistant_delta":
            text = event.data.get("text") or ""
            if text:
                streamed["any"] = True
                print(text, end="", flush=True)
            return
        if streamed["any"]:
            print()
            streamed["any"] = False
        payload = json.dumps(event.data, ensure_ascii=False)
        print(f"[{event.type}] {payload[:400]}")

    agent = LoopAgent(
        client=client,
        model=model,
        registry=registry,
        max_steps=args.max_steps,
        on_event=on_event,
    )
    result = await agent.run(args.prompt)

    print("\n=== 最终回答 ===")
    print(result.text)
    print("\n=== 运行摘要 ===")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.stop_reason == "answered" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loop Agent 试跑")
    parser.add_argument("prompt", nargs="?", help="用户问题")
    parser.add_argument("--list-tools", action="store_true", help="打印工具声明后退出")
    parser.add_argument("--dry-run", action="store_true", help="只做参数解析，不调 MCP/模型")
    parser.add_argument("--route", action="store_true", help="只打印路由决策（需要 BGE，不调 LLM/MCP）")
    parser.add_argument("--chat", action="store_true", help="走三级分流管线（L1 直连 + L2 兜底）")
    parser.add_argument("--discover", action="store_true", help="试跑 MCP 工具发现，打印筛出的工具")
    parser.add_argument("--max-steps", type=int, default=8, help="最大工具调用步数")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
