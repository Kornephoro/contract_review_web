import json
import html
import time
import re
import docx
import PyPDF2
from pathlib import Path
from typing import List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

from legal_review.llm import completion_with_tool_loop
from legal_review.mcp_bridge import call_tool_sync, load_mcp_config
from legal_review.ocr import (
    extract_text_with_paddle,
    get_ocr_init_command,
    get_paddle_ocr_not_ready_message,
    get_paddle_ocr_status,
    get_python_runtime_requirement_message,
    is_image_file,
    is_required_python_version,
    should_use_ocr_for_pdf,
)
from legal_review.review_html import build_risk_deck_html
from legal_review.prompts import (
    CHAT_SYSTEM_PREFIX,
    REVIEW_SYSTEM_BASE,
    REVIEW_MCP_SUFFIX,
    RISK_FOLLOWUP_PREFIX,
    build_dynamic_review_system,
)
from legal_review.review_postprocess import (
    get_actionable_risk_indices,
    postprocess_review_risks,
)
from legal_review.templates import (
    CONTRACT_TYPE_LABELS,
    CONTRACT_TYPE_OPTIONS,
    format_template_option_label,
    get_builtin_template,
    get_default_review_templates,
    get_review_template_by_id,
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
        "ollama_base_url", "ollama_model",
        "review_templates", "selected_review_template_id",
    ]:
        if k in st.session_state:
            s[k] = st.session_state[k]
    try:
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def _clear_template_editor_state(template_id: str) -> None:
    for prefix in ("template_name_", "template_scope_", "template_prompt_"):
        key = f"{prefix}{template_id}"
        if key in st.session_state:
            del st.session_state[key]


def _get_review_templates() -> list[dict]:
    templates = get_default_review_templates(st.session_state.get("review_templates"))
    st.session_state["review_templates"] = templates
    return templates


def _persist_review_templates(templates: list[dict], notice: Optional[str] = None) -> None:
    normalized_templates = get_default_review_templates(templates)
    st.session_state["review_templates"] = normalized_templates

    selected_template_id = st.session_state.get("selected_review_template_id", "auto")
    if selected_template_id != "auto" and not get_review_template_by_id(normalized_templates, selected_template_id):
        st.session_state["selected_review_template_id"] = "auto"

    if notice:
        st.session_state["template_notice"] = notice

    save_settings()


def _create_review_template(name: str, prompt: str, bound_contract_type: Optional[str]) -> None:
    templates = _get_review_templates()
    templates.append(
        {
            "id": f"ut-{time.time_ns()}",
            "name": name.strip(),
            "prompt": prompt.strip(),
            "is_builtin": False,
            "bound_contract_type": bound_contract_type or None,
        }
    )
    _persist_review_templates(templates, notice=f"已新增模板：{name.strip()}")


def _save_review_template(
    template_id: str,
    name: str,
    prompt: str,
    bound_contract_type: Optional[str],
) -> None:
    updated_templates: list[dict] = []
    target_name = ""
    for template in _get_review_templates():
        if template["id"] == template_id:
            target_name = name.strip() or template["name"]
            updated_templates.append(
                {
                    **template,
                    "name": target_name,
                    "prompt": prompt.strip(),
                    "bound_contract_type": bound_contract_type or None,
                }
            )
        else:
            updated_templates.append(template)

    _clear_template_editor_state(template_id)
    _persist_review_templates(updated_templates, notice=f"已保存模板：{target_name or template_id}")


def _reset_builtin_review_template(template_id: str) -> None:
    builtin_template = get_builtin_template(template_id)
    if not builtin_template:
        return

    reset_templates = [
        builtin_template if template["id"] == template_id else template
        for template in _get_review_templates()
    ]
    _clear_template_editor_state(template_id)
    _persist_review_templates(reset_templates, notice=f"已恢复默认模板：{builtin_template['name']}")


def _delete_review_template(template_id: str) -> None:
    removed_template = get_review_template_by_id(_get_review_templates(), template_id)
    remaining_templates = [
        template for template in _get_review_templates() if template["id"] != template_id
    ]

    if st.session_state.get("selected_review_template_id") == template_id:
        st.session_state["selected_review_template_id"] = "auto"

    _clear_template_editor_state(template_id)
    notice = f"已删除模板：{removed_template['name']}" if removed_template else "模板已删除"
    _persist_review_templates(remaining_templates, notice=notice)

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
    """合同面板固定对比色——墨律设计系统。"""
    if theme_key == "light":
        return {
            "panel_bg": "#faf8f5",
            "panel_fg": "#1a1f2e",
            "border": "#e2ddd5",
            "muted": "#8a857f",
        }
    if theme_key == "dark":
        return {
            "panel_bg": "#252a3a",
            "panel_fg": "#e8e4df",
            "border": "#353a4d",
            "muted": "#b0aca6",
        }
    return {
        "panel_bg": "#faf8f5",
        "panel_fg": "#1a1f2e",
        "border": "#e2ddd5",
        "muted": "#8a857f",
    }


def _risk_level_styles(theme_key: str) -> dict:
    if theme_key == "dark":
        return {
            "高风险": ("rgba(180, 70, 65, 0.30)", "#e89490"),
            "中风险": ("rgba(180, 140, 50, 0.25)", "#e0c470"),
            "低风险": ("rgba(80, 120, 180, 0.22)", "#8ab0d8"),
        }
    return {
        "高风险": ("rgba(155, 48, 48, 0.14)", "#9b3030"),
        "中风险": ("rgba(139, 106, 37, 0.14)", "#8b6a25"),
        "低风险": ("rgba(58, 90, 139, 0.10)", "#3a5a8b"),
    }


def _highlight_border_for_risk(risk: dict, theme_key: str) -> Tuple[str, str]:
    """优先按四维 dimension 着色，否则按风险等级。"""
    dim = (risk.get("dimension") or "").strip()
    if theme_key == "dark":
        dm = {
            "法律合规": ("rgba(90, 130, 190, 0.22)", "#8ab0d8"),
            "风险防控": ("rgba(180, 70, 65, 0.25)", "#e89490"),
            "条款完善": ("rgba(180, 140, 50, 0.22)", "#e0c470"),
            "利益保护": ("rgba(70, 150, 90, 0.20)", "#7dc09a"),
        }
    else:
        dm = {
            "法律合规": ("rgba(44, 74, 110, 0.12)", "#2c4a6e"),
            "风险防控": ("rgba(139, 53, 53, 0.14)", "#8b3535"),
            "条款完善": ("rgba(139, 106, 37, 0.14)", "#8b6a25"),
            "利益保护": ("rgba(46, 107, 69, 0.12)", "#2e6b45"),
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
        pal = _panel_palette(theme_key)
        empty = (
            f'<div style="max-height:520px;overflow-y:auto;padding:16px 18px;border:1px solid {pal["border"]};'
            f'border-radius:10px;background:{pal["panel_bg"]};color:{pal["muted"]};'
            f'font-family:Outfit,Noto Sans SC,sans-serif;">（合同正文为空，无法标注）</div>'
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
            bg = "rgba(46, 107, 69, 0.18)" if theme_key == "dark" else "rgba(46, 107, 69, 0.10)"
            border = "#7dc09a" if theme_key == "dark" else "#2e6b45"
            inner_text = risks[idx].get("suggestion", "")
            orig_txt = html.escape(text[pos:end])
            sug_txt = html.escape(inner_text)
            inner = f'<span style="color:#2e6b45;font-weight:600;">{sug_txt}</span>'
            parts.append(
                f'<span id="risk-anchor-{idx}" style="scroll-margin-top:88px;background:{bg};border-bottom:2px solid {border};'
                f'padding:2px 4px;border-radius:4px;color:inherit;" title="已应用修改，原文本为：{orig_txt}">{inner}'
                f'<sup style="font-size:0.7em;font-weight:700;margin-left:4px;color:{border};'
                f'font-family:Outfit,Noto Sans SC,sans-serif;">已修订</sup></span>'
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

    is_dark = theme_key == "dark"
    shell_bg = "rgba(29, 34, 48, 0.82)" if is_dark else "rgba(255, 252, 247, 0.72)"
    shell_border = "#343a4c" if is_dark else "#e6ddd0"
    shell_shadow = "0 20px 50px rgba(8, 12, 22, 0.26)" if is_dark else "0 18px 40px rgba(36, 40, 52, 0.08)"
    paper_bg = "#202634" if is_dark else "#fffefb"
    paper_border = "#3d4457" if is_dark else "#ece3d6"
    paper_shadow = "0 18px 36px rgba(0, 0, 0, 0.24)" if is_dark else "0 16px 30px rgba(42, 44, 57, 0.08)"
    line_color = "rgba(212, 176, 112, 0.25)" if is_dark else "rgba(184, 148, 95, 0.18)"
    title_color = "#f0ebe4" if is_dark else "#5f584f"
    body_color = pal["panel_fg"]
    header_strip = (
        "linear-gradient(90deg, rgba(212,176,112,0.95), rgba(122,158,212,0.24))"
        if is_dark
        else "linear-gradient(90deg, rgba(184,148,95,0.90), rgba(44,62,107,0.14))"
    )

    if theme_key == "system":
        wrapper = (
            "<style>"
            ".review-paper-shell{padding:18px;border-radius:24px;background:rgba(255,252,247,0.72);"
            "border:1px solid #e6ddd0;box-shadow:0 18px 40px rgba(36,40,52,0.08);}"
            ".review-paper{max-width:860px;margin:0 auto;border-radius:20px;background:#fffefb;"
            "border:1px solid #ece3d6;box-shadow:0 16px 30px rgba(42,44,57,0.08);overflow:hidden;}"
            ".review-paper-head{padding:16px 24px 12px 24px;border-bottom:1px solid rgba(184,148,95,0.14);"
            "font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.76rem;letter-spacing:0.14em;text-transform:uppercase;color:#5f584f;}"
            ".review-paper-body{max-height:640px;overflow-y:auto;padding:22px 30px 34px 30px;color:#1a1f2e;"
            "white-space:pre-wrap;word-break:break-word;line-height:1.88;font-size:1rem;font-family:Outfit,Noto Sans SC,sans-serif;"
            "background-image:linear-gradient(to bottom, rgba(184,148,95,0.18) 1px, transparent 1px);background-size:100% 2.05rem;}"
            ".review-paper-topline{height:5px;background:linear-gradient(90deg, rgba(184,148,95,0.90), rgba(44,62,107,0.14));}"
            "@media (prefers-color-scheme: dark) {"
            ".review-paper-shell{background:rgba(29,34,48,0.82)!important;border-color:#343a4c!important;box-shadow:0 20px 50px rgba(8,12,22,0.26)!important;}"
            ".review-paper{background:#202634!important;border-color:#3d4457!important;box-shadow:0 18px 36px rgba(0,0,0,0.24)!important;}"
            ".review-paper-head{color:#f0ebe4!important;border-bottom-color:rgba(212,176,112,0.20)!important;}"
            ".review-paper-body{color:#e8e4df!important;background-image:linear-gradient(to bottom, rgba(212,176,112,0.24) 1px, transparent 1px)!important;}"
            ".review-paper-topline{background:linear-gradient(90deg, rgba(212,176,112,0.95), rgba(122,158,212,0.24))!important;}"
            "}"
            "</style>"
            f'<div class="review-paper-shell"><div class="review-paper"><div class="review-paper-topline"></div>'
            f'<div class="review-paper-head">Contract Draft</div>'
            f'<div class="review-paper-body">{inner}</div></div></div>'
        )
    else:
        wrapper = (
            f'<div style="padding:18px;border-radius:24px;background:{shell_bg};border:1px solid {shell_border};'
            f'box-shadow:{shell_shadow};">'
            f'<div style="max-width:860px;margin:0 auto;border-radius:20px;background:{paper_bg};'
            f'border:1px solid {paper_border};box-shadow:{paper_shadow};overflow:hidden;">'
            f'<div style="height:5px;background:{header_strip};"></div>'
            f'<div style="padding:16px 24px 12px 24px;border-bottom:1px solid {line_color};'
            f'font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.76rem;letter-spacing:0.14em;'
            f'text-transform:uppercase;color:{title_color};">Contract Draft</div>'
            f'<div style="max-height:640px;overflow-y:auto;padding:22px 30px 34px 30px;color:{body_color};'
            f'white-space:pre-wrap;word-break:break-word;line-height:1.88;font-size:1rem;'
            f'font-family:Outfit,Noto Sans SC,sans-serif;'
            f'background-image:linear-gradient(to bottom, {line_color} 1px, transparent 1px);'
            f'background-size:100% 2.05rem;">{inner}</div>'
            f'</div></div>'
        )
    return wrapper, not_found


def _legend_html(theme_key: str) -> str:
    if theme_key == "dark":
        return (
            '<div style="margin-bottom:10px;font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.82rem;'
            'display:flex;gap:14px;align-items:center;">'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:8px;height:8px;border-radius:50%;background:#e89490;display:inline-block;"></span>'
            '<span style="color:#e89490;font-weight:600;">高风险</span></span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:8px;height:8px;border-radius:50%;background:#e0c470;display:inline-block;"></span>'
            '<span style="color:#e0c470;font-weight:600;">中风险</span></span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:8px;height:8px;border-radius:50%;background:#8ab0d8;display:inline-block;"></span>'
            '<span style="color:#8ab0d8;font-weight:600;">低风险</span></span>'
            "</div>"
        )
    if theme_key == "system":
        return (
            "<style>"
            ".legend-sys .dot-h { background:#9b3030; }"
            ".legend-sys .lbl-h { color:#9b3030; }"
            ".legend-sys .dot-m { background:#8b6a25; }"
            ".legend-sys .lbl-m { color:#8b6a25; }"
            ".legend-sys .dot-l { background:#3a5a8b; }"
            ".legend-sys .lbl-l { color:#3a5a8b; }"
            "@media (prefers-color-scheme: dark) {"
            ".legend-sys .dot-h { background:#e89490; }"
            ".legend-sys .lbl-h { color:#e89490; }"
            ".legend-sys .dot-m { background:#e0c470; }"
            ".legend-sys .lbl-m { color:#e0c470; }"
            ".legend-sys .dot-l { background:#8ab0d8; }"
            ".legend-sys .lbl-l { color:#8ab0d8; }"
            "}"
            "</style>"
            '<div class="legend-sys" style="margin-bottom:10px;font-family:Outfit,Noto Sans SC,sans-serif;'
            'font-size:0.82rem;display:flex;gap:14px;align-items:center;">'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span class="dot-h" style="width:8px;height:8px;border-radius:50%;display:inline-block;"></span>'
            '<span class="lbl-h" style="font-weight:600;">高风险</span></span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span class="dot-m" style="width:8px;height:8px;border-radius:50%;display:inline-block;"></span>'
            '<span class="lbl-m" style="font-weight:600;">中风险</span></span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span class="dot-l" style="width:8px;height:8px;border-radius:50%;display:inline-block;"></span>'
            '<span class="lbl-l" style="font-weight:600;">低风险</span></span>'
            "</div>"
        )
    return (
        '<div style="margin-bottom:10px;font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.82rem;'
        'display:flex;gap:14px;align-items:center;">'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#9b3030;display:inline-block;"></span>'
        '<span style="color:#9b3030;font-weight:600;">高风险</span></span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#8b6a25;display:inline-block;"></span>'
        '<span style="color:#8b6a25;font-weight:600;">中风险</span></span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#3a5a8b;display:inline-block;"></span>'
        '<span style="color:#3a5a8b;font-weight:600;">低风险</span></span>'
        "</div>"
    )


def _legend_dimensions_html() -> str:
    return (
        '<div style="margin:6px 0 10px 0;font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.78rem;'
        'color:#8a857f;line-height:1.6;display:flex;flex-wrap:wrap;gap:4px;align-items:center;">'
        '<span style="font-weight:600;color:#5a5650;">四维审查</span>'
        '<span style="color:#e2ddd5;">|</span>'
        '<span style="color:#2c4a6e;font-weight:500;">法律合规</span>'
        '<span style="color:#e2ddd5;">·</span>'
        '<span style="color:#8b3535;font-weight:500;">风险防控</span>'
        '<span style="color:#e2ddd5;">·</span>'
        '<span style="color:#8b6a25;font-weight:500;">条款完善</span>'
        '<span style="color:#e2ddd5;">·</span>'
        '<span style="color:#2e6b45;font-weight:500;">利益保护</span>'
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
            .stApp {
                background:
                    radial-gradient(circle at top left, rgba(184, 148, 95, 0.14), transparent 22%),
                    radial-gradient(circle at top right, rgba(44, 62, 107, 0.10), transparent 18%),
                    linear-gradient(180deg, #fbf8f2 0%, #f3ede3 100%) !important;
                color: var(--ml-text-primary) !important;
            }
            [data-testid="stHeader"] { background-color: var(--ml-bg-primary) !important; }
            div[data-testid="stSidebar"] { background-color: var(--ml-bg-secondary) !important; border-right: 1px solid var(--ml-border) !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    elif key == "dark":
        st.markdown(
            """
            <style>
            :root {
                --ml-bg-primary: #181c28;
                --ml-bg-secondary: #1e2233;
                --ml-bg-surface: #252a3a;
                --ml-text-primary: #e8e4df;
                --ml-text-secondary: #b0aca6;
                --ml-text-muted: #7a7672;
                --ml-accent-gold: #d4b070;
                --ml-accent-navy: #7a9ed4;
                --ml-border: #353a4d;
                --ml-border-light: #2e3345;
            }
            .stApp {
                background:
                    radial-gradient(circle at top left, rgba(212, 176, 112, 0.10), transparent 20%),
                    radial-gradient(circle at top right, rgba(122, 158, 212, 0.12), transparent 18%),
                    linear-gradient(180deg, #141925 0%, #1a2030 100%) !important;
                color: var(--ml-text-primary) !important;
            }
            [data-testid="stHeader"] { background-color: var(--ml-bg-primary) !important; }
            div[data-testid="stSidebar"] { background-color: var(--ml-bg-secondary) !important; border-right: 1px solid var(--ml-border) !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <style>
            @media (prefers-color-scheme: dark) {
              :root {
                --ml-bg-primary: #181c28;
                --ml-bg-secondary: #1e2233;
                --ml-bg-surface: #252a3a;
                --ml-text-primary: #e8e4df;
                --ml-text-secondary: #b0aca6;
                --ml-text-muted: #7a7672;
                --ml-accent-gold: #d4b070;
                --ml-accent-navy: #7a9ed4;
                --ml-border: #353a4d;
                --ml-border-light: #2e3345;
              }
              .stApp {
                background:
                    radial-gradient(circle at top left, rgba(212, 176, 112, 0.10), transparent 20%),
                    radial-gradient(circle at top right, rgba(122, 158, 212, 0.12), transparent 18%),
                    linear-gradient(180deg, #141925 0%, #1a2030 100%) !important;
                color: var(--ml-text-primary) !important;
              }
              [data-testid="stHeader"] { background-color: var(--ml-bg-primary) !important; }
              div[data-testid="stSidebar"] { background-color: var(--ml-bg-secondary) !important; border-right: 1px solid var(--ml-border) !important; }
            }
            @media (prefers-color-scheme: light) {
              .stApp {
                background:
                    radial-gradient(circle at top left, rgba(184, 148, 95, 0.14), transparent 22%),
                    radial-gradient(circle at top right, rgba(44, 62, 107, 0.10), transparent 18%),
                    linear-gradient(180deg, #fbf8f2 0%, #f3ede3 100%) !important;
                color: var(--ml-text-primary) !important;
              }
              [data-testid="stHeader"] { background-color: var(--ml-bg-primary) !important; }
              div[data-testid="stSidebar"] { background-color: var(--ml-bg-secondary) !important; border-right: 1px solid var(--ml-border) !important; }
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
    st.session_state["review_templates"] = get_default_review_templates(_init_cfg.get("review_templates"))
    st.session_state.setdefault("selected_review_template_id", "auto")
    if (
        st.session_state["selected_review_template_id"] != "auto"
        and not get_review_template_by_id(
            st.session_state["review_templates"],
            st.session_state["selected_review_template_id"],
        )
    ):
        st.session_state["selected_review_template_id"] = "auto"
    
    st.session_state["settings_loaded"] = True

# 页面基础设置
st.set_page_config(page_title="智审法务 - AI 合同审查助手", layout="wide")

if not is_required_python_version():
    st.error(get_python_runtime_requirement_message())

# ── 全局设计系统：字体 + 基础变量 + Streamlit 覆盖 ──
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=Outfit:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

    :root {
        --ml-font-display: "Cormorant Garamond", "Noto Serif SC", "STSong", serif;
        --ml-font-body: "Outfit", "Noto Sans SC", "Microsoft YaHei", sans-serif;
        --ml-bg-primary: #faf8f5;
        --ml-bg-secondary: #f2efe9;
        --ml-bg-surface: #ffffff;
        --ml-text-primary: #1a1f2e;
        --ml-text-secondary: #5a5650;
        --ml-text-muted: #8a857f;
        --ml-accent-gold: #b8945f;
        --ml-accent-navy: #2c3e6b;
        --ml-border: #e2ddd5;
        --ml-border-light: #ece8e1;
        --ml-risk-high: #9b3030;
        --ml-risk-mid: #8b6a25;
        --ml-risk-low: #3a5a8b;
    }

    /* ── Streamlit 全局覆盖 ── */
    .stApp {
        font-family: var(--ml-font-body) !important;
        background:
            radial-gradient(circle at top left, rgba(184, 148, 95, 0.14), transparent 22%),
            radial-gradient(circle at top right, rgba(44, 62, 107, 0.10), transparent 18%),
            linear-gradient(180deg, #fbf8f2 0%, #f3ede3 100%) !important;
    }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1580px;
        padding-top: 2rem;
        padding-bottom: 3rem;
        padding-left: 2.15rem;
        padding-right: 2.15rem;
    }
    .stApp h1, .stApp h2, .stApp h3, .stApp h4 {
        font-family: var(--ml-font-display) !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em;
    }
    .stApp h1 {
        font-size: 2.2rem !important;
        color: var(--ml-text-primary) !important;
    }

    /* Sidebar */
    div[data-testid="stSidebar"] {
        font-family: var(--ml-font-body) !important;
    }
    div[data-testid="stSidebar"] .stRadio label,
    div[data-testid="stSidebar"] .stCheckbox label {
        font-family: var(--ml-font-body) !important;
        font-size: 0.9rem !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        border-bottom: 2px solid var(--ml-border) !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: var(--ml-font-body) !important;
        font-weight: 500 !important;
        font-size: 0.95rem !important;
        padding: 10px 24px !important;
        color: var(--ml-text-muted) !important;
        border-bottom: 2px solid transparent !important;
        transition: all 0.2s ease;
    }
    .stTabs [aria-selected="true"] {
        color: var(--ml-accent-navy) !important;
        border-bottom-color: var(--ml-accent-gold) !important;
        font-weight: 600 !important;
    }
    .stTabs [data-baseweb="tab-panel"] {
        padding-top: 1rem;
    }

    /* Buttons */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="stBaseButton-primary"] {
        font-family: var(--ml-font-body) !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        background: linear-gradient(135deg, #344a77, #263657) !important;
        border: none !important;
        letter-spacing: 0.02em;
        transition: all 0.25s ease;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="stBaseButton-primary"]:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 14px rgba(44, 62, 107, 0.35) !important;
    }
    .stButton > button[kind="secondary"],
    .stButton > button[data-testid="stBaseButton-secondary"] {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
        border: 1.5px solid var(--ml-border) !important;
        background: rgba(255, 253, 249, 0.72) !important;
        color: var(--ml-text-secondary) !important;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="secondary"]:hover,
    .stButton > button[data-testid="stBaseButton-secondary"]:hover {
        border-color: var(--ml-accent-gold) !important;
        color: var(--ml-accent-gold) !important;
    }

    /* Dividers */
    hr {
        border-color: var(--ml-border-light) !important;
    }

    /* Text inputs */
    .stTextInput input, .stTextArea textarea {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
        border-color: var(--ml-border) !important;
        background: rgba(255, 253, 249, 0.82) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus {
        border-color: var(--ml-accent-gold) !important;
        box-shadow: 0 0 0 2px rgba(184, 148, 95, 0.15) !important;
    }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--ml-border); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--ml-text-muted); }

    /* Download button */
    .stDownloadButton > button {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
    }
    div[data-testid="stToolbar"] {
        right: 1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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
    with st.expander("审校模板库", expanded=False):
        template_notice = st.session_state.pop("template_notice", None)
        if template_notice:
            st.success(template_notice)

        review_templates = _get_review_templates()
        scope_options = ["none"] + [option_id for option_id, _ in CONTRACT_TYPE_OPTIONS]

        st.caption("模板会把专项审校重点追加到 AI system prompt。内置模板可改写并恢复默认，自定义模板会保存在本地。")

        with st.form("create_review_template_form", clear_on_submit=True):
            new_template_name = st.text_input("新模板名称")
            new_template_scope = st.selectbox(
                "绑定合同类型",
                options=scope_options,
                format_func=lambda value: "不限定合同类型" if value == "none" else CONTRACT_TYPE_LABELS.get(value, value),
            )
            new_template_prompt = st.text_area(
                "专项审校重点",
                height=180,
                placeholder="例如：\n- 重点检查付款与验收条款是否互相衔接。\n- 重点识别单方免责、责任上限和通知机制风险。",
            )
            create_template_submitted = st.form_submit_button("新增模板", use_container_width=True)

        if create_template_submitted:
            if not new_template_name.strip():
                st.warning("请输入模板名称。")
            elif not new_template_prompt.strip():
                st.warning("请输入专项审校重点。")
            else:
                _create_review_template(
                    new_template_name,
                    new_template_prompt,
                    None if new_template_scope == "none" else new_template_scope,
                )
                st.rerun()

        st.markdown("#### 现有模板")
        for template in review_templates:
            template_id = template["id"]
            scope_value = template.get("bound_contract_type") or "none"
            if scope_value not in scope_options:
                scope_value = "none"

            with st.expander(format_template_option_label(template), expanded=False):
                if template.get("is_builtin"):
                    st.caption("内置模板：可调整专项审校重点，也可一键恢复默认内容。")
                else:
                    st.caption("自定义模板：可编辑名称、适用合同类型和专项审校重点。")

                with st.form(f"template_form_{template_id}", clear_on_submit=False):
                    edited_name = st.text_input(
                        "模板名称",
                        value=template["name"],
                        key=f"template_name_{template_id}",
                        disabled=bool(template.get("is_builtin")),
                    )
                    edited_scope = st.selectbox(
                        "绑定合同类型",
                        options=scope_options,
                        index=scope_options.index(scope_value),
                        key=f"template_scope_{template_id}",
                        format_func=lambda value: "不限定合同类型" if value == "none" else CONTRACT_TYPE_LABELS.get(value, value),
                        disabled=bool(template.get("is_builtin")),
                    )
                    edited_prompt = st.text_area(
                        "专项审校重点",
                        value=template.get("prompt", ""),
                        height=180,
                        key=f"template_prompt_{template_id}",
                    )

                    action_col1, action_col2 = st.columns(2)
                    save_clicked = action_col1.form_submit_button("保存模板", use_container_width=True)
                    reset_clicked = False
                    delete_clicked = False
                    if template.get("is_builtin"):
                        reset_clicked = action_col2.form_submit_button("恢复默认", use_container_width=True)
                    else:
                        delete_clicked = action_col2.form_submit_button("删除模板", use_container_width=True)

                if save_clicked:
                    if not edited_prompt.strip():
                        st.warning("专项审校重点不能为空。")
                    elif not template.get("is_builtin") and not edited_name.strip():
                        st.warning("模板名称不能为空。")
                    else:
                        _save_review_template(
                            template_id,
                            edited_name,
                            edited_prompt,
                            None if edited_scope == "none" else edited_scope,
                        )
                        st.rerun()

                if reset_clicked:
                    _reset_builtin_review_template(template_id)
                    st.rerun()

                if delete_clicked:
                    _delete_review_template(template_id)
                    st.rerun()

    st.markdown("---")
    st.markdown("### 关于系统")
    st.markdown(
        "本系统利用大语言模型结合**法律专家提示词**，支持合同审查、**上下文追问对话**，"
        "以及可选的 **MCP 工具**挂接外部法律数据库。"
    )

inject_page_theme_css(st.session_state.get("ui_theme", "跟随系统"))

# --- 主页面 ---
st.markdown(
    '<div style="margin-bottom:4px;">'
    '<h1 style="font-family:Cormorant Garamond,Noto Serif SC,serif !important;font-size:2.4rem !important;'
    'font-weight:700;color:#1a1f2e;margin:0;letter-spacing:-0.02em;">智审法务</h1>'
    '<p style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.92rem;color:#8a857f;margin:4px 0 0 0;'
    'letter-spacing:0.03em;">AI-Powered Contract Risk Analysis</p>'
    '</div>',
    unsafe_allow_html=True,
)
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
if "last_uploaded_file_id" not in st.session_state:
    st.session_state.last_uploaded_file_id = None

def extract_text(file):
    text = ""
    suffix = Path(file.name).suffix.lower()
    file_bytes = file.getvalue()

    if suffix == ".docx":
        doc = docx.Document(file)
        text = "\n".join([para.text for para in doc.paragraphs])
    elif suffix == ".pdf":
        pdf_reader = PyPDF2.PdfReader(file)
        page_texts = []
        non_empty_pages = 0
        for page in pdf_reader.pages:
            page_text = (page.extract_text() or "").strip()
            if page_text:
                non_empty_pages += 1
            page_texts.append(page_text)
        text = "\n".join(page_texts).strip()

        if should_use_ocr_for_pdf(text, len(page_texts), non_empty_pages):
            ocr_status = get_paddle_ocr_status()
            if not ocr_status["ready"]:
                raise RuntimeError(
                    "当前 PDF 识别为扫描件或图片型 PDF，需使用 OCR。"
                    + get_paddle_ocr_not_ready_message()
                )
            text = extract_text_with_paddle(file_bytes, suffix)
    elif is_image_file(file.name):
        ocr_status = get_paddle_ocr_status()
        if not ocr_status["ready"]:
            raise RuntimeError(
                "图片合同识别依赖 OCR。"
                + get_paddle_ocr_not_ready_message()
            )
        text = extract_text_with_paddle(file_bytes, suffix)

    if not re.sub(r"\s+", "", text or ""):
        raise ValueError("未能从文件中提取出可用文本，请确认文件内容清晰可读。")
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
    suggestion_text = risk.get("suggestion_display") or risk.get("suggestion") or "无"
    suggestion_warning = risk.get("suggestion_warning") or ""
    suggestion_warning_line = f"修改建议状态：{suggestion_warning}\n" if suggestion_warning else ""
    return (
        f"{RISK_FOLLOWUP_PREFIX}\n\n"
        f"=========================\n"
        f"【当前探讨的风险焦点】\n"
        f"风险等级：{risk.get('level', '未知')}\n"
        f"涉事维度：{risk.get('dimension', '未知')}\n"
        f"原文摘录：\n{risk.get('original', '无')}\n"
        f"系统最初指出的问题：\n{risk.get('issue', '无')}\n"
        f"系统初步的修改建议：\n{suggestion_text}\n"
        f"{suggestion_warning_line}"
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
    selected_template_name = (snap.get("selected_template_name") or "").strip()
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid = sum(1 for r in risks if r.get("level") == "中风险")
    low = sum(1 for r in risks if r.get("level") == "低风险")

    theme_choice = st.session_state.get("ui_theme", "跟随系统")
    tk = THEME_MAP.get(theme_choice, "system")
    pal = _panel_palette(tk)
    is_dark = tk == "dark"
    gold = "#d4b070" if is_dark else "#b8945f"
    text_fg = pal["panel_fg"]
    header_bg = (
        "linear-gradient(145deg, rgba(36,41,55,0.96) 0%, rgba(28,33,44,0.96) 100%)"
        if is_dark
        else "linear-gradient(145deg, rgba(255,252,246,0.98) 0%, rgba(241,233,219,0.98) 100%)"
    )
    header_border = "#40475b" if is_dark else "#e6ddcf"
    header_title = "#f2ede6" if is_dark else "#182031"
    header_meta = "rgba(242,237,230,0.72)" if is_dark else "#72695f"
    info_bg = "rgba(255,255,255,0.05)" if is_dark else "rgba(255,253,249,0.8)"
    info_border = "#3a4153" if is_dark else "#e8dfd2"
    accent_bg = "rgba(212,176,112,0.08)" if is_dark else "rgba(184,148,95,0.07)"
    accent_border = "#524838" if is_dark else "#eadfcd"
    block_shadow = "0 14px 30px rgba(10,12,18,0.16)" if is_dark else "0 12px 24px rgba(26,31,46,0.05)"
    header_shadow = "0 18px 42px rgba(8,12,22,0.20)" if is_dark else "0 16px 34px rgba(26,31,46,0.08)"
    divider_color = "rgba(212,176,112,0.18)" if is_dark else "rgba(184,148,95,0.16)"
    badge_styles = {
        "高风险": (
            "rgba(191,98,90,0.16)" if is_dark else "rgba(196,101,94,0.10)",
            "#f0a49d" if is_dark else "#a14b46",
            "rgba(191,98,90,0.30)" if is_dark else "rgba(196,101,94,0.18)",
        ),
        "中风险": (
            "rgba(212,176,74,0.16)" if is_dark else "rgba(212,168,74,0.10)",
            "#e7cc85" if is_dark else "#9b7730",
            "rgba(212,176,74,0.28)" if is_dark else "rgba(212,168,74,0.18)",
        ),
        "低风险": (
            "rgba(122,158,196,0.18)" if is_dark else "rgba(122,158,196,0.10)",
            "#9dc0e1" if is_dark else "#44648b",
            "rgba(122,158,196,0.30)" if is_dark else "rgba(122,158,196,0.18)",
        ),
    }

    template_html = ""
    if selected_template_name:
        template_html = (
            f'<div style="margin:10px 0 0 0;font-family:Outfit,Noto Sans SC,sans-serif;'
            f'font-size:0.84rem;color:{header_meta};">'
            f'审校模板：{html.escape(selected_template_name)}</div>'
        )

    badge_html = []
    for label, count in [("高风险", high), ("中风险", mid), ("低风险", low)]:
        bg, fg, border = badge_styles[label]
        badge_html.append(
            f'<span style="padding:6px 15px;border-radius:999px;font-size:0.8rem;font-weight:600;'
            f'background:{bg};color:{fg};border:1px solid {border};">{label} {count}</span>'
        )

    st.markdown(
        f'<div style="padding:24px 26px 20px 26px;border-radius:24px;font-family:Outfit,Noto Sans SC,sans-serif;'
        f'background:{header_bg};border:1px solid {header_border};margin-bottom:16px;position:relative;overflow:hidden;'
        f'box-shadow:{header_shadow};">'
        f'<div style="position:absolute;inset:auto -46px -56px auto;width:180px;height:180px;border-radius:50%;'
        f'background:rgba(184,148,95,0.08);"></div>'
        f'<div style="position:absolute;top:0;left:0;right:0;height:5px;'
        f'background:linear-gradient(90deg, {gold}, rgba(44,62,107,0.14));"></div>'
        f'<div style="position:relative;z-index:1;">'
        f'<div style="display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;align-items:flex-start;">'
        f'<div>'
        f'<div style="font-size:0.73rem;color:{gold};font-weight:700;letter-spacing:0.16em;text-transform:uppercase;">AI 审查报告</div>'
        f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.6rem;font-weight:700;'
        f'color:{header_title};margin-top:6px;letter-spacing:0.01em;">{html.escape(ct)}</div>'
        f'{template_html}'
        f'</div>'
        f'<div style="max-width:340px;font-size:0.86rem;line-height:1.7;color:{header_meta};">'
        f'将合同原文置于工作台中央，左侧处理问题建议，右侧围绕单条风险持续追问。'
        f'</div>'
        f'</div>'
        f'<div style="height:1px;background:{divider_color};margin:16px 0 14px 0;"></div>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;">{"".join(badge_html)}</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    if ov:
        info_items = [
            ("参与方", "、".join(ov.get("parties") or []) or "未明确"),
            ("合同金额", ov.get("amount") or "未明确"),
            ("合同期限", ov.get("duration") or "未明确"),
            ("签署日期", ov.get("sign_date") or "未明确"),
            ("适用法律", ov.get("governing_law") or "未明确"),
        ]

        cols = st.columns(2, gap="medium")
        for i, (label, value) in enumerate(info_items):
            with cols[i % 2]:
                st.markdown(
                    f'<div style="padding:14px 16px;border-radius:18px;margin-bottom:10px;'
                    f'font-family:Outfit,Noto Sans SC,sans-serif;background:{info_bg};'
                    f'border:1px solid {info_border};box-shadow:{block_shadow};">'
                    f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:6px;'
                    f'letter-spacing:0.12em;text-transform:uppercase;">{label}</div>'
                    f'<div style="font-size:0.92rem;color:{text_fg};line-height:1.65;font-weight:400;">'
                    f'{html.escape(str(value))}</div></div>',
                    unsafe_allow_html=True,
                )

        summary = (ov.get("summary") or "").strip()
        if summary:
            st.markdown(
                f'<div style="padding:14px 18px;border-radius:18px;margin-top:2px;'
                f'font-family:Outfit,Noto Sans SC,sans-serif;background:{info_bg};'
                f'border:1px solid {info_border};box-shadow:{block_shadow};">'
                f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:7px;'
                f'letter-spacing:0.12em;text-transform:uppercase;">内容概览</div>'
                f'<div style="font-size:0.92rem;color:{text_fg};line-height:1.75;">{html.escape(summary)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    if risks:
        level_colors = {"高风险": "#c4655e", "中风险": "#b88a33", "低风险": "#5b7ea6"}
        if is_dark:
            level_colors = {"高风险": "#f0a49d", "中风险": "#e7cc85", "低风险": "#9dc0e1"}
        notable = sorted(
            risks,
            key=lambda r: {"高风险": 0, "中风险": 1, "低风险": 2}.get(r.get("level", "低风险"), 2),
        )[:3]
        items_html = "".join(
            f'<li style="margin-bottom:8px;line-height:1.62;">'
            f'<span style="color:{level_colors.get(r.get("level","低风险"), text_fg)};font-weight:700;'
            f'font-size:0.8rem;">{html.escape(r.get("level", ""))}</span> '
            f'<span style="color:{text_fg};">'
            f'{html.escape((r.get("issue") or "")[:90] + ("…" if len(r.get("issue", "")) > 90 else ""))}'
            f'</span></li>'
            for r in notable
        )
        st.markdown(
            f'<div style="padding:15px 18px;border-radius:18px;margin-top:10px;'
            f'font-family:Outfit,Noto Sans SC,sans-serif;background:{accent_bg};'
            f'border:1px solid {accent_border};box-shadow:{block_shadow};">'
            f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:8px;'
            f'letter-spacing:0.12em;text-transform:uppercase;">重点风险摘要</div>'
            f'<ul style="margin:0;padding-left:18px;font-size:0.88rem;color:{text_fg};">'
            f'{items_html}</ul></div>',
            unsafe_allow_html=True,
        )

def build_export_report_html(snap: dict) -> str:
    """生成可下载的 HTML 分析报告。"""
    import datetime as _dt
    ct = snap.get("contract_type") or "未识别"
    ov = snap.get("overview") or {}
    risks = snap.get("risks") or []
    selected_template_name = (snap.get("selected_template_name") or "").strip()
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid  = sum(1 for r in risks if r.get("level") == "中风险")
    low  = sum(1 for r in risks if r.get("level") == "低风险")
    now_str = _dt.datetime.now().strftime("%Y年%m月%d日 %H:%M")

    _lc = {"高风险": ("#9b3030", "#fdf5f4"), "中风险": ("#8b6a25", "#fdf8f0"), "低风险": ("#3a5a8b", "#f2f6fb")}
    _dc = {"法律合规": "#2c4a6e", "风险防控": "#8b3535", "条款完善": "#8b6a25", "利益保护": "#2e6b45"}

    # 概览信息行
    ov_rows = ""
    if ov:
        parties = "、".join(ov.get("parties") or []) or "未明确"
        if selected_template_name:
            ov_rows += f"<tr><td class='label'>审校模板</td><td>{html.escape(selected_template_name)}</td></tr>"
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
        f'<li><span style="color:{_lc.get(r.get("level","低风险"), ("#5a5650","#f5f5f5"))[0]};font-weight:700;font-size:.82rem;">'
        f'{html.escape(r.get("level",""))}</span> ' +
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
        sugg = risk.get("suggestion_display") or risk.get("suggestion") or ""
        sugg_warning = risk.get("suggestion_warning") or ""
        lb_  = risk.get("legal_basis") or ""
        sugg_html = f'<div class="rs suggestion"><strong>修改建议：</strong>{html.escape(sugg)}</div>' if sugg else ""
        if sugg_warning:
            sugg_html += f'<div class="rs legal"><strong>应用状态：</strong>{html.escape(sugg_warning)}</div>'
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=Outfit:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
body{{font-family:"Outfit","Noto Sans SC","Microsoft YaHei",sans-serif;margin:0;padding:32px;background:#f6f2ea;color:#1a1f2e;line-height:1.65;}}
.wrap{{max-width:960px;margin:0 auto;background:#ffffff;border-radius:22px;box-shadow:0 18px 40px rgba(26,31,46,0.08);overflow:hidden;}}
.hdr{{background:linear-gradient(145deg,#fcfaf6 0%,#efe6d8 100%);color:#1a1f2e;padding:36px 40px;position:relative;overflow:hidden;border-bottom:1px solid #e9dece;}}
.hdr::before{{content:'';position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,#b8945f,rgba(44,62,107,0.12));}}
.hdr::after{{content:'';position:absolute;bottom:-50px;right:-30px;width:160px;height:160px;border-radius:50%;background:rgba(184,148,95,0.08);}}
.hdr h1{{margin:0 0 6px 0;font-size:1.6rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;font-weight:700;color:#1a1f2e;letter-spacing:0.02em;}}
.hdr .ct{{font-size:1rem;font-weight:600;color:#3b4760;margin:6px 0;}}
.hdr .meta{{font-size:.82rem;color:#7d756a;margin-top:4px;}}
.badges{{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;}}
.badge{{padding:4px 14px;border-radius:20px;font-size:.78rem;font-weight:600;backdrop-filter:blur(4px);}}
.bh{{background:rgba(196,101,94,0.10);color:#9b4c46;border:1px solid rgba(196,101,94,0.18);}}
.bm{{background:rgba(212,168,74,0.10);color:#99742f;border:1px solid rgba(212,168,74,0.18);}}
.bl{{background:rgba(122,158,196,0.10);color:#4d6d93;border:1px solid rgba(122,158,196,0.18);}}
.section{{padding:24px 40px;border-bottom:1px solid #ece8e1;}}
.section h2{{font-size:1.1rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;color:#1a1f2e;margin:0 0 14px 0;padding-bottom:8px;border-bottom:2px solid #e2ddd5;font-weight:700;}}
table{{width:100%;border-collapse:collapse;}}
td{{padding:8px 12px;font-size:.88rem;border-bottom:1px solid #ece8e1;vertical-align:top;}}
td.label{{color:#b8945f;font-weight:600;width:90px;white-space:nowrap;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;}}
.summary-box{{padding:10px 14px;border-radius:8px;background:#faf8f5;border-left:3px solid #b8945f;font-size:.87rem;line-height:1.7;}}
.alert{{background:#fdf6ec;border-left:3px solid #b8945f;padding:12px 16px;border-radius:8px;border:1px solid #e8dcc8;}}
.alert ul{{margin:0;padding-left:16px;}}
.alert li{{margin-bottom:5px;font-size:.86rem;line-height:1.55;}}
.ri{{padding:14px 16px;border-radius:10px;margin-bottom:12px;border:1px solid #ece8e1;}}
.rh{{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;}}
.rn{{font-weight:700;font-size:.92rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;}}
.db{{font-size:.72rem;padding:2px 10px;border-radius:10px;border:1px solid;font-weight:500;}}
.rs{{font-size:.86rem;margin-bottom:6px;line-height:1.6;color:#2d2d2d;}}
.orig{{color:#5a5650;background:rgba(184,148,95,0.06);padding:3px 6px;border-radius:4px;border-left:2px solid #d4cfc7;display:inline;}}
.suggestion{{padding:8px 12px;background:#faf8f5;border-radius:6px;border-left:2px solid #b8945f;}}
.legal{{font-size:.8rem;color:#8a857f;}}
.foot{{text-align:center;padding:18px;font-size:.76rem;color:#8a857f;background:#f2efe9;letter-spacing:.02em;}}
</style></head>
<body><div class="wrap">
<div class="hdr">
  <h1>合同审查分析报告</h1>
  <div class="ct">{html.escape(ct)}</div>
  <div class="meta">生成时间：{now_str} · 智审法务 AI 系统</div>
  {"<div class='meta'>审校模板：" + html.escape(selected_template_name) + "</div>" if selected_template_name else ""}
  <div class="badges">
    <span class="badge bh">高风险 {high}</span>
    <span class="badge bm">中风险 {mid}</span>
    <span class="badge bl">低风险 {low}</span>
  </div>
</div>
{ov_section}
{issues_section}
<div class="section"><h2>逐条风险分析（共 {len(risks)} 条）</h2>{risk_rows}</div>
<div class="foot">本报告由 AI 自动生成，仅供参考，不构成正式法律意见。如需专业法律建议，请咨询执业律师。</div>
</div></body></html>"""


tab_review, tab_chat = st.tabs(["合同审查", "上下文对话"])

with tab_review:
    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.subheader("1. 上传或输入合同")
        uploaded_file = st.file_uploader(
            "拖拽文件到此或点击上传 (支持 .docx, .pdf, .png, .jpg, .jpeg)",
            type=["docx", "pdf", "png", "jpg", "jpeg"],
        )
        current_upload_id = None
        if uploaded_file is not None:
            current_upload_id = (
                uploaded_file.name,
                uploaded_file.size,
                getattr(uploaded_file, "type", ""),
            )
            if st.session_state.get("last_uploaded_file_id") != current_upload_id:
                st.session_state["last_uploaded_file_id"] = current_upload_id
        ocr_status = get_paddle_ocr_status()
        if ocr_status["ready"]:
            st.caption(f"OCR 状态：已初始化，可识别扫描件。模型缓存目录：{ocr_status['cache_dir']}")
        else:
            st.warning(
                f"OCR 状态：未初始化。若需识别扫描 PDF 或图片，请先运行 `{get_ocr_init_command()}`。"
            )
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
        review_templates = _get_review_templates()
        template_option_ids = ["auto"] + [template["id"] for template in review_templates]
        st.selectbox(
            "审校模板",
            options=template_option_ids,
            key="selected_review_template_id",
            on_change=save_settings,
            help="模板会把专项审校重点附加到本次审查提示词中。",
            format_func=lambda template_id: (
                "自动识别（使用通用四维策略）"
                if template_id == "auto"
                else format_template_option_label(
                    get_review_template_by_id(review_templates, template_id) or {"name": template_id}
                )
            ),
        )
        selected_template = get_review_template_by_id(
            review_templates,
            st.session_state.get("selected_review_template_id", "auto"),
        )
        if selected_template:
            template_scope = CONTRACT_TYPE_LABELS.get(
                selected_template.get("bound_contract_type") or "",
                "不限定合同类型",
            )
            st.caption(f"当前模板：{selected_template['name']} · {template_scope}")
            with st.expander("查看模板重点", expanded=False):
                if selected_template.get("prompt"):
                    st.markdown(selected_template["prompt"].replace("\n", "  \n"))
                else:
                    st.caption("当前模板未设置额外专项审校重点，将沿用基础四维审查框架。")
        st.markdown("<br>", unsafe_allow_html=True)
        analyze_button = st.button("开始审查", type="primary", use_container_width=True)

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
                        "导出分析报告 (HTML)",
                        data=build_export_report_html(snap_preview).encode("utf-8"),
                        file_name=_fname,
                        mime="text/html",
                        use_container_width=True,
                        key="export_btn_top",
                    )
            else:
                st.info("\u8bf7\u914d\u7f6e API Key\uff0c\u5728\u5de6\u4fa7\u8f93\u5165\u5408\u540c\u5185\u5bb9\u5e76\u70b9\u51fb\u5f00\u59cb\u5ba1\u67e5\u3002")

        if analyze_button:
            provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
            depth_choice = st.session_state.get("review_depth", "标准审查")
            persp_choice = st.session_state.get("review_perspective", "中立视角")
            selected_template = get_review_template_by_id(
                _get_review_templates(),
                st.session_state.get("selected_review_template_id", "auto"),
            )
            
            if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
                st.error("请先在左侧边栏输入 OpenAI API Key！")
            elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
                st.error("请先在左侧边栏输入 Anthropic API Key！")
            elif not uploaded_file and not contract_text.strip():
                st.warning("请先提供需要审查的合同内容！")
            else:
                final_text = ""
                try:
                    if uploaded_file:
                        file_suffix = Path(uploaded_file.name).suffix.lower()
                        parse_message = "正在解析上传文件..."
                        if file_suffix in {".pdf", ".png", ".jpg", ".jpeg"}:
                            parse_message = "正在解析上传文件，扫描件会先执行 OCR，请稍候..."
                        with st.spinner(parse_message):
                            final_text = extract_text(uploaded_file)
                            uploaded_file.seek(0)
                            st.session_state.original_file_bytes = uploaded_file.read()
                            st.session_state.original_file_name = uploaded_file.name
                    else:
                        final_text = contract_text
                        st.session_state.original_file_bytes = None
                        st.session_state.original_file_name = None
                except Exception as e:
                    st.error(f"文件解析失败：{str(e)}")
                    final_text = ""

                if final_text:
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
                                
                            system_prompt = build_dynamic_review_system(
                                depth_choice,
                                persp_choice,
                                selected_template=selected_template,
                            )
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

                            risks = postprocess_review_risks(risks, final_text)
                            actionable_risk_indices = get_actionable_risk_indices(risks)
                            non_actionable_count = max(0, len(risks) - len(actionable_risk_indices))

                            if not risks:
                                st.session_state.review_snapshot = {
                                    "text": final_text,
                                    "contract_type": contract_type,
                                    "overview": overview,
                                    "risks": [],
                                    "selected_template_name": selected_template["name"] if selected_template else None,
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
                                    "selected_template_name": selected_template["name"] if selected_template else None,
                                }
                                st.session_state.risk_followup_chats = {}
                                st.session_state.focus_risk_idx = None
                                st.success(f"✅ 审查完成！共发现 **{len(risks)}** 处风险，详见下方概览与三栏。")
                                if non_actionable_count:
                                    st.warning(
                                        f"其中 {non_actionable_count} 条仅生成了说明性修改意见，未给出可直接替换的合同条款，已禁止一键应用。"
                                    )
                            
                            st.rerun()

                        except Exception as e:
                            st.error(f"调用 AI 服务时出错，请检查 API Key、Base URL 或网络连接：{str(e)}")

    snap = st.session_state.review_snapshot
    if snap and snap.get("risks"):
        theme_choice = st.session_state.get("ui_theme", "跟随系统")
        theme_key = THEME_MAP.get(theme_choice, "system")
        pal = _panel_palette(theme_key)
        is_dark = theme_key == "dark"
        shell_bg = "rgba(28, 33, 45, 0.82)" if is_dark else "rgba(255, 252, 247, 0.76)"
        shell_border = "#384053" if is_dark else "#e6ddd0"
        shell_shadow = "0 18px 40px rgba(8,12,22,0.22)" if is_dark else "0 18px 34px rgba(26,31,46,0.06)"
        shell_text = pal["panel_fg"]
        shell_muted = pal["muted"]
        selected_template_name = (snap.get("selected_template_name") or "通用合同审校").strip()
        contract_type_name = snap.get("contract_type") or "未识别"

        st.divider()
        st.markdown(
            f'<div style="margin:2px 0 16px 0;">'
            f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-weight:700;'
            f'font-size:1.7rem;color:{shell_text};margin-bottom:4px;">合同审校工作台</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.92rem;'
            f'color:{shell_muted};line-height:1.7;">'
            f'中间保留合同原文，左侧处理问题建议，右侧围绕单个风险持续追问，更接近 Word 审校时的工作台。'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        applied = st.session_state.get("applied_risks", set())
        hl_html, not_found = build_highlighted_contract_html(snap["text"], snap["risks"], theme_key, applied)

        top_meta, top_apply, top_export = st.columns([1.45, 0.9, 0.92], gap="medium")
        with top_meta:
            st.markdown(
                f'<div style="padding:14px 18px;border-radius:18px;background:{shell_bg};'
                f'border:1px solid {shell_border};box-shadow:{shell_shadow};'
                f'font-family:Outfit,Noto Sans SC,sans-serif;">'
                f'<div style="font-size:0.72rem;letter-spacing:0.12em;text-transform:uppercase;'
                f'color:{"#d4b070" if is_dark else "#b8945f"};font-weight:700;margin-bottom:6px;">Workspace</div>'
                f'<div style="font-size:0.98rem;color:{shell_text};font-weight:600;">{html.escape(contract_type_name)}</div>'
                f'<div style="font-size:0.86rem;color:{shell_muted};margin-top:4px;line-height:1.65;">'
                f'当前模板：{html.escape(selected_template_name)} · 共 {len(snap["risks"])} 条风险提示</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with top_apply:
            if st.button("一键应用全部可替换建议", use_container_width=True, key="apply_all_workspace"):
                actionable_indices = get_actionable_risk_indices(snap["risks"])
                if actionable_indices:
                    st.session_state.applied_risks.update(actionable_indices)
                    st.rerun()
                else:
                    st.warning("当前没有可直接替换回正文的修订条款。")
        with top_export:
            if st.button("导出修改后的文件", use_container_width=True, type="primary", key="export_workspace"):
                st.session_state["show_export_dialog"] = True
                st.rerun()

        if st.session_state.get("show_export_dialog"):
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            st.info("导出功能已就绪，确认要落到文档里的修订后即可下载。")
            from legal_review.document_editor import execute_export_pipeline
            execute_export_pipeline()

        filter_col, sort_col = st.columns([0.9, 1.25], gap="medium")
        with filter_col:
            available_dimensions = [
                dimension
                for dimension in ["法律合规", "风险防控", "条款完善", "利益保护"]
                if any(r.get("dimension") == dimension for r in snap["risks"])
            ]
            dim_filter = st.selectbox(
                "按风险类型筛选",
                ["全部"] + available_dimensions,
                key="dim_filter_workspace",
            )
        with sort_col:
            sort_order = st.radio(
                "风险排序",
                ["原文顺序", "风险高到低", "风险低到高"],
                horizontal=True,
                key="risk_sort_order_workspace",
            )

        level_rank = {"高风险": 0, "中风险": 1, "低风险": 2}
        risks_with_idx = [(i, r) for i, r in enumerate(snap["risks"])]
        if dim_filter != "全部":
            risks_with_idx = [(i, r) for i, r in risks_with_idx if r.get("dimension") == dim_filter]
        if sort_order == "风险高到低":
            risks_with_idx = sorted(risks_with_idx, key=lambda x: level_rank.get(x[1].get("level", "低风险"), 2))
        elif sort_order == "风险低到高":
            risks_with_idx = sorted(risks_with_idx, key=lambda x: -level_rank.get(x[1].get("level", "低风险"), 2))
        deck_html = build_risk_deck_html(risks_with_idx, theme_key, st.session_state.get("applied_risks", set()))

        c1, c2, c3 = st.columns([0.96, 1.58, 0.96], gap="large")

        with c1:
            st.markdown(
                f'<div style="padding:14px 16px 10px 16px;border-radius:18px;background:{shell_bg};'
                f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:10px;">'
                f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.16rem;'
                f'font-weight:700;color:{shell_text};">问题与建议</div>'
                f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
                f'line-height:1.65;margin-top:4px;">筛选卡片、切换排序，并将某条风险拖拽到右侧继续追问。</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            focus_from_js_deck = risk_deck_component(cards_html=deck_html, key="risk_deck_v2")

            if focus_from_js_deck and isinstance(focus_from_js_deck, dict):
                new_idx = int(focus_from_js_deck.get("idx", -1))
                new_ts = focus_from_js_deck.get("ts", 0)
                action = focus_from_js_deck.get("action", "focus")

                if new_idx >= 0 and new_ts != st.session_state.get("last_deck_ts_v2"):
                    st.session_state["last_deck_ts_v2"] = new_ts
                    actual_idx = risks_with_idx[new_idx][0] if new_idx < len(risks_with_idx) else new_idx

                    if action == "apply":
                        applied_set = st.session_state.get("applied_risks", set())
                        if actual_idx in applied_set:
                            applied_set.remove(actual_idx)
                        else:
                            applied_set.add(actual_idx)
                        st.session_state.applied_risks = applied_set
                        st.rerun()
                    else:
                        st.session_state.focus_risk_idx = actual_idx
                        st.rerun()

        with c2:
            st.markdown(
                f'<div style="padding:14px 16px 10px 16px;border-radius:18px;background:{shell_bg};'
                f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:12px;">'
                f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.18rem;'
                f'font-weight:700;color:{shell_text};">合同正文</div>'
                f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
                f'line-height:1.65;margin-top:4px;">采用纸面式阅读区，便于在原文上下文中查看标注、应用修订和定位条款。</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(_legend_html(theme_key), unsafe_allow_html=True)
            st.markdown(_legend_dimensions_html(), unsafe_allow_html=True)
            st.markdown(hl_html, unsafe_allow_html=True)

        contract_ctx = snap.get("text") or ""

        with c3:
            st.markdown(
                f'<div style="padding:14px 16px 10px 16px;border-radius:18px;background:{shell_bg};'
                f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:10px;">'
                f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.16rem;'
                f'font-weight:700;color:{shell_text};">追问 AI</div>'
                f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
                f'line-height:1.65;margin-top:4px;">围绕单条风险继续推演修改方式、责任边界和对方可能的抗辩。</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            focus_idx = st.session_state.get("focus_risk_idx")

            if focus_idx is None:
                st.caption("把左侧卡片拖进这里，或在卡片上点“深入追问”后继续提问。")
                focus_from_js_dz = dropzone_component(
                    title="将左侧卡片拖到这里开始追问",
                    subtitle="也可以直接点击卡片上的“深入追问”",
                    min_height=200,
                    key="dropzone_large_workspace"
                )
                if focus_from_js_dz and isinstance(focus_from_js_dz, dict):
                    new_idx = int(focus_from_js_dz.get("idx", -1))
                    new_ts = focus_from_js_dz.get("ts", 0)
                    if new_idx >= 0 and new_ts != st.session_state.get("last_dz_ts_v2"):
                        st.session_state["last_dz_ts_v2"] = new_ts
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
                excerpt = risk.get("original", "") or ""
                st.caption((excerpt[:160] + "…") if len(excerpt) > 160 else excerpt)

                mini_dz_val = dropzone_component(
                    title="拖入新卡片可替换当前追问对象",
                    subtitle="",
                    min_height=52,
                    key="dropzone_mini_replace_workspace"
                )
                if mini_dz_val and isinstance(mini_dz_val, dict):
                    new_idx = int(mini_dz_val.get("idx", -1))
                    new_ts = mini_dz_val.get("ts", 0)
                    if new_idx >= 0 and new_ts != st.session_state.get("last_mini_dz_ts_v2"):
                        st.session_state["last_mini_dz_ts_v2"] = new_ts
                        st.session_state.focus_risk_idx = new_idx
                        st.rerun()

                applied_set = st.session_state.get("applied_risks", set())
                has_sugg = bool((risk.get("suggestion_display") or risk.get("suggestion") or "").strip())
                can_apply_sugg = risk.get("suggestion_actionable")
                if can_apply_sugg is None:
                    can_apply_sugg = bool((risk.get("suggestion") or "").strip())
                else:
                    can_apply_sugg = bool(can_apply_sugg) and bool((risk.get("suggestion") or "").strip())
                suggestion_warning = (risk.get("suggestion_warning") or "").strip()
                if suggestion_warning:
                    st.caption(f"修改建议状态：{suggestion_warning}")
                if can_apply_sugg:
                    if focus_idx not in applied_set:
                        if st.button("把建议应用到正文", key=f"apply_risk_workspace_{focus_idx}", use_container_width=True, type="primary"):
                            st.session_state.applied_risks.add(focus_idx)
                            st.rerun()
                    else:
                        if st.button("撤销应用并恢复原文", key=f"undo_risk_workspace_{focus_idx}", use_container_width=True):
                            st.session_state.applied_risks.remove(focus_idx)
                            st.rerun()
                elif has_sugg:
                    st.button(
                        "当前建议不可直接应用",
                        key=f"apply_risk_disabled_workspace_{focus_idx}",
                        use_container_width=True,
                        disabled=True,
                    )

                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    if st.button("清除追问对象", key="clear_focus_risk_workspace", use_container_width=True):
                        st.session_state.focus_risk_idx = None
                        st.rerun()
                with btn_col2:
                    if st.button("清空对话记录", key="risk_chat_clear_focus_workspace", use_container_width=True):
                        st.session_state.risk_followup_chats[idx] = []
                        st.rerun()

                with st.form(key="risk_followup_form_focus_workspace", clear_on_submit=True):
                    q = st.text_area(
                        "追问内容",
                        height=80,
                        placeholder="例如：如果对方拒绝接受这条修改，我方还能保留哪些权利？",
                    )
                    submitted = st.form_submit_button("发送给 AI", type="primary", use_container_width=True)

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
                        with st.spinner("思考中..."):
                            try:
                                client, model_name, _, use_tools = _get_llm_client_and_model()
                                tools, router = resolve_mcp_bundle()
                                if not use_tools:
                                    tools = []
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

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        st.markdown(
            f'<div style="padding:0 2px 4px 2px;">'
            f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-weight:700;'
            f'font-size:1.3rem;color:{shell_text};">补充视角</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.86rem;color:{shell_muted};'
            f'line-height:1.65;margin-top:4px;">'
            f'从条款修订和履约双方两个方向继续阅读，不打断中间的合同工作区。'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        tab_clause, tab_party = st.tabs(["📝 条款修订", "🧑‍🤝‍🧑 履约方影响"])
        with tab_clause:
            from legal_review.perspectives import render_clause_centric_view
            render_clause_centric_view(snap["risks"], theme_key)
        with tab_party:
            from legal_review.perspectives import render_party_centric_view
            render_party_centric_view(snap["overview"], snap["risks"], theme_key)

    if False and snap and snap.get("risks"):
        theme_choice = st.session_state.get("ui_theme", "跟随系统")
        theme_key = THEME_MAP.get(theme_choice, "system")

        st.divider()
        st.markdown(
            '<h3 style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-weight:700;'
            'color:#1a1f2e;margin:0 0 4px 0;">合同标注与风险详情</h3>',
            unsafe_allow_html=True,
        )
        st.caption("左侧：合同高亮原文 · 中间：风险卡片（筛选/排序） · 右侧：追问 AI")

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
                if st.button("一键应用所有推荐", use_container_width=True):
                    actionable_indices = get_actionable_risk_indices(snap["risks"])
                    if actionable_indices:
                        st.session_state.applied_risks.update(actionable_indices)
                        st.rerun()
                    else:
                        st.warning("当前没有可直接替换回正文的修订条款。")
            with c2_btn2:
                if st.button("导出修改后的文件", use_container_width=True, type="primary"):
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
                has_sugg = bool((risk.get("suggestion_display") or risk.get("suggestion") or "").strip())
                can_apply_sugg = risk.get("suggestion_actionable")
                if can_apply_sugg is None:
                    can_apply_sugg = bool((risk.get("suggestion") or "").strip())
                else:
                    can_apply_sugg = bool(can_apply_sugg) and bool((risk.get("suggestion") or "").strip())
                suggestion_warning = (risk.get("suggestion_warning") or "").strip()
                if suggestion_warning:
                    st.caption(f"修改建议状态：{suggestion_warning}")
                if can_apply_sugg:
                    if focus_idx not in applied_set:
                        if st.button("✔️ 把建议应用到正文", key=f"apply_risk_{focus_idx}", use_container_width=True, type="primary"):
                            st.session_state.applied_risks.add(focus_idx)
                            st.rerun()
                    else:
                        if st.button("❌ 撤销应用并在正文还原", key=f"undo_risk_{focus_idx}", use_container_width=True):
                            st.session_state.applied_risks.remove(focus_idx)
                            st.rerun()
                elif has_sugg:
                    st.button(
                        "当前建议不可直接应用",
                        key=f"apply_risk_disabled_{focus_idx}",
                        use_container_width=True,
                        disabled=True,
                    )
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

