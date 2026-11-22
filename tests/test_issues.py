"""争点、引用与核阅流程测试。"""

import json
import unittest

from tarchive import NotFoundError, Store, WorkflowError
from tarchive.identity import add_person, map_alias
from tarchive.ingest import ingest_batch
from tarchive.issues import (
    approve_issue,
    attach_evidence,
    complete_review,
    create_issue,
    link_issue,
    recheck_link,
    reject_issue,
    start_review,
)
from tarchive.report import render_issue


def make_store_with_case():
    store = Store(":memory:")
    store.ins(
        "INSERT INTO cases(case_no, title, created_at) VALUES(?,?,?)",
        ("2026-演-001", "合成演练案", store.now()),
    )
    case_id = store.one("SELECT id FROM cases")["id"]
    return store, case_id


CONTRADICTORY = [
    {"id": "w1", "kind": "transcript", "version": 1,
     "text": "证人甲称，仓库门当时是锁着的。"},
    {"id": "w2", "kind": "transcript", "version": 1,
     "text": "被告乙称，仓库门当时没有锁。"},
    {"id": "q1", "kind": "counsel_statement", "version": 1,
     "speaker": "原代",
     "text": "代理人主张证据 E-17 能证明门已上锁。"},
]


class IssueWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store, self.case_id = make_store_with_case()
        self.ingest(CONTRADICTORY)
        self.issue_id = create_issue(
            self.store, self.case_id, "仓库门是否上锁", "双方陈述不一致", "归档员"
        )

    def tearDown(self):
        self.store.close()

    def ingest(self, records):
        return ingest_batch(self.store, self.case_id, json.dumps(records, ensure_ascii=False))

    def test_link_and_duplicate_rejected(self):
        link_issue(self.store, self.issue_id, "w1")
        with self.assertRaises(WorkflowError):
            link_issue(self.store, self.issue_id, "w1")
        with self.assertRaises(NotFoundError):
            link_issue(self.store, self.issue_id, "不存在")

    def test_approve_requires_completed_review(self):
        # 先清理身份缺口，使认可不被缺口拦截
        for label in ("甲", "乙", "原代"):
            add_person(self.store, self.case_id, label, "待定")
            map_alias(self.store, self.case_id, label, label)
        link_issue(self.store, self.issue_id, "w1")
        with self.assertRaises(WorkflowError):
            approve_issue(self.store, self.issue_id, "负责人")
        start_review(self.store, self.issue_id, "负责人")
        with self.assertRaises(WorkflowError):
            approve_issue(self.store, self.issue_id, "负责人")
        complete_review(self.store, self.issue_id, "负责人", "已逐项核对")
        approve_issue(self.store, self.issue_id, "负责人")
        issue = self.store.one("SELECT * FROM issues WHERE id = ?", (self.issue_id,))
        self.assertEqual(issue["status"], "团队认可")
        self.assertEqual(issue["approved_by"], "负责人")

    def test_complete_requires_start_by_same_reviewer(self):
        start_review(self.store, self.issue_id, "甲")
        with self.assertRaises(WorkflowError):
            complete_review(self.store, self.issue_id, "乙")

    def test_reject_returns_to_draft(self):
        start_review(self.store, self.issue_id, "负责人")
        reject_issue(self.store, self.issue_id, "负责人", "引用不足")
        issue = self.store.one("SELECT * FROM issues WHERE id = ?", (self.issue_id,))
        self.assertEqual(issue["status"], "草稿")

    def test_open_gaps_block_approval_until_acknowledged(self):
        # 未识别发言人“证人甲”“被告乙”“原代”形成未处理缺口
        link_issue(self.store, self.issue_id, "w1")
        start_review(self.store, self.issue_id, "负责人")
        complete_review(self.store, self.issue_id, "负责人")
        with self.assertRaises(WorkflowError) as ctx:
            approve_issue(self.store, self.issue_id, "负责人")
        self.assertIn("未处理缺口", str(ctx.exception))
        approve_issue(self.store, self.issue_id, "负责人",
                      acknowledge_gaps=True, note="缺口已逐项查看")
        review = self.store.one(
            "SELECT * FROM reviews WHERE issue_id = ? AND action = '认可'",
            (self.issue_id,),
        )
        self.assertIn("未处理缺口", review["note"])

    def test_correction_flags_link_and_recheck_restores(self):
        link_id = link_issue(self.store, self.issue_id, "w1")
        self.ingest([
            {"id": "w1", "kind": "transcript", "version": 2,
             "text": "证人甲称，仓库门当时可能锁着。"},
        ])
        link = self.store.one("SELECT * FROM issue_links WHERE id = ?", (link_id,))
        self.assertEqual(link["status"], "待复核")
        self.assertIn("v1 → v2", link["flag_reason"])
        recheck_link(self.store, link_id, "复核员", "已对照新文本")
        link = self.store.one("SELECT * FROM issue_links WHERE id = ?", (link_id,))
        self.assertEqual(link["status"], "有效")
        self.assertEqual(link["rechecked_by"], "复核员")

    def test_parallel_statements_kept_side_by_side(self):
        for person, role in (("甲", "证人"), ("乙", "当事人"), ("丙", "代理人")):
            add_person(self.store, self.case_id, person, role)
        map_alias(self.store, self.case_id, "甲", "甲")
        map_alias(self.store, self.case_id, "乙", "乙")
        map_alias(self.store, self.case_id, "原代", "丙")
        link_issue(self.store, self.issue_id, "w1")
        link_issue(self.store, self.issue_id, "w2")
        link_issue(self.store, self.issue_id, "q1")
        output = render_issue(self.store, self.issue_id)
        # 两种相反陈述与代理人主张必须并列保留，谁也不被合并或覆盖
        self.assertIn("仓库门当时是锁着的", output)
        self.assertIn("仓库门当时没有锁", output)
        self.assertIn("证据 E-17 能证明门已上锁", output)
        self.assertIn("按发言人并列保留", output)
        self.assertNotIn("法庭认定", output)

    def test_issue_view_shows_basis_version_after_correction(self):
        link_id = link_issue(self.store, self.issue_id, "w1")
        self.ingest([
            {"id": "w1", "kind": "transcript", "version": 2,
             "text": "证人甲称，仓库门当时可能锁着。"},
        ])
        output = render_issue(self.store, self.issue_id)
        self.assertIn("整理依据: v1", output)
        self.assertIn("当时文本: 证人甲称，仓库门当时是锁着的。", output)
        self.assertIn("当前文本: v2", output)

    def test_attach_evidence_does_not_imply_admission(self):
        attach_evidence(self.store, self.issue_id, "E-17", actor="归档员")
        with self.assertRaises(WorkflowError):
            attach_evidence(self.store, self.issue_id, "E-17")
        confirm = self.store.one(
            "SELECT * FROM evidence_confirmations WHERE evidence_no = 'E-17'"
        )
        self.assertIsNone(confirm)


if __name__ == "__main__":
    unittest.main()
