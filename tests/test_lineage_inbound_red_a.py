"""入库断边三红测 · 甲：正式子批一经提交，正式血缘边必须同事务零增完整一笔。

口径甲（同事务正式边）：
- 写口成功的子批（cargo_ref 形如「父批:CODE」）必须在同一事务内恰好补一条
  正式 lineage_edges 行，边从父批指向新子批；
- 父批不存在等无法补边的情形，整笔回滚拒收，不得留下子批行。
本测在缺陷代码上必红：批次写口只记审计不插边。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _pick_chamber_probe(c, h):
    ch = next(
        x
        for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["chamber_code"] == "CK-HN-03"
    )
    probe = c.get(
        f"/api/probes?chamber_id={ch['id']}", headers=h
    ).json()["items"][0]
    return ch, probe


def test_subbatch_commit_inserts_exactly_one_formal_edge():
    c = TestClient(app)
    h = _auth(c)
    ch, probe = _pick_chamber_probe(c, h)
    with connect() as conn:
        parent = conn.execute(
            "SELECT id, batch_code FROM temp_batches "
            "WHERE batch_code='TB-CK-HD-01-001'"
        ).fetchone()
    payload = {
        "chamber_id": ch["id"],
        "probe_id": probe["id"],
        "cargo_ref": f"父批:{parent['batch_code']}",
        "batch_code": "TB-LINEAGE-RED-A-001",
        "temp_points": [{"t": "08:00", "c": 4.1}, {"t": "09:00", "c": 4.3}],
    }
    r = c.post("/api/batches", headers=h, json=payload)
    assert r.status_code == 200, r.text
    child_id = r.json()["id"]

    # 正式子批已存在（写口假绿条件成立）
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE id=?", (child_id,)
        ).fetchone()["c"] == 1
        # 盯死：正式血缘边仍为零行即为断边假绿
        edges = conn.execute(
            "SELECT * FROM lineage_edges WHERE from_batch_id=? AND to_batch_id=?",
            (parent["id"], child_id),
        ).fetchall()
    assert len(edges) == 1, "子批已正式入库却无父子正式血缘边（断边）"
    edge = edges[0]
    assert edge["to_batch_id"] == child_id
    assert edge["from_batch_id"] == parent["id"]
    # 口径甲只许正式边：不得出现影子/草案形态的边
    assert "影子" not in edge["edge_type"] and "草案" not in edge["edge_type"]
    # 同事务零增：不得重复补边
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM lineage_edges WHERE to_batch_id=?", (child_id,)
        ).fetchone()["c"]
    assert total == 1


def test_missing_parent_rolls_back_whole_batch():
    c = TestClient(app)
    h = _auth(c)
    ch, probe = _pick_chamber_probe(c, h)
    with connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "父批:TB-NEVER-EXISTS-999",
            "batch_code": "TB-LINEAGE-RED-A-ORPHAN",
            "temp_points": [{"t": "08:00", "c": 4.0}],
        },
    )
    # 补不出正式边必须整笔回滚拒收
    assert r.status_code in (404, 409, 422), r.text
    with connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
        orphan = conn.execute(
            "SELECT id FROM temp_batches WHERE batch_code='TB-LINEAGE-RED-A-ORPHAN'"
        ).fetchone()
    assert after == before
    assert orphan is None
