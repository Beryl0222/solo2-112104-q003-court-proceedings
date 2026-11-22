"""SQLite 存储层。

所有表仅追加或做状态翻转，不做物理删除，保证任何一次整理依据都可回溯。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_no TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '未知',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(case_id, name)
);

CREATE TABLE IF NOT EXISTS aliases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  person_id INTEGER NOT NULL REFERENCES persons(id),
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_aliases_label ON aliases(case_id, label, active);

CREATE TABLE IF NOT EXISTS identity_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  record_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  page INTEGER,
  seg INTEGER,
  speaker_label TEXT,
  speaker_id INTEGER REFERENCES persons(id),
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '当前',
  batch_note TEXT NOT NULL DEFAULT '',
  ingested_at TEXT NOT NULL,
  UNIQUE(case_id, record_key, version)
);
CREATE INDEX IF NOT EXISTS idx_records_key ON records(case_id, record_key, status);

CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  record_id INTEGER NOT NULL REFERENCES records(id),
  speaker_label TEXT NOT NULL DEFAULT '',
  speaker_id INTEGER REFERENCES persons(id),
  quoted_label TEXT,
  quoted_person_id INTEGER REFERENCES persons(id),
  modality TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '有效',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_record ON claims(record_id);

CREATE TABLE IF NOT EXISTS evidence_mentions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  evidence_no TEXT NOT NULL,
  record_id INTEGER NOT NULL REFERENCES records(id),
  context TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_case ON evidence_mentions(case_id, evidence_no);

CREATE TABLE IF NOT EXISTS procedural_actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  record_id INTEGER NOT NULL REFERENCES records(id),
  action_type TEXT NOT NULL,
  evidence_no TEXT,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  evidence_no TEXT NOT NULL,
  source_record_id INTEGER REFERENCES records(id),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 证据采信结论只能由复核人员显式录入；系统不从提及或程序记录推断。
CREATE TABLE IF NOT EXISTS evidence_confirmations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  evidence_no TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  decision TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  source_record_id INTEGER NOT NULL REFERENCES records(id),
  target_key TEXT NOT NULL,
  field TEXT NOT NULL,
  old_label TEXT NOT NULL DEFAULT '',
  new_label TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '待处理',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  title TEXT NOT NULL,
  statement TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '草稿',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT
);

CREATE TABLE IF NOT EXISTS issue_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id INTEGER NOT NULL REFERENCES issues(id),
  record_key TEXT NOT NULL,
  version_at_link INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '有效',
  flag_reason TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  rechecked_by TEXT,
  rechecked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_issue ON issue_links(issue_id);
CREATE INDEX IF NOT EXISTS idx_links_key ON issue_links(record_key);

CREATE TABLE IF NOT EXISTS issue_evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id INTEGER NOT NULL REFERENCES issues(id),
  evidence_no TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(issue_id, evidence_no)
);

CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id INTEGER NOT NULL REFERENCES issues(id),
  reviewer TEXT NOT NULL,
  action TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  gap_type TEXT NOT NULL,
  gap_key TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  issue_id INTEGER REFERENCES issues(id),
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '未处理',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gaps_case ON gaps(case_id, gap_key, status);
"""


class Store:
    """对单个 SQLite 归档库的访问。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 基础助手 ----
    def now(self) -> str:
        return utcnow()

    def run(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def ins(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.conn.execute(sql, tuple(params))
        return int(cur.lastrowid)

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    @contextmanager
    def tx(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    # ---- 常用查询 ----
    def case_by_no(self, case_no: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM cases WHERE case_no = ?", (case_no,))

    def require_case(self, case_no: str) -> sqlite3.Row:
        from .models import NotFoundError

        row = self.case_by_no(case_no)
        if row is None:
            raise NotFoundError(f"案件不存在: {case_no}（先用 case create 建立）")
        return row

    def current_record(self, case_id: int, record_key: str) -> Optional[sqlite3.Row]:
        return self.one(
            "SELECT * FROM records WHERE case_id = ? AND record_key = ? AND status = '当前'",
            (case_id, record_key),
        )

    def record_version(
        self, case_id: int, record_key: str, version: int
    ) -> Optional[sqlite3.Row]:
        return self.one(
            "SELECT * FROM records WHERE case_id = ? AND record_key = ? AND version = ?",
            (case_id, record_key, version),
        )

    def resolve_label(self, case_id: int, label: str) -> Optional[int]:
        """按当前有效的身份映射把笔录中的简称解析为人员 id。"""
        row = self.one(
            "SELECT person_id FROM aliases WHERE case_id = ? AND label = ? AND active = 1",
            (case_id, label),
        )
        return int(row["person_id"]) if row else None
