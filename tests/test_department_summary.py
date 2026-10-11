import re

import app.main as main
import pytest
from bson import BSON


class FakeEmployeeAnalyticsCollection:
    def __init__(self, result):
        self.result = result
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.result)


class FakeAnalyticsDatabase:
    def __init__(self, result):
        self.employees = FakeEmployeeAnalyticsCollection(result)


def _department_result():
    return [
        {
            "department": "Engineering",
            "headcount": 3,
            "present_days": 1.5,
            "avg_work_hours": 8.125,
            "late_count": 1,
            "total_late_minutes": 120,
            "leave_count": 1,
            "on_duty_count": 1,
        },
        {
            "department": "Support",
            "headcount": 1,
            "present_days": 0.0,
            "avg_work_hours": None,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        },
    ]


def _run_summary(monkeypatch, result=None, month="2026-10", department=None):
    database = FakeAnalyticsDatabase(result or _department_result())
    monkeypatch.setattr(main, "db", database)
    response = main.department_summary(month, department)
    return response, database.employees.pipeline


def test_department_summary_response_shape_and_sorted_departments(monkeypatch):
    response, _ = _run_summary(monkeypatch)

    assert response["month"] == "2026-10"
    assert response["items"] == _department_result()
    assert [item["department"] for item in response["items"]] == [
        "Engineering",
        "Support",
    ]
    assert set(response["items"][0]) == {
        "department",
        "headcount",
        "present_days",
        "avg_work_hours",
        "late_count",
        "total_late_minutes",
        "leave_count",
        "on_duty_count",
    }


def test_eligible_employees_and_no_log_headcount_are_preserved(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)

    assert pipeline[0] == {
        "$match": {"joined_on": {"$lte": "2026-10-31"}}
    }
    assert pipeline[2]["$set"]["effective_start"]["$cond"] == [
        {"$gt": ["$joined_on", "2026-10-01"]},
        "$joined_on",
        "2026-10-01",
    ]
    assert pipeline[6]["$group"]["headcount"] == {"$sum": 1}


def test_join_after_month_end_is_excluded(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    assert pipeline[0]["$match"]["joined_on"] == {"$lte": "2026-10-31"}


def test_exact_department_filter_is_applied_to_employee_match(monkeypatch):
    _, pipeline = _run_summary(monkeypatch, department="Engineering")
    assert pipeline[0]["$match"] == {
        "joined_on": {"$lte": "2026-10-31"},
        "department": "Engineering",
    }


def test_lookup_uses_employee_date_index_shape_and_month_range(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    lookup = pipeline[1]["$lookup"]

    assert lookup["localField"] == "emp_code"
    assert lookup["foreignField"] == "emp_code"
    assert lookup["pipeline"][0]["$match"] == {
        "date": {"$gte": "2026-10-01", "$lte": "2026-10-31"}
    }
    assert lookup["pipeline"][1]["$project"] == {
        "_id": 0,
        "date": 1,
        "status": 1,
        "late_minutes": 1,
        "work_hours": 1,
        "half_day": 1,
    }


def test_pipeline_uses_weekdays_presence_and_half_days(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    reduce_stage = pipeline[4]["$set"]["metrics"]["$reduce"]
    serialized = repr(reduce_stage)

    assert "[2, 3, 4, 5, 6]" in serialized
    assert "present_day_halves" in serialized
    assert "half_day" in serialized
    assert "PRESENT" in serialized
    assert "WFH" in serialized
    assert "ON_DUTY" in serialized


def test_weekend_late_metrics_are_not_filtered_out(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    reduce_stage = pipeline[4]["$set"]["metrics"]["$reduce"]
    serialized = repr(reduce_stage)

    assert "late_count" in serialized
    assert "total_late_minutes" in serialized
    assert "'late_count':" in serialized


def test_average_uses_qualifying_stored_work_hours_records(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    reduce_stage = pipeline[4]["$set"]["metrics"]["$reduce"]
    serialized = repr(reduce_stage)

    assert "work_hours_sum" in serialized
    assert "work_hours_count" in serialized
    assert "has_work_hours" in serialized
    assert "avg_work_hours" in repr(pipeline[7])


def test_leave_and_on_duty_counts_are_stored_status_metrics(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    reduce_stage = pipeline[4]["$set"]["metrics"]["$reduce"]
    serialized = repr(reduce_stage)

    assert "'LEAVE'" in serialized
    assert "'ON_DUTY'" in serialized
    assert "leave_count" in serialized
    assert "on_duty_count" in serialized


def test_null_average_is_returned_when_count_is_zero(monkeypatch):
    result = _department_result()
    result[0]["avg_work_hours"] = None
    response, pipeline = _run_summary(monkeypatch, result)

    assert response["items"][0]["avg_work_hours"] is None
    assert pipeline[7]["$set"]["avg_work_hours"]["$cond"][1] is None


def test_average_is_rounded_half_up_to_four_decimals(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    average = pipeline[7]["$set"]["avg_work_hours"]
    serialized = repr(average)

    assert "10000" in serialized
    assert "0.5" in serialized
    assert "floor" in serialized


def test_missing_or_invalid_month_is_rejected_by_route_metadata():
    route = next(
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/analytics/departments/summary"
    )
    month_parameter = next(
        parameter
        for parameter in route.dependant.query_params
        if parameter.name == "month"
    )
    pattern = month_parameter.field_info.metadata[0].pattern

    assert month_parameter.field_info.is_required()
    assert re.fullmatch(pattern, "2026-10")
    assert not re.fullmatch(pattern, "2026-13")
    assert not re.fullmatch(pattern, "2026-1")


def test_pipeline_can_be_bson_encoded(monkeypatch):
    _, pipeline = _run_summary(monkeypatch)
    BSON.encode({"pipeline": pipeline})
