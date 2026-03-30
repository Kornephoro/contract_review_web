"""MCP stdio 客户端：列出工具、执行工具调用。每次调用会启动子进程连接对应 MCP Server（演示可用，生产建议常驻代理）。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_DEFAULT = Path(__file__).resolve().parent.parent / "mcp_servers.json"

# 延迟加载 mcp：避免未安装 pywin32（Windows）时 import 即崩溃，并给出明确提示
_mcp_bundle: Optional[Tuple[Any, ...]] = None


def _ensure_mcp() -> Tuple[Any, ...]:
    global _mcp_bundle
    if _mcp_bundle is not None:
        return _mcp_bundle
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import CallToolResult, Tool
    except ImportError as e:
        msg = str(e).lower()
        hint = ""
        if "pywintypes" in msg or "win32" in msg or sys_platform_win():
            hint = " 在 Windows 上请先执行: pip install pywin32"
        raise RuntimeError(
            f"无法加载 MCP 依赖（{e}）。{hint} 然后重新安装: pip install -r requirements.txt"
        ) from e
    _mcp_bundle = (ClientSession, StdioServerParameters, stdio_client, CallToolResult, Tool)
    return _mcp_bundle


def sys_platform_win() -> bool:
    return os.name == "nt"


def load_mcp_config(path: Optional[Path] = None) -> Optional[dict]:
    p = path or CONFIG_DEFAULT
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _merge_env(extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    base = dict(os.environ)
    if extra:
        base.update({k: str(v) for k, v in extra.items()})
    return base


def _tool_to_openai(t: Any, openai_name: str) -> dict:
    schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": openai_name,
            "description": (t.description or "").strip() or f"MCP 工具：{t.name}",
            "parameters": schema,
        },
    }


def _format_result(result: Any) -> str:
    if result.isError:
        return "工具返回错误：" + _blocks_to_text(result.content)
    return _blocks_to_text(result.content)


def _blocks_to_text(blocks: Any) -> str:
    if not blocks:
        return ""
    parts: List[str] = []
    for b in blocks:
        t = getattr(b, "text", None)
        if t is not None:
            parts.append(t)
        else:
            parts.append(str(b))
    return "\n".join(parts).strip() or "(空结果)"


async def _list_tools_one(server: dict) -> List[Tuple[str, Any]]:
    ClientSession, StdioServerParameters, stdio_client, _CallToolResult, _Tool = _ensure_mcp()
    params = StdioServerParameters(
        command=server["command"],
        args=list(server.get("args") or []),
        env=_merge_env(server.get("env")),
    )
    out: List[Tuple[str, Any]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_tools()
            sname = server["name"]
            for t in res.tools:
                out.append((sname, t))
    return out


async def _call_tool_one(server: dict, tool_name: str, arguments: dict) -> str:
    ClientSession, StdioServerParameters, stdio_client, _CallToolResult, _Tool = _ensure_mcp()
    params = StdioServerParameters(
        command=server["command"],
        args=list(server.get("args") or []),
        env=_merge_env(server.get("env")),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return _format_result(result)


async def list_openai_tools_and_router(
    config: dict,
) -> Tuple[List[dict], Dict[str, Tuple[str, str, dict]]]:
    """
    返回 (OpenAI tools 列表, 路由表)。
    路由：openai_name -> (server_name, mcp_tool_name, server_config_dict)
    """
    _ensure_mcp()
    servers = [s for s in config.get("servers") or [] if s.get("name") and s.get("command")]
    if not servers:
        return [], {}

    tasks = [_list_tools_one(s) for s in servers]
    grouped = await asyncio.gather(*tasks)

    openai_tools: List[dict] = []
    router: Dict[str, Tuple[str, str, dict]] = {}
    servers_by_name = {s["name"]: s for s in servers}

    for server, pairs in zip(servers, grouped):
        sname = server["name"]
        for _, t in pairs:
            openai_name = f"{sname}__{t.name}"
            openai_tools.append(_tool_to_openai(t, openai_name))
            router[openai_name] = (sname, t.name, servers_by_name[sname])

    return openai_tools, router


def list_openai_tools_sync(config: dict) -> Tuple[List[dict], Dict[str, Tuple[str, str, dict]]]:
    return asyncio.run(list_openai_tools_and_router(config))


def call_tool_sync(router: Dict[str, Tuple[str, str, dict]], openai_name: str, arguments: dict) -> str:
    if openai_name not in router:
        return f"未知工具：{openai_name}"
    _sname, mcp_tool, server_cfg = router[openai_name]

    async def _run() -> str:
        return await _call_tool_one(server_cfg, mcp_tool, arguments or {})

    _ensure_mcp()
    return asyncio.run(_run())
