from datetime import date

import pytest
from bson import BSON

import app.main as main


class FakeTrendEmployees:
    def __init__(self, result, exists=True):
        self.result = result
        self.exists = exists
        self.pipeline = None

    def find_one(self, *args, **kwargs):
        return {"_id": "department"} if self.exists else None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.result)


class FakeTrendDatabase:
    def __init__(self, result, exists=True):
        self.employees = FakeTrendEmployees(result, exists)


def _items():
    return [
        {
            "date": "2026-10-01",
            "is_working_day": True,
            "headcount": 2,
            "present_count": 1.5,
            "late_count": 1,
            "attendance_rate": 0.75,
            "moving_avg_7d": 0.75,
        },
        {
            "date": "2026-10-03",
            "is_working_day": False,
            "headcount": 2,
            "present_count": 1.0,
            "late_count": 1,
            "attendance_rate": None,
            "moving_avg_7d": 0.75,
        },
    ]


def _run_trend(monkeypatch, result=None, exists=True):
    database = FakeTrendDatabase(result or _items(), exists)
    monkeypatch.setattr(main, "db", database)
    response = main.department_trend(
        "Engineering", date(2026, 10, 1), date(2026, 10, 3)
    )
    return response, database.employees.pipeline


def test_trend_response_shape_and_chronological_items(monkeypatch):
    response, _ = _run_trend(monkeypatch)

    assert response["department"] == "Engineering"
    assert [item["date"] for item in response["items"]] == [
        "2026-10-01",
        "2026-10-03",
    ]
    assert set(response["items"][0]) == {
        "date",
        "is_working_day",
        "headcount",
        "present_count",
        "late_count",
        "attendance_rate",
        "moving_avg_7d",
    }


def test_trend_pipeline_generates_inclusive_calendar_and_uses_window(monkeypatch):
    _, pipeline = _run_trend(monkeypatch)

    assert pipeline[0] == {"$match": {"department": "Engineering"}}
    assert pipeline[4]["$unwind"] == "$calendar_days"
    assert pipeline[5]["$set"]["trend_date"]["$dateToString"]["format"] == "%Y-%m-%d"
    window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
    assert window["sortBy"] == {"calendar_days": 1}
    assert window["output"]["moving_avg_7d"]["window"] == {"documents": [-6, 0]}


def test_trend_pipeline_uses_join_date_and_stored_attendance_values(monkeypatch):
    _, pipeline = _run_trend(monkeypatch)
    employee_lookup = pipeline[6]["$lookup"]
    employee_match = employee_lookup["pipeline"][0]["$match"]["$expr"]["$and"]
    assert {"$lte": ["$joined_on", "$$trend_date"]} in employee_match
    attendance_lookup = employee_lookup["pipeline"][1]["$lookup"]
    attendance_match = attendance_lookup["pipeline"][0]["$match"]["$expr"]["$and"]
    assert {"$eq": ["$date", "$$trend_date"]} in attendance_match
    assert "late_minutes" in attendance_lookup["pipeline"][1]["$project"]


def test_weekday_rate_and_weekend_presence_rules_are_in_pipeline(monkeypatch):
    _, pipeline = _run_trend(monkeypatch)
    rate_expression = next(
        stage["$set"]["attendance_rate"]
        for stage in pipeline
        if "$set" in stage and "attendance_rate" in stage["$set"]
    )
    assert "[2, 3, 4, 5, 6]" in repr(pipeline)
    assert "headcount" in repr(rate_expression)
    assert "PRESENT" in repr(pipeline)
    assert "WFH" in repr(pipeline)
    assert "ON_DUTY" in repr(pipeline)


def test_unknown_department_returns_404(monkeypatch):
    monkeypatch.setattr(main, "db", FakeTrendDatabase([], exists=False))

    with pytest.raises(main.HTTPException) as error:
        main.department_trend("Missing", date(2026, 10, 1), date(2026, 10, 1))

    assert error.value.status_code == 404


@pytest.mark.parametrize(
    ("from_date", "to_date"),
    [
        (date(2026, 10, 2), date(2026, 10, 1)),
        (date(2026, 1, 1), date(2026, 4, 3)),
    ],
)
def test_invalid_date_ranges_return_422(monkeypatch, from_date, to_date):
    monkeypatch.setattr(main, "db", FakeTrendDatabase([], exists=True))

    with pytest.raises(main.HTTPException) as error:
        main.department_trend("Engineering", from_date, to_date)

    assert error.value.status_code == 422


def test_route_requires_from_and_to_dates_and_uses_date_validation():
    route = next(
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/analytics/departments/{department}/trend"
    )
    params = {parameter.name: parameter for parameter in route.dependant.query_params}
    assert params["from_date"].alias == "from"
    assert params["to_date"].alias == "to"
    assert params["from_date"].field_info.is_required()
    assert params["to_date"].field_info.is_required()
    assert params["from_date"].field_info.annotation is date


def test_pipeline_covers_maximum_range_and_is_bson_serializable():
    pipeline = main._department_trend_pipeline(
        "Engineering", date(2026, 1, 1), date(2026, 4, 2)
    )
    BSON.encode({"pipeline": pipeline})
    calendar_range = pipeline[3]["$set"]["calendar_days"]["$map"]["input"]["$range"]
    assert calendar_range == [0, 92]


def test_rounding_and_null_moving_average_are_database_expressions():
    pipeline = main._department_trend_pipeline(
        "Engineering", date(2026, 10, 1), date(2026, 10, 1)
    )
    serialized = repr(pipeline)
    assert "10000" in serialized
    assert "0.5" in serialized
    assert "moving_avg_7d" in serialized
