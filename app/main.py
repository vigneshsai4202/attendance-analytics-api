"""Employee Attendance & Analytics API.

Run with: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
logger = logging.getLogger("attendance")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
HALF = Decimal128("0.5")  # exact 0.5 for half-up rounding inside pipelines


# ------------------------------- validation ---------------------------------
def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} must be a valid date")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:  # e.g. "0000-01" matches the pattern but is not a real year
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        raise HTTPException(422, "month is out of range")


# --------------------------- time and rule helpers ---------------------------
def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: int) -> datetime:
    # truncate to whole seconds as required by R1
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    local = to_ist(ts)
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_end_instant(day: str, shift_start: str, shift_end: str) -> datetime:
    end_day = date.fromisoformat(day)
    if shift_end <= shift_start:
        end_day += timedelta(days=1)
    h, m = map(int, shift_end.split(":"))
    return datetime.combine(end_day, time(h, m), IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, day: str | None = None) -> int:
    if day is None:
        day = to_ist(punch_in).date().isoformat()
    h, m = map(int, shift_start.split(":"))
    start = datetime.combine(date.fromisoformat(day), time(h, m), IST).astimezone(UTC)
    elapsed_seconds = (punch_in - start).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, day: str) -> int:
    end = shift_end_instant(day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count, current = 0, start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_half_up(value: float | int | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# ------------------------- document <-> API conversion -----------------------
def normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """Legacy documents may omit fields; missing means empty / false / 0 (DATA_MODEL.md)."""
    doc.setdefault("punch_in", None)
    doc.setdefault("punch_out", None)
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    return doc


def public_value(value: Any) -> Any:
    """Convert BSON datetimes to epoch milliseconds recursively and drop _id."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: public_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [public_value(v) for v in value]
    return value


def public_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return public_value(normalize(dict(doc)))


def require_employee(emp_code: str) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def require_department(department: str) -> None:
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")


# ---------------------------------- indexes ----------------------------------
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent indexes; the unique ones also make concurrent requests safe."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


def try_ensure_indexes() -> bool:
    """Never crash the app; /health retries until MongoDB is reachable and indexes exist."""
    global _indexes_ready
    if _indexes_ready:
        return True
    try:
        ensure_indexes()
        _indexes_ready = True
    except PyMongoError as exc:
        logger.warning("index creation failed, will retry: %s", exc)
    return _indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    try_ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------- models ----------------------------------
def _check_epoch(v: int | None) -> int | None:
    if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS:
        raise ValueError("timestamps must be epoch milliseconds in the supported range")
    return v


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        return _check_epoch(v)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        return _check_epoch(v)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_edit_fields(cls, values):
        if isinstance(values, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in values and values[key] is None:
                    raise ValueError(f"{key} cannot be null")
        return values

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, v):
        return _check_epoch(v)


# ------------------------------- query builders ------------------------------
ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_query(emp_code, date_from, date_to, status) -> dict[str, Any]:
    """Shared by GET /attendance and its explain so they can never drift apart."""
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def _weekday_expr(date_expr: Any) -> dict[str, Any]:
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": date_expr}}}  # Sun=1 ... Sat=7
    return {"$not": [{"$in": [dow, [1, 7]]}]}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list[dict[str, Any]]:
    present = {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, _weekday_expr("$date")]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [present, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def summary_pipeline(first: str, last: str, department: Optional[str]) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9: headcount, logs or not
    if department is not None:
        employee_match["department"] = department
    is_present = {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}
    has_hours = {"$and": [is_present, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]},
            ]}}},
        ], "as": "logs"}},
        # preserveNullAndEmptyArrays keeps employees that have no logs at all
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, _weekday_expr({"$ifNull": ["$logs.date", "1970-01-01"]})]},
                {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            # sum and count (not an average) so the exact half-up rounding is done once, in Python
            "hours_sum": {"$sum": {"$cond": [has_hours, "$logs.work_hours", 0]}},
            "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$emps"}, "present_days": 1,
                      "hours_sum": 1, "hours_n": 1, "late_count": 1, "total_late_minutes": 1,
                      "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(first: str, last: str, department: Optional[str], limit: int) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, next rank skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied after ranking
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def trend_logs_pipeline(department: str, from_: str, to: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$addFields": {"dayDate": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {"_id": "$dayDate",
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                         {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "dayDate": "$_id", "present_count": 1, "late_count": 1}},
    ]


# --------------------------------- endpoints --------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    try_ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_value(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_value(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:  # the unique (emp_code, date) index decides the race, not a check-then-insert
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    # Most recent record punched in at or before this instant (works across midnight).
    # Deliberately NOT filtered on punch_out: if that record is already closed the answer is 409.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    if ts <= record["punch_in"] or ts > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    update = {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime,
                       "half_day": work_hours < 4.50}}
    # punch_out: None in the filter makes two simultaneous punch-outs yield exactly one 200
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, update, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q, {"_id": 0}).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}


TRACKED = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = require_employee(emp_code)
    raw = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not raw:
        raise HTTPException(404, "attendance record not found")
    record = normalize(dict(raw))  # legacy docs: missing fields count as empty / false / 0
    requested = body.model_dump(exclude_unset=True)
    status = requested.get("status", record["status"])
    absent_leave = status in {"ABSENT", "LEAVE"}
    if absent_leave and (requested.get("punch_in") is not None or requested.get("punch_out") is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
    if absent_leave:
        pin = pout = None
    else:
        pin = from_epoch_ms(requested["punch_in"]) if requested.get("punch_in") is not None else record["punch_in"]
        pout = from_epoch_ms(requested["punch_out"]) if requested.get("punch_out") is not None else record["punch_out"]
        if pin is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if pout is not None and (pout <= pin or pout > pin + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(pin, emp["shift_start"], date) if pin else 0
    if pin is not None and pout is not None:
        hours = compute_work_hours(pin, pout)
        overtime = compute_overtime(pout, emp["shift_start"], emp["shift_end"], date)
        half = hours < 4.50
    else:
        hours, overtime, half = None, 0, False
    new_values = {"status": status, "punch_in": pin, "punch_out": pout, "late_minutes": late,
                  "work_hours": hours, "overtime_minutes": overtime, "half_day": half}
    changes: dict[str, dict[str, Any]] = {}
    for key in TRACKED:
        old, new = record.get(key), new_values[key]
        if isinstance(old, datetime) and isinstance(new, datetime):
            different = epoch_ms(old) != epoch_ms(new)
        else:
            different = old != new
        if different:
            # keep BSON datetimes in the database (DATA_MODEL.md); public_value turns them into ms on output
            changes[key] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Compare-and-swap on exactly what we read. A concurrent edit changes history, so the loser gets 409
    # instead of silently overwriting the winner's audit entry.
    guard: dict[str, Any] = {"emp_code": emp_code, "date": date}
    guard["history"] = raw["history"] if "history" in raw else {"$exists": False}
    for key in TRACKED:
        guard[key] = raw.get(key)  # None also matches a missing field
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_values, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently; retry the correction")
    return public_attendance(updated)


# -------------------------------- analytics ---------------------------------
def month_match(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = require_employee(emp_code)
    first, last, start, end = month_match(month)
    result = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    data = result[0] if result else {}
    wd = working_days(max(start, date.fromisoformat(emp["joined_on"])), end)
    present = Decimal(str(data.get("present_days", 0)))
    pct = (present / wd * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round_half_up(present, 2),
            "leave_days": int(data.get("leave_days", 0)), "late_count": int(data.get("late_count", 0)),
            "total_late_minutes": int(data.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(data.get("total_overtime_minutes", 0)),
            "attendance_pct": float(pct) if pct is not None else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last, _, _ = month_match(month)
    items = []
    for row in db.employees.aggregate(summary_pipeline(first, last, department), allowDiskUse=True):
        n, total = row.pop("hours_n"), row.pop("hours_sum")
        # values are 2-decimal doubles; round(sum, 6) removes float noise, then divide exactly and round half-up
        row["avg_work_hours"] = round_half_up(Decimal(str(round(total, 6))) / n, 2) if n else None
        row["present_days"] = round_half_up(row["present_days"], 2)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last, _, _ = month_match(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, department, limit), allowDiskUse=True))
    return {"month": month, "items": items}


def _round4(expr: Any) -> dict[str, Any]:
    """Half-up to 4 decimals on exact Decimal128 values (floats would mis-round ties). Null stays null."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, HALF]}}, 10000]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start = parse_date(from_, "from")
    end = parse_date(to, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    require_department(department)
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)
    pipeline = [
        # $documents seeds the first day so $densify has something to start from even with zero logs
        {"$documents": [{"dayDate": start_dt, "present_count": 0, "late_count": 0}]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": trend_logs_pipeline(department, from_, to)}},
        {"$group": {"_id": "$dayDate", "present_count": {"$max": "$present_count"}, "late_count": {"$max": "$late_count"}}},
        # explicit bounds are [lower, upper): the upper bound is exclusive, hence +1 day to include `to`
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                  "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round4({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}),
            None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": {"$toDouble": "$attendance_rate"},
                      "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$toDouble": _round4("$moving_avg_7d")}]}}},
        {"$sort": {"date": 1}},
    ]
    items = list(db.aggregate(pipeline, allowDiskUse=True))  # database-level aggregate ($documents)
    return {"department": department, "items": items}


# ---------------------------------- explain ---------------------------------
def _explain(coll: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
):
    # Every branch uses the same builder as the real endpoint.
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        explain = db.command("explain", {"find": coll, "filter": q, "sort": dict(ATT_SORT),
                                         "skip": (page - 1) * page_size, "limit": page_size},
                             verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last, _, _ = month_match(month)
        coll = "attendance_logs"
        explain = _explain(coll, monthly_pipeline(emp_code, first, last))
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_match(month)
        coll = "employees"
        explain = _explain(coll, summary_pipeline(first, last, department))
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_match(month)
        coll = "attendance_logs"
        explain = _explain(coll, leaderboard_pipeline(first, last, department, limit))
    else:
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        coll = "attendance_logs"  # the trend's data query; the $documents seed stage reads no collection
        explain = _explain(coll, trend_logs_pipeline(department, from_, to))
    # raw explain can hold Timestamp/Binary values that FastAPI cannot JSON-encode
    explain = json.loads(json_util.dumps(explain, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": explain}