from decimal import Decimal
import re

import app.main as main
import pytest


class FakeEmployeeCollection:
    def __init__(self, employee, aggregation_result=None):
        self.employee = employee
        self.aggregation_result = aggregation_result
        self.aggregate_pipeline = None

    def find_one(self, query, projection=None):
        if self.employee and query == {"emp_code": self.employee["emp_code"]}:
            return dict(self.employee)
        return None

    def aggregate(self, pipeline):
        self.aggregate_pipeline = pipeline
        return iter([self.aggregation_result] if self.aggregation_result else [])


class FakeAnalyticsDatabase:
    def __init__(self, employee, aggregation_result=None, logs=None):
        self.employees = FakeEmployeeCollection(employee, aggregation_result)
        self.logs = logs or []


def _response():
    return {
        "emp_code": "EMP001",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 18.5,
        "leave_days": 2,
        "late_count": 3,
        "total_late_minutes": 95,
        "total_overtime_minutes": 120,
        "attendance_pct": 80.4348,
    }


def _run_monthly(monkeypatch, employee, result=None, emp_code="EMP001", month="2026-07"):
    database = FakeAnalyticsDatabase(employee, result or _response())
    monkeypatch.setattr(main, "db", database)
    response = main.employee_monthly(emp_code, month)
    return response, database.employees.aggregate_pipeline


def test_month_is_required_and_has_contract_pattern():
    route = next(
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/analytics/employees/{emp_code}/monthly"
    )
    parameter = next(
        parameter
        for parameter in route.dependant.query_params
        if parameter.name == "month"
    )

    assert parameter.field_info.is_required()
    assert parameter.field_info.metadata[0].pattern == (
        r"^\d{4}-(0[1-9]|1[0-2])$"
    )
    pattern = parameter.field_info.metadata[0].pattern
    assert re.fullmatch(pattern, "2026-10")
    assert not re.fullmatch(pattern, "2026-13")
    assert not re.fullmatch(pattern, "2026-1")


def test_unknown_employee_returns_404(monkeypatch):
    monkeypatch.setattr(main, "db", FakeAnalyticsDatabase(None))

    with pytest.raises(main.HTTPException) as error:
        main.employee_monthly("EMP001", "2026-07")

    assert error.value.status_code == 404


def test_emp005_lookup_filter_returns_october_records():
    pipeline = main._employee_monthly_pipeline("EMP005", "2026-10")
    lookup = pipeline[1]["$lookup"]
    records = [
        {"emp_code": "EMP005", "date": "2026-10-10"},
        {"emp_code": "EMP005", "date": "2026-10-11"},
        {"emp_code": "EMP005", "date": "2026-09-30"},
        {"emp_code": "EMP006", "date": "2026-10-10"},
    ]
    match = lookup["pipeline"][0]["$match"]
    matched = [
        record
        for record in records
        if record["emp_code"] == "EMP005"
        and match["date"]["$gte"] <= record["date"] <= match["date"]["$lte"]
    ]

    assert lookup["localField"] == "emp_code"
    assert lookup["foreignField"] == "emp_code"
    assert match == {
        "date": {"$gte": "2026-10-01", "$lte": "2026-10-31"}
    }
    assert [record["date"] for record in matched[:2]] == [
        "2026-10-10",
        "2026-10-11",
    ]


def test_emp005_weekend_metrics_match_stored_october_records(monkeypatch):
    result = _response()
    result.update(
        emp_code="EMP005",
        month="2026-10",
        working_days=22,
        present_days=0.0,
        leave_days=0,
        late_count=2,
        total_late_minutes=240,
        total_overtime_minutes=30,
        attendance_pct=0.0,
    )
    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP005", "joined_on": "2026-01-01"},
        result,
        emp_code="EMP005",
        month="2026-10",
    )

    assert response == result
    assert pipeline[1]["$lookup"]["pipeline"][0]["$match"]["date"] == {
        "$gte": "2026-10-01",
        "$lte": "2026-10-31",
    }


def test_no_logs_response_is_preserved(monkeypatch):
    result = _response()
    result.update(
        working_days=23,
        present_days=0.0,
        leave_days=0,
        late_count=0,
        total_late_minutes=0,
        total_overtime_minutes=0,
        attendance_pct=0.0,
    )
    response, _ = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-01-01"},
        result,
    )

    assert response["working_days"] == 23
    assert response["present_days"] == 0.0
    assert response["attendance_pct"] == 0.0


def test_mid_month_joiner_uses_join_date_for_working_days(monkeypatch):
    result = _response()
    result.update(working_days=13, present_days=0.5, attendance_pct=3.8462)

    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-07-15"},
        result,
    )

    assert response["working_days"] == 13
    effective_start = pipeline[2]["$set"]["effective_start"]
    assert effective_start["$cond"][2] == "2026-07-01"


def test_weekend_late_and_overtime_are_retained(monkeypatch):
    result = _response()
    result.update(
        present_days=1.0,
        late_count=1,
        total_late_minutes=35,
        total_overtime_minutes=40,
    )
    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-07-01"},
        result,
    )

    assert response["present_days"] == 1.0
    assert response["late_count"] == 1
    assert response["total_overtime_minutes"] == 40
    assert pipeline[1]["$lookup"]["pipeline"][0]["$match"]["date"] == {
        "$gte": "2026-07-01",
        "$lte": "2026-07-31",
    }


def test_half_day_and_leave_metrics_are_returned(monkeypatch):
    result = _response()
    result.update(present_days=0.5, leave_days=1)

    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-07-01"},
        result,
    )

    assert response["present_days"] == 0.5
    assert response["leave_days"] == 1
    assert pipeline[5]["$set"]["metrics"]["$reduce"]["initialValue"]["leave_days"] == 0


def test_pipeline_reads_stored_derived_values_only(monkeypatch):
    _, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-07-01"},
    )
    project = pipeline[1]["$lookup"]["pipeline"][1]["$project"]
    serialized = repr(pipeline)

    assert project["late_minutes"] == 1
    assert project["overtime_minutes"] == 1
    assert project["half_day"] == 1
    assert "punch_in" not in serialized
    assert "punch_out" not in serialized


def test_joiner_after_month_end_returns_zero_working_days(monkeypatch):
    result = _response()
    result.update(working_days=0, present_days=0.0, attendance_pct=None)
    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-08-01"},
        result,
    )

    assert response["working_days"] == 0
    assert response["attendance_pct"] is None
    assert pipeline[4]["$set"]["working_days"]["$cond"][0] == {
        "$gt": ["$effective_start_date", "$month_end_date"]
    }


@pytest.mark.parametrize(
    ("present_days", "working_days", "attendance_pct"),
    [
        (1.0, 7, 14.2857),
        (1.0, 6, 16.6667),
        (0.5, 8, 6.25),
    ],
)
def test_attendance_rate_rounding_boundaries(
    monkeypatch, present_days, working_days, attendance_pct
):
    result = _response()
    result.update(
        present_days=present_days,
        working_days=working_days,
        attendance_pct=attendance_pct,
    )

    response, pipeline = _run_monthly(
        monkeypatch,
        {"emp_code": "EMP001", "joined_on": "2026-07-01"},
        result,
    )

    assert response["attendance_pct"] == attendance_pct
    assert pipeline[6]["$set"]["attendance_pct"]["$cond"][1] is None
    assert str(Decimal("0.5")) in repr(pipeline[6]["$set"]["attendance_pct"])
