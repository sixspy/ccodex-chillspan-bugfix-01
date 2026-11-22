"""温控批次入库：温度点序列 JSON。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from lineage_svc import (
    FORMAL_STATE,
    batch_lineage_state,
    insert_formal_inbound_edge,
    parent_code_of,
)
from locks import is_held, release, require_lock
from models import BatchIn
from validators import summarize_points, validate_probe_online, validate_temp_point

router = APIRouter(tags=["batches"])


def _next_batch_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"TB-{prefix}-{n + 1:03d}"


@router.get("/api/batches")
def list_batches(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT b.*, c.chamber_code, p.probe_code FROM temp_batches b
                   JOIN cold_chambers c ON c.id=b.chamber_id
                   JOIN probes p ON p.id=b.probe_id
                   WHERE b.status=? ORDER BY b.id DESC""",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT b.*, c.chamber_code, p.probe_code FROM temp_batches b
                   JOIN cold_chambers c ON c.id=b.chamber_id
                   JOIN probes p ON p.id=b.probe_id
                   ORDER BY b.id DESC"""
            ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["temp_points"] = json.loads(d["temp_points"] or "[]")
            # 五页同源：批次页血统字段直接走统一判定，不另立口径
            d["lineage"] = batch_lineage_state(conn, r)
            items.append(d)
        return {"items": items}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*, c.name chamber_name, c.target_min, c.target_max, p.probe_code
               FROM temp_batches b
               JOIN cold_chambers c ON c.id=b.chamber_id
               JOIN probes p ON p.id=b.probe_id WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        data = dict(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        exc = conn.execute(
            "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return {
            "batch": data,
            "excursions": [dict(e) for e in exc],
            "seals": [dict(s) for s in seals],
            "lineage": batch_lineage_state(conn, row),
        }


@router.post("/api/batches")
def create_batch(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    chamber_lock = f"chamber:{body.chamber_id}"
    parent_code = parent_code_of(body.cargo_ref)
    parent_lock = f"lineage-parent:{parent_code}" if parent_code else None
    with connect() as conn:
        # 锁序：先仓间锁，后父批锁；后到争用只记痕不写业务
        chamber_preheld = is_held(conn, chamber_lock, user)
        require_lock(conn, chamber_lock, user)
        chamber_acquired = not chamber_preheld
        parent_acquired = False
        if parent_lock is not None:
            parent_preheld = is_held(conn, parent_lock, user)
            try:
                require_lock(conn, parent_lock, user)
                parent_acquired = not parent_preheld
            except HTTPException:
                # 争用痕独立提交落库，随后回滚业务事务并拒收
                conn.commit()
                _release_if_acquired(conn, chamber_lock, user, chamber_acquired)
                raise
        try:
            ch = conn.execute(
                "SELECT * FROM cold_chambers WHERE id=?", (body.chamber_id,)
            ).fetchone()
            if not ch:
                raise HTTPException(404, "仓间不存在")
            active = conn.execute(
                """SELECT COUNT(*) c FROM temp_batches
                   WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
                (body.chamber_id,),
            ).fetchone()["c"]
            if active >= int(ch["quota_batches"]):
                raise HTTPException(409, "仓间在批配额已满")
            probe = conn.execute(
                "SELECT * FROM probes WHERE id=?", (body.probe_id,)
            ).fetchone()
            if not probe or probe["chamber_id"] != body.chamber_id:
                raise HTTPException(422, "探头与仓间不匹配")
            validate_probe_online(probe)
            points = body.temp_points or []
            for pt in points:
                validate_temp_point(
                    float(pt.get("c", pt.get("temp", 0))),
                    float(ch["target_min"]),
                    float(ch["target_max"]),
                )
            tmin, tmax = summarize_points(points)
            code = body.batch_code or _next_batch_code(conn, body.chamber_id)
            if conn.execute(
                "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
            ).fetchone():
                raise HTTPException(409, "批次编码已存在")
            # 甲路径：先解析父批，父批不存在则整笔回滚拒收，绝不留孤儿子批
            parent_row = None
            if parent_code is not None:
                parent_row = conn.execute(
                    "SELECT id, batch_code FROM temp_batches WHERE batch_code=?",
                    (parent_code,),
                ).fetchone()
                if not parent_row:
                    raise HTTPException(404, f"父批不存在：{parent_code}，整笔回滚拒收")
            status = "已入库" if points else "草稿"
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
                   temp_points,generation,status,min_observed,max_observed,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.chamber_id,
                    body.probe_id,
                    body.cargo_ref,
                    json.dumps(points, ensure_ascii=False),
                    1,
                    status,
                    tmin if points else None,
                    tmax if points else None,
                    now,
                ),
            )
            child_id = cur.lastrowid
            edge_id = None
            if parent_row is not None:
                # 同一事务、同一连接、单次 commit：正式边零增完整一笔
                edge_id = insert_formal_inbound_edge(
                    conn,
                    int(parent_row["id"]),
                    int(child_id),
                    f"{parent_row['batch_code']} 分装衍生至 {code}",
                )
            write_audit(
                conn,
                user,
                "batch_inbound",
                code,
                {
                    "batch_id": child_id,
                    "point_count": len(points),
                    "parent_code": parent_code,
                    "lineage_ok": parent_row is None or edge_id is not None,
                    "lineage_state": FORMAL_STATE if edge_id is not None else None,
                },
            )
            conn.commit()
            return {
                "id": child_id,
                "batch_code": code,
                "status": status,
                "point_count": len(points),
                "lineage_edge_id": edge_id,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            # 只释放本请求新取得的锁，外部预持锁原样保留（逆序释放）
            _release_if_acquired(conn, parent_lock, user, parent_acquired)
            _release_if_acquired(conn, chamber_lock, user, chamber_acquired)


def _release_if_acquired(conn, lock_key, holder: str, acquired: bool) -> None:
    if not acquired or lock_key is None:
        return
    if is_held(conn, lock_key, holder):
        release(conn, lock_key, holder)
        conn.commit()