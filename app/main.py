"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience;
      real environment variables win over .env values)

Contract: see openapi.yaml (paths, status codes, business rules R1-R10)
Storage:  see DATA_MODEL.md (employees / attendance_logs, BSON datetimes,
          epoch milliseconds at the API boundary)
"""
import calendar
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override variables already set in the environment

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EPOCH_MIN = 100_000_000_000          # values below this look like seconds, not ms
EPOCH_MAX = 4_102_444_800_000        # 2100-01-01 in ms
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotently)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)], name="employees_department_emp"
        )
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)], name="employees_department_joined"
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="employees_joined")
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="attendance_emp_date_unique",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_desc_emp"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp"
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            name="attendance_status_date_emp",
        )
    except Exception:
        # Startup must not crash if MongoDB is briefly unavailable;
        # /health reports 503 in that case.
        pass


@app.on_event("startup")
def _startup() -> None:
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Helpers: time conversion, rounding, business-rule maths
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # PyMongo can hand back naive datetimes; they are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value: Any, places: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    """Strict YYYY-MM-DD calendar date, or raise ValueError."""
    if not DATE_RE.match(value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)  # raises on impossible dates, e.g. 2026-02-30


def parse_shift(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; an overnight punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_shift(shift_end)
        if ist.time() < time(eh, em):
            d = d - timedelta(days=1)
    return d.isoformat()


def shift_start_dt(attendance_date: str, shift_start: str) -> datetime:
    h, m = parse_shift(shift_start)
    d = date.fromisoformat(attendance_date)
    return datetime.combine(d, time(h, m), tzinfo=IST).astimezone(UTC)


def shift_end_dt(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_shift(shift_end)
    d = date.fromisoformat(attendance_date)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return datetime.combine(d, time(h, m), tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, attendance_date: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 past shift_start; then the
    whole minutes elapsed since shift_start."""
    diff_seconds = (punch_in - shift_start_dt(attendance_date, shift_start)).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = Decimal(int((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes from shift_end to punch_out, counted only when >= 30."""
    diff_seconds = (punch_out - shift_end_dt(attendance_date, shift_start, shift_end)).total_seconds()
    minutes = int(diff_seconds // 60) if diff_seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def recompute_derived(emp: dict, attendance_date: str, status: str,
                      punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """Derived fields for a final (status, punch_in, punch_out) triple, R2-R5."""
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, attendance_date, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, attendance_date, emp["shift_start"], emp["shift_end"]),
        "half_day": work < 4.50,  # R5: based on the rounded work_hours
    }


def month_bounds(month: str) -> tuple[str, str, date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def working_days(joined_on: str, first: date, last: date) -> int:
    """R7: Mon-Fri days in [first, last], counted from joined_on."""
    start = max(first, date.fromisoformat(joined_on))
    count, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serializers (MongoDB document -> API shape; never leak _id)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_value(field: str, value: Any) -> Any:
    if field in ("punch_in", "punch_out"):
        return dt_to_ms(value) if isinstance(value, datetime) else value
    return value


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if isinstance(change, dict):
            changes[field] = {
                "from": _history_value(field, change.get("from")),
                "to": _history_value(field, change.get("to")),
            }
        else:
            changes[field] = change
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def _validate_date_string(v: str) -> str:
    parse_date(v)
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    _check_joined = field_validator("joined_on")(_validate_date_string)

    @model_validator(mode="after")
    def _shift_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out / list / regularize
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_truncated()
    att_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index decides the race: exactly one
        # insert succeeds, the rest raise DuplicateKeyError -> 409.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_truncated()
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        # No punch-in at or before punched_at. If the employee does have a
        # punch-in, it lies after punched_at, which is the 422 case below;
        # only a complete absence of punch-ins is "no punch-in found".
        later = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts}}
        )
        if later is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    start = candidate["punch_in"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if ts <= start:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - start).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
    derived = recompute_derived(emp, candidate["date"], candidate["status"], start, ts)
    # Atomic claim: only the request that still sees punch_out=None wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status.value if isinstance(status, StatusEnum) else status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value is not None:
            try:
                parse_date(value)
            except ValueError:
                raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    try:
        parse_date(date)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    emp = get_employee_or_404(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")

    sent = body.model_fields_set
    old_status = old["status"]
    old_in = old.get("punch_in")
    old_out = old.get("punch_out")
    if isinstance(old_in, datetime) and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=UTC)
    if isinstance(old_out, datetime) and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=UTC)
    old_derived = {
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes") or 0,
        "overtime_minutes": old.get("overtime_minutes") or 0,
        "half_day": bool(old.get("half_day", False)),
    }

    new_status = body.status.value if ("status" in sent and body.status is not None) else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if ("punch_in" in sent and body.punch_in is not None) or (
            "punch_out" in sent and body.punch_out is not None
        ):
            raise HTTPException(422, "punch times cannot be supplied with ABSENT/LEAVE")
        new_in, new_out = None, None
    else:
        if "punch_in" in sent:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
        else:
            new_in = old_in
        if "punch_out" in sent:
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None
        else:
            new_out = old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    new_derived = recompute_derived(emp, date, new_status, new_in, new_out)

    changes: dict[str, Any] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_in != old_in:
        changes["punch_in"] = {"from": old_in, "to": new_in}
    if new_out != old_out:
        changes["punch_out"] = {"from": old_out, "to": new_out}
    for field in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if new_derived[field] != old_derived[field]:
            changes[field] = {"from": old_derived[field], "to": new_derived[field]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_truncated(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only lands if nobody else has
    # touched this record's history since we read it; the loser gets 409
    # instead of silently overwriting the winner's audit entry.
    filt: dict[str, Any] = {"_id": old["_id"]}
    if "history" in old:
        filt["history"] = old["history"]
    else:
        filt["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        filt,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                **new_derived,
            },
            "$push": {"history": entry},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics (MongoDB aggregation pipelines; stored derived values are read,
# never recomputed - see DATA_MODEL.md)
# --------------------------------------------------------------------------- #
MonthQuery = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

# Weekday test for a stored "YYYY-MM-DD" string field: $dayOfWeek is
# 1=Sunday .. 7=Saturday, so Mon-Fri is 2..6.
def _weekday_cond(date_expr: Any) -> dict:
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": date_expr}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _present_value(date_expr: Any, status_expr: Any, half_expr: Any) -> dict:
    """0.5 / 1 present-day contribution of one record, Mon-Fri only (R5-R7)."""
    return {
        "$cond": [
            {"$and": [{"$in": [status_expr, list(PRESENCE)]}, _weekday_cond(date_expr)]},
            {"$cond": [{"$eq": [{"$ifNull": [half_expr, False]}, True]}, 0.5, 1.0]},
            0.0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first_s, last_s, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value("$date", "$status", "$half_day")},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    emp = get_employee_or_404(emp_code)
    _, _, first, last = month_bounds(month)
    wd = working_days(emp["joined_on"], first, last)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}
    present = float(row.get("present_days", 0.0))
    pct = None
    if wd > 0:
        pct = round_half_up(Decimal(str(present)) / Decimal(wd) * Decimal(100), "0.01")
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    _, last_s, _, _ = month_bounds(month)
    first_s = month_bounds(month)[0]
    match: dict[str, Any] = {"joined_on": {"$lte": last_s}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", first_s]},
                                    {"$lte": ["$date", last_s]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": _present_value("$logs.date", "$logs.status", "$logs.half_day")
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for r in rows:
        avg = None
        if r.get("work_count"):
            avg = round_half_up(
                Decimal(str(r["work_sum"])) / Decimal(r["work_count"]), "0.01"
            )
        items.append(
            {
                "department": r["_id"],
                "headcount": len(r.get("emp_codes", [])),
                "present_days": float(r.get("present_days", 0.0)),
                "avg_work_hours": avg,
                "late_count": int(r.get("late_count", 0)),
                "total_late_minutes": int(r.get("total_late_minutes", 0)),
                "leave_count": int(r.get("leave_count", 0)),
                "on_duty_count": int(r.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first_s, last_s, _, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQuery,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def trend_daily_pipeline(date_from: str, date_to: str, emp_codes: list) -> list:
    """Per-day present/late counts for a department, gap-filled with $densify."""
    from_dt = datetime.combine(date.fromisoformat(date_from), time.min, tzinfo=UTC)
    to_dt_excl = datetime.combine(
        date.fromisoformat(date_to) + timedelta(days=1), time.min, tzinfo=UTC
    )
    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "emp_code": {"$in": emp_codes}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_excl]},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    try:
        d_from, d_to = parse_date(from_), parse_date(to)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined_sorted = sorted(e["joined_on"] for e in emps)

    import bisect

    rows = list(
        db.attendance_logs.aggregate(trend_daily_pipeline(from_, to, codes))
    )
    by_date = {r["date"]: r for r in rows}
    items = []
    rates: list[Optional[Decimal]] = []
    d = d_from
    while d <= d_to:
        ds = d.isoformat()
        row = by_date.get(ds, {"present_count": 0.0, "late_count": 0})
        headcount = bisect.bisect_right(joined_sorted, ds)
        is_working = d.weekday() < 5
        present = float(row.get("present_count", 0.0))
        rate: Optional[Decimal] = None
        if is_working and headcount > 0:
            rate = (Decimal(str(present)) / Decimal(headcount)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        moving = None
        if window:
            moving = float(
                (sum(window) / Decimal(len(window))).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
        items.append(
            {
                "date": ds,
                "is_working_day": is_working,
                "headcount": headcount,
                "present_count": present,
                "late_count": int(row.get("late_count", 0)),
                "attendance_rate": float(rate) if rate is not None else None,
                "moving_avg_7d": moving,
            }
        )
        d += timedelta(days=1)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _explain_aggregate(collection: str, pipeline: list) -> dict:
    doc = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return json_util.loads(json_util.dumps(doc))


def _explain_find(q: dict, page: int, page_size: int) -> dict:
    doc = db.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        },
        verbosity="executionStats",
    )
    return json_util.loads(json_util.dumps(doc))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need_month() -> str:
        if month is None or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(422, "month (YYYY-MM) is required")
        return month

    if endpoint == ExplainEndpoint.attendance_list:
        for value in (date_from, date_to):
            if value is not None:
                try:
                    parse_date(value)
                except ValueError:
                    raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        q = attendance_filter(emp_code, date_from, date_to, status)
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": _explain_find(q, page, page_size),
        }
    if endpoint == ExplainEndpoint.employee_monthly:
        if emp_code is None:
            raise HTTPException(422, "emp_code is required")
        m = need_month()
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": _explain_aggregate(
                "attendance_logs", employee_monthly_pipeline(emp_code, m)
            ),
        }
    if endpoint == ExplainEndpoint.department_summary:
        m = need_month()
        return {
            "endpoint": endpoint.value,
            "collection": "employees",
            "explain": _explain_aggregate(
                "employees", department_summary_pipeline(m, department)
            ),
        }
    if endpoint == ExplainEndpoint.late_leaderboard:
        m = need_month()
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": _explain_aggregate(
                "attendance_logs", leaderboard_pipeline(m, limit, department)
            ),
        }
    # department_trend
    if department is None or from_ is None or to is None:
        raise HTTPException(422, "department, from and to are required")
    try:
        d_from, d_to = parse_date(from_), parse_date(to)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    if d_to < d_from or (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "invalid date range")
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
    return {
        "endpoint": endpoint.value,
        "collection": "attendance_logs",
        "explain": _explain_aggregate(
            "attendance_logs", trend_daily_pipeline(from_, to, codes)
        ),
    }
