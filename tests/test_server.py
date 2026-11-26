import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from huawei_mcp import server
from huawei_mcp.client import HuaweiClient
from tests.test_client import fresh


async def test_tools_read_only():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {"get_daily_activity", "get_daily_health", "get_workouts", "get_body_composition", "get_diet", "get_health_samples",
                                       "get_devices", "get_login_status"}
    for t in tools:
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False


async def test_filter_tools():
    from mcp.server.fastmcp import FastMCP
    srv = FastMCP("t")
    for name in ("a", "b", "c"):
        srv.add_tool(lambda: "", name=name)
    server.filter_tools(srv, "")
    assert len(await srv.list_tools()) == 3
    server.filter_tools(srv, " a, c ,")
    assert {t.name for t in await srv.list_tools()} == {"a", "c"}
    with pytest.raises(SystemExit, match="typo"):
        server.filter_tools(srv, "a,typo")


def test_date_range():
    server.date_range("2026-01-01", "2026-12-31")
    for a, b, m in [("2026-02-01", "2026-01-01", 366), ("2026-01-01", "2026-04-01", 62), ("1.1.2026", "2026-01-02", 10)]:
        with pytest.raises(ValueError):
            server.date_range(a, b, m)


async def test_unknown_metric():
    with pytest.raises(ValueError, match="metrics"):
        await server.get_daily_health("2026-01-01", "2026-01-02", ["weight"])


async def test_daily_activity_drops_empty_days(monkeypatch):
    async def fake_stat(a, b):
        return {"sportStat": [
            {"recordDay": 20261007, "sportType": 0, "sportBasicInfo": {"steps": 0, "calorie": 0, "duration": 0},
             "goalAchieveBasic": {"stepGoalValueStat": 10000}},
            {"recordDay": 20261010, "sportType": 0, "sportBasicInfo": {"steps": 4321, "distance": 3000, "calorie": 150000},
             "goalAchieveBasic": {"stepGoalValueStat": 10000}}]}
    monkeypatch.setattr(server.client, "sports_stat", fake_stat)
    out = await server.get_daily_activity("2026-10-07", "2026-10-10")
    assert out == '[{"date":"2026-10-10","steps":4321,"kcal":150.0,"distance_km":3.0,"step_goal":10000}]'


async def test_workouts_uses_formal_records_and_local_dates(monkeypatch, tmp_path):
    zone = ZoneInfo("Asia/Shanghai")

    def milliseconds(day, hour, minute=0):
        return int(datetime(2026, 6, day, hour, minute, tzinfo=zone).timestamp() * 1000)

    def workout(record_id, start, **fields):
        return {"recordId": record_id, "startTime": start, "endTime": start + 360000,
                "sportType": 3, "totalTime": 360000, **fields}

    strength = workout("9007199254740993", milliseconds(10, 21), sportType=140, totalTime=2374000,
                       endTime=milliseconds(10, 21) + 2380000, totalCalories=190000,
                       attribute="v=1; activeCalorie=160000; totalCalories=190000; avgHeartRate=119; minHeartRate=82; maxHeartRate=156")
    cycling = workout("9007199254740992", milliseconds(10, 22), totalDistance=7250, totalCalories=45000,
                      attribute={"activeCalorie": 0})
    pages = [
        {"currentVersion": milliseconds(12, 16), "detailInfos": [strength, cycling,
          workout("deleted", milliseconds(10, 8), dataId="37"), workout("next-day", milliseconds(11, 0)),
          workout("prior-day", milliseconds(9, 23, 59))]},
        {"currentVersion": milliseconds(12, 17), "deleteInfos": [{"dataId": "37"}]},
        {"currentVersion": milliseconds(12, 18), "detailInfos": []},
        {"currentVersion": milliseconds(12, 19), "detailInfos": [cycling,
          workout("late-sync", milliseconds(10, 23, 59), sportType=888, totalTime=240000, totalCalories=18000),
          workout("too-short", milliseconds(10, 12), totalTime=179999)]},
        {"detailInfos": []},
    ]
    versions = []

    def handler(request):
        assert request.url.path == "/dataQuery/path/getMotionPathByVersion"
        body = json.loads(request.content)
        assert (body["dataType"], body["deviceCode"]) == (2, 0)
        versions.append(body["version"])
        return httpx.Response(200, json={"resultCode": 0, **pages[len(versions) - 1]})

    client = HuaweiClient(fresh(tmp_path), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(server, "TZ", zone)
    try:
        rows = json.loads(await server.get_workouts("2026-06-10", "2026-06-10"))
    finally:
        await client.aclose()

    assert versions == [milliseconds(9, 0), milliseconds(12, 16), milliseconds(12, 17),
                        milliseconds(12, 18), milliseconds(12, 19)]
    assert [row["sport"] for row in rows] == ["strength_training", "cycling", "type_888"]
    assert rows[0]["duration_seconds"] == 2374
    assert (rows[0]["kcal"], rows[0]["active_kcal"], rows[0]["total_kcal"]) == (160, 160, 190)
    assert (rows[0]["avg_heart_rate_bpm"], rows[0]["min_heart_rate_bpm"], rows[0]["max_heart_rate_bpm"]) == (119, 82, 156)
    assert (rows[1]["distance_km"], rows[1]["active_kcal"], rows[1]["total_kcal"]) == (7.25, 0, 45)
    assert "avg_heart_rate_bpm" not in rows[1]
    assert rows[2]["sport_type"] == 888 and rows[2]["total_kcal"] == 18
    assert "active_kcal" not in rows[2] and "kcal" not in rows[2]


async def test_workouts_reports_a_stalled_cursor(monkeypatch, tmp_path):
    def handler(request):
        cursor = json.loads(request.content)["version"]
        return httpx.Response(200, json={"resultCode": 0, "currentVersion": cursor,
                                        "detailInfos": [{"recordId": "a"}]})

    client = HuaweiClient(fresh(tmp_path), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "client", client)
    try:
        with pytest.raises(RuntimeError, match="cursor did not advance"):
            await server.get_workouts("2026-06-10", "2026-06-10")
    finally:
        await client.aclose()


async def test_get_diet_reads_authenticated_version_pages(monkeypatch, tmp_path):
    zone = ZoneInfo("Asia/Shanghai")
    sample_time = int(datetime(2026, 6, 10, 12, tzinfo=zone).timestamp() * 1000)
    record = {"startTime": sample_time, "endTime": sample_time, "samplePoints": [{"key": "DIET_RECORD",
        "fieldsMetadata": {"dietRecord": {"localDate": "2026-06-10", "overview": {"inTake": 1600,
            "restingCalories": 1700, "consumption": 500, "caloricDeficitGoal": 400}, "meals": []}}}]}
    calls = []

    def handler(request):
        assert request.url.path == "/dataQuery/health/getHealthDataByVersion"
        body = json.loads(request.content)
        assert (body["type"], body["dataType"], body["deviceCode"]) == (600002, 2, 0)
        calls.append(body["version"])
        if len(calls) == 1:
            return httpx.Response(200, json={"resultCode": 0, "currentVersion": sample_time, "detailInfos": [record]})
        return httpx.Response(200, json={"resultCode": 0, "detailInfos": []})

    client = HuaweiClient(fresh(tmp_path), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(server, "TZ", zone)
    try:
        rows = json.loads(await server.get_diet("2026-06-10", "2026-06-10"))
    finally:
        await client.aclose()

    assert len(calls) == 2 and calls[-1] == sample_time
    assert rows[0]["date"] == "2026-06-10"
    assert rows[0]["target_deficit_kcal"] == 400
    assert rows[0]["calculated_deficit_kcal"] == 600


async def test_daily_health_accepts_spo2_only_without_requesting_default_metrics(monkeypatch):
    calls = []

    async def fake_health(start, end, types):
        calls.append(types)
        return {"bloodOxygenSaturationTotal": []}

    monkeypatch.setattr(server.client, "health_stat", fake_health)
    await server.get_daily_health("2026-06-10", "2026-06-10", ["spo2"])
    assert calls == [[16]]


async def test_health_samples_are_explicit_single_day_and_paginated(monkeypatch):
    zone = ZoneInfo("Asia/Shanghai")
    instant = int(datetime(2026, 6, 10, 12, tzinfo=zone).timestamp() * 1000)
    calls = []

    async def fake_points(kind, version):
        calls.append((kind, version))
        return [{"startTime": instant, "samplePoints": [
            {"startTime": instant, "key": "BLOOD_OXYGEN_SATURATION", "value": "97"},
            {"startTime": instant + 60000, "key": "BLOOD_OXYGEN_SATURATION", "value": "98"}]}]

    monkeypatch.setattr(server.client, "health_points_by_version", fake_points)
    monkeypatch.setattr(server, "TZ", zone)
    response = json.loads(await server.get_health_samples("2026-06-10", "spo2", limit=1))
    assert calls[0][0] == 16
    assert response["total_records"] == 2 and response["next_offset"] == 1
    assert len(response["records"]) == 1 and response["records"][0]["value"] == 97
    second = json.loads(await server.get_health_samples("2026-06-10", "spo2", offset=1, limit=1))
    assert len(second["records"]) == 1 and second["records"][0]["value"] == 98
    assert "next_offset" not in second
    for args in ({"metric": "bad"}, {"metric": "spo2", "offset": -1}, {"metric": "spo2", "limit": 0}):
        with pytest.raises(ValueError):
            await server.get_health_samples("2026-06-10", **args)
