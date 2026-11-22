"""规则化文本提取。

所有提取规则都是确定性的、保守的：
- 只识别明确出现的形态（“X称”“听Y说”“引号”“证据N号 / E-17”）；
- 不做任何法律层面的推断（不判断真假、不判断采信）；
- 识别不出的部分交给缺口机制呈现，而不是静默忽略。
"""

from __future__ import annotations

import re
from typing import Optional

from .models import ClaimDraft, CorrectionDraft

# ---- 发言人识别 ----
_ROLE_WORDS = (
    "审判长", "审判员", "公诉人", "辩护人", "代理人",
    "证人", "原告", "被告", "第三人", "鉴定人", "书记员",
)
_SPEAKER_RE = re.compile(
    r"^\s*(?P<role>" + "|".join(_ROLE_WORDS) + r")?"
    r"(?P<who>[一-鿿]{1,4}?)(?:称|说道|说|述)[，,：:]"
)
_ROLE_ONLY_RE = re.compile(
    r"^\s*(?P<role>" + "|".join(_ROLE_WORDS) + r")(?:主张|提出|认为|询问|宣布|出示)"
)


def detect_speaker(text: str) -> tuple[Optional[str], Optional[str]]:
    """从句首识别发言人简称与角色提示。识别不出返回 (None, None)。"""
    m = _SPEAKER_RE.match(text)
    if m:
        who = m.group("who")
        role = m.group("role")
        if who and who not in _ROLE_WORDS:
            return who, role
        if role:
            return role, role
    m = _ROLE_ONLY_RE.match(text)
    if m:
        return m.group("role"), m.group("role")
    return None, None


# ---- 转述与逐字引语 ----
_REPORTED_RE = re.compile(
    r"(?:据|听|向)(?P<who>[一-鿿]{1,6}?)(?:说|称|讲|表示|转述)"
)
_SAID_RE = re.compile(r"(?P<who>[一-鿿]{2,4})(?:说|称|表示)[：:]")
_QUOTE_RE = re.compile(r"[“「](?P<q>[^”」]+)[”」]")


def extract_claims(text: str) -> list[ClaimDraft]:
    """把一条陈述类文本拆成主张草稿。

    - 整句构成一条“直接陈述”或“转述”（出现“听/据某说”时）；
    - 句内引号内容另记为“逐字引语”，归属同一被引述者（若能识别）。
    """
    drafts: list[ClaimDraft] = []
    reported = _REPORTED_RE.search(text) or _SAID_RE.search(text)
    who = reported.group("who") if reported else None
    modality = "转述" if who else "直接陈述"
    drafts.append(ClaimDraft(modality=modality, content=text.strip(), quoted_label=who))
    for quote in _QUOTE_RE.findall(text):
        quote = quote.strip()
        if quote:
            drafts.append(
                ClaimDraft(modality="逐字引语", content=quote, quoted_label=who)
            )
    return drafts


# ---- 证据编号 ----
_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_EV1 = re.compile(r"证据\s*第?\s*(?P<no>[0-9一二三四五六七八九十]+|[A-Za-z]-?[0-9]+)\s*号")
_EV2 = re.compile(r"(?<![A-Za-z0-9])(?P<no>[A-Za-z]-[0-9]+)(?![0-9])")
_EV3 = re.compile(r"(?P<no>[0-9一二三四五六七八九十]+)\s*号\s*证据")


def normalize_evidence_no(raw: str) -> str:
    raw = raw.strip().upper().replace(" ", "")
    if raw in _CN_DIGITS:
        return str(_CN_DIGITS[raw])
    if re.fullmatch(r"[一二三四五六七八九十]+", raw) and len(raw) == 1:
        return str(_CN_DIGITS[raw])
    return raw


def extract_evidence_nos(text: str) -> list[str]:
    """按出现顺序提取证据编号并去重。提及不代表采信。"""
    found: list[str] = []
    for pattern in (_EV1, _EV2, _EV3):
        for m in pattern.finditer(text):
            no = normalize_evidence_no(m.group("no"))
            if no and no not in found:
                found.append(no)
    return found


# ---- 程序动作 ----
_PROCEDURAL_RULES = (
    ("证据采信记录", ("采信",)),
    ("出示证据", ("出示", "举证")),
    ("休庭", ("休庭",)),
    ("核对身份", ("核对",)),
    ("开庭", ("开庭",)),
)


def classify_procedural(text: str) -> tuple[str, Optional[str]]:
    for action, keywords in _PROCEDURAL_RULES:
        if any(k in text for k in keywords):
            nos = extract_evidence_nos(text)
            return action, (nos[0] if nos else None)
    return "其他程序", None


# ---- 书记员更正指令 ----
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _ordinal(raw: str) -> Optional[int]:
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw)


_CORR_QUOTED_KEY = re.compile(
    r"更正[：:]\s*(?P<key>[A-Za-z0-9_-]+)\s*被引述者为(?P<new>[^，,。；;]+)[，,；;]\s*非(?P<old>[^。；;]+)"
)
_CORR_QUOTED_ORD = re.compile(
    r"更正[：:]\s*第(?P<n>[0-9一二三四五六七八九十]+)段被引述者为(?P<new>[^，,。；;]+)[，,；;]\s*非(?P<old>[^。；;]+)"
)
_CORR_SPEAKER_KEY = re.compile(
    r"更正[：:]\s*(?P<key>[A-Za-z0-9_-]+)\s*发言人为(?P<new>[^，,。；;]+)[，,；;]\s*非(?P<old>[^。；;]+)"
)
_CORR_SPEAKER_ORD = re.compile(
    r"更正[：:]\s*第(?P<n>[0-9一二三四五六七八九十]+)段发言人为(?P<new>[^，,。；;]+)[，,；;]\s*非(?P<old>[^。；;]+)"
)


def parse_correction(text: str) -> Optional[CorrectionDraft]:
    """解析书记员更正指令；无法解析返回 None（交由缺口机制呈现）。"""
    for pattern, field, by_key in (
        (_CORR_QUOTED_KEY, "被引述者", True),
        (_CORR_QUOTED_ORD, "被引述者", False),
        (_CORR_SPEAKER_KEY, "发言人", True),
        (_CORR_SPEAKER_ORD, "发言人", False),
    ):
        m = pattern.search(text)
        if m:
            draft = CorrectionDraft(
                field=field,
                new_label=m.group("new").strip(),
                old_label=m.group("old").strip(),
            )
            if by_key:
                draft.target_key = m.group("key")
            else:
                ordinal = _ordinal(m.group("n"))
                if ordinal is None:
                    return None
                draft.target_ordinal = ordinal
            return draft
    return None
