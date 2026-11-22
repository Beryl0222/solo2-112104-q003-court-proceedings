"""批次录入管线。

输入为 JSON 数组，元素形如：
  {"id": "court-1", "kind": "transcript", "version": 1, "text": "……",
   "page": 2, "seg": 3, "speaker": "证人甲"}

可重复处理保证：
- 同一记录同一版本重复录入是幂等的（内容一致则跳过）；
- 同一 id 的更高版本构成更正：旧版本保留为“被取代”，
  其上的主张标记“被取代”，相关争点引用置为“待复核”；
- 同版本内容不一致、或版本回退，都会报错并整批回滚；
- 书记员更正记录在录入时落实，落实不了的形成缺口。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from . import gaps as gapmod
from .extract import (
    classify_procedural,
    detect_speaker,
    extract_claims,
    extract_evidence_nos,
    parse_correction,
)
from .issues import flag_links_for_records, identity_event
from .models import (
    CLAIM_ACTIVE,
    CLAIM_RECHECK,
    CLAIM_SUPERSEDED,
    GAP_CORRECTION_FAILED,
    GAP_MISSING_PAGE,
    GAP_QUOTED_UNKNOWN,
    GAP_QUOTE_SOURCELESS,
    GAP_SPEAKER_UNKNOWN,
    IngestError,
    RECORD_CURRENT,
    RECORD_KINDS,
    RECORD_SUPERSEDED,
)
from .store import Store

REQUIRED_FIELDS = {"id", "kind", "version", "text"}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _validate(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise IngestError(f"第 {index} 条记录不是对象")
    missing = REQUIRED_FIELDS - raw.keys()
    if missing:
        raise IngestError(f"记录 {raw.get('id', index)} 缺少字段: {sorted(missing)}")
    if raw["kind"] not in RECORD_KINDS:
        raise IngestError(
            f"记录 {raw['id']} 的 kind「{raw['kind']}」无法识别，"
            f"可选: {sorted(RECORD_KINDS)}"
        )
    if not isinstance(raw["version"], int) or raw["version"] < 1:
        raise IngestError(f"记录 {raw['id']} 的 version 必须是正整数")
    if not str(raw["text"]).strip():
        raise IngestError(f"记录 {raw['id']} 的 text 为空")
    return raw


def ingest_batch(
    store: Store, case_id: int, source_text: str, note: str = "", actor: str = ""
) -> dict:
    """录入一个批次（JSON 数组文本）。整批在一个事务内完成。"""
    try:
        data = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise IngestError(f"批次不是合法 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise IngestError("批次必须是 JSON 数组")

    stats = {
        "inserted": 0,
        "superseded": 0,
        "skipped": 0,
        "corrections_applied": 0,
        "corrections_failed": 0,
        "gaps_opened": 0,
        "links_flagged": 0,
    }

    with store.tx():
        transcript_ordinals: dict[int, str] = {}
        for index, raw in enumerate(data, start=1):
            record = _validate(raw, index)
            key = str(record["id"]).strip()
            if record["kind"] == "transcript":
                transcript_ordinals[len(transcript_ordinals) + 1] = key
            outcome = _ingest_record(store, case_id, record, key, note, stats)
            if record["kind"] == "clerk_correction" and outcome in ("inserted", "superseded"):
                _apply_correction(
                    store, case_id, record, key, transcript_ordinals, stats, actor
                )
        _refresh_page_gaps(store, case_id, stats)
    return stats


def _ingest_record(
    store: Store, case_id: int, record: dict, key: str, note: str, stats: dict
) -> str:
    """写入单条记录；返回 inserted / superseded / skipped。"""
    version = int(record["version"])
    text = str(record["text"]).strip()
    kind = record["kind"]
    current = store.current_record(case_id, key)

    if current is not None:
        cur_version = int(current["version"])
        if version < cur_version:
            raise IngestError(
                f"记录 {key} 版本回退: 已存在 v{cur_version}，收到 v{version}"
            )
        if version == cur_version:
            if current["content_hash"] == _hash(text) and current["kind"] == kind:
                stats["skipped"] += 1
                return "skipped"
            raise IngestError(
                f"记录 {key} v{version} 与已存内容不一致；更正请使用更高版本号"
            )
        # 更高版本：旧版本转为被取代，相关主张与引用随之标记
        store.run(
            "UPDATE records SET status = ? WHERE id = ?", (RECORD_SUPERSEDED, current["id"])
        )
        store.run(
            "UPDATE claims SET status = ? WHERE record_id = ? AND status != ?",
            (CLAIM_SUPERSEDED, current["id"], CLAIM_SUPERSEDED),
        )
        stats["links_flagged"] += flag_links_for_records(
            store, case_id, [key], f"记录已更正: v{cur_version} → v{version}"
        )
        stats["superseded"] += 1

    speaker_label, speaker_id = _resolve_speaker(store, case_id, record, text, key)
    record_id = store.ins(
        "INSERT INTO records(case_id, record_key, version, kind, page, seg,"
        " speaker_label, speaker_id, text, content_hash, status, batch_note, ingested_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            key,
            version,
            kind,
            record.get("page"),
            record.get("seg"),
            speaker_label,
            speaker_id,
            text,
            _hash(text),
            RECORD_CURRENT,
            note,
            store.now(),
        ),
    )
    stats["inserted"] += 1

    if kind in ("transcript", "counsel_statement", "other"):
        _extract_claims(store, case_id, record_id, key, speaker_label, speaker_id, text, stats)
    if kind == "procedural":
        action, evidence_no = classify_procedural(text)
        store.ins(
            "INSERT INTO procedural_actions(case_id, record_id, action_type, evidence_no,"
            " detail, created_at) VALUES(?,?,?,?,?,?)",
            (case_id, record_id, action, evidence_no, text, store.now()),
        )
    if kind == "review_note":
        for no in extract_evidence_nos(text):
            store.ins(
                "INSERT INTO evidence_notes(case_id, evidence_no, source_record_id, note,"
                " created_at) VALUES(?,?,?,?,?)",
                (case_id, no, record_id, text, store.now()),
            )
    for no in extract_evidence_nos(text):
        store.ins(
            "INSERT INTO evidence_mentions(case_id, evidence_no, record_id, context,"
            " created_at) VALUES(?,?,?,?,?)",
            (case_id, no, record_id, text, store.now()),
        )
    return "superseded" if current is not None else "inserted"


def _resolve_speaker(
    store: Store, case_id: int, record: dict, text: str, key: str
) -> tuple[Optional[str], Optional[int]]:
    # 核阅记录与更正指令是整理过程的元信息，不要求发言人
    if record["kind"] in ("clerk_correction", "review_note"):
        return None, None
    label = record.get("speaker")
    if not label:
        label, _role_hint = detect_speaker(text)
    if not label:
        gapmod.open_gap(
            store, case_id, GAP_SPEAKER_UNKNOWN,
            f"{GAP_SPEAKER_UNKNOWN}:未指明:{key}",
            ref=key, detail="记录未指明发言人，文本规则也无法识别",
        )
        return None, None
    person_id = store.resolve_label(case_id, label)
    if person_id is None:
        gapmod.open_gap(
            store, case_id, GAP_SPEAKER_UNKNOWN,
            f"{GAP_SPEAKER_UNKNOWN}:{label}",
            ref=key, detail=f"发言人简称「{label}」尚未登记身份映射",
        )
    return label, person_id


def _extract_claims(
    store: Store,
    case_id: int,
    record_id: int,
    key: str,
    speaker_label: Optional[str],
    speaker_id: Optional[int],
    text: str,
    stats: dict,
) -> None:
    for draft in extract_claims(text):
        quoted_person_id = None
        if draft.quoted_label:
            quoted_person_id = store.resolve_label(case_id, draft.quoted_label)
            if quoted_person_id is None:
                gapmod.open_gap(
                    store, case_id, GAP_QUOTED_UNKNOWN,
                    f"{GAP_QUOTED_UNKNOWN}:{draft.quoted_label}",
                    ref=key,
                    detail=f"被引述者「{draft.quoted_label}」尚未登记身份映射",
                )
        elif draft.modality == "逐字引语":
            gapmod.open_gap(
                store, case_id, GAP_QUOTE_SOURCELESS,
                f"{GAP_QUOTE_SOURCELESS}:{key}",
                ref=key, detail="存在逐字引语但无法确认被引述者",
            )
        store.ins(
            "INSERT INTO claims(case_id, record_id, speaker_label, speaker_id,"
            " quoted_label, quoted_person_id, modality, content, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                record_id,
                speaker_label or "",
                speaker_id,
                draft.quoted_label,
                quoted_person_id,
                draft.modality,
                draft.content,
                CLAIM_ACTIVE,
                store.now(),
            ),
        )


def _apply_correction(
    store: Store,
    case_id: int,
    record: dict,
    source_key: str,
    transcript_ordinals: dict[int, str],
    stats: dict,
    actor: str,
) -> None:
    """落实书记员更正；落实不了的形成缺口，绝不静默跳过。"""
    source = store.current_record(case_id, source_key)
    draft = parse_correction(str(record["text"]))
    if draft is None:
        stats["corrections_failed"] += 1
        gapmod.open_gap(
            store, case_id, GAP_CORRECTION_FAILED,
            f"{GAP_CORRECTION_FAILED}:{source_key}",
            ref=source_key, detail=f"无法解析的更正指令: {record['text']}",
        )
        return
    target_key = draft.target_key or transcript_ordinals.get(draft.target_ordinal or -1)
    if not target_key:
        stats["corrections_failed"] += 1
        gapmod.open_gap(
            store, case_id, GAP_CORRECTION_FAILED,
            f"{GAP_CORRECTION_FAILED}:{source_key}",
            ref=source_key, detail="更正指向的段落在本批次中不存在",
        )
        return
    target = store.current_record(case_id, target_key)
    if target is None:
        stats["corrections_failed"] += 1
        gapmod.open_gap(
            store, case_id, GAP_CORRECTION_FAILED,
            f"{GAP_CORRECTION_FAILED}:{source_key}",
            ref=source_key, detail=f"更正目标 {target_key} 不存在",
        )
        return

    if draft.field == "被引述者":
        matched = store.q(
            "SELECT id FROM claims WHERE record_id = ? AND quoted_label = ?"
            " AND status != '被取代'",
            (target["id"], draft.old_label),
        )
        if not matched:
            stats["corrections_failed"] += 1
            gapmod.open_gap(
                store, case_id, GAP_CORRECTION_FAILED,
                f"{GAP_CORRECTION_FAILED}:{source_key}",
                ref=source_key,
                detail=f"{target_key} 中找不到被引述者「{draft.old_label}」",
            )
            return
        new_person = store.resolve_label(case_id, draft.new_label)
        store.run(
            "UPDATE claims SET quoted_label = ?, quoted_person_id = ?, status = ?"
            " WHERE record_id = ? AND quoted_label = ? AND status != '被取代'",
            (draft.new_label, new_person, CLAIM_RECHECK, target["id"], draft.old_label),
        )
        if new_person is None:
            gapmod.open_gap(
                store, case_id, GAP_QUOTED_UNKNOWN,
                f"{GAP_QUOTED_UNKNOWN}:{draft.new_label}",
                ref=target_key,
                detail=f"更正后的被引述者「{draft.new_label}」尚未登记身份映射",
            )
    else:  # 发言人
        if target["speaker_label"] != draft.old_label:
            stats["corrections_failed"] += 1
            gapmod.open_gap(
                store, case_id, GAP_CORRECTION_FAILED,
                f"{GAP_CORRECTION_FAILED}:{source_key}",
                ref=source_key,
                detail=f"{target_key} 的发言人不是「{draft.old_label}」",
            )
            return
        new_person = store.resolve_label(case_id, draft.new_label)
        store.run(
            "UPDATE records SET speaker_label = ?, speaker_id = ? WHERE id = ?",
            (draft.new_label, new_person, target["id"]),
        )
        store.run(
            "UPDATE claims SET speaker_label = ?, speaker_id = ?, status = ?"
            " WHERE record_id = ? AND status != '被取代'",
            (draft.new_label, new_person, CLAIM_RECHECK, target["id"]),
        )
        if new_person is None:
            gapmod.open_gap(
                store, case_id, GAP_SPEAKER_UNKNOWN,
                f"{GAP_SPEAKER_UNKNOWN}:{draft.new_label}",
                ref=target_key,
                detail=f"更正后的发言人「{draft.new_label}」尚未登记身份映射",
            )

    store.ins(
        "INSERT INTO corrections(case_id, source_record_id, target_key, field,"
        " old_label, new_label, status, detail, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            case_id, source["id"], target_key, draft.field,
            draft.old_label, draft.new_label, "已落实",
            str(record["text"]), store.now(),
        ),
    )
    identity_event(
        store, case_id, "笔录更正", actor,
        {"source": source_key, "target": target_key, "field": draft.field,
         "from": draft.old_label, "to": draft.new_label},
    )
    stats["links_flagged"] += flag_links_for_records(
        store, case_id, [target_key],
        f"书记员更正({source_key}): {draft.field} {draft.old_label} → {draft.new_label}",
    )
    stats["corrections_applied"] += 1


def _refresh_page_gaps(store: Store, case_id: int, stats: dict) -> None:
    """按当前版本记录的页码检查缺页；补录到位后旧缺口自动关闭。"""
    rows = store.q(
        "SELECT DISTINCT page FROM records WHERE case_id = ? AND status = ?"
        " AND page IS NOT NULL ORDER BY page",
        (case_id, RECORD_CURRENT),
    )
    pages = {int(r["page"]) for r in rows}
    if not pages:
        return
    missing = sorted(set(range(min(pages), max(pages) + 1)) - pages)
    wanted = {f"{GAP_MISSING_PAGE}:{p}" for p in missing}
    existing = {
        r["gap_key"]
        for r in store.q(
            "SELECT gap_key FROM gaps WHERE case_id = ? AND gap_type = ? AND status = '未处理'",
            (case_id, GAP_MISSING_PAGE),
        )
    }
    for page in missing:
        gap_key = f"{GAP_MISSING_PAGE}:{page}"
        if gap_key not in existing:
            gapmod.open_gap(
                store, case_id, GAP_MISSING_PAGE,
                gap_key,
                ref=f"第{page}页", detail=f"笔录缺第 {page} 页（现有页码不连续）",
            )
            stats["gaps_opened"] += 1
    for gap_key in existing:
        if gap_key not in wanted:
            gapmod.resolve_gaps(store, case_id, gap_key)
