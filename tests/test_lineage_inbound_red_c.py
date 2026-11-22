"""入库断边三红测 · 丙：双会话同父批抢建子批须抢父批锁。

两个写会话（admin / ops）在同一父批下抢建新子批（故意落在不同仓间，
使仓间锁无法覆盖）：先到持父批锁，后到争用只记痕（contention_log），
不得增子批行、不得补血缘边。持锁会话自身可重入续建。

本测在缺陷代码上必红：写口只有仓间锁，同父批跨仓间抢建会双成。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

PARENT_CODE = "TB-CK-HD-01-001"
PARENT_LOCK = f"lineage-parent:{PARENT_CODE}"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _hn_chamber_probe(c, h):
    ch = next(
        x
        for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["chamber_code"] == "CK-HN-03"
    )
    probe = c.get(
        f"/api/probes?chamber_id={ch['id']}", headers=h
    ).json()["items"][0]
    return ch, probe


def test_second_session_contention_logged_without_child_row():
    c1 = TestClient(app)
    c2 = TestClient(app)
    h1 = _auth(c1, "admin")
    h2 = _auth(c2, "ops")
    ch, probe = _hn_chamber_probe(c1, h1)

    # 会话一：在途子批事务持有父批锁
    acq = c1.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": PARENT_LOCK}
    )
    assert acq.status_code == 200, acq.text

    with connect() as conn:
        batches_before = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
        edges_before = conn.execute(
            "SELECT COUNT(*) c FROM lineage_edges"
        ).fetchone()["c"]

    # 会话二：另一线程内抢建同一父批的子批（跨仓间，仓间锁管不到）
    result = {}

    def race():
        r = c2.post(
            "/api/batches",
            headers=h2,
            json={
                "chamber_id": ch["id"],
                "probe_id": probe["id"],
                "cargo_ref": f"父批:{PARENT_CODE}",
                "batch_code": "TB-LINEAGE-RED-C-OPS",
                "temp_points": [{"t": "10:00", "c": 4.4}],
            },
        )
        result["status"] = r.status_code
        result["body"] = r.text

    t = threading.Thread(target=race)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()

    # 后到争用：只记痕，不增子批行
    assert result["status"] == 409, result
    with connect() as conn:
        batches_after = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
        edges_after = conn.execute(
            "SELECT COUNT(*) c FROM lineage_edges"
        ).fetchone()["c"]
        ops_row = conn.execute(
            "SELECT id FROM temp_batches WHERE batch_code='TB-LINEAGE-RED-C-OPS'"
        ).fetchone()
        contentions = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (PARENT_LOCK,)
        ).fetchall()
    assert batches_after == batches_before
    assert edges_after == edges_before
    assert ops_row is None
    assert any(row["actor"] == "ops" for row in contentions), "后到争用须留痕"

    # 持锁会话可重入：自己的子批照常建成且同事务带正式边
    ok = c1.post(
        "/api/batches",
        headers=h1,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": f"父批:{PARENT_CODE}",
            "batch_code": "TB-LINEAGE-RED-C-ADMIN",
            "temp_points": [{"t": "11:00", "c": 4.5}],
        },
    )
    assert ok.status_code == 200, ok.text
    child_id = ok.json()["id"]
    with connect() as conn:
        parent = conn.execute(
            "SELECT id FROM temp_batches WHERE batch_code=?", (PARENT_CODE,)
        ).fetchone()
        edge_count = conn.execute(
            "SELECT COUNT(*) c FROM lineage_edges "
            "WHERE from_batch_id=? AND to_batch_id=?",
            (parent["id"], child_id),
        ).fetchone()["c"]
    assert edge_count == 1
