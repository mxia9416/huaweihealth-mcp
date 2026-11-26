"""Small, identifier-free projections of timestamped Huawei measurements."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, tzinfo
from typing import Any, Iterable


SAMPLE_TYPES = {"heart_rate": 7, "hrv": 500044, "spo2": 16, "emotion": 500031}
_KEYS = {
    "heart_rate": {"DATA_POINT_DYNAMIC_HEARTRATE": "dynamic", "DATA_POINT_REST_HEARTRATE": "resting",
                   "DATA_POINT_NEW_REST_HEARTRATE": "resting"},
    "hrv": {"HEART_RATE_VARIABILITY": None},
    "spo2": {"BLOOD_OXYGEN_SATURATION": None},
    "emotion": {"EMOTION": None},
}
_VALUE_FIELDS = {"heart_rate": "heartRate", "hrv": "heartRateVariabilityRMSSD",
                 "spo2": "saturation", "emotion": "emotionStatus"}
_UNITS = {"heart_rate": "bpm", "hrv": "ms", "spo2": "%", "emotion": "code"}
_LIMITS = {"heart_rate": 300, "hrv": 200, "spo2": 100}
_EMOTION_LABELS = {1: "unpleasant", 2: "neutral", 3: "pleasant"}


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _time(value: Any, zone: tzinfo) -> datetime | None:
    number = _number(value)
    if number is None or number < 1e12:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, zone)
    except (ValueError, OSError, OverflowError):
        return None


def _value(point: dict, metric: str) -> float | None:
    raw = _decoded(point.get("value"))
    key = _VALUE_FIELDS[metric]
    if isinstance(raw, dict):
        raw = raw.get("avgSaturation", raw.get(key)) if metric == "spo2" else raw.get(key)
    value = _number(raw)
    if value is None:
        metadata = _decoded(point.get("fieldsMetadata"))
        if isinstance(metadata, dict):
            value = _number(_decoded(metadata.get(key)))
    if value is None:
        return None
    if metric == "emotion":
        return value if value.is_integer() and 0 <= value < 100 else None
    return value if 0 < value <= _LIMITS[metric] else None


def health_samples(records: Iterable[dict], metric: str, day: date, zone: tzinfo) -> list[dict]:
    """Project one local day's samples; invalid values and duplicate samples are omitted."""
    if metric not in SAMPLE_TYPES:
        raise ValueError(f"Unknown sample metric: {metric}")
    result = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        points = _decoded(record.get("samplePoints"))
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict) or point.get("key") not in _KEYS[metric]:
                continue
            raw_time = point.get("startTime", record.get("startTime"))
            instant = _time(raw_time, zone)
            value = _value(point, metric)
            if instant is None or instant.date() != day or value is None:
                continue
            kind = _KEYS[metric][point["key"]]
            timestamp = instant.timestamp()
            identity = timestamp, value, kind
            if identity in seen:
                continue
            seen.add(identity)
            precision = "milliseconds" if instant.microsecond else "seconds"
            sample = {"time": instant.isoformat(timespec=precision), "value": value, "unit": _UNITS[metric]}
            if kind:
                sample["kind"] = kind
            if metric == "emotion":
                sample["label"] = _EMOTION_LABELS.get(int(value), "unknown")
            result.append((timestamp, sample))
    return [sample for _, sample in sorted(result, key=lambda item: item[0])]
