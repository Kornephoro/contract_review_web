import re
import difflib

def clean_whitespaces(text: str) -> str:
    """去除所有不可见字符"""
    return "".join(c for c in text if not c.isspace())

def clean_punctuation(text: str) -> str:
    """仅保留字母、数字、汉字（CJK），极致剥除标点"""
    # \u4e00-\u9fa5 covers basic CJK. We also include a-zA-Z0-9.
    # We will just strip out anything that isn't word characters and isn't CJK.
    return re.sub(r'[^\w\u4e00-\u9fa5]', '', text)

def _jaccard_similarity(s1: set, s2: set) -> float:
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    return len(s1.intersection(s2)) / len(s1.union(s2))

def find_best_text_span(full_text: str, query: str) -> tuple[int, int]:
    """
    多层级模糊搜索算法，确保 AI 提出的 original snippet 无论如何残缺，
    都能在全文本 full_text 中找到对应的起始和结束位置 (相对于原始字符串的 index)。
    如果极度没找到，将会找出相似度最高的自然段。
    返回: (start_idx, end_idx)
    """
    query = query.strip()
    if not query:
        return -1, -1
        
    # 构建原文本与位置的映射，以便清洗后能找回真实坐标
    clean_ws = []
    ws_mapping = []
    for i, char in enumerate(full_text):
        if not char.isspace():
            clean_ws.append(char)
            ws_mapping.append(i)
    clean_ws_str = "".join(clean_ws)
    
    clean_q_ws = clean_whitespaces(query)
    
    # 策略 1: 完全的字符匹配 (无视空格换行)
    pos = clean_ws_str.find(clean_q_ws)
    if pos != -1:
        return ws_mapping[pos], ws_mapping[pos + len(clean_q_ws) - 1] + 1
        
    # 策略 2: 忽视标点符号的匹配
    # 建表：仅包含 alphanumeric + CJK
    clean_punc = []
    punc_mapping = []
    for i, char in enumerate(full_text):
        if re.match(r'[\w\u4e00-\u9fa5]', char):
            clean_punc.append(char)
            punc_mapping.append(i)
    clean_punc_str = "".join(clean_punc)
    
    clean_q_punc = clean_punctuation(query)
    if clean_q_punc:
        pos2 = clean_punc_str.find(clean_q_punc)
        if pos2 != -1:
            return punc_mapping[pos2], punc_mapping[pos2 + len(clean_q_punc) - 1] + 1
            
    # 策略 3: 头尾包抄 (解决省略号截断或中间幻觉)
    # 取 query 最有代表性的首部和尾部各 10~15 个有效字符
    if len(clean_q_punc) > 20:
        prefix = clean_q_punc[:10]
        suffix = clean_q_punc[-10:]
        
        pos_prefix = clean_punc_str.find(prefix)
        # 从 prefix 之后找 suffix
        if pos_prefix != -1:
            pos_suffix = clean_punc_str.find(suffix, pos_prefix + 10)
            if pos_suffix != -1 and (pos_suffix - pos_prefix) < 1500: # 限制跨度合理
                # 找到了一个合理的包围圈
                start_real = punc_mapping[pos_prefix]
                end_real = punc_mapping[pos_suffix + len(suffix) - 1] + 1
                return start_real, end_real
                
    # 策略 4: Jaccard Semantic 段落兜底
    # 把文本按照换行符拆分成段落
    paragraphs = []
    start = 0
    for line in full_text.split("\n"):
        end = start + len(line)
        if line.strip():
            paragraphs.append({
                "start": start,
                "end": end,
                "text": line,
                "set": set(clean_punctuation(line))
            })
        start = end + 1 # +1 for \n
        
    query_set = set(clean_q_punc)
    
    best_iou = -1.0
    best_para = None
    
    for p in paragraphs:
        if not p["set"]: continue
        iou = _jaccard_similarity(query_set, p["set"])
        # 同时考虑覆盖度: AI 返回的字，在这个段落里出现了多少？
        coverage = len(query_set.intersection(p["set"])) / max(1, len(query_set))
        
        score = iou * 0.4 + coverage * 0.6
        if score > best_iou:
            best_iou = score
            best_para = p
            
    if best_para and best_iou > 0.1: # 至少有一丁点相似
        # 直接返回整个自然段作为高亮/挂载区域
        return best_para["start"], best_para["end"]
        
    # 如果真的什么都找不到（不可能发生的极端情况），返回开头一点点作为保底
    return 0, min(10, len(full_text))

def find_best_paragraph_for_docx(paragraphs, query: str):
    """
    专门为 docx.Document(x).paragraphs 提供的方法。
    找出这堆对象中，最匹配 query 的那一段对象。
    """
    query_punc = clean_punctuation(query)
    query_set = set(query_punc)
    
    if not query_punc:
        return None
        
    best_score = -1.0
    best_para = None
    
    for para in paragraphs:
        if not para.text.strip():
            continue
            
        p_clean = clean_punctuation(para.text)
        if not p_clean: continue
        
        # Exact substring check after clean
        if query_punc in p_clean:
            return para # 100% 确定是它
            
        # Punctuation stripped head-tail enclosing
        if len(query_punc) > 20:
            prefix = query_punc[:10]
            suffix = query_punc[-10:]
            if prefix in p_clean and suffix in p_clean:
                if p_clean.find(prefix) < p_clean.find(suffix):
                    return para # 极大概率是它

        p_set = set(p_clean)
        iou = _jaccard_similarity(query_set, p_set)
        coverage = len(query_set.intersection(p_set)) / max(1, len(query_set))
        score = iou * 0.4 + coverage * 0.6
        
        if score > best_score:
            best_score = score
            best_para = para
            
    if best_score > 0.15:
        return best_para
    return None
