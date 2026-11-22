"""身份映射与更正后的重新核对测试。"""

import json
import unittest

from tarchive import Store
from tarchive.identity import add_person, map_alias
from tarchive.ingest import ingest_batch
from tarchive.issues import create_issue, link_issue


def make_store_with_case():
    store = Store(":memory:")
    store.ins(
        "INSERT INTO cases(case_no, title, created_at) VALUES(?,?,?)",
        ("2026-演-001", "合成演练案", store.now()),
    )
    case_id = store.one("SELECT id FROM cases")["id"]
    return store, case_id


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.store, self.case_id = make_store_with_case()

    def tearDown(self):
        self.store.close()

    def ingest(self, records):
        return ingest_batch(self.store, self.case_id, json.dumps(records, ensure_ascii=False))

    def test_alias_resolves_speaker_gap_and_claims(self):
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 1, "text": "证人周某称，文件已交。"},
        ])
        self.assertIsNotNone(
            self.store.one("SELECT id FROM gaps WHERE gap_key = '发言人未识别:周某' AND status = '未处理'")
        )
        add_person(self.store, self.case_id, "周某", "证人")
        result = map_alias(self.store, self.case_id, "周某", "周某", actor="归档员")
        self.assertTrue(result["changed"])
        self.assertEqual(result["gaps_resolved"], 1)
        record = self.store.current_record(self.case_id, "t1")
        self.assertIsNotNone(record["speaker_id"])

    def test_alias_correction_reflags_links(self):
        add_person(self.store, self.case_id, "张三", "当事人")
        add_person(self.store, self.case_id, "张三甲", "证人")
        map_alias(self.store, self.case_id, "张某", "张三")
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 1, "text": "证人张某称，门是锁着的。"},
        ])
        issue_id = create_issue(self.store, self.case_id, "门是否上锁")
        link_id = link_issue(self.store, issue_id, "t1")
        link = self.store.one("SELECT * FROM issue_links WHERE id = ?", (link_id,))
        self.assertEqual(link["status"], "有效")

        # 书记员确认“张某”实为张三甲：身份更正后引用必须重新核对
        result = map_alias(self.store, self.case_id, "张某", "张三甲", actor="书记员")
        self.assertTrue(result["changed"])
        link = self.store.one("SELECT * FROM issue_links WHERE id = ?", (link_id,))
        self.assertEqual(link["status"], "待复核")
        self.assertIn("张某", link["flag_reason"])
        claim = self.store.one("SELECT * FROM claims WHERE speaker_label = '张某'")
        self.assertEqual(claim["status"], "待复核")
        person = self.store.one("SELECT name FROM persons WHERE id = ?", (claim["speaker_id"],))
        self.assertEqual(person["name"], "张三甲")

        # 事件留痕：新增一次、更正一次
        events = self.store.q(
            "SELECT * FROM identity_events WHERE event_type LIKE '%身份映射%' ORDER BY id"
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["event_type"], "更正身份映射")

    def test_old_version_keeps_original_resolution(self):
        add_person(self.store, self.case_id, "张三", "当事人")
        map_alias(self.store, self.case_id, "张某", "张三")
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 1, "text": "证人张某称，门是锁着的。"},
        ])
        self.ingest([
            {"id": "t1", "kind": "transcript", "version": 2, "text": "证人张某称，门没有锁。"},
        ])
        add_person(self.store, self.case_id, "张三甲", "证人")
        map_alias(self.store, self.case_id, "张某", "张三甲")
        old = self.store.record_version(self.case_id, "t1", 1)
        old_person = self.store.one("SELECT name FROM persons WHERE id = ?", (old["speaker_id"],))
        self.assertEqual(old_person["name"], "张三")  # 历史版本保持当时的整理依据
        current = self.store.current_record(self.case_id, "t1")
        cur_person = self.store.one("SELECT name FROM persons WHERE id = ?", (current["speaker_id"],))
        self.assertEqual(cur_person["name"], "张三甲")


if __name__ == "__main__":
    unittest.main()
