"""命令行冒烟测试：参数接线与关键输出。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tarchive.cli import main

BATCH = [
    {"id": "t1", "kind": "transcript", "version": 1, "page": 1,
     "text": "证人甲称，听乙说“门是锁着的”。"},
    {"id": "t2", "kind": "transcript", "version": 1, "page": 2,
     "text": "被告丙称，门当时没有锁。"},
]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cli.db")
        self.batch = Path(self.tmp.name) / "batch.json"
        self.batch.write_text(json.dumps(BATCH, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--db", self.db, *argv])
        return code, out.getvalue()

    def test_full_flow_via_cli(self):
        code, _ = self.run_cli("case", "create", "--no", "C-1", "--title", "合成案")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("ingest", "--case", "C-1", "--file", str(self.batch))
        self.assertEqual(code, 0)

        # 未识别发言人在缺口中可见
        code, out = self.run_cli("gaps", "--case", "C-1")
        self.assertIn("发言人未识别", out)
        self.assertIn("被引述者未识别", out)

        # 登记身份后缺口关闭
        for label in ("甲", "乙", "丙"):
            self.run_cli("person", "add", "--case", "C-1", "--name", label, "--role", "待定")
            self.run_cli("alias", "--case", "C-1", "--label", label, "--person", label)
        code, out = self.run_cli("gaps", "--case", "C-1")
        self.assertNotIn("未处理", out)

        # 争点链接与并列展示
        self.run_cli("issue", "create", "--case", "C-1", "--title", "门是否上锁")
        self.run_cli("issue", "link", "--id", "1", "--record", "t1")
        self.run_cli("issue", "link", "--id", "1", "--record", "t2")
        code, out = self.run_cli("issue", "show", "--id", "1")
        self.assertIn("门是锁着的", out)
        self.assertIn("门当时没有锁", out)
        self.assertIn("逐字引语", out)

        # 核阅门控
        self.run_cli("review", "start", "--issue", "1", "--reviewer", "负责人")
        code, _ = self.run_cli("issue", "approve", "--id", "1", "--reviewer", "负责人")
        self.assertEqual(code, 1)  # 未完成核阅不能认可
        self.run_cli("review", "complete", "--issue", "1", "--reviewer", "负责人")
        code, _ = self.run_cli("issue", "approve", "--id", "1", "--reviewer", "负责人")
        self.assertEqual(code, 0)
        code, out = self.run_cli("issue", "list", "--case", "C-1")
        self.assertIn("团队认可", out)

    def test_unknown_case_rejected(self):
        code, _ = self.run_cli("gaps", "--case", "不存在")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
