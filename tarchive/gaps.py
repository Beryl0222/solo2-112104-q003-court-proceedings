"""缺口管理：让复核人员看到具体缺口，而不是看似完整的摘要。

缺口按 (case_id, gap_key) 去重；同一缺口在未处理状态下不会重复生成。
"""

from __future__ import annotations

from typing import Optional

from .models import GAP_OPEN, GAP_RESOLVED
from .store import Store


def open_gap(
    store: Store,
    case_id: int,
    gap_type: str,
    gap_key: str,
    ref: str = "",
    detail: str = "",
    issue_id: Optional[int] = None,
) -> int:
    """登记一个缺口；若同类缺口仍未处理则返回已有记录 id。"""
    existing = store.one(
        "SELECT id FROM gaps WHERE case_id = ? AND gap_key = ? AND status = ?",
        (case_id, gap_key, GAP_OPEN),
    )
    if existing:
        return int(existing["id"])
    return store.ins(
        "INSERT INTO gaps(case_id, gap_type, gap_key, ref, issue_id, detail, status, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (case_id, gap_type, gap_key, ref, issue_id, detail, GAP_OPEN, store.now()),
    )


def resolve_gaps(store: Store, case_id: int, gap_key: str) -> int:
    """把指定 key 的未处理缺口标记为已解决，返回处理条数。"""
    cur = store.run(
        "UPDATE gaps SET status = ?, resolved_at = ?"
        " WHERE case_id = ? AND gap_key = ? AND status = ?",
        (GAP_RESOLVED, store.now(), case_id, gap_key, GAP_OPEN),
    )
    return cur.rowcount


def resolve_gap_by_id(store: Store, gap_id: int, actor: str, note: str = "") -> None:
    from .models import NotFoundError

    row = store.one("SELECT * FROM gaps WHERE id = ?", (gap_id,))
    if row is None:
        raise NotFoundError(f"缺口不存在: #{gap_id}")
    detail = row["detail"]
    suffix = f"｜人工处理({actor}): {note}" if note else f"｜人工处理({actor})"
    store.run(
        "UPDATE gaps SET status = ?, resolved_at = ?, detail = ? WHERE id = ?",
        (GAP_RESOLVED, store.now(), detail + suffix, gap_id),
    )


def open_gaps(store: Store, case_id: int) -> list:
    return store.q(
        "SELECT * FROM gaps WHERE case_id = ? AND status = ? ORDER BY id",
        (case_id, GAP_OPEN),
    )


def gaps_for_issue(store: Store, issue_id: int) -> list:
    return store.q(
        "SELECT * FROM gaps WHERE issue_id = ? AND status = ? ORDER BY id",
        (issue_id, GAP_OPEN),
    )
