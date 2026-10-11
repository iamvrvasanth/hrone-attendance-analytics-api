
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from app.main import (
    IST,
    _as_ist_second,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    migrate_attendance_schema,
)
import app.main as main
import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


def test_late_grace_boundary():
    punch = datetime(2026, 7, 6, 9, 40, tzinfo=IST)
    assert compute_late_minutes(punch, "09:30") == 0


def test_late_one_second_beyond_grace():
    punch = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(punch, "09:30") == 10


def test_work_hours_half_up():
    start = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    end = start + timedelta(seconds=8 * 3600 + 57 * 60 + 54)
    assert compute_work_hours(start, end) == 8.97


def test_overtime_under_30_minutes():
    punch = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(
        punch, "09:30", "18:30", "2026-07-06"
    ) == 0


def test_overtime_exactly_30_minutes():
    punch = datetime(2026, 7, 6, 19, 0, tzinfo=IST)
    assert compute_overtime(
        punch, "09:30", "18:30", "2026-07-06"
    ) == 30


def test_overnight_overtime():
    punch = datetime(2026, 7, 7, 6, 40, tzinfo=IST)
    assert compute_overtime(
        punch, "22:00", "06:00", "2026-07-06"
    ) == 40


def test_naive_mongodb_datetime_is_utc():
    value = datetime(2026, 7, 6, 4, 30)
    result = _as_ist_second(value)
    assert result == datetime(2026, 7, 6, 10, 0, tzinfo=IST)


class FakeAttendanceCollection:
    def __init__(self, records):
        self.records = records
        self.update_calls = []

    def find(self, *_args, **_kwargs):
        return [dict(record) for record in self.records]

    def update_many(self, query, update):
        self.update_calls.append((query, update))
        changed = 0
        for record in self.records:
            if record["_id"] in query["_id"]["$in"] and "date" not in record:
                record["date"] = record.pop("attendance_date")
                changed += 1

        return type("Result", (), {"modified_count": changed})()

    def index_information(self):
        return {}


def test_migration_renames_only_legacy_date_and_preserves_history(monkeypatch):
    history = [{"at": 123, "changes": {}}]
    collection = FakeAttendanceCollection([{
        "_id": 1,
        "emp_code": "EMP001",
        "attendance_date": "2026-07-06",
        "history": history,
        "status": "PRESENT",
    }])
    monkeypatch.setattr(main, "db", type("DB", (), {
        "attendance_logs": collection,
    })())

    migrate_attendance_schema()

    assert collection.records[0]["date"] == "2026-07-06"
    assert "attendance_date" not in collection.records[0]
    assert collection.records[0]["history"] == history


def test_migration_stops_on_conflicting_date_fields(monkeypatch):
    collection = FakeAttendanceCollection([{
        "_id": 1,
        "emp_code": "EMP001",
        "date": "2026-07-06",
        "attendance_date": "2026-07-07",
    }])
    monkeypatch.setattr(main, "db", type("DB", (), {
        "attendance_logs": collection,
    })())

    with pytest.raises(RuntimeError, match="Conflicting"):
        migrate_attendance_schema()

    assert collection.update_calls == []


class FakeEndpointCollection:
    def __init__(self, records=None):
        self.records = [dict(record) for record in (records or [])]
        self.inserted = None
        self.duplicate_on_insert = False
        self.modified_count = 1

    def find_one(self, query, projection=None, sort=None):
        def matches_query(record):
            for key, expected in query.items():
                actual = record.get(key)
                if isinstance(expected, dict):
                    if "$lte" in expected and not actual <= expected["$lte"]:
                        return False
                elif actual != expected:
                    return False
            return True

        matches = [record for record in self.records if matches_query(record)]
        if sort and matches:
            key, direction = sort[0]
            matches.sort(key=lambda record: record[key], reverse=direction < 0)
        return dict(matches[0]) if matches else None

    def insert_one(self, document):
        if self.duplicate_on_insert:
            raise DuplicateKeyError("duplicate employee/date")
        self.inserted = dict(document)
        self.records.append(self.inserted)
        return SimpleNamespace(inserted_id="new-id")

    def update_one(self, query, update):
        if self.modified_count != 1:
            return SimpleNamespace(modified_count=self.modified_count)
        for record in self.records:
            if all(record.get(key) == value for key, value in query.items()):
                record.update(update["$set"])
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


class FakeEndpointDatabase:
    def __init__(self, attendance_records=None):
        self.employees = FakeEndpointCollection([{
            "emp_code": "EMP001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }])
        self.attendance_logs = FakeEndpointCollection(attendance_records)


def test_create_employee_duplicate_key_race_returns_conflict(monkeypatch):
    database = FakeEndpointDatabase()
    database.employees.duplicate_on_insert = True
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.create_employee(main.EmployeeIn(
            emp_code="EMP002",
            name="New Employee",
            email="new.employee@example.com",
            department="Engineering",
            joined_on="2026-07-06",
        ))

    assert error.value.status_code == 409
    assert error.value.detail == "emp_code already exists"


def test_create_employee_stores_bson_datetime_and_returns_epoch_ms(monkeypatch):
    database = FakeEndpointDatabase()
    monkeypatch.setattr(main, "db", database)

    response = main.create_employee(main.EmployeeIn(
        emp_code="EMP002",
        name="New Employee",
        email="new.employee@example.com",
        department="Engineering",
        joined_on="2026-07-06",
    ))

    stored = database.employees.inserted
    assert isinstance(stored["created_at"], datetime)
    assert stored["created_at"].tzinfo is not None
    assert response["created_at"] == int(stored["created_at"].timestamp() * 1000)
    assert isinstance(response["created_at"], int)
    assert response["emp_code"] == "EMP002"
    assert response["name"] == "New Employee"
    assert response["email"] == "new.employee@example.com"
    assert response["department"] == "Engineering"
    assert response["shift_start"] == "09:30"
    assert response["shift_end"] == "18:30"
    assert response["joined_on"] == "2026-07-06"


def _epoch_ms(value):
    return int(value.timestamp() * 1000)


def _open_attendance_record():
    return {
        "_id": "record-id",
        "emp_code": "EMP001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def _open_record(record_id, attendance_date, punch_in):
    record = _open_attendance_record()
    record.update({
        "_id": record_id,
        "date": attendance_date,
        "punch_in": punch_in,
    })
    return record


def test_punch_in_stores_empty_history(monkeypatch):
    database = FakeEndpointDatabase()
    monkeypatch.setattr(main, "db", database)

    main.punch_in(main.PunchInIn(
        emp_code="EMP001",
        punched_at=_epoch_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)),
    ))

    assert database.attendance_logs.inserted["history"] == []


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_punch_in_accepts_presence_statuses(monkeypatch, status):
    database = FakeEndpointDatabase()
    monkeypatch.setattr(main, "db", database)

    response = main.punch_in(main.PunchInIn(
        emp_code="EMP001",
        status=status,
        punched_at=_epoch_ms(
            datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        ),
    ))

    assert response["status"] == status
    assert database.attendance_logs.inserted["status"] == status


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_punch_in_rejects_non_presence_statuses_without_writes(
    monkeypatch, status
):
    existing = _open_attendance_record()
    database = FakeEndpointDatabase([existing])
    monkeypatch.setattr(main, "db", database)
    original_records = [dict(record) for record in database.attendance_logs.records]

    with pytest.raises(HTTPException) as error:
        main.punch_in(main.PunchInIn(
            emp_code="EMP001",
            status=status,
            punched_at=_epoch_ms(
                datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 422
    assert error.value.detail == "Invalid attendance status"
    assert database.attendance_logs.inserted is None
    assert database.attendance_logs.records == original_records
    assert database.attendance_logs.records[0]["history"] == []


def test_punch_in_duplicate_key_returns_conflict(monkeypatch):
    database = FakeEndpointDatabase()
    database.attendance_logs.duplicate_on_insert = True
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.punch_in(main.PunchInIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(
                datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 409


@pytest.mark.parametrize("offset_seconds", [0, -1])
def test_punch_out_rejects_non_later_timestamp(monkeypatch, offset_seconds):
    record = _open_attendance_record()
    database = FakeEndpointDatabase([record])
    monkeypatch.setattr(main, "db", database)
    punch_out = record["punch_in"] + timedelta(seconds=offset_seconds)

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(punch_out),
        ))

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(hours=24, seconds=1),
    ],
)
def test_punch_out_rejects_more_than_24_hours(monkeypatch, offset):
    record = _open_attendance_record()
    original = dict(record)
    database = FakeEndpointDatabase([record])
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(record["punch_in"] + offset),
        ))

    assert error.value.status_code == 422
    assert database.attendance_logs.records[0] == original


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(hours=24, seconds=-1),
        timedelta(hours=24),
    ],
)
def test_punch_out_accepts_at_most_24_hours(monkeypatch, offset):
    record = _open_attendance_record()
    database = FakeEndpointDatabase([record])
    monkeypatch.setattr(main, "db", database)

    response = main.punch_out(main.PunchOutIn(
        emp_code="EMP001",
        punched_at=_epoch_ms(record["punch_in"] + offset),
    ))

    assert response["punch_out"] is not None
    assert database.attendance_logs.records[0]["history"] == []


def test_punch_out_accepts_later_timestamp(monkeypatch):
    record = _open_attendance_record()
    database = FakeEndpointDatabase([record])
    monkeypatch.setattr(main, "db", database)

    response = main.punch_out(main.PunchOutIn(
        emp_code="EMP001",
        punched_at=_epoch_ms(
            record["punch_in"] + timedelta(hours=8)
        ),
    ))

    assert response["punch_out"] is not None
    assert response["work_hours"] == 8.0


def test_punch_out_selects_eligible_record_over_future_open_record(monkeypatch):
    eligible = _open_record(
        "eligible",
        "2026-07-06",
        datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
    )
    future = _open_record(
        "future",
        "2026-07-07",
        datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
    )
    database = FakeEndpointDatabase([eligible, future])
    monkeypatch.setattr(main, "db", database)

    main.punch_out(main.PunchOutIn(
        emp_code="EMP001",
        punched_at=_epoch_ms(
            datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        ),
    ))

    assert database.attendance_logs.records[0]["punch_out"] is not None
    assert database.attendance_logs.records[1]["punch_out"] is None


def test_punch_out_does_not_modify_only_future_open_records(monkeypatch):
    future = _open_record(
        "future",
        "2026-07-07",
        datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
    )
    original = dict(future)
    database = FakeEndpointDatabase([future])
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(
                datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 422
    assert database.attendance_logs.records[0] == original


def test_repeated_punch_out_returns_conflict_without_modifying_history(
    monkeypatch,
):
    record = _open_record(
        "completed",
        "2026-07-06",
        datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
    )
    record["punch_out"] = datetime(
        2026, 7, 6, 12, 0, tzinfo=timezone.utc
    )
    record["work_hours"] = 8.0
    record["history"] = [{"at": "existing", "changes": {}}]
    database = FakeEndpointDatabase([record])
    monkeypatch.setattr(main, "db", database)
    original = dict(database.attendance_logs.records[0])

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(
                datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 409
    assert database.attendance_logs.records[0] == original


def test_punch_out_nonexistent_record_returns_not_found(monkeypatch):
    database = FakeEndpointDatabase([])
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(
                datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 404
    assert database.attendance_logs.records == []


def test_punch_out_selects_most_recent_eligible_record(monkeypatch):
    oldest = _open_record(
        "oldest",
        "2026-07-05",
        datetime(2026, 7, 5, 4, 0, tzinfo=timezone.utc),
    )
    newest = _open_record(
        "newest",
        "2026-07-06",
        datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
    )
    database = FakeEndpointDatabase([oldest, newest])
    monkeypatch.setattr(main, "db", database)

    main.punch_out(main.PunchOutIn(
        emp_code="EMP001",
        punched_at=_epoch_ms(
            datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        ),
    ))

    assert database.attendance_logs.records[1]["punch_out"] is not None
    assert database.attendance_logs.records[0]["punch_out"] is None


def test_punch_out_concurrent_update_returns_conflict(monkeypatch):
    database = FakeEndpointDatabase([_open_attendance_record()])
    database.attendance_logs.modified_count = 0
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(HTTPException) as error:
        main.punch_out(main.PunchOutIn(
            emp_code="EMP001",
            punched_at=_epoch_ms(
                datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
            ),
        ))

    assert error.value.status_code == 409
