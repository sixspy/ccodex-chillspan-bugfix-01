"""入库断边三红测 · 乙：未晋升的连边不得被血缘页/对账页当作正式血统。

口径甲封死后，系统内不存在「影子连边草案」晋升通道：cargo_ref 中的
「父批:CODE」声明只是货载文本，不是血缘边。任何批次只要在正式
lineage_edges 中没有入边，对账互证就必须报 lineage_complete=false，
血缘页也不得展示其父向边——两页同源，禁止假完整。

本测在缺陷代码上必红：对账互证 lineage_complete 恒为 True。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

SEED_ORPHAN = "TB-CK-HD-01-SUB-001"
SEED_PARENT = "TB-CK-HD-01-001"


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


def test_unpromoted_claim_must_not_read_as_formal_lineage():
    c = TestClient(app)
    h = _auth(c)
    with connect() as conn:
        orphan = conn.execute(
            "SELECT id FROM temp_batches WHERE batch_code=?", (SEED_ORPHAN,)
        ).fetchone()
        parent = conn.execute(
            "SELECT id FROM temp_batches WHERE batch_code=?", (SEED_PARENT,)
        ).fetchone()
    assert orphan is not None, "种子须先打出入库成功却无父子边的假绿子批"

    # 血缘页：该子批没有任何正式入边（声明从未晋升为边）
    inbound = c.get(
        f"/api/lineage/batch/{orphan['id']}", headers=h
    ).json()["inbound"]
    assert inbound == []

    edges = c.get("/api/lineage", headers=h).json()["items"]
    assert not any(e.get("to_code") == SEED_ORPHAN for e in edges)
    # 血缘页只许出现正式边：任何影子/草案形态都不得混上正式血缘页
    assert not any(
        ("影子" in (e.get("edge_type") or "")) or ("草案" in (e.get("edge_type") or ""))
        for e in edges
    )

    # 对账页：零正式入边却报血统完整，就是「影子连边被当成正式血统」
    proof = c.get(f"/api/proof/batch/{SEED_ORPHAN}", headers=h).json()
    assert proof["lineage_complete"] is False
    assert proof["proof"]["lineage_complete"] is False

    # 正式补边之后两页须立刻同源转真
    ch = next(
        x
        for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["chamber_code"] == "CK-HN-03"
    )
    probe = c.get(
        f"/api/probes?chamber_id={ch['id']}", headers=h
    ).json()["items"][0]
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": f"父批:{SEED_PARENT}",
            "batch_code": "TB-LINEAGE-RED-B-001",
            "temp_points": [{"t": "08:00", "c": 4.2}],
        },
    )
    assert r.status_code == 200, r.text
    child_code = r.json()["batch_code"]

    page_edges = c.get("/api/lineage", headers=h).json()["items"]
    formal = [
        e
        for e in page_edges
        if e.get("to_code") == child_code and e.get("from_code") == SEED_PARENT
    ]
    assert len(formal) == 1
    proof2 = c.get(f"/api/proof/batch/{child_code}", headers=h).json()
    assert proof2["lineage_complete"] is True
    # 五页同源：对账结论与血缘页边数必须一致
    assert proof2["lineage_complete"] == (len(formal) == 1)
    # 未受影响的无父批次（独立入库）不应被误判为缺边：无父批声明即无血统义务
    standalone = c.get(
        "/api/proof/batch/TB-CK-HB-02-001", headers=h
    ).json()
    assert standalone["lineage_complete"] is True
