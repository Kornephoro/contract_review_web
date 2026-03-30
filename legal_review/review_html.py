"""审查结果区：可拖拽风险卡片 + 追问投放区（iframe 内，通过 URL 参数与 Streamlit 同步）。"""

from __future__ import annotations

import html


def build_risk_deck_html(risks_with_idx: list, _theme_key: str, applied_risks: set = None) -> str:
    """
    左：风险卡片（可拖拽、可点击选为追问）。
    背景按高/中/低风险等级着色（视觉突出），仅维度小徽章使用维度配色。
    """
    # 维度徽章配色（仅用于 badge，不影响卡片背景）
    dim_colors = {
        "法律合规": ("#1565c0", "rgba(21,101,192,0.13)"),
        "风险防控": ("#c62828", "rgba(198,40,40,0.12)"),
        "条款完善": ("#f57c00", "rgba(245,124,0,0.13)"),
        "利益保护": ("#2e7d32", "rgba(46,125,50,0.12)"),
    }

    # 风险等级样式：(icon, card_bg, card_border, left_border_color, title_color)
    level_styles = {
        "高风险": (
            "🔴",
            "linear-gradient(135deg,#ffebee 0%,#fff5f5 100%)",
            "#ef9a9a",
            "#c62828",
            "#b71c1c",
        ),
        "中风险": (
            "🟡",
            "linear-gradient(135deg,#fff8e1 0%,#fffff5 100%)",
            "#ffe082",
            "#e65100",
            "#bf360c",
        ),
        "低风险": (
            "🔵",
            "linear-gradient(135deg,#e3f2fd 0%,#f0f8ff 100%)",
            "#90caf9",
            "#1565c0",
            "#0d47a1",
        ),
    }

    applied_risks = applied_risks or set()
    cards = []
    
    # risks_with_idx -> list of (actual_global_idx, risk_dict)
    for dom_idx, (actual_idx, risk) in enumerate(risks_with_idx):
        level = risk.get("level", "低风险")
        dim = risk.get("dimension") or "风险防控"

        icon, card_bg, card_border, left_color, title_color = level_styles.get(
            level, level_styles["低风险"]
        )
        dim_fg, dim_bg = dim_colors.get(dim, ("#546e7a", "rgba(84,110,122,0.12)"))

        issue_text = risk.get("issue") or ""
        issue_snippet = issue_text[:160] + ("…" if len(issue_text) > 160 else "")

        suggestion = (risk.get("suggestion") or "").strip()
        legal_basis = (risk.get("legal_basis") or "").strip()

        has_sugg = bool(suggestion)
        is_applied = (actual_idx in applied_risks)

        loc_js = (
            f"try{{var el=window.parent.document.getElementById('risk-anchor-{actual_idx}');"
            f"if(el)el.scrollIntoView({{behavior:'smooth',block:'center'}});}}catch(e){{}}"
        )

        # 修改建议（折叠）
        suggestion_html = ""
        if suggestion:
            sugg_esc = html.escape(suggestion)
            suggestion_html = (
                f'<details style="margin-top:8px;">'
                f'<summary style="font-size:0.8rem;color:{left_color};cursor:pointer;'
                f'font-weight:600;list-style:none;display:flex;align-items:center;gap:4px;">'
                f'<span>✏️ 修改建议</span></summary>'
                f'<div style="margin-top:6px;padding:8px 10px;border-radius:6px;'
                f'background:rgba(255,255,255,0.85);border-left:3px solid {left_color};'
                f'font-size:0.82rem;line-height:1.5;color:#263238;">{sugg_esc}</div>'
                f'</details>'
            )

        # 法律依据
        legal_html = ""
        if legal_basis and legal_basis != "暂无明确法条依据":
            legal_esc = html.escape(legal_basis)
            legal_html = (
                f'<div style="margin-top:6px;padding:4px 8px;border-radius:4px;'
                f'background:rgba(33,33,33,0.05);font-size:0.78rem;color:#546e7a;'
                f'display:flex;align-items:flex-start;gap:4px;">'
                f'<span style="flex-shrink:0;">⚖️</span><span>{legal_esc}</span></div>'
            )

        cards.append(
            f'<div class="risk-card" draggable="true" data-risk-index="{actual_idx}" '
            f'ondragstart="event.dataTransfer.setData(\'text/plain\',\'{actual_idx}\');" '
            f'style="margin-bottom:12px;padding:10px 12px;border-radius:10px;'
            f'border:1px solid {card_border};border-left:4px solid {left_color};'
            f'background:{card_bg};cursor:grab;">'
            # 标题行：等级（突出）+ 维度徽章
            f'<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:6px;">'
            f'<span style="font-weight:700;color:{title_color};font-size:0.93rem;">'
            f'{icon} 风险点 {actual_idx + 1} · {html.escape(level)}</span>'
            f'<span style="font-size:0.75rem;padding:2px 8px;border-radius:6px;'
            f'background:{dim_bg};color:{dim_fg};border:1px solid {dim_fg};">'
            f'{html.escape(dim)}</span>'
            f'</div>'
            f'<p style="margin:0;font-size:0.85rem;line-height:1.45;color:#37474f;">'
            f'{html.escape(issue_snippet)}</p>'
            f'{suggestion_html}'
            f'{legal_html}'
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">'
        )
        
        # 应用按钮 (Only if suggestion exists)
        if has_sugg:
            if is_applied:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-size:0.8rem;padding:4px 10px;border-radius:6px;'
                    f'border:1px solid #4caf50;background:#e8f5e9;color:#2e7d32;cursor:pointer;font-weight:600;">'
                    f'❌ 撤销应用</button>'
                )
            else:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-size:0.8rem;padding:4px 10px;border-radius:6px;'
                    f'border:1px solid #4caf50;background:#4caf50;color:#fff;cursor:pointer;font-weight:600;">'
                    f'✔️ 应用修改</button>'
                )

        cards.append(
            f'<button type="button" class="pick-btn" data-idx="{dom_idx}" '
            f'style="font-size:0.8rem;padding:4px 10px;border-radius:6px;'
            f'border:1px solid {left_color};background:#fff;color:{left_color};cursor:pointer;">'
            f'深入追问</button>'
            f'<button type="button" onclick="{loc_js}" '
            f'style="font-size:0.8rem;padding:4px 10px;border-radius:6px;'
            f'border:1px solid #78909c;background:#fff;color:#546e7a;cursor:pointer;">'
            f'定位原文</button>'
            f'</div></div>'
        )

    return "\n".join(cards)
