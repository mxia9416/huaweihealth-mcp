"""Read-only projections of Huawei's versioned diet journal."""

from __future__ import annotations

import json
import math
from datetime import date, tzinfo
from typing import Any, Iterable

from . import protocol as p

DIET_RECORD_TYPE = 600002
DIET_POINT_KEYS = {"DIET_RECORD", "600002699"}
OVERVIEW_FIELDS = {"inTake": "intake_kcal", "consumption": "active_kcal", "restingCalories": "resting_kcal",
                   "caloricDeficitGoal": "target_deficit_kcal"}
FOOD_FIELDS = {"count": "quantity", "kiloCalorie": "kcal", "protein": "protein", "fat": "fat",
               "carbohydrate": "carbohydrate"}


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _object(value: Any) -> dict:
    decoded = _decode(value)
    return decoded if isinstance(decoded, dict) else {}


def _number(value: Any) -> float | None:
    try:
        number = p.to_float(value)
    except OverflowError:
        return None
    return number if number is not None and math.isfinite(number) else None


def _numbers(source: dict, fields: dict[str, str]) -> dict:
    result = {}
    for raw_key, key in fields.items():
        value = _number(source.get(raw_key))
        if value is not None and (value >= 0 or key == "target_deficit_kcal"):
            result[key] = value
    return result


def _foods_summary(raw: list) -> list[dict]:
    items = []
    for food in raw:
        if not isinstance(food, dict):
            continue
        item = _numbers(food, FOOD_FIELDS)
        for raw_key, key in (("foodName", "name"), ("unit", "unit")):
            value = food.get(raw_key)
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()
        if item:
            items.append(item)
    return items


def _meal_summary(raw: dict, include_foods: bool) -> dict:
    meal = _numbers(raw, {"kiloCalorie": "kcal"})
    code = raw.get("whichMeal")
    if isinstance(code, (int, str)) and not isinstance(code, bool) and code != "":
        meal["meal_code"] = code
    foods = _decode(raw.get("foodDescriptors"))
    if isinstance(foods, list):
        meal["food_count"] = sum(isinstance(food, dict) for food in foods)
        if include_foods:
            meal["foods"] = _foods_summary(foods)
    return meal


def _day_summary(day: date, source: dict, include_foods: bool) -> dict:
    summary = {"date": day.isoformat(), **_numbers(_object(source.get("overview")), OVERVIEW_FIELDS)}
    if "active_kcal" in summary and "resting_kcal" in summary:
        total = summary["active_kcal"] + summary["resting_kcal"]
        summary["total_expenditure_kcal"] = round(total, 3)
        if "intake_kcal" in summary:
            summary["calculated_deficit_kcal"] = round(total - summary["intake_kcal"], 3)
    meals = _decode(source.get("meals"))
    if isinstance(meals, list):
        summary["meals"] = [meal for raw in meals if isinstance(raw, dict)
                            and (meal := _meal_summary(raw, include_foods))]
    return summary


def _local_day(source: dict, row: dict, point: dict, zone: tzinfo) -> date | None:
    for value in (source.get("localDate"), row.get("recordDay"), source.get("time"),
                  point.get("startTime"), row.get("startTime"), point.get("endTime"), row.get("endTime")):
        try:
            day = p.normalize_day(value, zone)
        except (OverflowError, ValueError, OSError):
            continue
        if day is not None:
            return day
    return None


def _updated_at(source: dict, row: dict, point: dict) -> float:
    field_version = _object(point.get("fieldsModifyTime")).get("dietRecord")
    for value in (source.get("modifiedTime"), field_version, row.get("modifiedTime"), row.get("version")):
        version = _number(value)
        if version is not None and version > 0:
            return version
    return 0


def diet_days(records: Iterable[dict], start: date, end: date, zone: tzinfo,
              include_foods: bool = False) -> list[dict]:
    """Project the latest in-range snapshot for each recorded local date."""
    days: dict[date, tuple[float, dict]] = {}
    for row in records:
        points = _decode(row.get("samplePoints"))
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict) or str(point.get("key")) not in DIET_POINT_KEYS:
                continue
            source = _object(_object(point.get("fieldsMetadata")).get("dietRecord"))
            if not source:
                value = _object(point.get("value"))
                source = _object(value.get("dietRecord", value))
            if "overview" not in source and "meals" not in source:
                continue
            day = _local_day(source, row, point, zone)
            if day is None or not start <= day <= end:
                continue
            modified = _updated_at(source, row, point)
            previous = days.get(day)
            if previous is None or modified >= previous[0]:
                days[day] = (modified, source)
    return [_day_summary(day, days[day][1], include_foods) for day in sorted(days)]
