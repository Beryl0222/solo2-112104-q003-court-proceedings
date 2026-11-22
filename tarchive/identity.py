"""人员与简称（别名）登记。

补录或更正姓名后，相关引用应随身份关系重新核对：
- 当前版本记录中该简称的发言人、被引述者解析随之更新；
- 受影响的主张与争点引用被置为“待复核”，由复核人员确认；
- 历史版本记录保持不变，作为当时整理依据留存。
"""

from __future__ import annotations

from typing import Optional

from . import gaps as gapmod
from .issues import flag_links_for_records, identity_event
from .models import (
    CLAIM_ACTIVE,
    CLAIM_RECHECK,
    GAP_QUOTED_UNKNOWN,
    GAP_SPEAKER_UNKNOWN,
    NotFoundError,
    RECORD_CURRENT,
)
from .store import Store


def add_person(
    store: Store, case_id: int, name: str, role: str = "未知", note: str = "", actor: str = ""
) -> int:
    existing = store.one(
        "SELECT id FROM persons WHERE case_id = ? AND name = ?", (case_id, name)
    )
    if existing:
        return int(existing["id"])
    person_id = store.ins(
        "INSERT INTO persons(case_id, name, role, note, created_at) VALUES(?,?,?,?,?)",
        (case_id, name, role, note, store.now()),
    )
    identity_event(store, case_id, "登记人员", actor, {"name": name, "role": role})
    return person_id


def person_by_name(store: Store, case_id: int, name: str) -> Optional[int]:
    row = store.one(
        "SELECT id FROM persons WHERE case_id = ? AND name = ?", (case_id, name)
    )
    return int(row["id"]) if row else None


def map_alias(
    store: Store, case_id: int, label: str, person_name: str, actor: str = ""
) -> dict:
    """登记或更正简称映射，并触发相关引用的重新核对。"""
    person_id = person_by_name(store, case_id, person_name)
    if person_id is None:
        raise NotFoundError(f"人员不存在: {person_name}（先用 person add 登记）")
    current = store.one(
        "SELECT * FROM aliases WHERE case_id = ? AND label = ? AND active = 1",
        (case_id, label),
    )
    if current and int(current["person_id"]) == person_id:
        return {"changed": False, "label": label, "person": person_name}
    if current:
        old = store.one("SELECT name FROM persons WHERE id = ?", (current["person_id"],))
        store.run(
            "UPDATE aliases SET active = 0, retired_at = ? WHERE id = ?",
            (store.now(), current["id"]),
        )
        event_type = "更正身份映射"
        detail = {"label": label, "from": old["name"] if old else "?", "to": person_name}
    else:
        event_type = "新增身份映射"
        detail = {"label": label, "to": person_name}
    store.ins(
        "INSERT INTO aliases(case_id, person_id, label, active, created_at)"
        " VALUES(?,?,?,1,?)",
        (case_id, person_id, label, store.now()),
    )
    identity_event(store, case_id, event_type, actor, detail)
    affected = re_resolve_label(store, case_id, label)
    return {"changed": True, "label": label, "person": person_name, **affected}


def re_resolve_label(store: Store, case_id: int, label: str) -> dict:
    """按当前身份映射重新解析某个简称，并把受影响内容置为待复核。"""
    person_id = store.resolve_label(case_id, label)
    affected_keys: set[str] = set()

    # 1) 当前版本记录中，以该简称发言的记录
    records = store.q(
        "SELECT id, record_key, speaker_id FROM records"
        " WHERE case_id = ? AND status = ? AND speaker_label = ?",
        (case_id, RECORD_CURRENT, label),
    )
    for rec in records:
        if rec["speaker_id"] != person_id:
            store.run(
                "UPDATE records SET speaker_id = ? WHERE id = ?", (person_id, rec["id"])
            )
            affected_keys.add(rec["record_key"])

    # 2) 当前版本记录上、以该简称发言的主张
    speaker_claims = store.q(
        "SELECT c.id, c.speaker_id, r.record_key FROM claims c"
        " JOIN records r ON r.id = c.record_id"
        " WHERE c.case_id = ? AND r.status = ? AND c.status != '被取代'"
        " AND c.speaker_label = ?",
        (case_id, RECORD_CURRENT, label),
    )
    for claim in speaker_claims:
        if claim["speaker_id"] != person_id:
            store.run(
                "UPDATE claims SET speaker_id = ?, status = ? WHERE id = ?",
                (person_id, CLAIM_RECHECK, claim["id"]),
            )
            affected_keys.add(claim["record_key"])

    # 3) 当前版本记录上、以该简称为被引述者的主张
    quoted_claims = store.q(
        "SELECT c.id, c.quoted_person_id, r.record_key FROM claims c"
        " JOIN records r ON r.id = c.record_id"
        " WHERE c.case_id = ? AND r.status = ? AND c.status != '被取代'"
        " AND c.quoted_label = ?",
        (case_id, RECORD_CURRENT, label),
    )
    for claim in quoted_claims:
        if claim["quoted_person_id"] != person_id:
            store.run(
                "UPDATE claims SET quoted_person_id = ?, status = ? WHERE id = ?",
                (person_id, CLAIM_RECHECK, claim["id"]),
            )
            affected_keys.add(claim["record_key"])

    # 4) 相关争点引用置为待复核
    flagged = flag_links_for_records(
        store, case_id, sorted(affected_keys), f"身份关系变更: 简称「{label}」重新核对"
    )

    # 5) 该简称对应的未识别缺口已获身份，关闭
    resolved = 0
    if person_id is not None:
        resolved += gapmod.resolve_gaps(store, case_id, f"{GAP_SPEAKER_UNKNOWN}:{label}")
        resolved += gapmod.resolve_gaps(store, case_id, f"{GAP_QUOTED_UNKNOWN}:{label}")

    return {
        "records_updated": len(records),
        "claims_flagged": len(speaker_claims) + len(quoted_claims),
        "links_flagged": flagged,
        "gaps_resolved": resolved,
    }


def list_persons(store: Store, case_id: int) -> list:
    return store.q(
        "SELECT * FROM persons WHERE case_id = ? ORDER BY id", (case_id,)
    )


def list_aliases(store: Store, case_id: int) -> list:
    return store.q(
        "SELECT a.*, p.name AS person_name FROM aliases a"
        " JOIN persons p ON p.id = a.person_id"
        " WHERE a.case_id = ? ORDER BY a.id",
        (case_id,),
    )
