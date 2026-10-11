import re

import app.main as main
import pytest
from bson import BSON


class FakeAttendanceAnalyticsCollection:
    def __init__(self, result):
        self.result = result
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.result)


class FakeLeaderboardDatabase:
    def __init__(self, result):
        self.attendance_logs = FakeAttendanceAnalyticsCollection(result)


def _items():
    return [
        {
            "rank": 1,
            "emp_code": "EMP001",
            "name": "One",
            "department": "Engineering",
            "total_late_minutes": 120,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP002",
            "name": "Two",
            "department": "Engineering",
            "total_late_minutes": 90,
            "late_count": 1,
        },
        {
            "rank": 2,
            "emp_code": "EMP003",
            "name": "Three",
            "department": "Engineering",
            "total_late_minutes": 90,
            "late_count": 3,
        },
        {
            "rank": 4,
            "emp_code": "EMP004",
            "name": "Four",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 1,
        },
    ]


def _run_leaderboard(
    monkeypatch,
    result=None,
    month="2026-10",
    limit=10,
    department=None,
):
    database = FakeLeaderboardDatabase(result or _items())
    monkeypatch.setattr(main, "db", database)
    response = main.late_leaderboard(month, limit, department)
    return response, database.attendance_logs.pipeline


def test_competition_ranking_and_tie_order_are_returned(monkeypatch):
    response, _ = _run_leaderboard(monkeypatch)

    assert [item["rank"] for item in response["items"]] == [1, 2, 2, 4]
    assert [item["emp_code"] for item in response["items"]] == [
        "EMP001",
        "EMP002",
        "EMP003",
        "EMP004",
    ]
    assert response["month"] == "2026-10"


def test_limit_is_applied_after_ranking_and_includes_cutoff_ties(monkeypatch):
    result = _items()[:3]
    response, pipeline = _run_leaderboard(
        monkeypatch,
        result=result,
        limit=2,
    )

    assert [item["rank"] for item in response["items"]] == [1, 2, 2]
    assert pipeline[7] == {"$match": {"rank": {"$lte": 2}}}


def test_department_filter_is_applied_before_ranking(monkeypatch):
    _, pipeline = _run_leaderboard(monkeypatch, department="Engineering")

    department_stage = next(
        stage
        for stage in pipeline
        if stage.get("$match", {}).get("employee.department") is not None
    )
    ranking_stage = next(stage for stage in pipeline if "$setWindowFields" in stage)

    assert department_stage == {
        "$match": {"employee.department": "Engineering"}
    }
    assert ranking_stage["$setWindowFields"]["partitionBy"] is None


def test_month_filter_and_stored_late_metrics_are_used(monkeypatch):
    _, pipeline = _run_leaderboard(monkeypatch)

    assert pipeline[0] == {
        "$match": {
            "date": {"$gte": "2026-10-01", "$lte": "2026-10-31"}
        }
    }
    assert pipeline[1]["$group"]["total_late_minutes"] == {
        "$sum": {"$ifNull": ["$late_minutes", 0]}
    }
    assert pipeline[1]["$group"]["late_count"]["$sum"]["$cond"][0] == {
        "$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]
    }


def test_zero_totals_and_missing_employees_are_excluded_by_pipeline():
    pipeline = main._late_leaderboard_pipeline("2026-10", 10)

    assert pipeline[2] == {"$match": {"total_late_minutes": {"$gt": 0}}}
    assert pipeline[4] == {"$unwind": "$employee"}


def test_pipeline_uses_standard_competition_rank_and_final_sort():
    pipeline = main._late_leaderboard_pipeline("2026-10", 10)

    assert pipeline[6]["$setWindowFields"] == {
        "partitionBy": None,
        "sortBy": {"total_late_minutes": -1},
        "output": {"rank": {"$rank": {}}},
    }
    assert pipeline[9] == {
        "$sort": {
            "total_late_minutes": -1,
            "emp_code": 1,
        }
    }


def test_response_schema_contains_exact_fields(monkeypatch):
    response, _ = _run_leaderboard(monkeypatch, result=_items()[:1])

    assert set(response) == {"month", "items"}
    assert set(response["items"][0]) == {
        "rank",
        "emp_code",
        "name",
        "department",
        "total_late_minutes",
        "late_count",
    }


def test_month_and_limit_validation_metadata():
    route = next(
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/analytics/leaderboard/late"
    )
    params = {parameter.name: parameter for parameter in route.dependant.query_params}
    month_pattern = params["month"].field_info.metadata[0].pattern

    assert params["month"].field_info.is_required()
    assert re.fullmatch(month_pattern, "2026-10")
    assert not re.fullmatch(month_pattern, "2026-13")
    assert params["limit"].field_info.default == 10
    assert params["limit"].field_info.metadata[0].ge == 1
    assert params["limit"].field_info.metadata[1].le == 50


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_limits_are_rejected_by_route_constraints(limit):
    route = next(
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/analytics/leaderboard/late"
    )
    parameter = next(
        parameter
        for parameter in route.dependant.query_params
        if parameter.name == "limit"
    )
    ge = parameter.field_info.metadata[0].ge
    le = parameter.field_info.metadata[1].le

    assert limit < ge or limit > le


def test_pipeline_can_be_bson_encoded(monkeypatch):
    _, pipeline = _run_leaderboard(monkeypatch, department="Engineering")
    BSON.encode({"pipeline": pipeline})
