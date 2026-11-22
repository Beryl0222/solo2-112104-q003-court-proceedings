"""争点（issue）管理、引用链接与核阅流程。

流程约束：
- 争点状态：草稿 → 核阅中 → 团队认可；另有“退回”回到草稿。
- 只有负责人员完成核阅后，争点才可进入团队认可状态。
- 进入认可状态时若仍存在未处理缺口或待复核引用，必须显式确认，
  确认内容会写入核阅记录，保证“看似完整”的摘要无法静默通过。
"""

from __future__ import annotations

import json
from typing import Optional

from . import gaps as gapmod
from .models import (
    ISSUE_APPROVED,
    ISSUE_DRAFT,
    ISSUE_IN_REVIEW,
    LINK_OK,
    LINK_RECHECK,
    NotFoundError,
    REVIEW_APPROVE,
    REVIEW_COMPLETE,
    REVIEW_REJECT,
    REVIEW_START,
    WorkflowError,
)
from .store import Store


# ---- 争点 ----
def create_issue(
    store: Store, case_id: int, title: str, statement: str = "", creator: str = ""
) -> int:
    return store.ins(
        "INSERT INTO issues(case_id, title, statement, status, created_by, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (case_id, title, statement, ISSUE_DRAFT, creator, store.now()),
    )


def require_issue(store: Store, issue_id: int):
    row = store.one("SELECT * FROM issues WHERE id = ?", (issue_id,))
    if row is None:
        raise NotFoundError(f"争点不存在: #{issue_id}")
    return row


def list_issues(store: Store, case_id: int) -> list:
    return store.q("SELECT * FROM issues WHERE case_id = ? ORDER BY id", (case_id,))


# ---- 引用链接 ----
def link_issue(
    store: Store,
    issue_id: int,
    record_key: str,
    actor: str = "",
    note: str = "",
) -> int:
    """把争点链接到一条记录的当前版本，并记录整理依据版本。"""
    issue = require_issue(store, issue_id)
    record = store.current_record(issue["case_id"], record_key)
    if record is None:
        raise NotFoundError(f"记录不存在或已被取代: {record_key}")
    dup = store.one(
        "SELECT id FROM issue_links WHERE issue_id = ? AND record_key = ?",
        (issue_id, record_key),
    )
    if dup:
        raise WorkflowError(f"争点 #{issue_id} 已链接记录 {record_key}（链接 #{dup['id']}）")
    return store.ins(
        "INSERT INTO issue_links(issue_id, record_key, version_at_link, status, note,"
        " created_by, created_at) VALUES(?,?,?,?,?,?,?)",
        (issue_id, record_key, int(record["version"]), LINK_OK, note, actor, store.now()),
    )


def flag_links_for_records(
    store: Store, case_id: int, record_keys: list[str], reason: str
) -> int:
    """记录被更正或身份关系变化后，把相关引用置为待复核。返回影响条数。"""
    if not record_keys:
        return 0
    marks = ",".join("?" for _ in record_keys)
    cur = store.run(
        f"UPDATE issue_links SET status = ?, flag_reason = ?"
        f" WHERE record_key IN ({marks}) AND status = ?"
        f" AND issue_id IN (SELECT id FROM issues WHERE case_id = ?)",
        [LINK_RECHECK, reason, *record_keys, LINK_OK, case_id],
    )
    return cur.rowcount


def recheck_link(store: Store, link_id: int, actor: str, note: str = "") -> None:
    row = store.one("SELECT * FROM issue_links WHERE id = ?", (link_id,))
    if row is None:
        raise NotFoundError(f"引用链接不存在: #{link_id}")
    new_note = row["note"]
    if note:
        new_note = (new_note + "｜复核: " + note) if new_note else ("复核: " + note)
    store.run(
        "UPDATE issue_links SET status = ?, flag_reason = '', note = ?,"
        " rechecked_by = ?, rechecked_at = ? WHERE id = ?",
        (LINK_OK, new_note, actor, store.now(), link_id),
    )


def links_for_issue(store: Store, issue_id: int) -> list:
    return store.q(
        "SELECT * FROM issue_links WHERE issue_id = ? ORDER BY id", (issue_id,)
    )


def attach_evidence(
    store: Store, issue_id: int, evidence_no: str, actor: str = "", note: str = ""
) -> int:
    require_issue(store, issue_id)
    existing = store.one(
        "SELECT id FROM issue_evidence WHERE issue_id = ? AND evidence_no = ?",
        (issue_id, evidence_no),
    )
    if existing:
        raise WorkflowError(f"争点 #{issue_id} 已关联材料 {evidence_no}")
    return store.ins(
        "INSERT INTO issue_evidence(issue_id, evidence_no, note, created_by, created_at)"
        " VALUES(?,?,?,?,?)",
        (issue_id, evidence_no, note, actor, store.now()),
    )


# ---- 核阅流程 ----
def _record_review(store: Store, issue_id: int, reviewer: str, action: str, note: str) -> int:
    return store.ins(
        "INSERT INTO reviews(issue_id, reviewer, action, note, created_at)"
        " VALUES(?,?,?,?,?)",
        (issue_id, reviewer, action, note, store.now()),
    )


def start_review(store: Store, issue_id: int, reviewer: str) -> None:
    issue = require_issue(store, issue_id)
    if issue["status"] != ISSUE_DRAFT:
        raise WorkflowError(f"争点 #{issue_id} 当前状态为「{issue['status']}」，不能开始核阅")
    store.run("UPDATE issues SET status = ? WHERE id = ?", (ISSUE_IN_REVIEW, issue_id))
    _record_review(store, issue_id, reviewer, REVIEW_START, "")


def complete_review(store: Store, issue_id: int, reviewer: str, note: str = "") -> None:
    issue = require_issue(store, issue_id)
    if issue["status"] != ISSUE_IN_REVIEW:
        raise WorkflowError(f"争点 #{issue_id} 当前状态为「{issue['status']}」，不能完成核阅")
    started = store.one(
        "SELECT id FROM reviews WHERE issue_id = ? AND reviewer = ? AND action = ?"
        " ORDER BY id DESC LIMIT 1",
        (issue_id, reviewer, REVIEW_START),
    )
    if started is None:
        raise WorkflowError(f"{reviewer} 尚未对争点 #{issue_id} 开始核阅，不能完成")
    _record_review(store, issue_id, reviewer, REVIEW_COMPLETE, note)


def reject_issue(store: Store, issue_id: int, reviewer: str, note: str = "") -> None:
    issue = require_issue(store, issue_id)
    if issue["status"] != ISSUE_IN_REVIEW:
        raise WorkflowError(f"争点 #{issue_id} 当前状态为「{issue['status']}」，不能退回")
    store.run("UPDATE issues SET status = ? WHERE id = ?", (ISSUE_DRAFT, issue_id))
    _record_review(store, issue_id, reviewer, REVIEW_REJECT, note)


def approval_blockers(store: Store, issue_id: int) -> list[str]:
    """列出阻止直接进入团队认可状态的事项（不阻止时返回空列表）。"""
    issue = require_issue(store, issue_id)
    blockers: list[str] = []
    open_gaps = gapmod.open_gaps(store, issue["case_id"])
    if open_gaps:
        blockers.append(f"案件存在 {len(open_gaps)} 项未处理缺口")
    pending = store.q(
        "SELECT id, record_key, flag_reason FROM issue_links"
        " WHERE issue_id = ? AND status = ?",
        (issue_id, LINK_RECHECK),
    )
    for link in pending:
        reason = link["flag_reason"] or "待复核"
        blockers.append(f"引用 #{link['id']}（{link['record_key']}）待复核: {reason}")
    return blockers


def approve_issue(
    store: Store,
    issue_id: int,
    reviewer: str,
    acknowledge_gaps: bool = False,
    note: str = "",
) -> None:
    issue = require_issue(store, issue_id)
    if issue["status"] == ISSUE_APPROVED:
        raise WorkflowError(f"争点 #{issue_id} 已处于团队认可状态")
    if issue["status"] != ISSUE_IN_REVIEW:
        raise WorkflowError(f"争点 #{issue_id} 尚未进入核阅中，不能认可")
    completed = store.one(
        "SELECT id FROM reviews WHERE issue_id = ? AND reviewer = ? AND action = ?",
        (issue_id, reviewer, REVIEW_COMPLETE),
    )
    if completed is None:
        raise WorkflowError(
            f"负责人员 {reviewer} 尚未完成对争点 #{issue_id} 的核阅，不能进入团队认可"
        )
    blockers = approval_blockers(store, issue_id)
    if blockers and not acknowledge_gaps:
        raise WorkflowError(
            "存在未处理事项，不能直接进入团队认可：\n- "
            + "\n- ".join(blockers)
            + "\n如已逐项查看仍要认可，请使用 --acknowledge-gaps 并留下说明。"
        )
    full_note = note
    if blockers:
        ack = "认可时已查看并确认以下未处理事项: " + "；".join(blockers)
        full_note = f"{note}｜{ack}" if note else ack
    store.run(
        "UPDATE issues SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
        (ISSUE_APPROVED, reviewer, store.now(), issue_id),
    )
    _record_review(store, issue_id, reviewer, REVIEW_APPROVE, full_note)


def review_history(store: Store, issue_id: int) -> list:
    return store.q(
        "SELECT * FROM reviews WHERE issue_id = ? ORDER BY id", (issue_id,)
    )


# ---- 证据复核结论（只能显式录入） ----
def confirm_evidence(
    store: Store, case_id: int, evidence_no: str, reviewer: str, decision: str, note: str = ""
) -> int:
    from .models import EVIDENCE_DECISIONS

    if decision not in EVIDENCE_DECISIONS:
        raise WorkflowError(
            f"无效的复核结论「{decision}」，可选: {'、'.join(sorted(EVIDENCE_DECISIONS))}"
        )
    return store.ins(
        "INSERT INTO evidence_confirmations(case_id, evidence_no, reviewer, decision, note,"
        " created_at) VALUES(?,?,?,?,?,?)",
        (case_id, evidence_no, reviewer, decision, note, store.now()),
    )


def identity_event(store: Store, case_id: int, event_type: str, actor: str, detail: dict) -> int:
    return store.ins(
        "INSERT INTO identity_events(case_id, event_type, actor, detail, created_at)"
        " VALUES(?,?,?,?,?)",
        (case_id, event_type, actor, json.dumps(detail, ensure_ascii=False), store.now()),
    )
