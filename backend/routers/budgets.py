"""Annual budgets stored as immutable Workpapers events."""
import json
import secrets
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from ..database import get_connection
from .common import assert_account_set
from .finance_controls import identity

router = APIRouter(prefix="/api/control/budgets", tags=["全面预算"])
PROJECTS = {"revenue", "cost", "sales", "admin", "research", "capex"}
MAX_CENTS = 100_000_000_000_000


class BudgetChange(BaseModel):
    year: int = Field(ge=1902, le=9998)
    dept: str = Field(max_length=60)
    revision: int = Field(ge=0)
    kind: str
    actor: str = Field(default="", max_length=60)
    base: dict[str, list[StrictInt | None]] | None = None
    project: str = ""
    month: int = 0
    delta: StrictInt = 0
    reason: str = Field(default="", max_length=500)
    request_id: str = Field(default="", max_length=80)


def read_events(conn, account_set_id, prefix="budget-plan:"):
    return [
        json.loads(row[0])
        for row in conn.execute(
            """SELECT payload_json FROM app_records
               WHERE account_set_id=? AND store_name='Workpapers'
                 AND substr(record_id,1,length(?))=?""",
            (account_set_id, prefix, prefix),
        )
    ]


def invalid(message, code=422):
    raise HTTPException(code, message)


def identify(conn, request, change):
    account = identity(conn, request)
    if conn.execute("SELECT 1 FROM fw_users LIMIT 1").fetchone():
        roles = {"admin", "reviewer"} if change.kind in {"approve", "reject"} else {"admin", "clerk"}
        if not account or account["role"] not in roles:
            invalid("当前账号没有此预算操作的权限", 403)
    name = account["name"] if account else change.actor.strip()
    if not name:
        invalid("请填写经办人或审批人")
    return name


def build_event(records, change, actor):
    event = {"kind": change.kind, "actor": actor, "at": datetime.now(timezone.utc).isoformat()}
    if change.kind == "base":
        base = change.base
        if records:
            invalid("该范围已保存预算，请通过预算调整修改", 409)
        if not base or set(base) != PROJECTS or any(
            len(months) != 12 or any(value is not None and not 0 <= value <= MAX_CENTS for value in months)
            for months in base.values()
        ) or not any(value is not None for months in base.values() for value in months):
            invalid("请至少填写一个有效预算金额，并核对六个项目的 12 个月数据")
        return {**event, "base": base}
    if not records:
        invalid("请先保存年度预算")
    if change.kind == "request":
        if (change.project not in PROJECTS or not 1 <= change.month <= 12
                or not 0 < abs(change.delta) <= MAX_CENTS or not change.reason.strip()):
            invalid("请核对调整项目、月份、金额与原因")
        return {**event, "requestId": secrets.token_hex(12), "project": change.project,
                "month": change.month, "delta": change.delta, "reason": change.reason.strip()}
    if change.kind not in {"approve", "reject"}:
        invalid("无效的预算操作")
    events = [row["event"] for row in records]
    pending = next((e for e in events if e["kind"] == "request" and e["requestId"] == change.request_id), None)
    decisions = {e["requestId"]: e for e in events if e["kind"] in {"approve", "reject"}}
    if not pending or change.request_id in decisions:
        invalid("该调整申请已处理或不存在", 409)
    if pending["actor"] == actor:
        invalid("请由另一位审批人处理调整申请", 403)
    if change.kind == "reject" and not change.reason.strip():
        invalid("请填写退回原因")
    if change.kind == "approve":
        project, month = pending["project"], pending["month"]
        budget = events[0]["base"][project][month - 1] or 0
        budget += sum(e["delta"] for e in events if e["kind"] == "request"
                      and e["project"] == project and e["month"] == month
                      and decisions.get(e["requestId"], {}).get("kind") == "approve")
        if not 0 <= budget + pending["delta"] <= MAX_CENTS:
            invalid("审批后预算将低于零或超出金额上限，请退回后重新申请")
    return {**event, "requestId": change.request_id, "reason": change.reason.strip()}


@router.get("")
def list_budgets(account_set_id: str = "default"):
    with closing(get_connection()) as conn:
        assert_account_set(conn, account_set_id)
        return {"items": read_events(conn, account_set_id)}


@router.post("")
def change_budget(change: BudgetChange, request: Request, account_set_id: str = "default"):
    dept = change.dept.strip()
    if dept in {"全公司", "未归属"} or any(ord(char) < 32 for char in dept):
        invalid("部门名称无效")
    prefix = f"budget-plan:{change.year}:{quote(dept, safe='')}:"
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        assert_account_set(conn, account_set_id)
        actor = identify(conn, request, change)
        rows = sorted(read_events(conn, account_set_id, prefix), key=lambda row: row["revision"])
        revision = rows[-1]["revision"] if rows else 0
        if change.revision != revision:
            invalid("预算已被更新，请刷新后重试", 409)
        event = build_event(rows, change, actor)
        record = {"id": prefix + str(revision + 1), "year": change.year,
                  "dept": dept, "revision": revision + 1, "event": event}
        conn.execute(
            "INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,?,?,?)",
            (account_set_id, "Workpapers", record["id"], json.dumps(record, ensure_ascii=False)),
        )
        return record
