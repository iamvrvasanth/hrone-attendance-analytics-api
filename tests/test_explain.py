from datetime import date

import pytest
from bson import BSON
from pymongo.errors import PyMongoError

import app.main as main


class FakeExplainDatabase:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
            "executionStats": {"nReturned": 1},
        }
        self.error = error
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        if self.error:
            raise self.error
        return self.result


def _run(monkeypatch, endpoint, **kwargs):
    database = FakeExplainDatabase()
    monkeypatch.setattr(main, "db", database)
    kwargs.setdefault("limit", 10)
    kwargs.setdefault("from_date", None)
    kwargs.setdefault("to_date", None)
    kwargs.setdefault("page", 1)
    kwargs.setdefault("page_size", 20)
    response = main.explain_endpoint(endpoint, **kwargs)
    return response, database.commands[0]


@pytest.mark.parametrize(
    ("endpoint", "kwargs", "collection"),
    [
        (
            "attendance_list",
            {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 31)},
            "attendance_logs",
        ),
        (
            "employee_monthly",
            {"emp_code": "EMP001", "month": "2026-10"},
            "employees",
        ),
        (
            "department_summary",
            {"month": "2026-10"},
            "employees",
        ),
        (
            "late_leaderboard",
            {"month": "2026-10", "limit": 10},
            "attendance_logs",
        ),
        (
            "department_trend",
            {
                "department": "Engineering",
                "from_date": date(2026, 10, 1),
                "to_date": date(2026, 10, 7),
            },
            "employees",
        ),
    ],
)
def test_supported_endpoints_return_execution_stats(monkeypatch, endpoint, kwargs, collection):
    response, command = _run(monkeypatch, endpoint, **kwargs)

    assert response["endpoint"] == endpoint
    assert response["collection"] == collection
    assert response["explain"]["executionStats"]["nReturned"] == 1
    assert command["verbosity"] == "executionStats"


def test_attendance_list_reuses_find_filters_sort_and_pagination(monkeypatch):
    _, command = _run(
        monkeypatch,
        "attendance_list",
        emp_code="EMP001",
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 31),
        status="PRESENT",
        page=2,
        page_size=5,
    )

    find = command["explain"]
    assert find["find"] == "attendance_logs"
    assert find["filter"] == {
        "emp_code": "EMP001",
        "date": {"$gte": "2026-10-01", "$lte": "2026-10-31"},
        "status": "PRESENT",
    }
    assert find["sort"] == {"date": -1, "emp_code": 1}
    assert find["skip"] == 5
    assert find["limit"] == 5


def test_aggregation_explain_reuses_pipeline_builder(monkeypatch):
    _, command = _run(
        monkeypatch,
        "late_leaderboard",
        month="2026-10",
        department="Engineering",
        limit=3,
    )

    assert command["explain"]["aggregate"] == "attendance_logs"
    assert command["explain"]["pipeline"] == main._late_leaderboard_pipeline(
        "2026-10", 3, "Engineering"
    )
    BSON.encode(command)


def test_unsupported_endpoint_returns_422(monkeypatch):
    monkeypatch.setattr(main, "db", FakeExplainDatabase())

    with pytest.raises(main.HTTPException) as error:
        main.explain_endpoint("not_supported")

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("endpoint", "kwargs"),
    [
        ("employee_monthly", {}),
        ("employee_monthly", {"emp_code": "EMP001"}),
        ("department_summary", {}),
        ("late_leaderboard", {}),
        ("department_trend", {"department": "Engineering"}),
        (
            "department_trend",
            {
                "department": "Engineering",
                "from_date": date(2026, 10, 2),
                "to_date": date(2026, 10, 1),
            },
        ),
    ],
)
def test_required_parameters_and_ranges_return_422(monkeypatch, endpoint, kwargs):
    monkeypatch.setattr(main, "db", FakeExplainDatabase())

    with pytest.raises(main.HTTPException) as error:
        call_kwargs = {
            "limit": 10,
            "from_date": None,
            "to_date": None,
            "page": 1,
            "page_size": 20,
        }
        call_kwargs.update(kwargs)
        main.explain_endpoint(
            endpoint,
            **call_kwargs,
        )

    assert error.value.status_code == 422


def test_explain_failure_returns_503_without_writing(monkeypatch):
    database = FakeExplainDatabase(error=PyMongoError("server unavailable"))
    monkeypatch.setattr(main, "db", database)

    with pytest.raises(main.HTTPException) as error:
        main.explain_endpoint("department_summary", month="2026-10")

    assert error.value.status_code == 503
    assert len(database.commands) == 1
