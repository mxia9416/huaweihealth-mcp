"""Optional daily summaries backed by the Huawei app's cloud."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, tzinfo
from typing import Any

from . import protocol as p
from .client import HuaweiClient


EXTENDED_METRICS = frozenset({"spo2", "sleep_details", "naps", "sleep_insights", "training_load"})

_SPO2_FIELDS = {
    "minSaturation": "min_pct", "maxSaturation": "max_pct", "avgSaturation": "avg_pct", "lastSaturation": "last_pct",
}
_LOAD_FIELDS = {
    "trainingLoads": "load", "aerobicTrainingLoads": "aerobic_load", "anaerobicTrainingLoads": "anaerobic_load",
    "bestTrainingLoadsFloor": "target_min", "bestTrainingLoadsCeiling": "target_max",
}
_SLEEP_FIELDS = {
    "avgBreathrate": "breaths_per_min_avg", "minBreathrate": "breaths_per_min_min",
    "maxBreathrate": "breaths_per_min_max", "avgHeartrate": "heart_rate_avg_bpm",
    "minHeartrate": "heart_rate_min_bpm", "maxHeartrate": "heart_rate_max_bpm", "avgHrv": "hrv_avg_ms",
    "avgOxygenSaturation": "spo2_avg_pct", "minOxygenSaturation": "spo2_min_pct",
    "maxOxygenSaturation": "spo2_max_pct", "sleepEfficiency": "efficiency_pct", "sleepScore": "score",
    "minBreathrateBaseline": "breaths_per_min_baseline_min", "maxBreathrateBaseline": "breaths_per_min_baseline_max",
    "minHeartrateBaseline": "heart_rate_baseline_min_bpm", "maxHeartrateBaseline": "heart_rate_baseline_max_bpm",
    "minHrvBaseline": "hrv_baseline_min_ms", "maxHrvBaseline": "hrv_baseline_max_ms",
    "minOxygenSaturationBaseline": "spo2_baseline_min_pct", "maxOxygenSaturationBaseline": "spo2_baseline_max_pct",
}
_SLEEP_ZERO_FIELDS = {
    "snoreFreq": "snore_frequency_code", "wakeCount": "wake_count", "turnOverCount": "turn_over_count",
    "wakeUpFeeling": "wake_up_feeling_code", "sleepDataQuality": "sleep_data_quality_code", "rdi": "rdi",
}


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _object(value: Any) -> dict:
    value = _decoded(value)
    return value if isinstance(value, dict) else {}


def _objects(value: Any) -> list[dict]:
    value = _decoded(value)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _numbers(values: dict, fields: dict[str, str], *, retain_zero: bool = False, max_value: float = math.inf) -> dict:
    result = {}
    for source, target in fields.items():
        number = p.to_float(values.get(source))
        if number is not None and math.isfinite(number) and number <= max_value and (number > 0 or (retain_zero and number == 0)):
            result[target] = round(number, 2)
    return result


def _scalars(values: dict, fields: dict[str, str]) -> dict:
    result = {}
    for source, target in fields.items():
        value = values.get(source)
        if isinstance(value, str) and value.strip():
            result[target] = value[:300]
        elif isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value):
            result[target] = value
    return result


def _instant(value: Any, zone: tzinfo) -> datetime | None:
    value = p.to_float(value)
    if value is None or not math.isfinite(value):
        return None
    scale = {10: 1, 13: 1000, 19: 1_000_000_000}.get(len(str(int(value))))
    if value > 0 and scale:
        try:
            return datetime.fromtimestamp(value / scale, zone)
        except (OSError, OverflowError, ValueError):
            pass
    return None


def _date(value: Any, zone: tzinfo) -> date | None:
    try:
        return p.normalize_day(value, zone)
    except (OSError, OverflowError, ValueError):
        return None


def _record_day(record: dict, zone: tzinfo) -> date | None:
    for name in ("recordDay", "day", "date", "endTime", "startTime"):
        if day := _date(record.get(name), zone):
            return day
    return None


def _local_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _summary_rows(data: Any) -> list[dict]:
    data = _decoded(data)
    if isinstance(data, list):
        return _objects(data)
    records = []
    for key, value in _object(data).items():
        if isinstance(value, dict) and "fields" in value:
            records.append({"recordDay": key, **value})
        elif isinstance(value, list):
            records.extend({"recordDay": key, **row} for row in _objects(value))
    return records


def _sleep_points(record: dict) -> dict:
    values = {}
    for point in _objects(record.get("samplePoints")):
        values.update(_object(point.get("value")))
        values.update(_object(point.get("fieldsMetadata")))
    return values


def _insight(values: dict) -> dict:
    insight = _scalars(values, {"dailySleepDegree": "degree_code", "dailySleepProblem": "problem_code",
                                "dailyTargetProblem": "target_problem_code"})
    interpretation = _object(values.get("dailySleepInterpretParam"))
    insight.update(_scalars(interpretation, {
        "dailyProblemDesc": "problem_description_code", "dailyProblemImpact": "problem_impact_code",
        "dailyQualityDesc": "quality_description_code", "dailyFactorDesc": "factor_description_code",
    }))
    for source, target, fields in (
        ("dailyProblemDescInput", "description_inputs", {"val": "val", "trans": "trans"}),
        ("dailyFactorDescInput", "factor_inputs", {"val": "val", "trans": "trans"}),
        ("dailyAdvice", "advice", {"adviceCode": "code", "adviceType": "type"}),
    ):
        selected = [_scalars(row, fields) for row in _objects(interpretation.get(source))]
        if selected := [row for row in selected if row]:
            insight[target] = selected
    tasks = []
    for task in _objects(_object(values.get("dailySleepTasks")).get("dailyTask")):
        known = _scalars(task, {"taskCode": "code", "taskType": "type"})
        card = _scalars(_object(task.get("dailyTaskCard")), {"taskNum": "count", "taskTime": "time_parameter"})
        if card:
            known["card"] = card
        if known:
            tasks.append(known)
    if tasks:
        insight["tasks"] = tasks
    return insight


async def get_extended_daily_health(client: HuaweiClient, start: date, end: date, metrics: list[str],
                                    zone: tzinfo) -> dict[str, dict]:
    """Fetch only requested optional metrics and group them by the account-local date."""
    wanted = EXTENDED_METRICS.intersection(metrics)
    if not wanted:
        return {}
    result: dict[str, dict] = {}

    def add(day: date | None, metric: str, value: dict) -> None:
        if day is not None and start <= day <= end and value:
            result.setdefault(day.isoformat(), {}).setdefault(metric, {}).update(value)

    for first, last in p.windows(start, end, 30):
        if "spo2" in wanted:
            response = await client.health_stat(first, last, [16])
            for row in _objects(response.get("bloodOxygenSaturationTotal")):
                block = _object(row.get("bloodOxygenSaturationBasic"))
                add(_record_day(row, zone), "spo2", _numbers(block, _SPO2_FIELDS, max_value=100))
        if "training_load" in wanted:
            response = await client.training_load_daily(first, last)
            for row in _summary_rows(response.get("data")):
                add(_record_day(row, zone), "training_load", _numbers(_object(row.get("fields")), _LOAD_FIELDS,
                                                                     retain_zero=True))

    version = p.history_cursor(start, zone)
    if "sleep_details" in wanted:
        summaries: dict[date, dict] = {}
        for record in await client.health_sequences_by_version(700013, version):
            summary = _object(record.get("summaryData"))
            wake = _instant(summary.get("wakeupTime"), zone) or _instant(record.get("endTime"), zone)
            day = wake.date() if wake else _record_day(record, zone)
            if day is None or not start <= day <= end:
                continue
            fields = _numbers(summary, _SLEEP_FIELDS)
            fields.update(_numbers(summary, _SLEEP_ZERO_FIELDS, retain_zero=True))
            if fields.get("snore_frequency_code") == 255:
                del fields["snore_frequency_code"]
            if fields:
                begin = _instant(summary.get("fallAsleepTime"), zone) or _instant(record.get("startTime"), zone)
                if begin:
                    fields["start"] = _local_time(begin)
                if wake:
                    fields["end"] = _local_time(wake)
                for source, target in (("bedTime", "bed_time"), ("risingTime", "rise_time")):
                    if timestamp := _instant(summary.get(source), zone):
                        fields[target] = _local_time(timestamp)
                key = (begin, wake) if begin or wake else json.dumps(fields, sort_keys=True)
                summaries.setdefault(day, {}).setdefault(key, {}).update(fields)
        for day, periods in summaries.items():
            periods = sorted(periods.values(), key=lambda period: (period.get("start", ""), period.get("end", "")))
            result.setdefault(day.isoformat(), {})["sleep_details"] = periods

    if wanted.intersection({"sleep_insights", "naps"}):
        nap_intervals: dict[date, set[tuple[datetime, datetime]]] = {}
        for record in await client.health_points_by_version(500005, version):
            values = _sleep_points(record)
            if "sleep_insights" in wanted:
                wake = _instant(values.get("wakeupTime"), zone) or _instant(record.get("endTime"), zone)
                add(wake.date() if wake else _record_day(record, zone), "sleep_insights", _insight(values))
            if "naps" in wanted:
                nap = _object(values.get("noonSleepInfo"))
                for interval in _objects(nap.get("noonSleepTimeIntervalList")):
                    begin = _instant(interval.get("startTime"), zone)
                    finish = _instant(interval.get("endTime"), zone)
                    if begin is not None and finish is not None and start <= begin.date() <= end and finish > begin:
                        nap_intervals.setdefault(begin.date(), set()).add((begin, finish))
        for day, intervals in nap_intervals.items():
            durations = [(begin, finish, (finish.timestamp() - begin.timestamp()) / 60)
                         for begin, finish in sorted(intervals)]
            sessions = [{"start": _local_time(begin), "end": _local_time(finish), "duration_min": round(minutes, 2)}
                        for begin, finish, minutes in durations]
            add(day, "naps", {"sessions": sessions, "total_min": round(sum(minutes for _, _, minutes in durations), 2)})
    return result
