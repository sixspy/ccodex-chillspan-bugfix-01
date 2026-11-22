"""收工核验：五页血统同源 + 甲路径封死 + 假绿种子如实暴露。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db

tmp = Path(tempfile.mkdtemp()) / "verify.db"
db.DB_PATH = tmp
db.init_db()

from fastapi.testclient import TestClient

from app import app

c = TestClient(app)
tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
h = {"Authorization": "Bearer " + tok}

ORPHAN = "TB-CK-HD-01-SUB-001"
PARENT = "TB-CK-HD-01-001"

# 1) 假绿种子：对账页/批次页/血缘页都须如实显示断链
proof = c.get(f"/api/proof/batch/{ORPHAN}", headers=h).json()
assert proof["lineage_complete"] is False, proof
batch_list = c.get("/api/batches", headers=h).json()["items"]
b_orphan = next(b for b in batch_list if b["batch_code"] == ORPHAN)
assert b_orphan["lineage"]["lineage_complete"] is False
detail = c.get(f"/api/batches/{b_orphan['id']}", headers=h).json()
assert detail["lineage"]["lineage_state"] == "断链"
lineage = c.get("/api/lineage", headers=h).json()["items"]
assert not any(e.get("to_code") == ORPHAN for e in lineage)
bl = c.get(f"/api/lineage/batch/{b_orphan['id']}", headers=h).json()
assert bl["inbound"] == [] and bl["lineage"]["lineage_complete"] is False
print("1) 假绿种子三页同源断链：OK")

# 2) 新建子批：同事务正式边，五页同源转真
ch = next(x for x in c.get("/api/chambers", headers=h).json()["items"]
          if x["chamber_code"] == "CK-HN-03")
probe = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"][0]
r = c.post("/api/batches", headers=h, json={
    "chamber_id": ch["id"], "probe_id": probe["id"],
    "cargo_ref": f"父批:{PARENT}", "batch_code": "TB-VERIFY-CHILD-1",
    "temp_points": [{"t": "08:00", "c": 4.0}]})
assert r.status_code == 200, r.text
child = r.json()
assert child["lineage_edge_id"] is not None
proof2 = c.get(f"/api/proof/batch/TB-VERIFY-CHILD-1", headers=h).json()
assert proof2["lineage_complete"] is True
bl2 = c.get(f"/api/lineage/batch/{child['id']}", headers=h).json()
assert len(bl2["inbound"]) == 1 and bl2["inbound"][0]["lineage_state"] == "正式"
lineage2 = c.get("/api/lineage", headers=h).json()["items"]
edge = next(e for e in lineage2 if e["to_code"] == "TB-VERIFY-CHILD-1")
assert edge["from_code"] == PARENT and edge["lineage_state"] == "正式"
b2 = next(b for b in c.get("/api/batches", headers=h).json()["items"]
          if b["batch_code"] == "TB-VERIFY-CHILD-1")
assert b2["lineage"]["formal_inbound_edges"] == 1
print("2) 同事务正式边、批次页/血缘页/对账页同源：OK")

# 3) 孤儿子批整笔回滚
before = c.get("/api/batches", headers=h).json()["items"]
r3 = c.post("/api/batches", headers=h, json={
    "chamber_id": ch["id"], "probe_id": probe["id"],
    "cargo_ref": "父批:TB-GHOST-404", "batch_code": "TB-VERIFY-ORPHAN",
    "temp_points": [{"t": "08:00", "c": 4.0}]})
assert r3.status_code == 404, r3.text
codes = [b["batch_code"] for b in c.get("/api/batches", headers=h).json()["items"]]
assert "TB-VERIFY-ORPHAN" not in codes and len(codes) == len(before)
print("3) 父批缺失整笔回滚拒收：OK")

# 4) 影子/草案拒写（乙路径封死）
shadow = c.post("/api/lineage", headers=h, json={
    "from_batch_id": 1, "to_batch_id": child["id"],
    "edge_type": "影子连边草案", "detail": "待晋升"})
assert shadow.status_code == 422, shadow.text
draft = c.post("/api/lineage", headers=h, json={
    "from_batch_id": 1, "to_batch_id": child["id"],
    "edge_type": "分装衍生", "detail": "draft 影子"})
assert draft.status_code == 422, draft.text
# 重复正式边 409
dup = c.post("/api/lineage", headers=h, json={
    "from_batch_id": next(b for b in before if b["batch_code"] == PARENT)["id"],
    "to_batch_id": child["id"], "edge_type": "分装衍生", "detail": "x"})
assert dup.status_code == 409, dup.text
print("4) 影子/草案拒写、重复正式边拦截：OK")

# 5) 温谱页与仓间页同源
spec = c.get("/api/spectrum", headers=h).json()["items"]
for s in spec:
    assert "lineage" in s and "lineage_state" in s["lineage"]
recon = c.get("/api/reconcile", headers=h).json()
hd = next(i for i in recon["items"] if i["chamber_code"] == "CK-HD-01")
assert ORPHAN in hd["lineage_broken_batches"] and hd["lineage_broken_count"] >= 1
assert recon["summary"]["lineage_broken"] >= 1
chd = c.get(f"/api/chambers/{hd['chamber_id']}", headers=h).json()
assert ORPHAN in chd["lineage_broken_batches"]
# 独立批无血统义务，计完整
assert c.get("/api/proof/batch/TB-CK-HB-02-001", headers=h).json()["lineage_complete"] is True
print("5) 温谱页/仓间页/对账页同源，独立批不误报：OK")

# 6) 双会话争用：后到只记痕不增批
h2 = {"Authorization": "Bearer " + c.post(
    "/api/login", json={"username": "ops", "password": "123456"}).json()["token"]}
lk = f"lineage-parent:{PARENT}"
assert c.post("/api/locks/acquire", headers=h, json={"lock_key": lk}).status_code == 200
race = c.post("/api/batches", headers=h2, json={
    "chamber_id": ch["id"], "probe_id": probe["id"],
    "cargo_ref": f"父批:{PARENT}", "batch_code": "TB-VERIFY-RACE",
    "temp_points": [{"t": "09:00", "c": 4.1}]})
assert race.status_code == 409, race.text
codes = [b["batch_code"] for b in c.get("/api/batches", headers=h).json()["items"]]
assert "TB-VERIFY-RACE" not in codes
cont = c.get("/api/contention", headers=h).json()["items"]
assert any(x["lock_key"] == lk and x["actor"] == "ops" for x in cont)
# 持锁人重入仍可建
ok = c.post("/api/batches", headers=h, json={
    "chamber_id": ch["id"], "probe_id": probe["id"],
    "cargo_ref": f"父批:{PARENT}", "batch_code": "TB-VERIFY-REENTER",
    "temp_points": [{"t": "10:00", "c": 4.2}]})
assert ok.status_code == 200, ok.text
assert ok.json()["lineage_edge_id"] is not None
# 预持锁未被写口 finally 误释放
locks = c.get("/api/locks", headers=h).json()["items"]
assert any(x["lock_key"] == lk and x["holder"] == "admin" for x in locks)
print("6) 双会话父批锁争用记痕、不增批、持锁人重入、预持锁保留：OK")

print("\n全部收工核验通过。")
