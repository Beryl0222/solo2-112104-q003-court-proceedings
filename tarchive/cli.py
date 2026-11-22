"""命令行入口：python3 -m tarchive [--db 路径] <子命令> ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import gaps as gapmod
from . import identity, issues, report
from .ingest import ingest_batch
from .models import ArchiveError
from .store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tarchive",
        description="庭审笔录争点与引用归档（合成材料专用，不作法律结论）",
    )
    parser.add_argument("--db", default="archive.db", help="归档库路径（默认 ./archive.db）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("case", help="案件管理")
    p.add_argument("action", choices=["create", "list"])
    p.add_argument("--no", help="案件编号")
    p.add_argument("--title", default="", help="案件标题")

    p = sub.add_parser("person", help="登记人员")
    p.add_argument("action", nargs="?", default="add", choices=["add"])
    p.add_argument("--case", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="未知")
    p.add_argument("--note", default="")

    p = sub.add_parser("alias", help="登记或更正简称映射（更正后相关引用自动待复核）")
    p.add_argument("--case", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--person", required=True)
    p.add_argument("--actor", default="")

    p = sub.add_parser("persons", help="查看人员与简称映射")
    p.add_argument("--case", required=True)

    p = sub.add_parser("ingest", help="录入一个批次（JSON 文件，可重复执行）")
    p.add_argument("--case", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="")

    p = sub.add_parser("records", help="列出当前版本记录")
    p.add_argument("--case", required=True)

    p = sub.add_parser("record", help="查看一条记录的版本历史与提取结果")
    p.add_argument("--case", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("issue", help="争点管理")
    p.add_argument("action", choices=["create", "list", "show", "link", "attach", "recheck", "approve"])
    p.add_argument("--case")
    p.add_argument("--id", type=int, help="争点 id")
    p.add_argument("--title")
    p.add_argument("--statement", default="")
    p.add_argument("--record", help="链接的记录号")
    p.add_argument("--link", type=int, help="引用链接 id")
    p.add_argument("--evidence", help="关联材料编号")
    p.add_argument("--reviewer", default="")
    p.add_argument("--actor", default="")
    p.add_argument("--note", default="")
    p.add_argument("--acknowledge-gaps", action="store_true",
                   help="认可时确认已查看全部未处理缺口与待复核引用")

    p = sub.add_parser("review", help="核阅流程")
    p.add_argument("action", choices=["start", "complete", "reject"])
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("evidence", help="证据材料")
    p.add_argument("action", choices=["list", "confirm"])
    p.add_argument("--case", required=True)
    p.add_argument("--no", help="证据编号")
    p.add_argument("--reviewer", default="")
    p.add_argument("--decision", default="", help="确认采信 / 确认未采信 / 存疑")
    p.add_argument("--note", default="")

    p = sub.add_parser("gaps", help="查看缺口清单")
    p.add_argument("--case", required=True)

    p = sub.add_parser("gap", help="人工处理缺口")
    p.add_argument("action", choices=["resolve"])
    p.add_argument("--id", type=int, help="缺口 id")
    p.add_argument("--key", help="缺口键（如 被引述者未识别:值班员小杜）")
    p.add_argument("--actor", default="")
    p.add_argument("--note", default="")

    p = sub.add_parser("overview", help="案件总览")
    p.add_argument("--case", required=True)

    p = sub.add_parser("events", help="身份与更正事件")
    p.add_argument("--case", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    try:
        return _dispatch(store, args)
    except ArchiveError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _dispatch(store: Store, args: argparse.Namespace) -> int:
    cmd = args.cmd

    if cmd == "case":
        if args.action == "create":
            if not args.no:
                raise ArchiveError("case create 需要 --no")
            with store.tx():
                store.ins(
                    "INSERT INTO cases(case_no, title, created_at) VALUES(?,?,?)",
                    (args.no, args.title, store.now()),
                )
            print(f"已建立案件 {args.no}")
        else:
            for row in store.q("SELECT * FROM cases ORDER BY id"):
                print(f"{row['case_no']}　{row['title']}　建立于 {row['created_at']}")
        return 0

    if cmd == "person":
        case = store.require_case(args.case)
        with store.tx():
            pid = identity.add_person(store, case["id"], args.name, args.role, args.note)
        print(f"已登记人员 #{pid} {args.name}（{args.role}）")
        return 0

    if cmd == "alias":
        case = store.require_case(args.case)
        with store.tx():
            result = identity.map_alias(store, case["id"], args.label, args.person, args.actor)
        if not result["changed"]:
            print(f"简称「{args.label}」已指向 {args.person}，无需变更")
        else:
            print(
                f"简称「{args.label}」→ {args.person}；"
                f"更新记录 {result['records_updated']} 条，"
                f"主张待复核 {result['claims_flagged']} 条，"
                f"引用待复核 {result['links_flagged']} 条，"
                f"关闭缺口 {result['gaps_resolved']} 项"
            )
        return 0

    if cmd == "persons":
        case = store.require_case(args.case)
        for p in identity.list_persons(store, case["id"]):
            print(f"#{p['id']} {p['name']}（{p['role']}）{('　' + p['note']) if p['note'] else ''}")
        for a in identity.list_aliases(store, case["id"]):
            state = "有效" if a["active"] else f"已停用({a['retired_at']})"
            print(f"  简称「{a['label']}」→ {a['person_name']}　{state}")
        return 0

    if cmd == "ingest":
        case = store.require_case(args.case)
        text = Path(args.file).read_text(encoding="utf-8")
        stats = ingest_batch(store, case["id"], text, note=args.note, actor=args.actor)
        print(
            f"录入完成: 新增 {stats['inserted']}，取代旧版 {stats['superseded']}，"
            f"跳过（幂等） {stats['skipped']}；更正落实 {stats['corrections_applied']}，"
            f"更正未落实 {stats['corrections_failed']}；引用待复核 {stats['links_flagged']}"
        )
        return 0

    if cmd == "records":
        case = store.require_case(args.case)
        rows = store.q(
            "SELECT * FROM records WHERE case_id = ? AND status = '当前' ORDER BY record_key",
            (case["id"],),
        )
        for rec in rows:
            speaker = rec["speaker_label"] or "（未指明发言人）"
            print(f"{rec['record_key']} v{rec['version']} [{rec['kind']}] 发言人: {speaker}")
            print(f"    {rec['text']}")
        return 0

    if cmd == "record":
        case = store.require_case(args.case)
        print(report.render_record_history(store, case["id"], args.key))
        return 0

    if cmd == "issue":
        return _issue(store, args)

    if cmd == "review":
        with store.tx():
            if args.action == "start":
                issues.start_review(store, args.issue, args.reviewer)
                print(f"争点 #{args.issue} 进入核阅中（负责人: {args.reviewer}）")
            elif args.action == "complete":
                issues.complete_review(store, args.issue, args.reviewer, args.note)
                print(f"{args.reviewer} 已完成对争点 #{args.issue} 的核阅")
            else:
                issues.reject_issue(store, args.issue, args.reviewer, args.note)
                print(f"争点 #{args.issue} 已退回草稿")
        return 0

    if cmd == "evidence":
        case = store.require_case(args.case)
        if args.action == "list":
            print(report.render_evidence(store, case["id"]))
        else:
            if not args.no or not args.reviewer or not args.decision:
                raise ArchiveError("evidence confirm 需要 --no、--reviewer、--decision")
            with store.tx():
                issues.confirm_evidence(
                    store, case["id"], args.no, args.reviewer, args.decision, args.note
                )
            print(f"已记录复核结论: {args.no} → {args.decision}（{args.reviewer}）")
        return 0

    if cmd == "gaps":
        case = store.require_case(args.case)
        print(report.render_gaps(store, case["id"]))
        return 0

    if cmd == "gap":
        with store.tx():
            if args.key:
                n = 0
                for row in store.q("SELECT DISTINCT case_id FROM gaps WHERE gap_key = ? AND status = '未处理'", (args.key,)):
                    n += gapmod.resolve_gaps(store, row["case_id"], args.key)
                if n == 0:
                    raise ArchiveError(f"没有未处理的缺口: {args.key}")
                print(f"已处理 {n} 项缺口（{args.key}）")
            elif args.id:
                gapmod.resolve_gap_by_id(store, args.id, args.actor, args.note)
                print(f"缺口 #{args.id} 已标记为已解决")
            else:
                raise ArchiveError("gap resolve 需要 --id 或 --key")
        return 0

    if cmd == "overview":
        case = store.require_case(args.case)
        print(report.render_overview(store, case["id"]))
        return 0

    if cmd == "events":
        case = store.require_case(args.case)
        print(report.render_identity_events(store, case["id"]))
        return 0

    raise ArchiveError(f"未知命令: {cmd}")


def _issue(store: Store, args: argparse.Namespace) -> int:
    action = args.action
    if action == "create":
        if not args.case or not args.title:
            raise ArchiveError("issue create 需要 --case 与 --title")
        case = store.require_case(args.case)
        with store.tx():
            iid = issues.create_issue(
                store, case["id"], args.title, args.statement, args.actor
            )
        print(f"已建立争点 #{iid} {args.title}（状态: 草稿）")
        return 0
    if action == "list":
        if not args.case:
            raise ArchiveError("issue list 需要 --case")
        case = store.require_case(args.case)
        for issue in issues.list_issues(store, case["id"]):
            links = issues.links_for_issue(store, issue["id"])
            pending = sum(1 for l in links if l["status"] == "待复核")
            flag = f"，待复核 {pending}" if pending else ""
            print(f"#{issue['id']} {issue['title']}　{issue['status']}（引用 {len(links)}{flag}）")
        return 0
    if action == "show":
        if not args.id:
            raise ArchiveError("issue show 需要 --id")
        print(report.render_issue(store, args.id))
        return 0
    if action == "link":
        if not args.id or not args.record:
            raise ArchiveError("issue link 需要 --id 与 --record")
        with store.tx():
            lid = issues.link_issue(store, args.id, args.record, args.actor, args.note)
        print(f"已建立引用 #{lid}: 争点 #{args.id} ← {args.record}")
        return 0
    if action == "attach":
        if not args.id or not args.evidence:
            raise ArchiveError("issue attach 需要 --id 与 --evidence")
        with store.tx():
            issues.attach_evidence(store, args.id, args.evidence, args.actor, args.note)
        print(f"争点 #{args.id} 已关联材料 {args.evidence}（关联不代表采信）")
        return 0
    if action == "recheck":
        if not args.link:
            raise ArchiveError("issue recheck 需要 --link")
        with store.tx():
            issues.recheck_link(store, args.link, args.actor, args.note)
        print(f"引用 #{args.link} 已复核，恢复有效")
        return 0
    if action == "approve":
        if not args.id or not args.reviewer:
            raise ArchiveError("issue approve 需要 --id 与 --reviewer")
        with store.tx():
            issues.approve_issue(
                store, args.id, args.reviewer, args.acknowledge_gaps, args.note
            )
        print(f"争点 #{args.id} 已进入团队认可状态（认可人: {args.reviewer}）")
        return 0
    raise ArchiveError(f"未知争点操作: {action}")


if __name__ == "__main__":
    raise SystemExit(main())
