"""OpenAI 兼容接口上的工具循环（适用于 DeepSeek）。"""

from __future__ import annotations

import json
from typing import Callable, List, Optional

from openai import OpenAI


def completion_with_tool_loop(
    client: OpenAI,
    model: str,
    messages: List[dict],
    tools: Optional[List[dict]],
    execute_tool: Callable[[str, dict], str],
    max_tool_rounds: int = 8,
    temperature: float = 0.2,
) -> str:
    """
    多轮工具调用直到模型不再请求工具，返回最终 assistant 文本内容。
    tools 为空时等价于单次补全。
    """
    use_tools = bool(tools)
    tool_rounds = 0

    while True:
        kwargs = {"model": model, "messages": messages, "temperature": temperature}
        if use_tools and tools:
            kwargs["tools"] = tools

        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return (msg.content or "").strip()

        if tool_rounds >= max_tool_rounds:
            return (
                (msg.content or "").strip()
                or "已达到工具调用次数上限，请缩小任务范围或关闭 MCP 后重试。"
            )

        messages.append(_assistant_to_dict(msg))
        for tc in msg.tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            out = execute_tool(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": out,
                }
            )
        tool_rounds += 1


def _assistant_to_dict(msg) -> dict:
    if not msg.tool_calls:
        return {"role": "assistant", "content": msg.content}

    serialized = []
    for tc in msg.tool_calls:
        serialized.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
        )
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": serialized,
    }
