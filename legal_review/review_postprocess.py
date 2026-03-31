from __future__ import annotations

import re
from typing import Iterable


SENTENCE_END_RE = re.compile(r"[。；！？\n]")
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
META_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:应|应该|应当|必须|需|需要)(?:尽快|及时)?(?:明确|补充|增加|删除|修改|修正|约定|写明|注明|核实|完善|补全|改为|改成|调整|细化)"
    r"|(?:建议|可|可以)(?:改为|修改为|表述为|写为|调整为|补充为|改成)"
    r"|(?:修改建议|建议修改|建议补充|建议增加|建议删除|建议明确|建议写明|示例|例如|比如)"
    r"|请(?:补充|增加|删除|修改|明确|写明|约定|核实|修正)"
    r")"
)
META_MARKER_RE = re.compile(
    r"(?:可改为|修改为|建议修改为|建议改为|可表述为|表述为|可写为|建议如下|修改建议如下|例如|示例|比如)[:：]\s*(.+)",
    re.S,
)
QUOTE_RE = re.compile(r"[“\"「『](.+?)[”\"」』]", re.S)


def _normalize_text(value: str) -> str:
    value = ZERO_WIDTH_RE.sub("", value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.strip()


def _find_in_document(doc_text: str, target: str) -> tuple[str, str, int] | None:
    if not doc_text or not target:
        return None

    raw_idx = doc_text.find(target)
    if raw_idx != -1:
        return doc_text, target, raw_idx

    norm_doc = ZERO_WIDTH_RE.sub("", doc_text)
    norm_target = ZERO_WIDTH_RE.sub("", target)
    norm_idx = norm_doc.find(norm_target)
    if norm_idx == -1:
        return None
    return norm_doc, norm_target, norm_idx


def _extend_original_to_sentence_boundary(original: str, doc_text: str) -> tuple[str, str]:
    original = _normalize_text(original)
    if not original or SENTENCE_END_RE.search(original[-1:]):
        return original, ""

    located = _find_in_document(doc_text, original)
    if not located:
        return original, ""

    searchable_doc, searchable_target, start_idx = located
    after_start = start_idx + len(searchable_target)
    after_slice = searchable_doc[after_start : after_start + 80]
    end_match = SENTENCE_END_RE.search(after_slice)
    if not end_match:
        return original, ""

    extension = after_slice[: end_match.start() + 1]
    if not extension.strip():
        return original, ""

    return original + extension, extension[-1]


def _strip_wrapping_quotes(text: str) -> str:
    text = _normalize_text(text)
    if len(text) >= 2 and text[0] in '“"「『' and text[-1] in '”"」』':
        return text[1:-1].strip()
    return text


def _extract_quoted_candidate(text: str) -> str:
    matches = [match.group(1).strip() for match in QUOTE_RE.finditer(text or "")]
    if not matches:
        return ""
    matches.sort(key=len, reverse=True)
    return _strip_wrapping_quotes(matches[0])


def _extract_candidate_after_marker(text: str) -> str:
    match = META_MARKER_RE.search(text or "")
    if not match:
        return ""

    tail = _normalize_text(match.group(1))
    quoted = _extract_quoted_candidate(tail)
    if quoted:
        return quoted
    return _strip_wrapping_quotes(tail)


def _looks_like_instruction(text: str) -> bool:
    text = _normalize_text(text)
    if not text:
        return False
    if META_PREFIX_RE.match(text):
        return True

    marker = META_MARKER_RE.search(text)
    if marker and marker.start() <= max(12, len(text) // 3):
        return True

    return False


def _append_sentence_punctuation(text: str, punct: str) -> str:
    text = _normalize_text(text)
    if not text or not punct:
        return text
    if SENTENCE_END_RE.search(text[-1:]):
        return text
    return text + punct


def _build_processed_risk(risk: dict, doc_text: str) -> dict:
    processed = dict(risk or {})

    original = _normalize_text(str(processed.get("original") or ""))
    suggestion_raw = _normalize_text(str(processed.get("suggestion") or ""))
    suggestion_warning = ""

    expanded_original, end_punct = _extend_original_to_sentence_boundary(original, doc_text)
    processed["original"] = expanded_original or original

    actionable_text = suggestion_raw
    if _looks_like_instruction(suggestion_raw):
        extracted = _extract_candidate_after_marker(suggestion_raw) or _extract_quoted_candidate(suggestion_raw)
        if extracted and not _looks_like_instruction(extracted):
            actionable_text = extracted
            suggestion_warning = "AI 给出的是说明性意见，系统已自动提取其中的示例条款作为可替换文本。"
        else:
            actionable_text = ""
            suggestion_warning = "该修改建议属于说明性审稿意见，不是可直接替换原文的合同条款，已禁止直接应用。"

    actionable_text = _append_sentence_punctuation(actionable_text, end_punct)

    processed["suggestion"] = actionable_text
    processed["suggestion_display"] = actionable_text or suggestion_raw
    processed["suggestion_actionable"] = bool(actionable_text)
    if suggestion_warning:
        processed["suggestion_warning"] = suggestion_warning
    elif "suggestion_warning" in processed:
        processed.pop("suggestion_warning", None)

    return processed


def postprocess_review_risks(risks: Iterable[dict], doc_text: str) -> list[dict]:
    return [_build_processed_risk(risk, doc_text) for risk in (risks or [])]


def get_actionable_risk_indices(risks: Iterable[dict]) -> list[int]:
    actionable: list[int] = []
    for idx, risk in enumerate(risks or []):
        risk = risk or {}
        suggestion = _normalize_text(str(risk.get("suggestion") or ""))
        suggestion_actionable = risk.get("suggestion_actionable")
        if suggestion_actionable is None:
            suggestion_actionable = bool(suggestion)
        if bool(suggestion_actionable) and suggestion:
            actionable.append(idx)
    return actionable
