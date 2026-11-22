"""冷链血缘边登记（甲路径封死：只许同事务正式边，无影子/草案通道）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import LineageIn

router = APIRouter(tags=["lineage"])

FORMAL_STATE = "正式"
PARENT_REF_PREFIX = "父批:"
# 纪要封死：乙路径整体禁用，任何影子/草案形态一律拒写
FORBIDDEN_STATE_TOKENS = ("影子", "草案", "shadow", "draft", "pending", "晋升")


def parent_code_of(cargo_ref: str) -> str | None:
    """从货载引用解析「父批:CODE」声明；无声明返回 None。"""
    if not cargo_ref:
        return None
    if cargo_ref.startswith(PARENT_REF_PREFIX):
        return cargo_ref.split(":", 1)[1].strip() or None
    return None


def insert_formal_inbound_edge(conn, parent_id: int, child_id: int, detail: str) -> int:
    """同事务补一条正式父子边。仅可在调用方事务内调用，零增完整一笔。

    - 同一父子对正式边已存在：409（防止重复补边）；
    - 插入失败由调用方整体回滚，绝不留半截。
    """
    exists = conn.execute(
        """SELECT id FROM lineage_edges
           WHERE from_batch_id=? AND to_batch_id=? AND lineage_state=?""",
        (parent_id, child_id, FORMAL_STATE),
    ).fetchone()
    if exists:
        raise HTTPException(409, "该父子正式血缘边已存在，禁止重复补边")
    cur = conn.execute(
        """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,
           lineage_state,created_at)
           VALUES (?,?,?,?,?,?)""",
        (parent_id, child_id, "分装衍生", detail, FORMAL_STATE, now_iso()),
    )
    return int(cur.lastrowid)


def batch_lineage_state(conn, batch) -> dict:
    """五页同源血统判定。真相源：lineage_edges 正式行 + cargo_ref 父批声明。"""
    cargo_ref = batch["cargo_ref"] or ""
    parent_code = parent_code_of(cargo_ref)
    child_id = batch["id"]
    if parent_code is None:
        # 无父批声明：独立入库，无血统义务
        return {
            "lineage_required": False,
            "lineage_complete": True,
            "lineage_state": FORMAL_STATE,
            "parent_code": None,
            "formal_inbound_edges": 0,
        }
    parent = conn.execute(
        "SELECT id, batch_code FROM temp_batches WHERE batch_code=?",
        (parent_code,),
    ).fetchone()
    edge_count = conn.execute(
        """SELECT COUNT(*) c FROM lineage_edges
           WHERE to_batch_id=? AND lineage_state=?""",
        (child_id, FORMAL_STATE),
    ).fetchone()["c"]
    complete = parent is not None and edge_count == 1
    return {
        "lineage_required": True,
        "lineage_complete": complete,
        "lineage_state": FORMAL_STATE if complete else "断链",
        "parent_code": parent_code,
        "formal_inbound_edges": edge_count,
    }


def _reject_non_formal(*texts: str) -> None:
    for t in texts:
        low = (t or "").lower()
        for token in FORBIDDEN_STATE_TOKENS:
            if token in t or token in low:
                raise HTTPException(
                    422,
                    "入库血缘纪要封死甲路径：影子/草案连边禁用，只许正式边",
                )


@router.get("/api/lineage")
def list_edges(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*, a.batch_code from_code, b.batch_code to_code
               FROM lineage_edges e
               JOIN temp_batches a ON a.id=e.from_batch_id
               LEFT JOIN temp_batches b ON b.id=e.to_batch_id
               WHERE e.lineage_state=?
               ORDER BY e.id DESC""",
            (FORMAL_STATE,),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    _reject_non_formal(body.edge_type, body.detail)
    with connect() as conn:
        src = conn.execute(
            "SELECT id FROM temp_batches WHERE id=?", (body.from_batch_id,)
        ).fetchone()
        if not src:
            raise HTTPException(404, "来源批次不存在")
        if body.to_batch_id:
            dst = conn.execute(
                "SELECT id FROM temp_batches WHERE id=?", (body.to_batch_id,)
            ).fetchone()
            if not dst:
                raise HTTPException(404, "目标批次不存在")
            dup = conn.execute(
                """SELECT id FROM lineage_edges
                   WHERE from_batch_id=? AND to_batch_id=? AND lineage_state=?""",
                (body.from_batch_id, body.to_batch_id, FORMAL_STATE),
            ).fetchone()
            if dup:
                raise HTTPException(409, "该父子正式血缘边已存在")
        cur = conn.execute(
            """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,
               lineage_state,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                body.from_batch_id,
                body.to_batch_id,
                body.edge_type,
                body.detail,
                FORMAL_STATE,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_add",
            str(body.from_batch_id),
            {
                "edge_id": cur.lastrowid,
                "type": body.edge_type,
                "lineage_state": FORMAL_STATE,
            },
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "edge_type": body.edge_type,
            "lineage_state": FORMAL_STATE,
        }


@router.get("/api/lineage/batch/{batch_id}")
def batch_lineage(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT id,cargo_ref FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        out_edges = conn.execute(
            """SELECT * FROM lineage_edges
               WHERE from_batch_id=? AND lineage_state=?""",
            (batch_id, FORMAL_STATE),
        ).fetchall()
        in_edges = conn.execute(
            """SELECT * FROM lineage_edges
               WHERE to_batch_id=? AND lineage_state=?""",
            (batch_id, FORMAL_STATE),
        ).fetchall()
        return {
            "outbound": [dict(e) for e in out_edges],
            "inbound": [dict(e) for e in in_edges],
            "lineage": batch_lineage_state(conn, batch),
        }
