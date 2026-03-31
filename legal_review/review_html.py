"""审查结果区：可拖拽风险卡片 + 追问投放区（iframe 内，通过 URL 参数与 Streamlit 同步）。"""

from __future__ import annotations

import html


def build_risk_deck_html(risks_with_idx: list, _theme_key: str, applied_risks: set = None) -> str:
    """
    左：风险卡片（可拖拽、可点击选为追问）。
    「墨律」editorial design — premium warm aesthetic with serif titles,
    colored dot indicators, and refined dimension badges.
    """
    # -- 墨律 Design Tokens --------------------------------------------------
    is_dark = _theme_key == "dark"
    FONT_BODY = '"Outfit","Noto Sans SC",sans-serif'
    FONT_TITLE = '"Cormorant Garamond","Noto Serif SC",serif'

    # 维度徽章配色 (fg, bg at ~0.08 opacity tint)
    if is_dark:
        dim_colors = {
            "法律合规": ("#8ab0d8", "rgba(90,130,190,0.12)"),
            "风险防控": ("#e89490", "rgba(180,70,65,0.12)"),
            "条款完善": ("#e0c470", "rgba(180,140,50,0.12)"),
            "利益保护": ("#7dc09a", "rgba(70,150,90,0.12)"),
        }
        # (dot_color, card_bg, card_border, left_color, title_color)
        level_styles = {
            "高风险": ("#e89490", "#2a2025", "#5a3535", "#e89490", "#e89490"),
            "中风险": ("#e0c470", "#28251a", "#4a4025", "#e0c470", "#e0c470"),
            "低风险": ("#8ab0d8", "#1e2530", "#35455a", "#8ab0d8", "#8ab0d8"),
        }
        BODY_FG = "#d0ccc6"
        MUTED_FG = "#8a857f"
    else:
        dim_colors = {
            "法律合规": ("#2c4a6e", "rgba(44,74,110,0.08)"),
            "风险防控": ("#8b3535", "rgba(139,53,53,0.08)"),
            "条款完善": ("#8b6a25", "rgba(139,106,37,0.08)"),
            "利益保护": ("#2e6b45", "rgba(46,107,69,0.08)"),
        }
        level_styles = {
            "高风险": ("#c4655e", "#fffaf7", "#ead4ce", "#bf5d54", "#7a2d29"),
            "中风险": ("#d4a84a", "#fffaf3", "#ead9b8", "#b88731", "#71551d"),
            "低风险": ("#7a9ec4", "#f8fbff", "#d7e2ee", "#6282a7", "#35567d"),
        }
        BODY_FG = "#2d2d2d"
        MUTED_FG = "#7a7672"

    # Accent gold for applied-state buttons
    ACCENT_GOLD = "#d4b070" if is_dark else "#b8945f"

    applied_risks = applied_risks or set()
    cards = []

    # risks_with_idx -> list of (actual_global_idx, risk_dict)
    for dom_idx, (actual_idx, risk) in enumerate(risks_with_idx):
        level = risk.get("level", "低风险")
        dim = risk.get("dimension") or "风险防控"

        dot_color, card_bg, card_border, left_color, title_color = level_styles.get(
            level, level_styles["低风险"]
        )
        dim_fg, dim_bg = dim_colors.get(dim, ("#546e7a", "rgba(84,110,122,0.08)"))

        issue_text = risk.get("issue") or ""
        issue_snippet = issue_text[:160] + ("\u2026" if len(issue_text) > 160 else "")

        suggestion = (risk.get("suggestion_display") or risk.get("suggestion") or "").strip()
        suggestion_actionable = risk.get("suggestion_actionable")
        if suggestion_actionable is None:
            suggestion_actionable = bool((risk.get("suggestion") or "").strip())
        else:
            suggestion_actionable = bool(suggestion_actionable) and bool((risk.get("suggestion") or "").strip())
        suggestion_warning = (risk.get("suggestion_warning") or "").strip()
        legal_basis = (risk.get("legal_basis") or "").strip()

        has_sugg = bool(suggestion)
        is_applied = (actual_idx in applied_risks)

        loc_js = (
            f"try{{var el=window.parent.document.getElementById('risk-anchor-{actual_idx}');"
            f"if(el)el.scrollIntoView({{behavior:'smooth',block:'center'}});}}catch(e){{}}"
        )

        # -- Colored dot indicator (replaces emoji circles) -------------------
        dot_html = (
            f'<span style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:{dot_color};flex-shrink:0;'
            f'margin-top:2px;"></span>'
        )

        # -- 修改建议（折叠） -------------------------------------------------
        suggestion_html = ""
        if suggestion:
            sugg_esc = html.escape(suggestion)
            suggestion_html = (
                f'<details style="margin-top:10px;">'
                f'<summary style="font-family:{FONT_BODY};font-size:0.8rem;'
                f'color:{left_color};cursor:pointer;font-weight:600;'
                f'list-style:none;display:flex;align-items:center;gap:5px;'
                f'letter-spacing:0.02em;">'
                f'<span style="font-size:0.85rem;line-height:1;">&#9998;</span>'
                f'<span>修改建议</span></summary>'
                f'<div style="margin-top:8px;padding:10px 14px;border-radius:6px;'
                f'background:{"rgba(255,255,255,0.05)" if is_dark else "rgba(255,255,255,0.7)"};'
                f'border-left:3px solid {ACCENT_GOLD};'
                f'font-family:{FONT_BODY};font-size:0.82rem;'
                f'line-height:1.6;color:{BODY_FG};'
                f'letter-spacing:0.01em;">{sugg_esc}</div>'
                f'</details>'
            )
        if suggestion_warning:
            suggestion_html += (
                f'<div style="margin-top:8px;padding:6px 10px;border-radius:6px;'
                f'background:{"rgba(255,255,255,0.04)" if is_dark else "rgba(0,0,0,0.025)"};'
                f'font-family:{FONT_BODY};font-size:0.76rem;color:{MUTED_FG};line-height:1.5;">'
                f'{html.escape(suggestion_warning)}</div>'
            )

        # -- 法律依据 ---------------------------------------------------------
        legal_html = ""
        if legal_basis and legal_basis != "暂无明确法条依据":
            legal_esc = html.escape(legal_basis)
            legal_html = (
                f'<div style="margin-top:8px;padding:5px 10px;border-radius:5px;'
                f'background:{"rgba(255,255,255,0.04)" if is_dark else "rgba(0,0,0,0.025)"};'
                f'font-family:{FONT_BODY};font-size:0.76rem;color:{MUTED_FG};'
                f'line-height:1.5;letter-spacing:0.01em;'
                f'display:flex;align-items:flex-start;gap:5px;">'
                f'<span style="flex-shrink:0;color:{MUTED_FG};font-size:0.78rem;">\u00a7</span>'
                f'<span>{legal_esc}</span></div>'
            )

        # -- Card shell -------------------------------------------------------
        cards.append(
            f'<div class="risk-card" draggable="true" data-risk-index="{actual_idx}" '
            f'ondragstart="event.dataTransfer.setData(\'text/plain\',\'{actual_idx}\');" '
            f'style="margin-bottom:12px;padding:16px 18px;border-radius:16px;'
            f'border:1px solid {card_border};border-left:3px solid {left_color};'
            f'background:{card_bg};cursor:grab;box-shadow:0 10px 22px rgba(26,31,46,0.04);'
            f'font-family:{FONT_BODY};'
            f'transition:box-shadow 0.15s ease;">'

            # -- Title row: dot + title + dimension badge --------------------
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;'
            f'align-items:center;margin-bottom:8px;">'
            f'{dot_html}'
            f'<span style="font-family:{FONT_TITLE};font-weight:700;'
            f'color:{title_color};font-size:0.95rem;letter-spacing:0.02em;">'
            f'\u98ce\u9669\u70b9 {actual_idx + 1} \u00b7 {html.escape(level)}</span>'
            f'<span style="font-family:{FONT_BODY};font-size:0.72rem;'
            f'padding:2px 9px;border-radius:4px;'
            f'background:{dim_bg};color:{dim_fg};'
            f'font-weight:500;letter-spacing:0.03em;">'
            f'{html.escape(dim)}</span>'
            f'</div>'

            # -- Issue body text ---------------------------------------------
            f'<p style="margin:0;font-family:{FONT_BODY};font-size:0.84rem;'
            f'line-height:1.55;color:{BODY_FG};letter-spacing:0.01em;">'
            f'{html.escape(issue_snippet)}</p>'

            f'{suggestion_html}'
            f'{legal_html}'

            # -- Button row --------------------------------------------------
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;">'
        )

        # -- 应用 / 撤销 button (only if suggestion exists) -------------------
        if suggestion_actionable:
            if is_applied:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                    f'padding:5px 14px;border-radius:6px;'
                    f'border:1px solid {ACCENT_GOLD};background:rgba(184,148,95,0.1);'
                    f'color:{ACCENT_GOLD};cursor:pointer;font-weight:600;'
                    f'letter-spacing:0.02em;transition:all 0.15s ease;">'
                    f'\u64a4\u9500</button>'
                )
            else:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                    f'padding:5px 14px;border-radius:6px;'
                    f'border:1px solid {ACCENT_GOLD};background:{ACCENT_GOLD};'
                    f'color:#fff;cursor:pointer;font-weight:600;'
                    f'letter-spacing:0.02em;transition:all 0.15s ease;">'
                    f'\u5e94\u7528</button>'
                )
        elif has_sugg:
            cards.append(
                f'<button type="button" disabled '
                f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                f'padding:5px 14px;border-radius:6px;'
                f'border:1px solid {"#4a4640" if is_dark else "#c5c0b8"};background:transparent;'
                f'color:{MUTED_FG};cursor:not-allowed;font-weight:500;letter-spacing:0.02em;">'
                f'\u4e0d\u53ef\u76f4\u63a5\u5e94\u7528</button>'
            )

        # -- 深入追问 & 定位原文 buttons --------------------------------------
        cards.append(
            f'<button type="button" class="pick-btn" data-idx="{dom_idx}" '
            f'style="font-family:{FONT_BODY};font-size:0.8rem;'
            f'padding:5px 14px;border-radius:6px;'
            f'border:1px solid {left_color};background:transparent;'
            f'color:{left_color};cursor:pointer;font-weight:500;'
            f'letter-spacing:0.02em;transition:all 0.15s ease;">'
            f'\u6df1\u5165\u8ffd\u95ee</button>'
            f'<button type="button" onclick="{loc_js}" '
            f'style="font-family:{FONT_BODY};font-size:0.8rem;'
            f'padding:5px 14px;border-radius:6px;'
            f'border:1px solid {"#4a4640" if is_dark else "#c5c0b8"};background:transparent;'
            f'color:{MUTED_FG};cursor:pointer;font-weight:500;'
            f'letter-spacing:0.02em;transition:all 0.15s ease;">'
            f'\u5b9a\u4f4d\u539f\u6587</button>'
            f'</div></div>'
        )

    return "\n".join(cards)
