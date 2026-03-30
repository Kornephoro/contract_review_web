import json
import html
import docx
import PyPDF2
from pathlib import Path
from typing import List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

from legal_review.llm import completion_with_tool_loop
from legal_review.mcp_bridge import call_tool_sync, load_mcp_config
from legal_review.review_html import build_risk_deck_html
from legal_review.prompts import (
    CHAT_SYSTEM_PREFIX,
    REVIEW_SYSTEM_BASE,
    REVIEW_MCP_SUFFIX,
    RISK_FOLLOWUP_PREFIX,
    build_dynamic_review_system,
)

LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "api_settings.json"

def load_settings():
    if LOCAL_CONFIG_PATH.exists():
        try:
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings():
    s = {}
    for k in [
        "ai_provider_radio", "anthropic_api_key", "anthropic_model", 
        "openai_api_key", "openai_base_url", "openai_model",
        "ollama_base_url", "ollama_model"
    ]:
        if k in st.session_state:
            s[k] = st.session_state[k]
    try:
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass

MCP_CONFIG_PATH = Path(__file__).resolve().parent / "mcp_servers.json"

RISK_DECK_DIR = Path(__file__).resolve().parent / "legal_review" / "components" / "risk_deck"
DROPZONE_DIR = Path(__file__).resolve().parent / "legal_review" / "components" / "dropzone"

risk_deck_component = components.declare_component("risk_deck", path=str(RISK_DECK_DIR))
dropzone_component = components.declare_component("dropzone", path=str(DROPZONE_DIR))


@st.cache_resource
def cached_mcp_tools(path_str: str, mtime: float, enabled: bool) -> Tuple[list, dict]:
    if not enabled:
        return [], {}
    from legal_review.mcp_bridge import list_openai_tools_sync

    cfg = load_mcp_config(Path(path_str))
    if not cfg or not cfg.get("enabled"):
        return [], {}
    return list_openai_tools_sync(cfg)


THEME_MAP = {"跟随系统": "system", "浅色": "light", "深色": "dark"}


def _spans_overlap(a0, a1, b0, b1):
    return not (a1 <= b0 or b1 <= a0)


def _panel_palette(theme_key: str) -> dict:
    """合同面板固定对比色，避免继承 Streamlit 暗色主题导致浅字叠白底。"""
    if theme_key == "light":
        return {
            "panel_bg": "#f4f4f0",
            "panel_fg": "#1a1a1e",
            "border": "#c8c8c4",
            "muted": "#5c5c5c",
        }
    if theme_key == "dark":
        return {
            "panel_bg": "#2d333b",
            "panel_fg": "#eceff1",
            "border": "#5a6570",
            "muted": "#b0bec5",
        }
    # system: 由外层 CSS + class 控制
    return {
        "panel_bg": "#f4f4f0",
        "panel_fg": "#1a1a1e",
        "border": "#c8c8c4",
        "muted": "#5c5c5c",
    }


def _risk_level_styles(theme_key: str) -> dict:
    if theme_key == "dark":
        return {
            "高风险": ("rgba(239, 83, 80, 0.28)", "#ff8a80"),
            "中风险": ("rgba(255, 213, 79, 0.22)", "#ffe082"),
            "低风险": ("rgba(100, 181, 246, 0.22)", "#90caf9"),
        }
    return {
        "高风险": ("rgba(198, 40, 40, 0.18)", "#c62828"),
        "中风险": ("rgba(249, 168, 37, 0.22)", "#f9a825"),
        "低风险": ("rgba(21, 101, 192, 0.12)", "#1565c0"),
    }


def _highlight_border_for_risk(risk: dict, theme_key: str) -> Tuple[str, str]:
    """优先按四维 dimension 着色，否则按风险等级。"""
    dim = (risk.get("dimension") or "").strip()
    if theme_key == "dark":
        dm = {
            "法律合规": ("rgba(100, 181, 246, 0.22)", "#90caf9"),
            "风险防控": ("rgba(239, 83, 80, 0.28)", "#ff8a80"),
            "条款完善": ("rgba(255, 213, 79, 0.22)", "#ffe082"),
            "利益保护": ("rgba(102, 187, 106, 0.2)", "#a5d6a7"),
        }
    else:
        dm = {
            "法律合规": ("rgba(21, 101, 192, 0.16)", "#1565c0"),
            "风险防控": ("rgba(198, 40, 40, 0.18)", "#c62828"),
            "条款完善": ("rgba(249, 168, 37, 0.22)", "#f9a825"),
            "利益保护": ("rgba(46, 125, 50, 0.14)", "#2e7d32"),
        }
    if dim in dm:
        return dm[dim]
    level = risk.get("level", "低风险")
    return _risk_level_styles(theme_key).get(level, _risk_level_styles(theme_key)["低风险"])


def build_highlighted_contract_html(
    text: str, risks: list, theme_key: str, applied_risks: set = None
) -> Tuple[str, List[int]]:
    """
    在合同正文中为每条风险的 original 片段添加高亮 HTML。
    返回 (html, not_found_indices)。
    """
    if not risks:
        return "", []

    if not (text or "").strip():
        empty = (
            f'<div style="max-height:520px;overflow-y:auto;padding:12px 14px;border:1px solid {pal["border"]};'
            f'border-radius:8px;background:{pal["panel_bg"]};color:{pal["muted"]};">（合同正文为空，无法标注）</div>'
        )
        return empty, []

    from legal_review.text_matcher import find_best_text_span
    
    candidates = []
    for idx, risk in enumerate(risks):
        orig = (risk.get("original") or "").strip()
        if not orig:
            continue
            
        start_idx, end_idx = find_best_text_span(text, orig)
        if start_idx != -1 and end_idx != -1:
            candidates.append((start_idx, end_idx, idx, orig))

    # Sort candidates by exact position, break ties using shorter spans (less likely to envelop everything)
    candidates.sort(key=lambda x: (x[0], (x[1] - x[0])))

    chosen = []
    used_ranges = []
    for pos, end, idx, orig in candidates:
        if any(_spans_overlap(pos, end, u0, u1) for u0, u1 in used_ranges):
            continue
        chosen.append((pos, end, idx))
        used_ranges.append((pos, end))

    chosen.sort(key=lambda x: x[0])
    # The fuzzy locator never fails entirely; 'not_found' is always empty effectively.
    not_found = []

    parts = []
    last = 0
    for pos, end, idx in chosen:
        parts.append(html.escape(text[last:pos]))
        num = idx + 1
        
        if applied_risks and idx in applied_risks:
            bg = "rgba(76, 175, 80, 0.22)" if theme_key == "dark" else "rgba(76, 175, 80, 0.15)"
            border = "#4caf50" if theme_key == "dark" else "#388e3c"
            inner_text = risks[idx].get("suggestion", "")
            orig_txt = html.escape(text[pos:end])
            sug_txt = html.escape(inner_text)
            inner = f'<span style="color:#2e7d32;font-weight:600;">{sug_txt}</span>'
            parts.append(
                f'<span id="risk-anchor-{idx}" style="scroll-margin-top:88px;background:{bg};border-bottom:2px solid {border};'
                f'padding:2px 4px;border-radius:4px;color:inherit;" title="✅ 已应用修改，原文本为：{orig_txt}">{inner}'
                f'<sup style="font-size:0.75em;font-weight:700;margin-left:4px;color:{border};">已应用</sup></span>'
            )
        else:
            bg, border = _highlight_border_for_risk(risks[idx], theme_key)
            inner = html.escape(text[pos:end])
            parts.append(
                f'<span id="risk-anchor-{idx}" style="scroll-margin-top:88px;background:{bg};border-bottom:2px solid {border};'
                f'padding:0 1px;color:inherit;" title="风险点 {num}">{inner}'
                f'<sup style="font-size:0.75em;font-weight:700;margin-left:2px;color:{border};">{num}</sup></span>'
            )
        last = end
    parts.append(html.escape(text[last:]))

    pal = _panel_palette(theme_key)
    inner = "".join(parts)

    if theme_key == "system":
        wrapper = (
            "<style>"
            ".review-contract-sys { background:#f4f4f0; color:#1a1a1e; border:1px solid #c8c8c4; }"
            "@media (prefers-color-scheme: dark) {"
            ".review-contract-sys { background:#2d333b !important; color:#eceff1 !important; border-color:#5a6570 !important; }"
            "}"
            "</style>"
            f'<div class="review-contract-sys" style="max-height:520px;overflow-y:auto;padding:12px 14px;'
            f'border-radius:8px;white-space:pre-wrap;word-break:break-word;line-height:1.65;font-size:0.95rem;">'
            f"{inner}</div>"
        )
    else:
        wrapper = (
            f'<div style="max-height:520px;overflow-y:auto;padding:12px 14px;border:1px solid {pal["border"]};'
            f'border-radius:8px;background:{pal["panel_bg"]};color:{pal["panel_fg"]};white-space:pre-wrap;'
            f'word-break:break-word;line-height:1.65;font-size:0.95rem;">{inner}</div>'
        )
    return wrapper, not_found


def _legend_html(theme_key: str) -> str:
    if theme_key == "dark":
        return (
            '<div style="margin-bottom:10px;font-size:0.9rem;">'
            '<span style="margin-right:12px;"><span style="background:rgba(239,83,80,0.28);padding:2px 8px;'
            'border-bottom:2px solid #ff8a80;color:#eceff1;">高风险</span></span>'
            '<span style="margin-right:12px;"><span style="background:rgba(255,213,79,0.22);padding:2px 8px;'
            'border-bottom:2px solid #ffe082;color:#eceff1;">中风险</span></span>'
            '<span><span style="background:rgba(100,181,246,0.22);padding:2px 8px;'
            'border-bottom:2px solid #90caf9;color:#eceff1;">低风险</span></span>'
            "</div>"
        )
    if theme_key == "system":
        return (
            "<style>"
            ".legend-sys .x-h { background:rgba(198,40,40,0.18); border-bottom:2px solid #c62828; color:#1a1a1e; }"
            ".legend-sys .x-m { background:rgba(249,168,37,0.22); border-bottom:2px solid #f9a825; color:#1a1a1e; }"
            ".legend-sys .x-l { background:rgba(21,101,192,0.12); border-bottom:2px solid #1565c0; color:#1a1a1e; }"
            "@media (prefers-color-scheme: dark) {"
            ".legend-sys .x-h { background:rgba(239,83,80,0.28); border-color:#ff8a80; color:#eceff1; }"
            ".legend-sys .x-m { background:rgba(255,213,79,0.22); border-color:#ffe082; color:#eceff1; }"
            ".legend-sys .x-l { background:rgba(100,181,246,0.22); border-color:#90caf9; color:#eceff1; }"
            "}"
            "</style>"
            '<div class="legend-sys" style="margin-bottom:10px;font-size:0.9rem;">'
            '<span style="margin-right:12px;"><span class="x-h" style="padding:2px 8px;">高风险</span></span>'
            '<span style="margin-right:12px;"><span class="x-m" style="padding:2px 8px;">中风险</span></span>'
            '<span><span class="x-l" style="padding:2px 8px;">低风险</span></span>'
            "</div>"
        )
    return (
        '<div style="margin-bottom:10px;font-size:0.9rem;color:#333;">'
        '<span style="margin-right:12px;"><span style="background:rgba(198,40,40,0.18);padding:2px 8px;'
        'border-bottom:2px solid #c62828;">高风险</span></span>'
        '<span style="margin-right:12px;"><span style="background:rgba(249,168,37,0.22);padding:2px 8px;'
        'border-bottom:2px solid #f9a825;">中风险</span></span>'
        '<span><span style="background:rgba(21,101,192,0.12);padding:2px 8px;'
        'border-bottom:2px solid #1565c0;">低风险</span></span>'
        "</div>"
    )


def _legend_dimensions_html() -> str:
    return (
        '<div style="margin:8px 0 10px 0;font-size:0.82rem;color:#455a64;line-height:1.6;">'
        "<strong>四维审查（高亮优先按维度着色）：</strong>"
        '<span style="color:#1565c0;">法律合规</span> · '
        '<span style="color:#c62828;">风险防控</span> · '
        '<span style="color:#f57c00;">条款完善</span> · '
        '<span style="color:#2e7d32;">利益保护</span>'
        "</div>"
    )


def _get_llm_client_and_model() -> Tuple[OpenAI, str, str, bool]:
    """返回 (client, model_name, provider_name, use_tools) 基于系统配置"""
    provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
    
    if provider_choice == "Anthropic":
        api_key = st.session_state.get("anthropic_api_key", "")
        base_url = "https://api.anthropic.com/v1" 
        model_name = st.session_state.get("anthropic_model", "claude-3-opus-20240229")
        use_tools = True
    elif provider_choice == "Ollama (本地)":
        api_key = "ollama"
        base_url = st.session_state.get("ollama_base_url", "http://localhost:11434/v1")
        model_name = st.session_state.get("ollama_model", "qwen2.5:latest")
        use_tools = False
    else: # OpenAI 兼容
        api_key = st.session_state.get("openai_api_key", "")
        base_url = st.session_state.get("openai_base_url", "https://api.deepseek.com")
        model_name = st.session_state.get("openai_model", "deepseek-chat")
        use_tools = True

    client = OpenAI(api_key=api_key or "sk-dummy", base_url=base_url)
    return client, model_name, provider_choice, use_tools

def inject_page_theme_css(theme_choice: str) -> None:
    key = THEME_MAP.get(theme_choice, "system")
    if key == "light":
        st.markdown(
            """
            <style>
            .stApp { background-color: #ffffff !important; color: #262730 !important; }
            [data-testid="stHeader"] { background-color: #ffffff !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    elif key == "dark":
        st.markdown(
            """
            <style>
            .stApp { background-color: #0e1117 !important; color: #fafafa !important; }
            [data-testid="stHeader"] { background-color: #0e1117 !important; }
            div[data-testid="stSidebar"] { background-color: #161b22 !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <style>
            @media (prefers-color-scheme: dark) {
              .stApp { background-color: #0e1117 !important; color: #fafafa !important; }
              [data-testid="stHeader"] { background-color: #0e1117 !important; }
              div[data-testid="stSidebar"] { background-color: #161b22 !important; }
            }
            @media (prefers-color-scheme: light) {
              .stApp { background-color: #ffffff !important; color: #262730 !important; }
              [data-testid="stHeader"] { background-color: #ffffff !important; }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )


# 初始化持久化配置
if "settings_loaded" not in st.session_state:
    _init_cfg = load_settings()
    for _k, _v in _init_cfg.items():
        st.session_state[_k] = _v
    
    # 初始化缺少默认值的配置项
    st.session_state.setdefault("anthropic_model", "claude-3-sonnet-20240229")
    st.session_state.setdefault("openai_base_url", "https://api.deepseek.com")
    st.session_state.setdefault("openai_model", "deepseek-chat")
    st.session_state.setdefault("ollama_base_url", "http://localhost:11434/v1")
    st.session_state.setdefault("ollama_model", "qwen2.5:latest")
    
    st.session_state["settings_loaded"] = True

# 页面基础设置
st.set_page_config(page_title="智审法务 - AI 合同审查助手", layout="wide")

with st.sidebar:
    st.header("⚙️ 系统配置")
    st.radio(
        "界面主题",
        ["跟随系统", "浅色", "深色"],
        horizontal=True,
        key="ui_theme",
        help="浅色/深色为固定配色；跟随系统则使用系统明暗与合同面板联动。",
    )
    provider_choice = st.radio(
        "AI 提供商",
        ["Anthropic", "OpenAI", "Ollama (本地)"],
        captions=["Claude 系列模型", "GPT 系列及兼容接口 (DeepSeek 等)", "完全离线，保护数据安全"],
        key="ai_provider_radio",
        on_change=save_settings,
    )
    
    if provider_choice == "Anthropic":
        st.text_input("Anthropic API Key", type="password", key="anthropic_api_key", on_change=save_settings)
        st.text_input("模型", placeholder="例如：claude-3-7-sonnet-20250219", key="anthropic_model", on_change=save_settings)
    elif provider_choice == "OpenAI":
        st.text_input("OpenAI API Key", type="password", key="openai_api_key", on_change=save_settings)
        st.text_input("API Base URL", key="openai_base_url", on_change=save_settings)
        st.text_input("模型", key="openai_model", on_change=save_settings)
    else:
        st.text_input("Ollama 地址", key="ollama_base_url", on_change=save_settings)
        st.text_input("本地模型", placeholder="输入模型名称，如 qwen2.5:32b", key="ollama_model", on_change=save_settings)
        
    st.checkbox(
        "启用 MCP 工具（审查与对话可检索外部资料）",
        value=False,
        key="use_mcp",
        help="需在同目录配置 mcp_servers.json，并确保本机可启动对应 MCP Server（如 npx/uv/python）。",
    )
    st.caption("MCP：可将 `mcp_servers.example.json` 复制为 `mcp_servers.json` 后按需修改。")
    st.markdown("---")
    st.markdown("### 👨‍💻 面试演示说明")
    st.markdown(
        "本系统利用大语言模型（DeepSeek）结合**法律专家提示词**，支持合同审查、**上下文追问对话**，"
        "以及可选的 **MCP 工具**挂接外部法律数据库（需自行配置 `mcp_servers.json`）。"
    )

inject_page_theme_css(st.session_state.get("ui_theme", "跟随系统"))

# --- 主页面 ---
st.title("⚖️ 智审法务 - AI 合同审查 Demo")
st.markdown("基于大模型的自动化合同风险排查工具")
st.divider()

if "review_snapshot" not in st.session_state:
    st.session_state.review_snapshot = None
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "contract_text_for_chat" not in st.session_state:
    st.session_state.contract_text_for_chat = ""
if "risk_followup_chats" not in st.session_state:
    st.session_state.risk_followup_chats = {}
if "focus_risk_idx" not in st.session_state:
    st.session_state.focus_risk_idx = None
if "modified_contract_text" not in st.session_state:
    st.session_state.modified_contract_text = ""
if "applied_risks" not in st.session_state:
    st.session_state.applied_risks = set()
if "original_file_bytes" not in st.session_state:
    st.session_state.original_file_bytes = None
if "original_file_name" not in st.session_state:
    st.session_state.original_file_name = None

def extract_text(file):
    text = ""
    if file.name.endswith(".docx"):
        doc = docx.Document(file)
        text = "\n".join([para.text for para in doc.paragraphs])
    elif file.name.endswith(".pdf"):
        pdf_reader = PyPDF2.PdfReader(file)
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
    return text


def build_chat_system_prompt(contract: str, review_snap: Optional[dict]) -> str:
    body = CHAT_SYSTEM_PREFIX + "\n\n--- 合同正文（节选） ---\n" + (contract or "")[:12000]
    if review_snap:
        ct = review_snap.get("contract_type")
        if ct:
            body += f"\n\n--- 合同类型 ---\n{ct}"
        if review_snap.get("risks"):
            body += "\n\n--- 最近一次审查结果（JSON 节选） ---\n"
            body += json.dumps(review_snap["risks"], ensure_ascii=False)[:8000]
    return body


def build_risk_followup_system(contract: str, risk: dict, risk_idx: int) -> str:
    return (
        f"{RISK_FOLLOWUP_PREFIX}\n\n"
        f"=========================\n"
        f"【当前探讨的风险焦点】\n"
        f"风险等级：{risk.get('level', '未知')}\n"
        f"涉事维度：{risk.get('dimension', '未知')}\n"
        f"原文摘录：\n{risk.get('original', '无')}\n"
        f"系统最初指出的问题：\n{risk.get('issue', '无')}\n"
        f"系统初步的修改建议：\n{risk.get('suggestion', '无')}\n"
        f"=========================\n\n"
        f"注意：以上是目前双方正在讨论的核心风险点！请紧密围绕上述【原文摘录】和【指出的问题】来回答用户的提问。\n\n"
        f"以下附上合同部分正文作为背景参考：\n\n--- 合同正文（节选） ---\n"
        f"{(contract or '')[:10000]}"
    )


def resolve_mcp_bundle():
    use = bool(st.session_state.get("use_mcp"))
    if not use or not MCP_CONFIG_PATH.exists():
        return [], {}
    mtime = MCP_CONFIG_PATH.stat().st_mtime
    return cached_mcp_tools(str(MCP_CONFIG_PATH), mtime, True)

def _render_overview_panel(snap: dict) -> None:
    """在右上方渲染合同概览面板。"""
    ct = snap.get("contract_type") or "未识别"
    ov = snap.get("overview") or {}
    risks = snap.get("risks") or []
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid = sum(1 for r in risks if r.get("level") == "中风险")
    low = sum(1 for r in risks if r.get("level") == "低风险")

    # 合同类型 + 风险统计徽章
    st.markdown(
        f'<div style="padding:12px 16px;border-radius:10px;'
        f'background:linear-gradient(135deg,#e3f2fd 0%,#fff8e1 100%);'
        f'border:1px solid #90caf9;margin-bottom:12px;">'
        f'<div style="font-size:1.05rem;font-weight:700;color:#0d47a1;margin-bottom:8px;">'
        f'📑 {html.escape(ct)}</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;">'
        f'<span style="padding:3px 10px;border-radius:12px;font-size:0.82rem;font-weight:600;'
        f'background:#ffebee;color:#c62828;border:1px solid #ef9a9a;">🔴 高风险 {high}</span>'
        f'<span style="padding:3px 10px;border-radius:12px;font-size:0.82rem;font-weight:600;'
        f'background:#fff8e1;color:#f57f17;border:1px solid #ffe082;">🟡 中风险 {mid}</span>'
        f'<span style="padding:3px 10px;border-radius:12px;font-size:0.82rem;font-weight:600;'
        f'background:#e3f2fd;color:#1565c0;border:1px solid #90caf9;">🔵 低风险 {low}</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    if ov:
        # 关键信息网格
        info_items = [
            ("👥 参与方", "、".join(ov.get("parties") or []) or "未明确"),
            ("💰 合同金额", ov.get("amount") or "未明确"),
            ("📅 合同期限", ov.get("duration") or "未明确"),
            ("🗓️ 签署日期", ov.get("sign_date") or "未明确"),
            ("⚖️ 适用法律", ov.get("governing_law") or "未明确"),
        ]

        cols = st.columns(2)
        for i, (label, value) in enumerate(info_items):
            with cols[i % 2]:
                st.markdown(
                    f'<div style="padding:8px 12px;border-radius:8px;margin-bottom:8px;'
                    f'background:#f8f9fa;border:1px solid #e0e0e0;">'
                    f'<div style="font-size:0.75rem;color:#607d8b;font-weight:600;margin-bottom:2px;">{label}</div>'
                    f'<div style="font-size:0.88rem;color:#263238;line-height:1.45;">{html.escape(str(value))}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # 内容概览
        summary = (ov.get("summary") or "").strip()
        if summary:
            st.markdown(
                f'<div style="padding:10px 14px;border-radius:8px;margin-top:4px;'
                f'background:#f3f8ff;border-left:4px solid #1e88e5;">'
                f'<div style="font-size:0.75rem;color:#607d8b;font-weight:600;margin-bottom:4px;">📋 内容概览</div>'
                f'<div style="font-size:0.88rem;color:#37474f;line-height:1.6;">{html.escape(summary)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # 主要风险概括（取高风险或前3条）
    if risks:
        _lc_map = {"高风险": "#c62828", "中风险": "#e65100", "低风险": "#1565c0"}
        _notable = sorted(risks, key=lambda r: {"高风险": 0, "中风险": 1, "低风险": 2}.get(r.get("level", "低风险"), 2))[:3]
        _items_html = "".join(
            f'<li style="margin-bottom:5px;">'
            f'<span style="color:{_lc_map.get(r.get("level","低风险"),"#546e7a")};font-weight:700;">'
            f'[{html.escape(r.get("level",""))}]</span> '
            f'{html.escape((r.get("issue") or "")[:90] + ("…" if len(r.get("issue",""))>90 else ""))}'
            f'</li>'
            for r in _notable
        )
        st.markdown(
            f'<div style="padding:10px 14px;border-radius:8px;margin-top:8px;'
            f'background:#fff3e0;border-left:4px solid #ff6f00;">'
            f'<div style="font-size:0.75rem;color:#bf360c;font-weight:700;margin-bottom:6px;">⚠️ 主要风险概括</div>'
            f'<ul style="margin:0;padding-left:18px;font-size:0.85rem;color:#37474f;line-height:1.6;">'
            f'{_items_html}</ul></div>',
            unsafe_allow_html=True,
        )

def build_export_report_html(snap: dict) -> str:
    """生成可下载的 HTML 分析报告。"""
    import datetime as _dt
    ct = snap.get("contract_type") or "未识别"
    ov = snap.get("overview") or {}
    risks = snap.get("risks") or []
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid  = sum(1 for r in risks if r.get("level") == "中风险")
    low  = sum(1 for r in risks if r.get("level") == "低风险")
    now_str = _dt.datetime.now().strftime("%Y年%m月%d日 %H:%M")

    _lc = {"高风险": ("#c62828", "#ffebee"), "中风险": ("#e65100", "#fff8e1"), "低风险": ("#1565c0", "#e3f2fd")}
    _dc = {"法律合规": "#1565c0", "风险防控": "#c62828", "条款完善": "#f57c00", "利益保护": "#2e7d32"}

    # 概览信息行
    ov_rows = ""
    if ov:
        parties = "、".join(ov.get("parties") or []) or "未明确"
        for label, value in [
            ("参与方", parties),
            ("合同金额", ov.get("amount") or "未明确"),
            ("合同期限", ov.get("duration") or "未明确"),
            ("签署日期", ov.get("sign_date") or "未明确"),
            ("适用法律", ov.get("governing_law") or "未明确"),
        ]:
            ov_rows += f"<tr><td class='label'>{label}</td><td>{html.escape(str(value))}</td></tr>"

    # 主要风险
    notable = sorted(risks, key=lambda r: {"高风险":0,"中风险":1,"低风险":2}.get(r.get("level","低风险"),2))[:3]
    issues_li = "".join(
        f'<li><span style="color:{_lc.get(r.get("level","低风险"), ("#546e7a","#f5f5f5"))[0]};font-weight:700;">[{html.escape(r.get("level",""))}]</span> ' +
        html.escape((r.get("issue") or "")[:100] + ("…" if len(r.get("issue",""))>100 else "")) + "</li>"
        for r in notable
    )

    # 逐条风险
    risk_rows = ""
    for i, risk in enumerate(risks):
        level = risk.get("level", "低风险")
        dim   = risk.get("dimension", "")
        lc, lb = _lc.get(level, ("#546e7a", "#f5f5f5"))
        dc = _dc.get(dim, "#546e7a")
        sugg = risk.get("suggestion") or ""
        lb_  = risk.get("legal_basis") or ""
        sugg_html = f'<div class="rs suggestion"><strong>修改建议：</strong>{html.escape(sugg)}</div>' if sugg else ""
        lb_html   = f'<div class="rs legal"><strong>法律依据：</strong>{html.escape(lb_)}</div>' if lb_ and lb_ != "暂无明确法条依据" else ""
        risk_rows += (
            f'<div class="ri" style="border-left:4px solid {lc};background:{lb};">' +
            f'<div class="rh"><span class="rn" style="color:{lc};">风险点 {i+1} · {html.escape(level)}</span>' +
            f'<span class="db" style="color:{dc};border-color:{dc};">{html.escape(dim)}</span></div>' +
            f'<div class="rs"><strong>原文摘录：</strong><span class="orig">{html.escape(risk.get("original",""))}</span></div>' +
            f'<div class="rs"><strong>风险说明：</strong>{html.escape(risk.get("issue",""))}</div>' +
            sugg_html + lb_html +
            f'</div>'
        )

    ov_section = ""
    if ov:
        summary_html = ""
        if ov.get("summary"):
            summary_html = f'<div class="summary-box" style="margin-top:10px;"><strong>内容概览：</strong>{html.escape(ov["summary"])}</div>'
        ov_section = f'<div class="section"><h2>📋 合同概览</h2><table>{ov_rows}</table>{summary_html}</div>'

    issues_section = f'<div class="section"><h2>⚠️ 主要风险概括</h2><div class="alert"><ul>{issues_li}</ul></div></div>' if issues_li else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>合同审查报告 - {html.escape(ct)}</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:24px;background:#f5f7fa;color:#263238;}}
.wrap{{max-width:900px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);overflow:hidden;}}
.hdr{{background:linear-gradient(135deg,#1a237e,#0d47a1);color:#fff;padding:28px 32px;}}
.hdr h1{{margin:0 0 6px 0;font-size:1.5rem;}}
.hdr .ct{{opacity:.9;margin:4px 0;}}
.hdr .meta{{font-size:.85rem;opacity:.75;}}
.badges{{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;}}
.badge{{padding:3px 12px;border-radius:12px;font-size:.82rem;font-weight:600;}}
.bh{{background:#ffebee;color:#c62828;border:1px solid #ef9a9a;}}
.bm{{background:#fff8e1;color:#e65100;border:1px solid #ffe082;}}
.bl{{background:#e3f2fd;color:#1565c0;border:1px solid #90caf9;}}
.section{{padding:18px 32px;border-bottom:1px solid #eceff1;}}
.section h2{{font-size:1.05rem;color:#1a237e;margin:0 0 12px 0;padding-bottom:6px;border-bottom:2px solid #e3f2fd;}}
table{{width:100%;border-collapse:collapse;}}
td{{padding:7px 10px;font-size:.88rem;border-bottom:1px solid #eceff1;vertical-align:top;}}
td.label{{color:#607d8b;font-weight:600;width:90px;white-space:nowrap;}}
.summary-box{{padding:8px 12px;border-radius:6px;background:#f3f8ff;border-left:3px solid #1e88e5;font-size:.87rem;line-height:1.6;}}
.alert{{background:#fff3e0;border-left:4px solid #ff6f00;padding:10px 14px;border-radius:6px;}}
.alert ul{{margin:0;padding-left:18px;}}
.alert li{{margin-bottom:4px;font-size:.87rem;line-height:1.5;}}
.ri{{padding:12px 14px;border-radius:8px;margin-bottom:10px;}}
.rh{{display:flex;align-items:center;gap:8px;margin-bottom:7px;flex-wrap:wrap;}}
.rn{{font-weight:700;font-size:.93rem;}}
.db{{font-size:.75rem;padding:2px 8px;border-radius:5px;border:1px solid;}}
.rs{{font-size:.86rem;margin-bottom:5px;line-height:1.55;color:#37474f;}}
.orig{{font-style:italic;color:#546e7a;background:rgba(0,0,0,.04);padding:2px 5px;border-radius:3px;}}
.suggestion{{padding:7px 10px;background:rgba(255,255,255,.7);border-radius:5px;}}
.legal{{font-size:.8rem;color:#607d8b;}}
.foot{{text-align:center;padding:14px;font-size:.78rem;color:#90a4ae;background:#f5f7fa;}}
</style></head>
<body><div class="wrap">
<div class="hdr">
  <h1>⚖️ 合同审查分析报告</h1>
  <div class="ct">📑 {html.escape(ct)}</div>
  <div class="meta">生成时间：{now_str} · 由智审法务 AI 系统自动生成</div>
  <div class="badges">
    <span class="badge bh">🔴 高风险 {high}</span>
    <span class="badge bm">🟡 中风险 {mid}</span>
    <span class="badge bl">🔵 低风险 {low}</span>
  </div>
</div>
{ov_section}
{issues_section}
<div class="section"><h2>🔍 逐条风险分析（共 {len(risks)} 条）</h2>{risk_rows}</div>
<div class="foot">本报告由 AI 自动生成，仅供参考，不构成正式法律意见。如需专业法律建议，请咨询执业律师。</div>
</div></body></html>"""


tab_review, tab_chat = st.tabs(["合同审查", "上下文对话"])

with tab_review:
    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.subheader("1. 上传或输入合同")
        uploaded_file = st.file_uploader("拖拽文件到此或点击上传 (支持 .docx, .pdf)", type=["docx", "pdf"])
        st.markdown("或者：")
        contract_text = st.text_area(
            "直接粘贴合同文本：",
            height=200,
            placeholder="在此输入需要审查的合同内容...",
            key="contract_input",
        )
        
        st.markdown("---")
        st.radio(
            "审校深度",
            ["快速审查", "标准审查", "深度审查"],
            captions=["约 30s, 关注...", "约 1-2min, 四..", "约 3-5min, 逐.."],
            horizontal=True,
            key="review_depth",
            index=1,
        )
        st.radio(
            "审校立场",
            ["中立视角", "委托方视角", "相对方视角"],
            horizontal=True,
            key="review_perspective",
            index=0,
        )
        st.markdown("<br>", unsafe_allow_html=True)
        analyze_button = st.button("🚀 开始审查", type="primary", use_container_width=True)

    with col2:
        st.subheader("2. AI 审查报告")

        if not analyze_button:
            snap_preview = st.session_state.get("review_snapshot")
            if snap_preview and snap_preview.get("contract_type"):
                _render_overview_panel(snap_preview)
                if snap_preview.get("risks"):
                    import datetime as _dt
                    _fname = f"合同审查报告_{_dt.datetime.now().strftime('%Y%m%d_%H%M')}.html"
                    st.download_button(
                        "📥 导出分析报告 (HTML)",
                        data=build_export_report_html(snap_preview).encode("utf-8"),
                        file_name=_fname,
                        mime="text/html",
                        use_container_width=True,
                        key="export_btn_top",
                    )
            else:
                st.info("👈 请配置 API Key，然后在左侧输入合同内容并点击“开始审查”。")

        if analyze_button:
            provider_choice = st.session_state.get("ai_provider", "OpenAI")
            depth_choice = st.session_state.get("review_depth", "标准审查")
            persp_choice = st.session_state.get("review_perspective", "中立视角")
            
            if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
                st.error("请先在左侧边栏输入 OpenAI API Key！")
            elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
                st.error("请先在左侧边栏输入 Anthropic API Key！")
            elif not uploaded_file and not contract_text.strip():
                st.warning("请先提供需要审查的合同内容！")
            else:
                final_text = ""
                if uploaded_file:
                    final_text = extract_text(uploaded_file)
                    uploaded_file.seek(0)
                    st.session_state.original_file_bytes = uploaded_file.read()
                    st.session_state.original_file_name = uploaded_file.name
                else:
                    final_text = contract_text
                    st.session_state.original_file_bytes = None
                    st.session_state.original_file_name = None

                st.session_state.contract_text_for_chat = final_text
                st.session_state.modified_contract_text = final_text
                st.session_state.applied_risks = set()

                with st.spinner("AI 正在逐条比对审查中，请稍候..."):
                    try:
                        client, model_name, provider_choice, use_tools = _get_llm_client_and_model()
                        if provider_choice == "Anthropic":
                            st.warning("注：Anthropic需确保URL指向兼容网关(如LiteLLM)。")

                        tools, router = resolve_mcp_bundle()
                        if not use_tools:
                            tools = []
                            
                        system_prompt = build_dynamic_review_system(depth_choice, persp_choice)
                        if tools:
                            system_prompt += REVIEW_MCP_SUFFIX

                        messages = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"请审查以下合同文本：\n\n{final_text[:4000]}"},
                        ]

                        def exec_tool(name: str, args: dict) -> str:
                            return call_tool_sync(router, name, args)

                        result_content = completion_with_tool_loop(
                            client,
                            model_name,
                            messages,
                            tools if tools else None,
                            exec_tool,
                            max_tool_rounds=2 if depth_choice == "快速审查" else 6,
                            temperature=0.4 if depth_choice == "快速审查" else 0.1,
                        )

                        if result_content.startswith("```json"):
                            result_content = result_content[7:-3].strip()
                        elif result_content.startswith("```"):
                            result_content = result_content[3:-3].strip()

                        parsed = json.loads(result_content)
                        if isinstance(parsed, dict) and "risks" in parsed:
                            contract_type = parsed.get("contract_type") or "未识别"
                            overview = parsed.get("overview") or {}
                            risks = parsed.get("risks") or []
                            if not isinstance(risks, list):
                                risks = []
                        elif isinstance(parsed, list):
                            contract_type = "未分类"
                            overview = {}
                            risks = parsed
                        else:
                            raise ValueError("模型返回既不是对象也不是数组")

                        if not risks:
                            st.session_state.review_snapshot = {
                                "text": final_text,
                                "contract_type": contract_type,
                                "overview": overview,
                                "risks": [],
                            }
                            st.session_state.risk_followup_chats = {}
                            st.session_state.focus_risk_idx = None
                            st.success("✅ 审查完成！未发现明显法律风险。")
                        else:
                            st.session_state.review_snapshot = {
                                "text": final_text,
                                "contract_type": contract_type,
                                "overview": overview,
                                "risks": risks,
                            }
                            st.session_state.risk_followup_chats = {}
                            st.session_state.focus_risk_idx = None
                            st.success(f"✅ 审查完成！共发现 **{len(risks)}** 处风险，详见下方概览与三栏。")
                        
                        st.rerun()

                    except Exception as e:
                        st.error(f"调用 AI 服务时出错，请检查 API Key、Base URL 或网络连接：{str(e)}")

    snap = st.session_state.review_snapshot
    if snap and snap.get("risks"):
        theme_choice = st.session_state.get("ui_theme", "跟随系统")
        theme_key = THEME_MAP.get(theme_choice, "system")

        st.divider()
        st.subheader("📌 合同标注与风险详情")
        st.caption("三栏：左侧合同高亮；中间风险卡片（可按类型筛选/排序）；右侧拖入卡片追问 AI。")

        applied = st.session_state.get("applied_risks", set())
        hl_html, not_found = build_highlighted_contract_html(snap["text"], snap["risks"], theme_key, applied)

        c1, c2, c3 = st.columns([1.12, 1.05, 0.95], gap="medium")

        with c1:
            st.markdown("**标注合同（原文）**")
            st.markdown(_legend_html(theme_key), unsafe_allow_html=True)
            st.markdown(_legend_dimensions_html(), unsafe_allow_html=True)
            st.markdown(hl_html, unsafe_allow_html=True)

        with c2:
            st.markdown(
                '<div style="display:flex; justify-content:space-between; margin-bottom: 8px;">', unsafe_allow_html=True
            )
            c2_btn1, c2_btn2 = st.columns(2)
            with c2_btn1:
                if st.button("🚀 一键应用所有推荐", use_container_width=True):
                    st.session_state.applied_risks.update(range(len(snap["risks"])))
                    st.rerun()
            with c2_btn2:
                if st.button("💾 导出修改后的文件", use_container_width=True, type="primary"):
                    st.session_state["show_export_dialog"] = True
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 导出模态框逻辑
            if st.session_state.get("show_export_dialog"):
                st.info("导出功能正在初始化...")
                from legal_review.document_editor import execute_export_pipeline
                execute_export_pipeline()
            
            tab_risk, tab_clause, tab_party = st.tabs(["⚡ 风险焦点", "📜 条款修订", "👥 履约方影响"])
            
            with tab_risk:
                _fc, _sc = st.columns([1, 1.6])
                with _fc:
                    _avail_dims = [d for d in ["法律合规", "风险防控", "条款完善", "利益保护"]
                                   if any(r.get("dimension") == d for r in snap["risks"])]
                    dim_filter = st.selectbox(
                        "按风险类型筛选",
                        ["全部"] + _avail_dims,
                        key="dim_filter",
                        label_visibility="collapsed",
                    )
                with _sc:
                    sort_order = st.radio(
                        "风险点排序",
                        ["原文顺序", "风险高→低", "风险低→高"],
                        horizontal=True,
                        key="risk_sort_order",
                        label_visibility="collapsed",
                    )
                level_rank = {"高风险": 0, "中风险": 1, "低风险": 2}
                risks_with_idx = [(i, r) for i, r in enumerate(snap["risks"])]
                if dim_filter != "全部":
                    risks_with_idx = [(i, r) for i, r in risks_with_idx if r.get("dimension") == dim_filter]
                if sort_order == "风险高→低":
                    risks_with_idx = sorted(risks_with_idx, key=lambda x: level_rank.get(x[1].get("level", "低风险"), 2))
                elif sort_order == "风险低→高":
                    risks_with_idx = sorted(risks_with_idx, key=lambda x: -level_rank.get(x[1].get("level", "低风险"), 2))
                deck_html = build_risk_deck_html(risks_with_idx, theme_key, st.session_state.get("applied_risks", set()))
                focus_from_js_deck = risk_deck_component(cards_html=deck_html, key="risk_deck_v1")
                
                if focus_from_js_deck and isinstance(focus_from_js_deck, dict):
                    new_idx = int(focus_from_js_deck.get("idx", -1))
                    new_ts = focus_from_js_deck.get("ts", 0)
                    action = focus_from_js_deck.get("action", "focus")
                    
                    if new_idx >= 0 and new_ts != st.session_state.get("last_deck_ts"):
                        st.session_state["last_deck_ts"] = new_ts
                        if new_idx < len(risks_with_idx):
                            actual_idx = risks_with_idx[new_idx][0]
                        else:
                            actual_idx = new_idx
                            
                        if action == "apply":
                            # Toggle apply status
                            applied = st.session_state.get("applied_risks", set())
                            if actual_idx in applied:
                                applied.remove(actual_idx)
                            else:
                                applied.add(actual_idx)
                            st.session_state.applied_risks = applied
                            st.rerun()
                        else:
                            st.session_state.focus_risk_idx = actual_idx
                            st.rerun()

            with tab_clause:
                from legal_review.perspectives import render_clause_centric_view
                render_clause_centric_view(snap["risks"], theme_key)
                
            with tab_party:
                from legal_review.perspectives import render_party_centric_view
                render_party_centric_view(snap["overview"], snap["risks"], theme_key)

        contract_ctx = snap.get("text") or ""

        with c3:
            st.markdown("**追问 AI**")
            focus_idx = st.session_state.get("focus_risk_idx")

            if focus_idx is None:
                st.caption("直接将卡片拖拽入本区域，或点「选为追问对象」后在下方输入。")
                focus_from_js_dz = dropzone_component(
                    title="将左侧卡片拖拽至此",
                    subtitle="或点击卡片上的「选为追问对象」",
                    min_height=200,
                    key="dropzone_large"
                )
                if focus_from_js_dz and isinstance(focus_from_js_dz, dict):
                    new_idx = int(focus_from_js_dz.get("idx", -1))
                    new_ts = focus_from_js_dz.get("ts", 0)
                    if new_idx >= 0 and new_ts != st.session_state.get("last_dz_ts"):
                        st.session_state["last_dz_ts"] = new_ts
                        st.session_state.focus_risk_idx = new_idx
                        st.rerun()
            elif focus_idx < 0 or focus_idx >= len(snap["risks"]):
                st.warning("追问对象无效，请重新选择。")
                st.session_state.focus_risk_idx = None
            else:
                risk = snap["risks"][focus_idx]
                idx = focus_idx
                hist = st.session_state.risk_followup_chats.setdefault(idx, [])
                dim = risk.get("dimension", "")
                lvl = risk.get("level", "")

                st.markdown(f"**当前追问：** 风险点 {idx + 1} · {dim} · {lvl}")
                excerpt = risk.get("original", "无") or ""
                st.caption((excerpt[:160] + "…") if len(excerpt) > 160 else excerpt)

                mini_dz_val = dropzone_component(
                    title="拖入新卡片可替换追问对象",
                    subtitle="",
                    min_height=52,
                    key="dropzone_mini_replace"
                )
                if mini_dz_val and isinstance(mini_dz_val, dict):
                    new_idx = int(mini_dz_val.get("idx", -1))
                    new_ts = mini_dz_val.get("ts", 0)
                    if new_idx >= 0 and new_ts != st.session_state.get("last_mini_dz_ts"):
                        st.session_state["last_mini_dz_ts"] = new_ts
                        st.session_state.focus_risk_idx = new_idx
                        st.rerun()

                # --- 应用修改按钮 ---
                applied_set = st.session_state.get("applied_risks", set())
                has_sugg = bool((risk.get("suggestion") or "").strip())
                if has_sugg:
                    if focus_idx not in applied_set:
                        if st.button("✔️ 把建议应用到正文", key=f"apply_risk_{focus_idx}", use_container_width=True, type="primary"):
                            st.session_state.applied_risks.add(focus_idx)
                            st.rerun()
                    else:
                        if st.button("❌ 撤销应用并在正文还原", key=f"undo_risk_{focus_idx}", use_container_width=True):
                            st.session_state.applied_risks.remove(focus_idx)
                            st.rerun()
                # -------------------

                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    if st.button("清除追问对象", key="clear_focus_risk", use_container_width=True):
                        st.session_state.focus_risk_idx = None
                        st.rerun()
                with btn_col2:
                    if st.button("清空对话记录", key="risk_chat_clear_focus", use_container_width=True):
                        st.session_state.risk_followup_chats[idx] = []
                        st.rerun()

                with st.form(key="risk_followup_form_focus", clear_on_submit=True):
                    q = st.text_area(
                        "追问内容",
                        height=80,
                        placeholder="例如：若对方违反该条，我方可主张哪些权利？条款如何改更稳妥？",
                    )
                    submitted = st.form_submit_button("🚀 发送给 AI", type="primary", use_container_width=True)

                chat_container = st.container(height=380)
                for m in hist:
                    with chat_container.chat_message(m["role"]):
                        st.markdown(m["content"])

                if submitted and (q or "").strip():
                    provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
                    if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
                        st.error("请先在侧栏填写 OpenAI API Key。")
                    elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
                        st.error("请先在侧栏填写 Anthropic API Key。")
                    else:
                        with st.spinner("思考中…"):
                            try:
                                client, model_name, _, use_tools = _get_llm_client_and_model()
                                tools, router = resolve_mcp_bundle()
                                if not use_tools: tools = []
                                sys_p = build_risk_followup_system(contract_ctx, risk, idx)
                                thread = [{"role": "system", "content": sys_p}]
                                thread.extend(hist)
                                thread.append({"role": "user", "content": q.strip()})

                                def exec_rf(name: str, args: dict) -> str:
                                    return call_tool_sync(router, name, args)

                                reply = completion_with_tool_loop(
                                    client,
                                    model_name,
                                    thread,
                                    tools if tools else None,
                                    exec_rf,
                                    max_tool_rounds=6 if use_tools else 0,
                                    temperature=0.35,
                                )
                                hist.append({"role": "user", "content": q.strip()})
                                hist.append({"role": "assistant", "content": reply})
                                st.session_state.risk_followup_chats[idx] = hist
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

        # Fuzzy matcher guarantees all items are found, so no not_found warning is needed.

with tab_chat:
    st.subheader("💬 上下文对话")
    st.caption("基于当前合同与最近一次审查结果追问；勾选侧栏「启用 MCP」后，可检索已配置的 MCP 工具。")

    contract_for_chat = (st.session_state.get("contract_text_for_chat") or "").strip() or (
        st.session_state.get("contract_input") or ""
    ).strip()

    if st.button("清空对话记录", key="clear_chat"):
        st.session_state.chat_messages = []
        st.rerun()

    if not contract_for_chat:
        st.info("请先在「合同审查」中上传/粘贴合同并完成一次加载（或粘贴后切换到本标签页将自动读取输入框）。")
    else:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        if prompt := st.chat_input("针对合同内容追问…"):
            provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
            if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
                st.error("请先在侧栏填写 OpenAI API Key。")
            elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
                st.error("请先在侧栏填写 Anthropic API Key。")
            else:
                with st.spinner("思考中…"):
                    try:
                        client, model_name, _, use_tools = _get_llm_client_and_model()
                        tools, router = resolve_mcp_bundle()
                        if not use_tools: tools = []
                        sys_p = build_chat_system_prompt(
                            contract_for_chat,
                            st.session_state.get("review_snapshot"),
                        )
                        thread = [{"role": "system", "content": sys_p}]
                        thread.extend(st.session_state.chat_messages)
                        thread.append({"role": "user", "content": prompt})

                        def exec_tool_c(name: str, args: dict) -> str:
                            return call_tool_sync(router, name, args)

                        reply = completion_with_tool_loop(
                            client,
                            model_name,
                            thread,
                            tools if tools else None,
                            exec_tool_c,
                            max_tool_rounds=6 if use_tools else 0,
                            temperature=0.3,
                        )
                        st.session_state.chat_messages.append({"role": "user", "content": prompt})
                        st.session_state.chat_messages.append({"role": "assistant", "content": reply})
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

