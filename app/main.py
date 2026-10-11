
"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB

Based on the HROne Engineering Assignment.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, EmailStr, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# --------------------------------------------------------------------------- #
# Configuration and database setup
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Schema migration and index creation
# --------------------------------------------------------------------------- #


def migrate_attendance_schema():
    """Rename the legacy attendance date field without changing any other data."""
    collection = db.attendance_logs
    seen = {}
    legacy_ids = []

    for record in collection.find(
        {},
        {"_id": 1, "emp_code": 1, "date": 1, "attendance_date": 1},
    ):
        canonical_date = record.get("date")
        legacy_date = record.get("attendance_date")

        if canonical_date is not None and legacy_date is not None:
            if canonical_date != legacy_date:
                raise RuntimeError(
                    "Conflicting date and attendance_date values exist; "
                    "migration stopped without changes"
                )
        elif canonical_date is None and legacy_date is not None:
            canonical_date = legacy_date
            legacy_ids.append(record["_id"])
        elif canonical_date is None:
            raise RuntimeError(
                "An attendance record has neither date nor attendance_date; "
                "migration stopped without changes"
            )

        key = (record.get("emp_code"), canonical_date)
        previous_id = seen.get(key)
        if previous_id is not None and previous_id != record["_id"]:
            raise RuntimeError(
                "Duplicate employee/date attendance records exist; "
                "migration stopped without changes"
            )
        seen[key] = record["_id"]

    index_info = collection.index_information()
    old_index_names = (
        "uq_attendance_employee_date",
        "ix_attendance_date_employee",
        "ix_attendance_status_date",
    )
    old_unique_exists = "uq_attendance_employee_date" in index_info
    replacement_name = "uq_attendance_employee_date_v2"

    if old_unique_exists and replacement_name not in index_info:
        collection.create_index(
            [("emp_code", 1), ("date", 1)],
            unique=True,
            partialFilterExpression={"date": {"$exists": True}},
            name=replacement_name,
        )
        index_info = collection.index_information()

    if old_unique_exists:
        replacement = index_info.get(replacement_name)
        if replacement is None or replacement["key"] != [
            ("emp_code", 1),
            ("date", 1),
        ] or not replacement.get("unique"):
            raise RuntimeError(
                "The replacement attendance uniqueness index was not validated"
            )
        for name in old_index_names:
            if name in index_info:
                collection.drop_index(name)

    if legacy_ids:
        result = collection.update_many(
            {"_id": {"$in": legacy_ids}, "date": {"$exists": False}},
            {"$rename": {"attendance_date": "date"}},
        )
        if result.modified_count != len(legacy_ids):
            raise RuntimeError(
                "Attendance schema migration did not update every legacy record"
            )

    replacement = collection.index_information().get(replacement_name)
    if replacement and replacement.get("partialFilterExpression"):
        collection.drop_index(replacement_name)


def create_indexes():
    # Employees: unique employee code and department listing.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employee_department_code",
    )

    # Attendance: enforce one record per employee per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date_v2",
    )

    # Attendance listing by date and employee.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_employee_v2",
    )

    # Attendance filters and department/employee analytics.
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)],
        name="ix_attendance_status_date_v2",
    )


    index_info = db.attendance_logs.index_information()
    expected_indexes = {
        "uq_attendance_employee_date_v2": {
            "key": [("emp_code", 1), ("date", 1)],
            "unique": True,
        },
        "ix_attendance_date_employee_v2": {
            "key": [("date", -1), ("emp_code", 1)],
        },
        "ix_attendance_status_date_v2": {
            "key": [("status", 1), ("date", -1)],
        },
    }
    for name, expected in expected_indexes.items():
        actual = index_info.get(name)
        if actual is None or actual["key"] != expected["key"]:
            raise RuntimeError(f"Attendance index {name} was not created correctly")
        if expected.get("unique") and not actual.get("unique"):
            raise RuntimeError(f"Attendance index {name} is not unique")

    for name in (
        "uq_attendance_employee_date",
        "ix_attendance_date_employee",
        "ix_attendance_status_date",
    ):
        legacy = db.attendance_logs.index_information().get(name)
        if legacy and any(field == "attendance_date" for field, _ in legacy["key"]):
            db.attendance_logs.drop_index(name)


@app.on_event("startup")
def startup_create_indexes():
    migrate_attendance_schema()
    create_indexes()

# --------------------------------------------------------------------------- #
# Regularization models and serialization helpers
# --------------------------------------------------------------------------- #


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch_millis(cls, value):
        if value is None:
            raise ValueError("Timestamp must be epoch milliseconds")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Timestamp must be an integer in milliseconds")
        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("Timestamp is outside the allowed range")
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value):
        value = value.strip()
        if len(value) < 5 or len(value) > 200:
            raise ValueError("Reason must contain 5 to 200 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, value):
        value = value.strip()
        if not value or len(value) > 50:
            raise ValueError("regularized_by must contain 1 to 50 characters")
        return value


def _to_utc_datetime(epoch_ms: int) -> datetime:
    # Contract timestamps are milliseconds; stored datetimes are UTC.
    return datetime.fromtimestamp(epoch_ms // 1000, tz=UTC)


def _to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp()) * 1000


def _history_for_response(history):
    result = []
    for entry in history:
        converted = dict(entry)
        if isinstance(converted.get("at"), datetime):
            converted["at"] = _to_epoch_ms(converted["at"])
        changes = {}
        for field, change in converted.get("changes", {}).items():
            change = dict(change)
            if field in {"punch_in", "punch_out"}:
                if isinstance(change.get("from"), datetime):
                    change["from"] = _to_epoch_ms(change["from"])
                if isinstance(change.get("to"), datetime):
                    change["to"] = _to_epoch_ms(change["to"])
            changes[field] = change
        converted["changes"] = changes
        result.append(converted)
    return result


def attendance_response(record):
    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": _to_epoch_ms(record.get("punch_in")),
        "punch_out": _to_epoch_ms(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
        "history": _history_for_response(record.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Attendance regularization
# --------------------------------------------------------------------------- #


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeIn,
):
    date_str = date.isoformat()

    # Check that the employee exists.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date_str,
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set

    # ABSENT/LEAVE cannot be submitted with either punch field.
    if body.status in {"ABSENT", "LEAVE"} and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT or LEAVE cannot be supplied with punch times",
        )

    old_status = record.get("status", "PRESENT")

# Preserve the original values for the MongoDB concurrency check.
    raw_old_in = record.get("punch_in")
    raw_old_out = record.get("punch_out")

# Normalize stored datetimes to timezone-aware UTC.
    old_in = (
        raw_old_in.replace(tzinfo=UTC)
        if raw_old_in is not None and raw_old_in.tzinfo is None
        else raw_old_in
    )
    old_out = (
        raw_old_out.replace(tzinfo=UTC)
        if raw_old_out is not None and raw_old_out.tzinfo is None
        else raw_old_out
)

    # Start with the current values; omitted fields remain unchanged.
    new_status = body.status if "status" in supplied else old_status
    new_in = old_in
    new_out = old_out

    if "punch_in" in supplied:
        new_in = _to_utc_datetime(body.punch_in)

    if "punch_out" in supplied:
        new_out = _to_utc_datetime(body.punch_out)

    # An explicit ABSENT/LEAVE status clears both punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        new_in = None
        new_out = None

    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if new_status in presence_statuses and new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires a punch_in",
        )

    if new_in is not None:
        new_in = new_in.replace(microsecond=0)

        # The punch-in must belong to the record's attendance date,
        # including the overnight-shift rule.
        calculated_date = get_attendance_date(
            new_in.astimezone(IST),
            emp["shift_start"],
            emp["shift_end"],
        )
        if calculated_date != date_str:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    if new_out is not None:
        new_out = new_out.replace(microsecond=0)

    if new_in is None and new_out is not None:
        raise HTTPException(
            status_code=422,
            detail="punch_out requires a punch_in",
        )

    if new_in is not None and new_out is not None:
        duration = (new_out - new_in).total_seconds()

        if duration <= 0:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )

        if duration > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be within 24 hours of punch_in",
            )

    # Recompute every derived field from the final values.

    # Recompute late minutes from the final punch-in time.
    if new_in is not None:
        new_late = compute_late_minutes(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
        )
    else:
        new_late = 0


    if new_in is not None and new_out is not None:
        new_work_hours = compute_work_hours(new_in, new_out)
        new_overtime = compute_overtime(
            new_out.astimezone(IST),
            emp["shift_start"],
            emp["shift_end"],
            date_str,
        )
        new_half_day = new_work_hours < 4.50
    else:
        new_work_hours = None
        new_overtime = 0
        new_half_day = False

    # Record only values that actually changed.
    changes = {}

    def add_change(field, old_value, new_value, timestamp=False):
        old_compare = old_value
        new_compare = new_value

        if timestamp:
            old_compare = _to_epoch_ms(old_value)
            new_compare = _to_epoch_ms(new_value)

        if old_compare != new_compare:
            changes[field] = {
                "from": old_compare,
                "to": new_compare,
            }

    add_change("status", old_status, new_status)
    add_change("punch_in", old_in, new_in, timestamp=True)
    add_change("punch_out", old_out, new_out, timestamp=True)
    add_change("late_minutes", record.get("late_minutes", 0), new_late)
    add_change("work_hours", record.get("work_hours"), new_work_hours)
    add_change(
        "overtime_minutes",
        record.get("overtime_minutes", 0),
        new_overtime,
    )
    add_change("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change the attendance record",
        )

    history = record.get("history", [])

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated_history = history + [history_entry]

    # Optimistic concurrency: only update if the values and history
    # still match the version we read. A competing edit gets 409.
    query = {
        "_id": record["_id"],
        "status": old_status,
        "punch_in": raw_old_in,
        "punch_out": raw_old_out
    }

    if "history" in record:
        query["history"] = history
    else:
        query["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        query,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "late_minutes": new_late,
                "work_hours": new_work_hours,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
                "history": updated_history,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    # Return the OpenAPI response shape, with epoch-millisecond instants.
    updated_record = dict(record)
    updated_record.update({
        "date": date_str,
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
        "history": updated_history,
    })
    return attendance_response(updated_record)


# --------------------------------------------------------------------------- #
# Date/time and attendance calculation helpers
# --------------------------------------------------------------------------- #


def _as_ist_second(value: datetime) -> datetime:
    """
    Interpret naive MongoDB datetimes as UTC.
    Convert the instant to IST and truncate microseconds.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(IST).replace(microsecond=0)


def _parse_shift_time(value: str) -> tuple[int, int]:
    """Parse and validate an HH:MM shift time."""
    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError

        h, m = map(int, parts)

        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError

        return h, m

    except (ValueError, AttributeError):
        raise ValueError("Shift time must use HH:MM format")


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    """Calculate whole late minutes after the 10-minute grace period."""
    punch = _as_ist_second(punch_in)
    start_h, start_m = _parse_shift_time(shift_start)

    shift_date = punch.date()

    # Early-morning punch-ins for overnight shifts belong
    # to the shift that started on the previous day.
    if shift_end is not None:
        end_h, end_m = _parse_shift_time(shift_end)

        if (end_h, end_m) <= (start_h, start_m):
            if (punch.hour, punch.minute) < (end_h, end_m):
                shift_date -= timedelta(days=1)

    start = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        start_h,
        start_m,
        tzinfo=IST,
    )

    elapsed_seconds = (punch - start).total_seconds()

    # Exactly 10 minutes is within the grace period.
    if elapsed_seconds <= 600:
        return 0

    # The contract requires whole minutes, rounded down.
    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate hours using two-decimal half-up rounding."""
    start = _as_ist_second(punch_in)
    end = _as_ist_second(punch_out)

    seconds = int((end - start).total_seconds())

    if seconds < 0:
        raise ValueError("Punch-out cannot be earlier than punch-in")

    hours = (
        Decimal(seconds) / Decimal(3600)
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    """Calculate overtime after the 30-minute minimum threshold."""
    start_h, start_m = _parse_shift_time(shift_start)
    end_h, end_m = _parse_shift_time(shift_end)

    try:
        shift_date = datetime.fromisoformat(date_str).date()
    except (ValueError, TypeError):
        raise ValueError("Attendance date must use YYYY-MM-DD format")

    # Construct the scheduled shift end on the attendance date.
    end_date = shift_date

    # Overnight shift: the scheduled end is the following day.
    if (end_h, end_m) <= (start_h, start_m):
        end_date += timedelta(days=1)

    scheduled_end = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_h,
        end_m,
        tzinfo=IST,
    )

    punch = _as_ist_second(punch_out)
    elapsed_seconds = (punch - scheduled_end).total_seconds()

    # Less than 30 minutes beyond shift end means no overtime.
    if elapsed_seconds < 30 * 60:
        return 0

    return int(elapsed_seconds // 60)


# --------------------------------------------------------------------------- #
# Regularization models and serialization helpers
# --------------------------------------------------------------------------- #


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: EmailStr
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator(
        "emp_code", "name", "department", mode="before"
    )
    @classmethod
    def validate_required_text(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Field must not be empty")
        return value.strip()

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", value
        ):
            raise ValueError("Time must use HH:MM format")
        return value



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItem]


class DepartmentTrendItem(BaseModel):
    date: date
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class DepartmentTrendResponse(BaseModel):
    department: str
    items: list[DepartmentTrendItem]


# --------------------------------------------------------------------------- #
# Request and response models
# --------------------------------------------------------------------------- #

# client.admin.command("ping") checks whether MongoDB is reachable.
# If MongoDB responds, the endpoint returns HTTP 200.
# if MongoDB is unavailable, it returns HTTP 503 Service Unavailable.
# Your existing client variable is reused; no second connection is created.



# --------------------------------------------------------------------------- #
# Health and employee endpoints
# --------------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    status: str
    database: str




@app.get("/health", response_model=HealthResponse)
def health():
    try:
        client.admin.command("ping")
        return HealthResponse(
            status="ok",
            database="connected",
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "database": "unavailable",
            },
        )









@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc = body.model_dump(mode="json")
    created_at = datetime.now(UTC)
    created_at = created_at.replace(
        microsecond=(created_at.microsecond // 1000) * 1000
    )
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    doc.pop("_id", None)
    doc["created_at"] = int(created_at.timestamp() * 1000)

    return doc




class EmployeeListResponse(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int


@app.get("/employees", response_model=EmployeeListResponse)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch = _as_ist_second(punch_in)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_time = (start_h, start_m)
    end_time = (end_h, end_m)
    punch_time = (punch.hour, punch.minute)

    attendance_day = punch.date()

    # An overnight shift belongs to the day it started.
    if end_time <= start_time and punch_time < end_time:
        attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is None:
        punch_utc = datetime.now(UTC)
    else:
        try:

            punch_utc = _to_utc_datetime(body.punched_at)

        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )

    # Truncate the instant to whole seconds.
    punch_utc = punch_utc.replace(microsecond=0)
    punch_ist = punch_utc.astimezone(IST)

    attendance_day = get_attendance_date(
        punch_ist,
        emp["shift_start"],
        emp["shift_end"],
    )

    existing = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_day,
    })

    if existing:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this shift date",
        )

    late_minutes = compute_late_minutes(
        punch_ist,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "punch_in": punch_utc,
        "punch_out": None,
        "status": body.status,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False,
        "history": [],
        "created_at": datetime.now(UTC),
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this shift date",
        )
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is None:
        punch_out_utc = datetime.now(UTC)
    else:
        try:

            punch_out_utc = _to_utc_datetime(body.punched_at)

        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )

    punch_out_utc = punch_out_utc.replace(microsecond=0)

    # Find the most recent eligible attendance record without punch-out.
    attendance = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$lte": punch_out_utc},
        },
        sort=[("date", -1)],
    )

    if attendance is None:
        completed_attendance = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punch_out_utc},
            },
            sort=[("date", -1)],
        )
        if completed_attendance is not None:
            raise HTTPException(
                status_code=409,
                detail="Attendance record was already updated",
            )

        future_attendance = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_out": None,
            },
            sort=[("date", -1)],
        )
        if future_attendance is not None:
            raise HTTPException(
                status_code=422,
                detail="Punch-out cannot be before punch-in",
            )
        raise HTTPException(
            status_code=404,
            detail="No open attendance record found",
        )

    punch_in_utc = _as_ist_second(attendance["punch_in"]).astimezone(UTC)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be before punch-in",
        )

    if punch_out_utc - punch_in_utc > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be within 24 hours of punch-in",
        )

    punch_out_ist = punch_out_utc.astimezone(IST)

    work_hours = compute_work_hours(
        attendance["punch_in"],
        punch_out_utc,
    )

    overtime_minutes = compute_overtime(
        punch_out_ist,
        emp["shift_start"],
        emp["shift_end"],
        attendance["date"],
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": attendance["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already updated",
        )

    attendance.update({
        "punch_out": punch_out_utc,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    })

    return attendance_response(attendance)




# --------------------------------------------------------------------------- #
# Attendance listing and pagination
# --------------------------------------------------------------------------- #


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from.isoformat()

        if date_to:
            q["date"]["$lte"] = date_to.isoformat()

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([
            ("date", -1),
            ("emp_code", 1),
        ])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []

    for doc in docs:
        items.append(attendance_response(doc))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics pipeline builders
# --------------------------------------------------------------------------- #
def _month_bounds(month: str):
    year, month_number = (int(part) for part in month.split("-"))
    start = date(year, month_number, 1)
    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def _employee_monthly_pipeline(emp_code: str, month: str):
    month_start, month_end = _month_bounds(month)
    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": month_start,
                                "$lte": month_end,
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", month_start]},
                        "$joined_on",
                        month_start,
                    ]
                },
                "month_end": month_end,
            }
        },
        {
            "$set": {
                "effective_start_date": {
                    "$dateFromString": {
                        "dateString": "$effective_start",
                        "timezone": "+05:30",
                    }
                },
                "month_end_date": {
                    "$dateFromString": {
                        "dateString": "$month_end",
                        "timezone": "+05:30",
                    }
                },
            }
        },
        {
            "$set": {
                "working_days": {
                    "$cond": [
                        {"$gt": ["$effective_start_date", "$month_end_date"]},
                        0,
                        {
                            "$size": {
                                "$filter": {
                                    "input": {
                                        "$map": {
                                            "input": {
                                                "$range": [
                                                    0,
                                                    {
                                                        "$add": [
                                                            {
                                                                "$dateDiff": {
                                                                    "startDate": "$effective_start_date",
                                                                    "endDate": "$month_end_date",
                                                                    "unit": "day",
                                                                    "timezone": "+05:30",
                                                                }
                                                            },
                                                            1,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "as": "offset",
                                            "in": {
                                                "$dateAdd": {
                                                    "startDate": "$effective_start_date",
                                                    "unit": "day",
                                                    "amount": "$$offset",
                                                    "timezone": "+05:30",
                                                }
                                            },
                                        }
                                    },
                                    "as": "calendar_day",
                                    "cond": {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "date": "$$calendar_day",
                                                    "timezone": "+05:30",
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                }
                            }
                        },
                    ]
                }
            }
        },
        {
            "$set": {
                "metrics": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {
                            "present_day_halves": 0,
                            "leave_days": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "total_overtime_minutes": 0,
                        },
                        "in": {
                            "$let": {
                                "vars": {
                                    "log_day": {
                                        "$dateFromString": {
                                            "dateString": "$$this.date",
                                            "timezone": "+05:30",
                                        }
                                    },
                                    "late_minutes": {
                                        "$ifNull": ["$$this.late_minutes", 0]
                                    },
                                    "overtime_minutes": {
                                        "$ifNull": ["$$this.overtime_minutes", 0]
                                    },
                                    "is_present": {
                                        "$in": ["$$this.status", present_statuses]
                                    },
                                },
                                "in": {
                                    "$mergeObjects": [
                                        "$$value",
                                        {
                                            "present_day_halves": {
                                                "$add": [
                                                    "$$value.present_day_halves",
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$and": [
                                                                    "$$is_present",
                                                                    {
                                                                        "$in": [
                                                                            {
                                                                                "$dayOfWeek": {
                                                                                    "date": "$$log_day",
                                                                                    "timezone": "+05:30",
                                                                                }
                                                                            },
                                                                            [2, 3, 4, 5, 6],
                                                                        ]
                                                                    },
                                                                ]
                                                            },
                                                            {
                                                                "$cond": [
                                                                    {
                                                                        "$eq": [
                                                                            {
                                                                                "$ifNull": [
                                                                                    "$$this.half_day",
                                                                                    False,
                                                                                ]
                                                                            },
                                                                            True,
                                                                        ]
                                                                    },
                                                                    1,
                                                                    2,
                                                                ]
                                                            },
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "leave_days": {
                                                "$add": [
                                                    "$$value.leave_days",
                                                    {
                                                        "$cond": [
                                                            {"$eq": ["$$this.status", "LEAVE"]},
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "late_count": {
                                                "$add": [
                                                    "$$value.late_count",
                                                    {
                                                        "$cond": [
                                                            {"$gt": ["$$late_minutes", 0]},
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "total_late_minutes": {
                                                "$add": [
                                                    "$$value.total_late_minutes",
                                                    "$$late_minutes",
                                                ]
                                            },
                                            "total_overtime_minutes": {
                                                "$add": [
                                                    "$$value.total_overtime_minutes",
                                                    "$$overtime_minutes",
                                                ]
                                            },
                                        },
                                    ]
                                },
                            }
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "present_days": {
                    "$toDouble": {
                        "$divide": ["$metrics.present_day_halves", 2]
                    }
                },
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$divide": [
                                                        {
                                                            "$multiply": [
                                                                {
                                                                    "$toDecimal": "$metrics.present_day_halves"
                                                                },
                                                                Decimal128("500000"),
                                                            ]
                                                        },
                                                        {"$toDecimal": "$working_days"},
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                    ]
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": month,
                "working_days": 1,
                "present_days": 1,
                "leave_days": "$metrics.leave_days",
                "late_count": "$metrics.late_count",
                "total_late_minutes": "$metrics.total_late_minutes",
                "total_overtime_minutes": "$metrics.total_overtime_minutes",
                "attendance_pct": 1,
            }
        },
    ]


def _department_summary_pipeline(month: str, department: Optional[str] = None):
    month_start, month_end = _month_bounds(month)
    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": month_start,
                                "$lte": month_end,
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "late_minutes": 1,
                            "work_hours": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", month_start]},
                        "$joined_on",
                        month_start,
                    ]
                },
                "month_end": month_end,
            }
        },
        {
            "$set": {
                "effective_start_date": {
                    "$dateFromString": {
                        "dateString": "$effective_start",
                        "timezone": "+05:30",
                    }
                }
            }
        },
        {
            "$set": {
                "metrics": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {
                            "present_day_halves": 0,
                            "work_hours_sum": Decimal128("0"),
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                        "in": {
                            "$let": {
                                "vars": {
                                    "log_day": {
                                        "$dateFromString": {
                                            "dateString": "$$this.date",
                                            "timezone": "+05:30",
                                        }
                                    },
                                    "late_minutes": {
                                        "$ifNull": ["$$this.late_minutes", 0]
                                    },
                                    "is_present": {
                                        "$in": ["$$this.status", present_statuses]
                                    },
                                    "has_work_hours": {
                                        "$ne": ["$$this.work_hours", None]
                                    },
                                },
                                "in": {
                                    "$mergeObjects": [
                                        "$$value",
                                        {
                                            "present_day_halves": {
                                                "$add": [
                                                    "$$value.present_day_halves",
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$and": [
                                                                    "$$is_present",
                                                                    {
                                                                        "$gte": [
                                                                            "$$log_day",
                                                                            "$effective_start_date",
                                                                        ]
                                                                    },
                                                                    {
                                                                        "$in": [
                                                                            {
                                                                                "$dayOfWeek": {
                                                                                    "date": "$$log_day",
                                                                                    "timezone": "+05:30",
                                                                                }
                                                                            },
                                                                            [2, 3, 4, 5, 6],
                                                                        ]
                                                                    },
                                                                ]
                                                            },
                                                            {
                                                                "$cond": [
                                                                    {
                                                                        "$eq": [
                                                                            {
                                                                                "$ifNull": [
                                                                                    "$$this.half_day",
                                                                                    False,
                                                                                ]
                                                                            },
                                                                            True,
                                                                        ]
                                                                    },
                                                                    1,
                                                                    2,
                                                                ]
                                                            },
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "work_hours_sum": {
                                                "$add": [
                                                    "$$value.work_hours_sum",
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$and": [
                                                                    "$$is_present",
                                                                    "$$has_work_hours",
                                                                ]
                                                            },
                                                            "$$this.work_hours",
                                                            Decimal128("0"),
                                                        ]
                                                    },
                                                ]
                                            },
                                            "work_hours_count": {
                                                "$add": [
                                                    "$$value.work_hours_count",
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$and": [
                                                                    "$$is_present",
                                                                    "$$has_work_hours",
                                                                ]
                                                            },
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "late_count": {
                                                "$add": [
                                                    "$$value.late_count",
                                                    {
                                                        "$cond": [
                                                            {"$gt": ["$$late_minutes", 0]},
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "total_late_minutes": {
                                                "$add": [
                                                    "$$value.total_late_minutes",
                                                    "$$late_minutes",
                                                ]
                                            },
                                            "leave_count": {
                                                "$add": [
                                                    "$$value.leave_count",
                                                    {
                                                        "$cond": [
                                                            {"$eq": ["$$this.status", "LEAVE"]},
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "on_duty_count": {
                                                "$add": [
                                                    "$$value.on_duty_count",
                                                    {
                                                        "$cond": [
                                                            {"$eq": ["$$this.status", "ON_DUTY"]},
                                                            1,
                                                            0,
                                                        ]
                                                    },
                                                ]
                                            },
                                        },
                                    ]
                                },
                            }
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "present_days": {
                    "$toDouble": {
                        "$divide": ["$metrics.present_day_halves", 2]
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$metrics.work_hours_sum"},
                "work_hours_count": {"$sum": "$metrics.work_hours_count"},
                "late_count": {"$sum": "$metrics.late_count"},
                "total_late_minutes": {"$sum": "$metrics.total_late_minutes"},
                "leave_count": {"$sum": "$metrics.leave_count"},
                "on_duty_count": {"$sum": "$metrics.on_duty_count"},
            }
        },
        {
            "$set": {
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                "$work_hours_sum",
                                                                "$work_hours_count",
                                                            ]
                                                        },
                                                        Decimal128("10000"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _late_leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str] = None,
):
    month_start, month_end = _month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$project": {"_id": 0, "name": 1, "department": 1}}
                ],
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$set": {
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                }
            },
            {
                "$setWindowFields": {
                    "partitionBy": None,
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ]
    )
    return pipeline


def _department_trend_pipeline(
    department: str,
    from_date: date,
    to_date: date,
):
    range_days = (to_date - from_date).days + 1
    from_string = from_date.isoformat()
    to_string = to_date.isoformat()
    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    eligible_metrics = [
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "trend_date": "$$trend_date",
                },
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": from_string, "$lte": to_string},
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$eq": ["$date", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$set": {
                "attendance": {"$arrayElemAt": ["$attendance", 0]}
            }
        },
        {
            "$set": {
                "is_present": {
                    "$in": ["$attendance.status", present_statuses]
                },
                "present_value": {
                    "$cond": [
                        {"$in": ["$attendance.status", present_statuses]},
                        {
                            "$cond": [
                                {
                                    "$eq": [
                                        {"$ifNull": ["$attendance.half_day", False]},
                                        True,
                                    ]
                                },
                                Decimal128("0.5"),
                                Decimal128("1"),
                            ]
                        },
                        Decimal128("0"),
                    ]
                },
                "late_value": {
                    "$cond": [
                        {
                            "$gt": [
                                {"$ifNull": ["$attendance.late_minutes", 0]},
                                0,
                            ]
                        },
                        1,
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "headcount": {"$sum": 1},
                "present_count": {"$sum": "$present_value"},
                "late_count": {"$sum": "$late_value"},
            }
        },
        {"$project": {"_id": 0, "headcount": 1, "present_count": 1, "late_count": 1}},
    ]

    return [
        {"$match": {"department": department}},
        {"$sort": {"emp_code": 1}},
        {"$limit": 1},
        {
            "$set": {
                "calendar_days": {
                    "$map": {
                        "input": {"$range": [0, range_days]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": from_string,
                                        "timezone": "+05:30",
                                    }
                                },
                                "unit": "day",
                                "amount": "$$offset",
                                "timezone": "+05:30",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$calendar_days"},
        {
            "$set": {
                "trend_date": {
                    "$dateToString": {
                        "date": "$calendar_days",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$trend_date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    *eligible_metrics,
                ],
                "as": "daily_metrics",
            }
        },
        {
            "$set": {
                "daily_metrics": {"$arrayElemAt": ["$daily_metrics", 0]}
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "date": "$calendar_days",
                                "timezone": "+05:30",
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {"$ifNull": ["$daily_metrics.headcount", 0]},
                "present_count": {
                    "$toDouble": {
                        "$ifNull": ["$daily_metrics.present_count", 0]
                    }
                },
                "late_count": {"$ifNull": ["$daily_metrics.late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                {
                                                                    "$toDecimal": "$present_count"
                                                                },
                                                                {"$toDecimal": "$headcount"},
                                                            ]
                                                        },
                                                        Decimal128("10000"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": None,
                "sortBy": {"calendar_days": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$toDecimal": "$moving_avg_7d"
                                                        },
                                                        Decimal128("10000"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$trend_date",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyResponse,
)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    result = next(
        db.employees.aggregate(_employee_monthly_pipeline(emp_code, month)),
        None,
    )
    if result is None:
        raise RuntimeError("Employee monthly aggregation returned no result")
    return result


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryResponse,
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    result = list(
        db.employees.aggregate(
            _department_summary_pipeline(month, department)
        )
    )
    return {"month": month, "items": result}


@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardResponse,
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    result = list(
        db.attendance_logs.aggregate(
            _late_leaderboard_pipeline(month, limit, department)
        )
    )
    return {"month": month, "items": result}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=DepartmentTrendResponse,
)
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail="from must not be after to",
        )
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(
        db.employees.aggregate(
            _department_trend_pipeline(department, from_date, to_date)
        )
    )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin explain endpoint
# --------------------------------------------------------------------------- #


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported endpoint. Choose one of: {sorted(allowed)}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be after date_to",
            )

        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                query["date"]["$lte"] = date_to.isoformat()
        if status:
            query["status"] = status

        explain_command = {
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="Invalid month")

        explain_command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": _employee_monthly_pipeline(emp_code, month),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="Invalid month")

        explain_command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": _department_summary_pipeline(month, department),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="Invalid month")

        explain_command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": _late_leaderboard_pipeline(month, limit, department),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )
        if from_date > to_date:
            raise HTTPException(status_code=422, detail="from must not be after to")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range must not exceed 92 days",
            )

        explain_command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": _department_trend_pipeline(
                    department, from_date, to_date
                ),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection = "employees"

    try:
        explained = db.command(explain_command)
    except PyMongoError as error:
        raise HTTPException(
            status_code=503,
            detail="MongoDB explain could not be executed",
        ) from error

    return json.loads(json_util.dumps({
    "endpoint": endpoint,
    "collection": collection,
    "explain": explained,
}))
