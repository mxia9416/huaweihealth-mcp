import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from huawei_mcp.samples import health_samples


ZONE = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 25)


def stamp(day=25, hour=12):
    return int(datetime(2026, 11, day, hour, tzinfo=ZONE).timestamp() * 1000)


def envelope(key, value, *, timestamp=None, **extra):
    return {"startTime": stamp(24), "samplePoints": [
        {"key": key, "startTime": stamp() if timestamp is None else timestamp, "value": value, **extra}
    ]}


def test_heart_rate_filters_by_point_date_and_preserves_resting_kind():
    dynamic = envelope("DATA_POINT_DYNAMIC_HEARTRATE", "73")
    resting = envelope("DATA_POINT_REST_HEARTRATE", 54)
    rows = health_samples([dynamic, dynamic, resting,
                           envelope("DATA_POINT_DYNAMIC_HEARTRATE", 75, timestamp=stamp(26, 0)),
                           envelope("UNRELATED", 100)], "heart_rate", DAY, ZONE)
    assert rows == [
        {"time": "2026-11-25T12:00:00+08:00", "value": 73, "unit": "bpm", "kind": "dynamic"},
        {"time": "2026-11-25T12:00:00+08:00", "value": 54, "unit": "bpm", "kind": "resting"},
    ]


def test_hrv_spo2_and_emotion_decode_nested_values_without_exposing_metadata():
    hrv = envelope("HEART_RATE_VARIABILITY", json.dumps({"heartRateVariabilityRMSSD": 47, "uid": "private"}))
    assert health_samples([hrv], "hrv", DAY, ZONE) == [
        {"time": "2026-11-25T12:00:00+08:00", "value": 47, "unit": "ms"}]
    spo2 = envelope("BLOOD_OXYGEN_SATURATION", "96.5")
    assert health_samples([spo2], "spo2", DAY, ZONE)[0]["value"] == 96.5
    emotion = envelope("EMOTION", None, fieldsMetadata=json.dumps({"emotionStatus": 3, "deviceId": "private"}))
    unknown = envelope("EMOTION", '{"emotionStatus": 4}')
    assert health_samples([emotion, unknown], "emotion", DAY, ZONE) == [
        {"time": "2026-11-25T12:00:00+08:00", "value": 3, "unit": "code", "label": "pleasant"},
        {"time": "2026-11-25T12:00:00+08:00", "value": 4, "unit": "code", "label": "unknown"},
    ]


@pytest.mark.parametrize("metric,key,invalid", [
    ("heart_rate", "DATA_POINT_DYNAMIC_HEARTRATE", [0, -1, 301, True, "NaN", "Infinity"]),
    ("hrv", "HEART_RATE_VARIABILITY", [0, -1, 201, True, "NaN"]),
    ("spo2", "BLOOD_OXYGEN_SATURATION", [0, -1, 101, 255, True, "NaN"]),
    ("emotion", "EMOTION", [-1, 100, 255, 1.5, True, "NaN"]),
])
def test_invalid_and_sentinel_values_are_omitted(metric, key, invalid):
    records = [envelope(key, value) for value in invalid]
    assert health_samples(records, metric, DAY, ZONE) == []


def test_point_timestamp_falls_back_to_record_and_metric_is_validated():
    rows = [{"startTime": stamp(), "samplePoints": json.dumps([
        {"key": "HEART_RATE_VARIABILITY", "value": '{"heartRateVariabilityRMSSD": 42}'}])}]
    assert health_samples(rows, "hrv", DAY, ZONE)[0]["value"] == 42
    assert health_samples([envelope("HEART_RATE_VARIABILITY", 42, timestamp="bad")], "hrv", DAY, ZONE) == []
    with pytest.raises(ValueError):
        health_samples(rows, "unknown", DAY, ZONE)
