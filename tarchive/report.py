"""面向复核人员的文本报告。

原则：
- 只呈现“谁主张、谁被引述、原句在哪、材料被谁提及”，不下法律结论；
- 证据一栏固定标注“提及不等于采信”；
- 缺口、待复核引用、历史版本并列展示，不生成看似完整的摘要。
"""

from __future__ import annotations

from . import gaps as gapmod
from .identity import list_aliases, list_persons
from .issues import links_for_issue, review_history
from .models import (
    GAP_OPEN,
    LINK_RECHECK,
    RECORD_CURRENT,
    RECORD_KINDS,
)
from .store import Store


def _person_name(store: Store, person_id) -> str:
    if person_id is None:
        return "（未识别）"
    row = store.one("SELECT name, role FROM persons WHERE id = ?", (person_id,))
    return f"{row['name']}（{row['role']}）" if row else "（未识别）"


def _record_ref(row) -> str:
    parts = [row["record_key"], f"v{row['version']}"]
    if row["page"] is not None:
        parts.append(f"第{row['page']}页")
    if row["seg"] is not None:
        parts.append(f"段{row['seg']}")
    return " ".join(str(p) for p in parts)


def render_overview(store: Store, case_id: int) -> str:
    case = store.one("SELECT * FROM cases WHERE id = ?", (case_id,))
    lines: list[str] = []
    lines.append(f"案件 {case['case_no']}　{case['title']}")
    lines.append("=" * 60)

    records = store.q(
        "SELECT * FROM records WHERE case_id = ? AND status = ? ORDER BY record_key",
        (case_id, RECORD_CURRENT),
    )
    lines.append(f"当前记录 {len(records)} 条：")
    for rec in records:
        kind = RECORD_KINDS.get(rec["kind"], rec["kind"])
        speaker = rec["speaker_label"] or "（未指明发言人）"
        lines.append(f"  [{kind}] {_record_ref(rec)}　发言人: {speaker}")

    persons = list_persons(store, case_id)
    aliases = list_aliases(store, case_id)
    lines.append(f"\n登记人员 {len(persons)} 人：")
    active = {}
    for a in aliases:
        if a["active"]:
            active.setdefault(a["person_name"], []).append(a["label"])
    for p in persons:
        labels = "、".join(active.get(p["name"], [])) or "（无简称映射）"
        lines.append(f"  {p['name']}（{p['role']}）　简称: {labels}")

    issues = store.q("SELECT * FROM issues WHERE case_id = ? ORDER BY id", (case_id,))
    lines.append(f"\n争点 {len(issues)} 项：")
    for issue in issues:
        links = links_for_issue(store, issue["id"])
        pending = sum(1 for l in links if l["status"] == LINK_RECHECK)
        flag = f"，待复核引用 {pending} 条" if pending else ""
        lines.append(
            f"  #{issue['id']} {issue['title']}　状态: {issue['status']}"
            f"（引用 {len(links)} 条{flag}）"
        )

    lines.append("")
    lines.append(render_evidence(store, case_id))

    open_gaps = gapmod.open_gaps(store, case_id)
    lines.append(f"\n未处理缺口 {len(open_gaps)} 项：")
    for gap in open_gaps:
        lines.append(f"  [{gap['gap_type']}] {gap['ref']}　{gap['detail']}")
    if not open_gaps:
        lines.append("  （无）")
    return "\n".join(lines)


def render_issue(store: Store, issue_id: int) -> str:
    from .issues import require_issue

    issue = require_issue(store, issue_id)
    lines: list[str] = []
    lines.append(f"争点 #{issue['id']}　{issue['title']}　状态: {issue['status']}")
    if issue["statement"]:
        lines.append(f"  描述: {issue['statement']}")
    if issue["approved_by"]:
        lines.append(f"  认可: {issue['approved_by']}　{issue['approved_at']}")
    lines.append("-" * 60)

    links = links_for_issue(store, issue_id)
    lines.append(f"引用发言 {len(links)} 条（按发言人并列保留，不作合并）：")
    for link in links:
        case_id = issue["case_id"]
        current = store.current_record(case_id, link["record_key"])
        at_link = store.record_version(case_id, link["record_key"], link["version_at_link"])
        status = link["status"]
        lines.append(f"\n  链接 #{link['id']}　{link['record_key']}　状态: {status}")
        if link["flag_reason"]:
            lines.append(f"    待复核原因: {link['flag_reason']}")
        lines.append(f"    整理依据: v{link['version_at_link']}（{link['created_at']} 由 {link['created_by'] or '—'} 标注）")
        if at_link is not None:
            lines.append(f"    当时文本: {at_link['text']}")
        if current is not None and int(current["version"]) != int(link["version_at_link"]):
            lines.append(f"    当前文本: v{current['version']}　{current['text']}")
        if current is None:
            lines.append("    当前文本: （记录不存在）")
            continue
        claims = store.q(
            "SELECT * FROM claims WHERE record_id = ? AND status != '被取代' ORDER BY id",
            (current["id"],),
        )
        by_speaker: dict[str, list] = {}
        for claim in claims:
            speaker = _person_name(store, claim["speaker_id"]) if claim["speaker_id"] \
                else (claim["speaker_label"] or "（未识别）")
            by_speaker.setdefault(speaker, []).append(claim)
        for speaker, speaker_claims in by_speaker.items():
            lines.append(f"    发言人: {speaker}")
            for claim in speaker_claims:
                quoted = ""
                if claim["quoted_label"]:
                    who = _person_name(store, claim["quoted_person_id"]) \
                        if claim["quoted_person_id"] else "（未识别）"
                    quoted = f"，被引述者: {claim['quoted_label']}→{who}"
                mark = "　[待复核]" if claim["status"] == "待复核" else ""
                lines.append(
                    f"      〔{claim['modality']}{quoted}〕{claim['content']}{mark}"
                )
        mentions = store.q(
            "SELECT DISTINCT evidence_no FROM evidence_mentions WHERE record_id = ?",
            (current["id"],),
        )
        if mentions:
            nos = "、".join(m["evidence_no"] for m in mentions)
            lines.append(f"    提及材料: {nos}（提及不等于采信）")

    materials = store.q(
        "SELECT * FROM issue_evidence WHERE issue_id = ? ORDER BY id", (issue_id,)
    )
    if materials:
        lines.append("\n关联材料：")
        for m in materials:
            lines.append(f"  {m['evidence_no']}　{m['note']}")

    history = review_history(store, issue_id)
    lines.append("\n核阅记录：")
    if not history:
        lines.append("  （尚无核阅记录）")
    for rev in history:
        note = f"　{rev['note']}" if rev["note"] else ""
        lines.append(f"  {rev['created_at']}　{rev['reviewer']}　{rev['action']}{note}")

    issue_gaps = store.q(
        "SELECT * FROM gaps WHERE issue_id = ? AND status = ? ORDER BY id",
        (issue_id, GAP_OPEN),
    )
    if issue_gaps:
        lines.append("\n相关缺口：")
        for gap in issue_gaps:
            lines.append(f"  [{gap['gap_type']}] {gap['ref']}　{gap['detail']}")
    return "\n".join(lines)


def render_record_history(store: Store, case_id: int, record_key: str) -> str:
    versions = store.q(
        "SELECT * FROM records WHERE case_id = ? AND record_key = ? ORDER BY version",
        (case_id, record_key),
    )
    if not versions:
        return f"记录不存在: {record_key}"
    lines = [f"记录 {record_key}　共 {len(versions)} 个版本："]
    for rec in versions:
        kind = RECORD_KINDS.get(rec["kind"], rec["kind"])
        speaker = rec["speaker_label"] or "（未指明发言人）"
        lines.append(
            f"\n  v{rec['version']}　[{kind}]　状态: {rec['status']}"
            f"　录入: {rec['ingested_at']}　发言人: {speaker}"
        )
        lines.append(f"    {rec['text']}")
        claims = store.q(
            "SELECT * FROM claims WHERE record_id = ? ORDER BY id", (rec["id"],)
        )
        for claim in claims:
            quoted = f"，被引述者: {claim['quoted_label']}" if claim["quoted_label"] else ""
            lines.append(
                f"    〔{claim['modality']}{quoted}｜{claim['status']}〕{claim['content']}"
            )
    corrections = store.q(
        "SELECT * FROM corrections WHERE case_id = ? AND target_key = ? ORDER BY id",
        (case_id, record_key),
    )
    if corrections:
        lines.append("\n  更正历史：")
        for corr in corrections:
            lines.append(
                f"    {corr['created_at']}　{corr['field']}: "
                f"{corr['old_label']} → {corr['new_label']}（{corr['status']}）"
            )
    return "\n".join(lines)


def render_evidence(store: Store, case_id: int) -> str:
    nos = [
        r["evidence_no"]
        for r in store.q(
            "SELECT DISTINCT evidence_no FROM evidence_mentions WHERE case_id = ?"
            " ORDER BY evidence_no",
            (case_id,),
        )
    ]
    lines = ["证据材料（提及不等于采信；采信结论只能由复核人员显式录入）："]
    if not nos:
        lines.append("  （尚无材料被提及）")
        return "\n".join(lines)
    for no in nos:
        mentions = store.q(
            "SELECT COUNT(*) AS n FROM evidence_mentions WHERE case_id = ? AND evidence_no = ?",
            (case_id, no),
        )[0]["n"]
        actions = store.q(
            "SELECT action_type, detail FROM procedural_actions"
            " WHERE case_id = ? AND evidence_no = ? ORDER BY id",
            (case_id, no),
        )
        notes = store.q(
            "SELECT note FROM evidence_notes WHERE case_id = ? AND evidence_no = ?"
            " ORDER BY id",
            (case_id, no),
        )
        confirm = store.one(
            "SELECT decision, reviewer, created_at FROM evidence_confirmations"
            " WHERE case_id = ? AND evidence_no = ? ORDER BY id DESC LIMIT 1",
            (case_id, no),
        )
        lines.append(f"  {no}：被提及 {mentions} 次")
        for a in actions:
            lines.append(f"    程序记录〔{a['action_type']}〕: {a['detail']}")
        for n in notes:
            lines.append(f"    核阅记录: {n['note']}")
        if confirm:
            lines.append(
                f"    复核结论: {confirm['decision']}"
                f"（{confirm['reviewer']}，{confirm['created_at']}）"
            )
        else:
            lines.append("    复核结论: 无（未被确认采信）")
    return "\n".join(lines)


def render_gaps(store: Store, case_id: int) -> str:
    rows = store.q(
        "SELECT * FROM gaps WHERE case_id = ? ORDER BY status DESC, id", (case_id,)
    )
    lines = [f"缺口清单（共 {len(rows)} 项，含已解决）："]
    for gap in rows:
        lines.append(
            f"  #{gap['id']} [{gap['gap_type']}] {gap['status']}　{gap['ref']}"
            f"　{gap['detail']}"
        )
    if not rows:
        lines.append("  （无）")
    return "\n".join(lines)


def render_identity_events(store: Store, case_id: int) -> str:
    rows = store.q(
        "SELECT * FROM identity_events WHERE case_id = ? ORDER BY id", (case_id,)
    )
    lines = ["身份与更正事件："]
    for row in rows:
        actor = f"　操作: {row['actor']}" if row["actor"] else ""
        lines.append(
            f"  {row['created_at']}　[{row['event_type']}] {row['detail']}{actor}"
        )
    if not rows:
        lines.append("  （无）")
    return "\n".join(lines)
