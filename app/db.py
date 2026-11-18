"""SQLite 持久化层。

每次操作打开短连接（check_same_thread=False + 全局写锁），
满足单 uvicorn worker 下后台求解线程与 API 线程的并发访问。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .solver import CellResult, Conflict, SolveResult

DB_PATH = Path(
    os.environ.get("BREEDER_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "app.db")
)
_write_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 先设 busy 超时再切 WAL：并发初始化/写入时让锁等待生效
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    row_id INTEGER NOT NULL,
    line_no TEXT NOT NULL,
    plant_no TEXT NOT NULL,
    tray_no TEXT NOT NULL,
    variety TEXT NOT NULL,
    house TEXT NOT NULL,
    height TEXT NOT NULL,
    UNIQUE(session_id, row_id)
);
CREATE TABLE IF NOT EXISTS allowed_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    variety TEXT NOT NULL,
    house TEXT NOT NULL,
    UNIQUE(session_id, variety, house)
);
CREATE TABLE IF NOT EXISTS locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    row_id INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(field IN ('variety','house')),
    value TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('original','candidate')),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, row_id, field)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','done','error','superseded')),
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    optimal INTEGER NOT NULL,
    objective_proven INTEGER NOT NULL,
    total_changes INTEGER,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solution_id INTEGER NOT NULL REFERENCES solutions(id),
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    row_id INTEGER,
    field TEXT
);
CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    solution_id INTEGER NOT NULL REFERENCES solutions(id),
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id)
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    revision INTEGER NOT NULL,
    variety TEXT NOT NULL,
    threshold REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','done','undecidable','superseded','error')),
    min_count INTEGER,
    max_count INTEGER,
    min_rows TEXT,
    max_rows TEXT,
    reason TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""


def init_db() -> None:
    """建表。并发启动/测试切换数据库文件时可能瞬时锁冲突，短暂重试。"""
    last: Optional[sqlite3.OperationalError] = None
    for _ in range(50):  # 最多约 10 秒
        try:
            with _write_lock, get_conn() as conn:
                conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            last = exc
            time.sleep(0.2)
    raise last  # type: ignore[misc]


def create_session(rows: Sequence[dict], pairs: Sequence[Tuple[str, str]]) -> int:
    with _write_lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(revision, created_at) VALUES (1, ?)", (_now(),)
        )
        sid = cur.lastrowid
        conn.executemany(
            """INSERT INTO rows_data
               (session_id, row_id, line_no, plant_no, tray_no, variety, house, height)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    sid,
                    r["row_id"],
                    r["line_no"],
                    r["plant_no"],
                    r["tray_no"],
                    r["variety"],
                    r["house"],
                    r["height"],
                )
                for r in rows
            ],
        )
        conn.executemany(
            "INSERT INTO allowed_pairs(session_id, variety, house) VALUES (?,?,?)",
            [(sid, v, h) for v, h in pairs],
        )
        return sid


def get_session(sid: int) -> Optional[dict]:
    with get_conn() as conn:
        s = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if s is None:
            return None
        rows = conn.execute(
            "SELECT * FROM rows_data WHERE session_id=? ORDER BY row_id", (sid,)
        ).fetchall()
        pairs = conn.execute(
            "SELECT variety, house FROM allowed_pairs WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        locks = conn.execute(
            "SELECT row_id, field, value, source FROM locks WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        job = conn.execute(
            "SELECT id, status FROM jobs WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        sol = conn.execute(
            "SELECT id, revision, status, optimal, objective_proven, total_changes "
            "FROM solutions WHERE session_id=?",
            (sid,),
        ).fetchone()
        conf = conn.execute(
            "SELECT solution_id, revision FROM confirmations WHERE session_id=?",
            (sid,),
        ).fetchone()
        return {
            "session": dict(s),
            "rows": [dict(r) for r in rows],
            "pairs": [(p["variety"], p["house"]) for p in pairs],
            "locks": [dict(x) for x in locks],
            "job": dict(job) if job else None,
            "solution": dict(sol) if sol else None,
            "confirmation": dict(conf) if conf else None,
        }


def bump_revision(sid: int) -> int:
    """锁定/解锁后调用：版本号 +1，旧确认与旧分析区间立即作废（核实值已变）。"""
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET revision = revision + 1 WHERE id=?", (sid,)
        )
        conn.execute("DELETE FROM confirmations WHERE session_id=?", (sid,))
        # 核实值变化后旧区间立即失效：删除该会话全部分析结果，
        # 迟到分析作业的写入由 save_analysis 内的版本核对拦截。
        conn.execute("DELETE FROM analyses WHERE session_id=?", (sid,))
        rev = conn.execute(
            "SELECT revision FROM sessions WHERE id=?", (sid,)
        ).fetchone()[0]
        return rev


def upsert_lock(
    sid: int, row_id: int, field: str, value: str, source: str
) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO locks(session_id, row_id, field, value, source, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id, row_id, field)
               DO UPDATE SET value=excluded.value, source=excluded.source,
                             created_at=excluded.created_at""",
            (sid, row_id, field, value, source, _now()),
        )


def delete_lock(sid: int, row_id: int, field: str) -> int:
    with _write_lock, get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM locks WHERE session_id=? AND row_id=? AND field=?",
            (sid, row_id, field),
        )
        return cur.rowcount


def create_job(sid: int, revision: int) -> int:
    with _write_lock, get_conn() as conn:
        # 同版本已有的待处理/运行中作业标记为被取代。
        conn.execute(
            "UPDATE jobs SET status='superseded', finished_at=? "
            "WHERE session_id=? AND revision=? AND status IN ('pending','running')",
            (_now(), sid, revision),
        )
        cur = conn.execute(
            "INSERT INTO jobs(session_id, revision, status, created_at) "
            "VALUES (?,?, 'pending', ?)",
            (sid, revision, _now()),
        )
        return cur.lastrowid


def mark_job(job_id: int, status: str, error: Optional[str] = None) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
            (status, error, _now(), job_id),
        )


def get_job(job_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def current_revision(sid: int) -> Optional[int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT revision FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        return row[0] if row else None


def save_solution(
    sid: int, job_id: int, revision: int, result: SolveResult
) -> None:
    """仅当作业仍属于当前版本时写入；迟到结果由调用方先检查版本。"""
    payload = {
        "cells": [c.__dict__ for c in result.cells],
        "groups": result.groups,
    }
    with _write_lock, get_conn() as conn:
        conn.execute("DELETE FROM conflicts WHERE solution_id IN "
                     "(SELECT id FROM solutions WHERE session_id=?)", (sid,))
        conn.execute("DELETE FROM solutions WHERE session_id=?", (sid,))
        cur = conn.execute(
            """INSERT INTO solutions
               (session_id, job_id, revision, status, optimal, objective_proven,
                total_changes, payload, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                job_id,
                revision,
                result.status,
                int(result.optimal),
                int(result.objective_proven),
                result.total_changes,
                json.dumps(payload, ensure_ascii=False),
                _now(),
            ),
        )
        sol_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO conflicts(solution_id, kind, label, description, row_id, field)
               VALUES (?,?,?,?,?,?)""",
            [
                (
                    sol_id,
                    c.kind,
                    c.label,
                    c.description,
                    c.row_id,
                    getattr(c, "fld", None),
                )
                for c in result.conflicts
            ],
        )


def get_solution(sid: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM solutions WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        out["conflicts"] = [
            dict(c)
            for c in conn.execute(
                "SELECT kind, label, description, row_id, field FROM conflicts "
                "WHERE solution_id=?",
                (row["id"],),
            ).fetchall()
        ]
        return out


def get_confirmation(sid: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM confirmations WHERE session_id=?", (sid,)
        ).fetchone()
        return dict(row) if row else None


def add_confirmation(sid: int, solution_id: int, revision: int) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute("DELETE FROM confirmations WHERE session_id=?", (sid,))
        conn.execute(
            "INSERT INTO confirmations(session_id, solution_id, revision, created_at) "
            "VALUES (?,?,?,?)",
            (sid, solution_id, revision, _now()),
        )


# ---------------------------------------------------------------- 高株区间分析
def create_analysis(sid: int, revision: int, variety: str, threshold: float) -> int:
    """登记一次分析请求；同会话未完成的旧请求标记为被取代。"""
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE analyses SET status='superseded', finished_at=? "
            "WHERE session_id=? AND status IN ('pending','running')",
            (_now(), sid),
        )
        cur = conn.execute(
            "INSERT INTO analyses(session_id, revision, variety, threshold, status, created_at)"
            " VALUES (?,?,?,?, 'pending', ?)",
            (sid, revision, variety, threshold, _now()),
        )
        return cur.lastrowid


def mark_analysis_running(analysis_id: int) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE analyses SET status='running' WHERE id=? AND status='pending'",
            (analysis_id,),
        )


def supersede_analysis(analysis_id: int) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE analyses SET status='superseded', finished_at=? "
            "WHERE id=? AND status IN ('pending','running')",
            (_now(), analysis_id),
        )


def save_analysis(
    analysis_id: int,
    sid: int,
    revision: int,
    *,
    status: str,
    min_count: Optional[int] = None,
    max_count: Optional[int] = None,
    min_rows: Optional[List[str]] = None,
    max_rows: Optional[List[str]] = None,
    reason: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """原子写入分析结果。

    仅当会话修订号仍为 revision 且该分析仍待决（pending/running）时落库；
    否则（核实值已变、记录已被版本清理、或已被新请求取代）标记 superseded
    并返回 False——迟到计算绝不替换新版结果。
    """
    with _write_lock, get_conn() as conn:
        rev_row = conn.execute(
            "SELECT revision FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        row = conn.execute(
            "SELECT status FROM analyses WHERE id=?", (analysis_id,)
        ).fetchone()
        if row is None:
            return False  # 版本变更时已随旧区间一并清除
        current_rev = rev_row[0] if rev_row else None
        if current_rev != revision or row["status"] not in ("pending", "running"):
            if row["status"] in ("pending", "running"):
                conn.execute(
                    "UPDATE analyses SET status='superseded', finished_at=? WHERE id=?",
                    (_now(), analysis_id),
                )
            return False
        conn.execute(
            """UPDATE analyses
               SET status=?, min_count=?, max_count=?, min_rows=?, max_rows=?,
                   reason=?, error=?, finished_at=?
               WHERE id=?""",
            (
                status,
                min_count,
                max_count,
                json.dumps(min_rows, ensure_ascii=False)
                if min_rows is not None
                else None,
                json.dumps(max_rows, ensure_ascii=False)
                if max_rows is not None
                else None,
                reason,
                error,
                _now(),
                analysis_id,
            ),
        )
        return True


def get_latest_analysis(sid: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["min_rows"] = json.loads(out["min_rows"]) if out["min_rows"] else None
        out["max_rows"] = json.loads(out["max_rows"]) if out["max_rows"] else None
        return out
