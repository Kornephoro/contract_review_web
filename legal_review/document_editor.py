import io
import datetime
import streamlit as st

try:
    import docx
except ImportError:
    pass

def apply_changes_to_docx(original_bytes: bytes, modifications: list) -> io.BytesIO:
    """
    修改内存中的 docx 文件并在原格式下返回新的二进制流。
    """
    doc = docx.Document(io.BytesIO(original_bytes))
    from legal_review.text_matcher import find_best_paragraph_for_docx, find_best_text_span
    
    # 收集所有的段落对象（包括表格中的段落）
    all_paras = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                all_paras.extend(cell.paragraphs)

    for mod in modifications:
        orig_text = (mod.get("original") or "").strip()
        sugg_text = (mod.get("suggestion") or "").strip()
        if not orig_text or not sugg_text:
            continue
            
        best_para = find_best_paragraph_for_docx(all_paras, orig_text)
        if best_para:
            # 找到最佳段落后，再使用模糊匹配找出它在该段落字符串里的位置
            start_in_para, end_in_para = find_best_text_span(best_para.text, orig_text)
            if start_in_para != -1 and end_in_para != -1:
                # 只替换匹配到的那部分文字，保留段落开头或结尾无关内容
                new_text = best_para.text[:start_in_para] + sugg_text + best_para.text[end_in_para:]
                
                if best_para.runs:
                    for r in best_para.runs:
                        r.text = ""
                    best_para.runs[0].text = new_text
                else:
                    best_para.add_run(new_text)

    out_stream = io.BytesIO()
    doc.save(out_stream)
    out_stream.seek(0)
    return out_stream

def execute_export_pipeline():
    """
    在主界面中渲染导出操作面板。
    """
    st.markdown("### 💾 导出修改后的合同文件")
    
    snap = st.session_state.get("review_snapshot")
    applied_idx = st.session_state.get("applied_risks", set())
    
    if not snap or not snap.get("risks"):
        st.error("没有可供导出的审查数据。")
        if st.button("关闭面板"):
            st.session_state["show_export_dialog"] = False
            st.rerun()
        return

    modifications = [
        snap["risks"][i] for i in applied_idx if i < len(snap["risks"])
    ]
    
    if not modifications:
        st.info("⚠️ 您尚未在左侧或弹窗应用任何修改建议。导出的文件将与原文一致。")
    else:
        st.success(f"准备导出：已经包含 {len(modifications)} 处您确认应用的专业修改！")

    orig_bytes = st.session_state.get("original_file_bytes")
    orig_name = st.session_state.get("original_file_name") or "contract"
    
    now_str = datetime.datetime.now().strftime('%Y%m%d_%H%M')
    is_docx = orig_name.lower().endswith(".docx")
    
    if orig_bytes and is_docx:
        st.success("✅ 检测到了原始的 Word (.docx) 文档格式！我们将执行格式无损级别的底层内容替换。")
        try:
            out_bytesIO = apply_changes_to_docx(orig_bytes, modifications)
            col1, col2 = st.columns(2)
            with col1:
                st.download_button(
                    "⬇️ 点击下载 原格式修改版 (.docx)",
                    data=out_bytesIO.getvalue(),
                    file_name=f"已修订_{orig_name}",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    type="primary",
                    use_container_width=True
                )
            with col2:
                if st.button("关闭面板", use_container_width=True):
                    st.session_state["show_export_dialog"] = False
                    st.rerun()
        except Exception as e:
            st.error(f"处理 docx 文件时内部发生错误: {e}")
            if st.button("关闭面板"):
                st.session_state["show_export_dialog"] = False
                st.rerun()
    else:
        st.warning("⚠️ 由于原始文件不是纯正的 Word (.docx) 或者您是直接复制粘贴的纯文本（比如 PDF 等格式固定排版），程序将通过应用补丁为您打包一份标准的基础文档。")
        
        # 纯文本模式：手动替换 patch
        final_text = snap.get("text", "")
        for mod in modifications:
            o = (mod.get("original") or "").strip()
            s = (mod.get("suggestion") or "").strip()
            if o and s:
                final_text = final_text.replace(o, s)
                
        # 提供 TXT 与普通生成的 DOCX 下载
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.download_button(
                "⬇️ 下载为普通文本 (.txt)",
                data=final_text.encode("utf-8"),
                file_name=f"修订_合同_{now_str}.txt",
                mime="text/plain",
                use_container_width=True
            )
        with col_b:
            try:
                doc = docx.Document()
                for line in final_text.split("\n"):
                    doc.add_paragraph(line)
                bf = io.BytesIO()
                doc.save(bf)
                st.download_button(
                    "⬇️ 下载为普通版Word (.docx)",
                    data=bf.getvalue(),
                    file_name=f"修订_无格式版本_{now_str}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    type="primary",
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"生成 Word 备选时失败: {e}")
                
        with col_c:
            if st.button("关闭面板", use_container_width=True):
                st.session_state["show_export_dialog"] = False
                st.rerun()

    st.markdown("---")
