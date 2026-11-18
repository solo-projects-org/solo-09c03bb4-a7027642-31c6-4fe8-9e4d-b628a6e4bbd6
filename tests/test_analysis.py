"""高株数量区间分析测试：两端极值、整表约束、不可判定、版本交错、非有限输入。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.solver import LockSpec, solve, tall_count_bounds  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("BREEDER_DB_PATH", str(db_file))
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 求解器级用例
def make_rows(specs):
    """specs: (row_id, plant, tray, variety, house, height)。"""
    return [
        {
            "row_id": rid,
            "line_no": str(rid),
            "plant_no": plant,
            "tray_no": tray,
            "variety": v,
            "house": h,
            "height": height,
        }
        for rid, plant, tray, v, h, height in specs
    ]


PAIRS_ALL = [("V1", "H1"), ("V1", "H2"), ("V2", "H1"), ("V2", "H2")]


def test_bounds_ambiguous_single_cell():
    # 行1 品种 V9 不在取值域必须改；V1/V2 各花 1 格，均为最优
    rows = make_rows([(1, "P1", "T1", "V9", "H1", "50")])
    res = solve(rows, PAIRS_ALL, [])
    assert res.total_changes == 1
    out = tall_count_bounds(rows, PAIRS_ALL, [], "V1", 45.0, res.total_changes)
    assert out.status == "ok"
    assert (out.min_count, out.max_count) == (0, 1)
    assert out.min_rows == [] and out.max_rows == ["1"]
    # 阈值高于所有行：两端均为 0
    out_hi = tall_count_bounds(rows, PAIRS_ALL, [], "V1", 100.0, res.total_changes)
    assert (out_hi.min_count, out_hi.max_count) == (0, 0)
    assert out_hi.min_rows == [] and out_hi.max_rows == []


def test_bounds_joint_whole_table_not_stitched():
    # 同托盘耦合：允许组合 (V1,H1)/(V2,H2)，托盘温室是一个整体变量，
    # 整表最优方案只有 (V1,V1,H1) 与 (V2,V2,H2) 两种。
    pairs = [("V1", "H1"), ("V2", "H2")]
    rows = make_rows(
        [
            (1, "P1", "T1", "V9", "H9", "50"),
            (2, "P2", "T1", "V8", "H9", "60"),
        ]
    )
    res = solve(rows, pairs, [])
    assert res.total_changes == 4
    out = tall_count_bounds(rows, pairs, [], "V1", 45.0, res.total_changes)
    assert out.status == "ok"
    assert (out.min_count, out.max_count) == (0, 2)
    assert out.min_rows == [] and out.max_rows == ["1", "2"]
    # 阈值只留行2：最大值方案中只有行2 计入
    out2 = tall_count_bounds(rows, pairs, [], "V1", 55.0, res.total_changes)
    assert (out2.min_count, out2.max_count) == (0, 1)
    assert out2.max_rows == ["2"]


def test_bounds_respects_locks():
    rows = make_rows([(1, "P1", "T1", "V9", "H1", "50")])
    locks = [LockSpec(1, "variety", "V2", "candidate")]
    res = solve(rows, PAIRS_ALL, locks)
    assert res.total_changes == 1
    out = tall_count_bounds(rows, PAIRS_ALL, locks, "V1", 45.0, res.total_changes)
    assert (out.min_count, out.max_count) == (0, 0)
    out2 = tall_count_bounds(rows, PAIRS_ALL, locks, "V2", 45.0, res.total_changes)
    assert (out2.min_count, out2.max_count) == (1, 1)
    assert out2.max_rows == ["1"]


def test_bounds_unproven_extremum_is_undecidable(monkeypatch):
    # 模拟求解器无法在时限内证明极值（返回 UNKNOWN）
    from ortools.sat.python import cp_model

    import app.solver as solver_mod

    rows = make_rows([(1, "P1", "T1", "V9", "H1", "50")])
    res = solve(rows, PAIRS_ALL, [])
    monkeypatch.setattr(
        solver_mod.cp_model.CpSolver, "Solve", lambda self, m: cp_model.UNKNOWN
    )
    out = tall_count_bounds(rows, PAIRS_ALL, [], "V1", 45.0, res.total_changes)
    assert out.status == "undecidable"
    assert out.min_count is None and out.max_count is None
    assert out.min_rows == [] and out.max_rows == []


def test_bounds_rejects_non_finite_height():
    rows = make_rows([(1, "P1", "T1", "V9", "H1", "NaN")])
    res = solve(rows, PAIRS_ALL, [])
    out = tall_count_bounds(rows, PAIRS_ALL, [], "V1", 45.0, res.total_changes)
    assert out.status == "undecidable"


# ---------------------------------------------------------------- API 级用例
INSPECTION = (
    "行号,植株号,托盘号,品种,温室,株高\n"
    "1,P1,T1,V9,H1,50\n"
    "2,P2,T2,V1,H2,60\n"
)
ALLOWED = "品种,温室\nV1,H1\nV1,H2\nV2,H1\nV2,H2\n"


def upload(client: TestClient, body: str = INSPECTION, allowed: str = ALLOWED):
    files = {
        "inspection": ("insp.csv", body.encode("utf-8"), "text/csv"),
        "allowed": ("allow.csv", allowed.encode("utf-8"), "text/csv"),
    }
    r = client.post("/api/sessions", files=files)
    assert r.status_code == 201, r.text
    return r.json()


def wait_job(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job_id}").json()["status"]
        if status in ("done", "error", "superseded"):
            return status
        time.sleep(0.1)
    raise AssertionError("作业超时未完成")


def wait_solution(client, sid, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/sessions/{sid}").json()
        if d["solution_status"] in ("optimal", "feasible", "infeasible", "failed"):
            if not (d["job_status"] in ("pending", "running")):
                return d
        time.sleep(0.1)
    raise AssertionError("方案超时未生成")


def wait_analysis(client, sid, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = client.get(f"/api/sessions/{sid}/analysis").json()
        if a["status"] in ("done", "undecidable", "error", "superseded"):
            return a
        time.sleep(0.1)
    raise AssertionError("分析超时未完成")


def post_analysis(client, sid, variety="V1", threshold=45):
    return client.post(
        f"/api/sessions/{sid}/analysis",
        json={"variety": variety, "threshold": threshold},
    )


def test_analysis_interval_both_ends(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = post_analysis(client, sid, "V1", 45)
    assert r.status_code == 200, r.text
    a = wait_analysis(client, sid)
    assert a["status"] == "done"
    # 行1 品种必须改（V9→V1 或 V2），行2 恒为 V1：min=1（行2），max=2（行1+行2）
    assert (a["min_count"], a["max_count"]) == (1, 2)
    assert a["min_rows"] == ["2"]
    assert a["max_rows"] == ["1", "2"]
    assert a["stale"] is False and a["revision"] == a["current_revision"]
    # 阈值高于所有行
    post_analysis(client, sid, "V1", 100)
    a2 = wait_analysis(client, sid)
    assert a2["status"] == "done"
    assert (a2["min_count"], a2["max_count"]) == (0, 0)


def test_analysis_undecidable_when_solution_stale(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 改核实值但不重新求解：当前没有已证最优方案
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V1", "source": "original"},
    )
    assert r.status_code == 200
    assert post_analysis(client, sid).status_code == 200
    a = wait_analysis(client, sid)
    assert a["status"] == "undecidable"
    assert a["min_count"] is None and a["max_count"] is None
    assert a["min_rows"] is None and a["max_rows"] is None


def test_analysis_undecidable_when_infeasible(client):
    # 同株两行分别锁 V1/V2 → 同株品种一致冲突 → 无解 → 不可判定
    body = (
        "行号,植株号,托盘号,品种,温室,株高\n"
        "1,P1,T1,V1,H1,50\n"
        "2,P1,T2,V2,H2,60\n"
    )
    info = upload(client, body=body)
    sid = info["session_id"]
    wait_solution(client, sid)
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 1, "field": "variety", "value": "V1", "source": "candidate"},
    )
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V2", "source": "candidate"},
    )
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "infeasible"
    assert post_analysis(client, sid).status_code == 200
    a = wait_analysis(client, sid)
    assert a["status"] == "undecidable"
    assert a["min_count"] is None


def test_analysis_invalidated_immediately_on_lock_change(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    post_analysis(client, sid, "V1", 45)
    a = wait_analysis(client, sid)
    assert a["status"] == "done"
    old_id = a["id"]
    old_rev = a["revision"]
    # 核实值变化 → 旧区间立即失效
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V1", "source": "original"},
    )
    a2 = client.get(f"/api/sessions/{sid}/analysis").json()
    assert a2["status"] == "none"
    # 迟到写入不得复活旧区间
    ok = db.save_analysis(
        old_id, sid, old_rev, status="done",
        min_count=0, max_count=0, min_rows=[], max_rows=[],
    )
    assert ok is False
    assert client.get(f"/api/sessions/{sid}/analysis").json()["status"] == "none"


def test_late_analysis_job_cannot_overwrite_newer_revision(client, monkeypatch):
    # 让后台分析阻塞，期间修改核实值：迟到计算必须被丢弃
    import app.main as main_mod

    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    started = threading.Event()
    release = threading.Event()
    orig = main_mod.tall_count_bounds

    def slow_bounds(*args, **kwargs):
        started.set()
        release.wait(10)
        return orig(*args, **kwargs)

    monkeypatch.setattr(main_mod, "tall_count_bounds", slow_bounds)
    post_analysis(client, sid, "V1", 45)
    assert started.wait(10), "分析作业未启动"
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V1", "source": "original"},
    )
    release.set()
    # 单线程池：提交一个空任务，它完成即说明分析线程已结束（不会泄漏到下一个测试）
    drained = threading.Event()
    main_mod._pool().submit(lambda: drained.set())
    assert drained.wait(10), "后台分析线程未及时结束"
    a = client.get(f"/api/sessions/{sid}/analysis").json()
    assert a["status"] == "none", "迟到分析结果不得写入"
    # 重新求解 + 重新分析：新区间反映新核实值（行2 锁 V1，行1 必改 V1/V2）
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    wait_solution(client, sid)
    post_analysis(client, sid, "V1", 45)
    a = wait_analysis(client, sid)
    assert a["status"] == "done"
    assert (a["min_count"], a["max_count"]) == (1, 2)


def test_analysis_reflects_locks_after_resolve(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 锁行1 品种为 V2（候选值）后重算
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 1, "field": "variety", "value": "V2", "source": "candidate"},
    )
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    wait_solution(client, sid)
    post_analysis(client, sid, "V1", 45)
    a = wait_analysis(client, sid)
    assert a["status"] == "done"
    # 行1 锁 V2，只有行2 计入 V1
    assert (a["min_count"], a["max_count"]) == (1, 1)
    assert a["min_rows"] == ["2"] and a["max_rows"] == ["2"]


def test_analysis_rejects_non_finite_threshold_and_keeps_session(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    rev_before = client.get(f"/api/sessions/{sid}").json()["revision"]
    for bad in ["NaN", "Infinity", "-Infinity", "1e999", '"abc"']:
        r = client.post(
            f"/api/sessions/{sid}/analysis",
            content=f'{{"variety": "V1", "threshold": {bad}}}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422), (bad, r.status_code)
    # 原会话不受影响：修订号不变、无分析记录、方案仍可确认
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["revision"] == rev_before
    assert client.get(f"/api/sessions/{sid}/analysis").json()["status"] == "none"
    assert d["solution_status"] == "optimal" and not d["solution_stale"]


def test_analysis_rejects_non_finite_height_and_keeps_session(client):
    body = "行号,植株号,托盘号,品种,温室,株高\n1,P1,T1,V1,H1,NaN\n"
    info = upload(client, body=body)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = post_analysis(client, sid, "V1", 45)
    assert r.status_code == 400
    assert "株高" in r.json()["detail"]
    # 原会话保留：行数据与修订号不变
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["row_count"] == 1 and d["revision"] == 1
    assert client.get(f"/api/sessions/{sid}/analysis").json()["status"] == "none"


def test_analysis_rejects_unknown_variety_and_missing_session(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = post_analysis(client, sid, "V9", 45)
    assert r.status_code == 400
    assert client.get(f"/api/sessions/{sid}/analysis").json()["status"] == "none"
    assert client.get("/api/sessions/9999/analysis").status_code == 404
    assert post_analysis(client, 9999).status_code == 404


def test_confirm_download_still_work_with_analysis(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    post_analysis(client, sid, "V1", 45)
    a = wait_analysis(client, sid)
    assert a["status"] == "done"
    # 分析不影响确认与下载
    r = client.post(f"/api/sessions/{sid}/confirm")
    assert r.status_code == 200
    r = client.get(f"/api/sessions/{sid}/download")
    assert r.status_code == 200
    assert "行号,植株号,托盘号,品种,温室,株高" in r.content.decode("utf-8-sig")
