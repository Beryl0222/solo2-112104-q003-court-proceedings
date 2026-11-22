"""共享常量、数据类与异常。

设计约束：
- 主张（claim）永远携带来源信息（发言人、转述/逐字、被引述者），
  系统不提供也不生成“法庭认定”类别。
- 证据编号只有“被提及”与“复核人员显式确认”两种记录，
  提及本身不产生任何采信含义。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Optional


class ArchiveError(Exception):
    """归档系统基础异常。"""


class IngestError(ArchiveError):
    """批次录入失败（格式、版本冲突等）。"""


class NotFoundError(ArchiveError):
    """引用的对象不存在。"""


class WorkflowError(ArchiveError):
    """核阅流程约束被违反。"""


# ---- 记录类别（录入批次中的 kind 字段） ----
RECORD_KINDS = {
    "transcript": "笔录陈述",
    "counsel_statement": "代理人主张",
    "clerk_correction": "书记员更正",
    "review_note": "核阅记录",
    "procedural": "程序动作",
    "other": "其他",
}

# ---- 主张形态 ----
MODALITY_DIRECT = "直接陈述"
MODALITY_REPORTED = "转述"
MODALITY_QUOTE = "逐字引语"
MODALITIES = {MODALITY_DIRECT, MODALITY_REPORTED, MODALITY_QUOTE}

# ---- 主张状态 ----
CLAIM_ACTIVE = "有效"
CLAIM_RECHECK = "待复核"
CLAIM_SUPERSEDED = "被取代"

# ---- 记录状态 ----
RECORD_CURRENT = "当前"
RECORD_SUPERSEDED = "被取代"

# ---- 争点状态 ----
ISSUE_DRAFT = "草稿"
ISSUE_IN_REVIEW = "核阅中"
ISSUE_APPROVED = "团队认可"

# ---- 引用状态 ----
LINK_OK = "有效"
LINK_RECHECK = "待复核"

# ---- 缺口类型 ----
GAP_MISSING_PAGE = "缺页"
GAP_SPEAKER_UNKNOWN = "发言人未识别"
GAP_QUOTED_UNKNOWN = "被引述者未识别"
GAP_QUOTE_SOURCELESS = "引语来源不明"
GAP_CORRECTION_FAILED = "更正未落实"

GAP_OPEN = "未处理"
GAP_RESOLVED = "已解决"

# ---- 核阅动作 ----
REVIEW_START = "开始核阅"
REVIEW_COMPLETE = "完成核阅"
REVIEW_REJECT = "退回"
REVIEW_APPROVE = "认可"

# ---- 证据复核结论（只能由复核人员显式给出） ----
EVIDENCE_DECISIONS = {"确认采信", "确认未采信", "存疑"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass
class ClaimDraft:
    modality: str
    content: str
    quoted_label: Optional[str] = None


@dataclasses.dataclass
class CorrectionDraft:
    field: str            # 被引述者 / 发言人
    new_label: str
    old_label: str
    target_key: Optional[str] = None   # 显式记录号
    target_ordinal: Optional[int] = None  # 本批次内第 N 段笔录
