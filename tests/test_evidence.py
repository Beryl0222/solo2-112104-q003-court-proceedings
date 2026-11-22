"""证据“提及不等于采信”测试。"""

import json
import unittest

from tarchive import Store, WorkflowError
from tarchive.ingest import ingest_batch
from tarchive.issues import confirm_evidence
from tarchive.report import render_evidence


def make_store_with_case():
    store = Store(":memory:")
    store.ins(
        "INSERT INTO cases(case_no, title, created_at) VALUES(?,?,?)",
        ("2026-演-001", "合成演练案", store.now()),
    )
    case_id = store.one("SELECT id FROM cases")["id"]
    return store, case_id


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.case_id = make_store_with_case()
        batch = [
            {"id": "s1", "kind": "counsel_statement", "version": 1, "speaker": "原代",
             "text": "代理人主张证据 E-17 能证明文件已经送达。"},
            {"id": "s2", "kind": "procedural", "version": 1,
             "text": "审判长宣布出示证据3号，双方质证。"},
            {"id": "s3", "kind": "procedural", "version": 1,
             "text": "审判长宣布证据3号采信。"},
        ]
        ingest_batch(self.store, self.case_id, json.dumps(batch, ensure_ascii=False))

    def tearDown(self):
        self.store.close()

    def test_mention_does_not_imply_admission(self):
        output = render_evidence(self.store, self.case_id)
        self.assertIn("E-17：被提及 1 次", output)
        self.assertIn("复核结论: 无（未被确认采信）", output)
        self.assertIn("提及不等于采信", output)

    def test_procedural_record_is_not_a_confirmation(self):
        # 笔录中出现“采信”字样只形成程序记录，复核结论仍为空
        actions = self.store.q(
            "SELECT * FROM procedural_actions WHERE evidence_no = '3'"
        )
        self.assertTrue(any(a["action_type"] == "证据采信记录" for a in actions))
        confirm = self.store.one(
            "SELECT * FROM evidence_confirmations WHERE evidence_no = '3'"
        )
        self.assertIsNone(confirm)

    def test_explicit_confirmation_recorded(self):
        confirm_evidence(self.store, self.case_id, "E-17", "负责人", "存疑", "待补充质证记录")
        output = render_evidence(self.store, self.case_id)
        self.assertIn("复核结论: 存疑（负责人", output)

    def test_invalid_decision_rejected(self):
        with self.assertRaises(WorkflowError):
            confirm_evidence(self.store, self.case_id, "E-17", "负责人", "予以采信")


if __name__ == "__main__":
    unittest.main()
