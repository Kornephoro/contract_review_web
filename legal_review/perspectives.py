import streamlit as st
import html
from typing import List, Dict

def render_risk_centric_view(risks: List[Dict], theme_key: str):
    """当前主视角的简要汇总，可作为补充"""
    # 也可以复用原有的过滤排版逻辑
    st.markdown("**(主视图位于左侧卡片栏，本视图展示风险分布热力汇总)**")
    
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid = sum(1 for r in risks if r.get("level") == "中风险")
    low = sum(1 for r in risks if r.get("level") == "低风险")
    
    st.markdown(f"**总量:** {len(risks)} 项 / **高危:** {high} / **中危:** {mid} / **低危:** {low}")
    
    dims = {}
    for r in risks:
        d = r.get("dimension", "未知维度")
        dims[d] = dims.get(d, 0) + 1
        
    for k, v in dims.items():
        st.caption(f"- {k}: {v} 项")
        
    st.progress(high / max(1, len(risks)) if len(risks) > 0 else 0, text="高风险占比")

def render_clause_centric_view(risks: List[Dict], theme_key: str):
    """条款视角：以列表形式展示所有条款问题，淡化等级，强调修改。"""
    st.markdown("#### 📜 条款修订对照表")
    if not risks:
        st.success("无需要修订的条款。")
        return
        
    for i, r in enumerate(risks):
        orig = r.get("original", "")
        sugg = r.get("suggestion", "")
        if not sugg or not orig:
            continue
            
        st.markdown(f"**原文 {i+1}：** {html.escape(orig[:60])}..." if len(orig) > 60 else f"**原文 {i+1}：** {orig}")
        st.markdown(f"> **修改建议：** {sugg}")
        st.divider()

def render_party_centric_view(overview: Dict, risks: List[Dict], theme_key: str):
    """当事方视角：根据原文和描述推测对各方的影响（启发式）。"""
    st.markdown("#### 👥 履约方影响度分析")
    parties = overview.get("parties", [])
    if not parties:
        st.info("未能解析出明确的参与方信息。")
        return
        
    st.markdown("系统基于风险识别，为您估算以下主体在该合同中的潜在影响：")
    for party in parties:
        with st.expander(f"主体：{party}", expanded=True):
            # 简单的启发式匹配：如果风险涉及该主体名称，则列出
            party_risks = []
            for r in risks:
                issue = r.get("issue", "")
                if party in issue or ("甲方" in party and "甲方" in issue) or ("乙方" in party and "乙方" in issue):
                    party_risks.append(r)
            
            if party_risks:
                st.warning(f"⚠️ 识别到 {len(party_risks)} 项与该方高度相关的利益/义务风险：")
                for pr in party_risks:
                    st.markdown(f"- **{pr.get('level')}**: {pr.get('issue')}")
            else:
                st.success("暂未在明确的风险描述中直接匹配到该主体的异常责任。")
