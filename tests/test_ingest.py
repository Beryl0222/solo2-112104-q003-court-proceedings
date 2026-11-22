"""录入管线测试：幂等、版本、提取、缺口。"""

import json
import unittest
from pathlib import Path

from tarchive import IngestError, Store
from tarchive.ingest import ingest_batch

DATA = Path(__file__).parents[1] / "data" / "court.json"


def make_store():
    return Store(":memory:")


def make_case(store, no="2026-演-001"):
    store.ins(
        "INSERT INTO cases(case_no, title, created_at) VALUES(?,?,?)",
        (no, "合成演练案", store.now()),
    )
    return store.one("SELECT * FROM cases WHERE case_no = ?", (no,))["id"]


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.case_id = make_case(self.store)

    def tearDown(self):
        self.store.close()

    def ingest(self, records, note=""):
        text = records if isinstance(records, str) else json.dumps(records, ensure_ascii=False)
        return ingest_batch(self.store, self.case_id, text, note=note)

    def test_ingest_source_material(self):
        stats = self.ingest(DATA.read_text(encoding="utf-8"))
        self.assertEqual(stats["inserted"], 4)
        self.assertEqual(stats["corrections_applied"], 1)

        # 笔录记录：发言人周某、转述+逐字引语，书记员更正后被引述者为刘某
        record = self.store.current_record(self.case_id, "court-1")
        self.assertEqual(record["speaker_label"], "周某")
        claims = self.store.q(
            "SELECT * FROM claims WHERE record_id = ? ORDER BY id", (record["id"],)
        )
        self.assertEqual([c["modality"] for c in claims], ["转述", "逐字引语"])
        self.assertTrue(all(c["quoted_label"] == "刘某" for c in claims))
        self.assertTrue(all(c["status"] == "待复核" for c in claims))

        # 更正事件留痕
        corr = self.store.one("SELECT * FROM corrections WHERE target_key = 'court-1'")
        self.assertEqual(corr["status"], "已落实")
        self.assertEqual(corr["old_label"], "李某")
        self.assertEqual(corr["new_label"], "刘某")

        # 核阅记录只登记为材料备注，不产生采信结论
        note = self.store.one("SELECT * FROM evidence_notes WHERE evidence_no = 'E-17'")
        self.assertIn("尚未完成质证记录核对", note["note"])
        confirm = self.store.one(
            "SELECT * FROM evidence_confirmations WHERE evidence_no = 'E-17'"
        )
        self.assertIsNone(confirm)

    def test_ingest_is_idempotent(self):
        text = DATA.read_text(encoding="utf-8")
        self.ingest(text)
        stats = self.ingest(text)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["skipped"], 4)
        count = self.store.one("SELECT COUNT(*) AS n FROM records")["n"]
        self.assertEqual(count, 4)

    def test_same_version_different_content_rejected(self):
        self.ingest([{"id": "r1", "kind": "transcript", "version": 1, "text": "证人甲称，天在下雨。"}])
        with self.assertRaises(IngestError):
            self.ingest([{"id": "r1", "kind": "transcript", "version": 1, "text": "证人甲称，天没下雨。"}])

    def test_version_rollback_rejected(self):
        self.ingest([{"id": "r1", "kind": "transcript", "version": 2, "text": "证人甲称，天在下雨。"}])
        with self.assertRaises(IngestError):
            self.ingest([{"id": "r1", "kind": "transcript", "version": 1, "text": "证人甲称，天在下雨。"}])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(IngestError):
            self.ingest([{"id": "r1", "kind": "verdict", "version": 1, "text": "……"}])

    def test_supersede_marks_old_claims(self):
        self.ingest([{"id": "r1", "kind": "transcript", "version": 1, "text": "证人甲称，门是锁着的。"}])
        self.ingest([{"id": "r1", "kind": "transcript", "version": 2, "text": "证人甲称，门没有锁。"}])
        old = self.store.record_version(self.case_id, "r1", 1)
        self.assertEqual(old["status"], "被取代")
        old_claims = self.store.q("SELECT * FROM claims WHERE record_id = ?", (old["id"],))
        self.assertTrue(all(c["status"] == "被取代" for c in old_claims))
        current = self.store.current_record(self.case_id, "r1")
        self.assertEqual(current["version"], 2)

    def test_missing_page_gap_opens_and_closes(self):
        batch = [
            {"id": "p1", "kind": "transcript", "version": 1, "page": 1, "text": "审判长称，现在开庭。"},
            {"id": "p2", "kind": "transcript", "version": 1, "page": 2, "text": "证人甲称，我在场。"},
            {"id": "p4", "kind": "transcript", "version": 1, "page": 4, "text": "证人甲称，后来散了。"},
        ]
        self.ingest(batch)
        gap = self.store.one(
            "SELECT * FROM gaps WHERE gap_key = '缺页:3' AND status = '未处理'"
        )
        self.assertIsNotNone(gap)
        self.ingest([
            {"id": "p3", "kind": "transcript", "version": 1, "page": 3, "text": "被代孙衡称，需要质证。"},
        ])
        gap = self.store.one(
            "SELECT * FROM gaps WHERE gap_key = '缺页:3' AND status = '未处理'"
        )
        self.assertIsNone(gap)

    def test_unidentified_speaker_creates_gap(self):
        self.ingest([{"id": "r1", "kind": "transcript", "version": 1, "text": "证人周某称，文件已交。"}])
        gap = self.store.one(
            "SELECT * FROM gaps WHERE gap_key = '发言人未识别:周某' AND status = '未处理'"
        )
        self.assertIsNotNone(gap)

    def test_unparseable_correction_creates_gap(self):
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 1, "text": "证人甲称，听乙说“灯亮着”。"},
            {"id": "c1", "kind": "clerk_correction", "version": 1, "text": "更正：那段话好像不是乙说的。"},
        ])
        gap = self.store.one(
            "SELECT * FROM gaps WHERE gap_key = '更正未落实:c1' AND status = '未处理'"
        )
        self.assertIsNotNone(gap)

    def test_quote_without_source_creates_gap(self):
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 1, "text": "证人甲称，现场写着“禁止入内”。"},
        ])
        gap = self.store.one(
            "SELECT * FROM gaps WHERE gap_key = '引语来源不明:t1' AND status = '未处理'"
        )
        self.assertIsNotNone(gap)


if __name__ == "__main__":
    unittest.main()
