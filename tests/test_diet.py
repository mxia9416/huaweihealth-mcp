import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from huawei_mcp.diet import diet_days


SHANGHAI = ZoneInfo("Asia/Shanghai")
START = date(2026, 11, 2)
END = date(2026, 11, 3)


def record(diary, *, key="DIET_RECORD", modified=0):
    return {"modifiedTime": modified, "samplePoints": [
        {"key": key, "fieldsMetadata": json.dumps({"dietRecord": json.dumps(diary)})}
    ]}


def test_diet_has_day_totals_and_opt_in_food_details():
    food = {"foodName": "Food A", "foodId": "private", "deviceCode": "private", "count": 125, "unit": "g",
            "kiloCalorie": 345.5, "protein": 7.5, "fat": 0, "carbohydrate": 23.2}
    diary = {"localDate": "2026-11-02", "overview": {"inTake": 1120, "consumption": 650,
             "restingCalories": 1500, "caloricDeficitGoal": 430},
             "meals": [{"whichMeal": 20, "kiloCalorie": 345.5, "foodDescriptors": json.dumps([food])}]}
    summary, = diet_days([record(diary)], START, END, SHANGHAI)
    detailed, = diet_days([record(diary)], START, END, SHANGHAI, include_foods=True)

    assert summary == {"date": "2026-11-02", "intake_kcal": 1120, "active_kcal": 650, "resting_kcal": 1500,
                       "total_expenditure_kcal": 2150, "target_deficit_kcal": 430, "calculated_deficit_kcal": 1030,
                       "meals": [{"meal_code": 20, "kcal": 345.5, "food_count": 1}]}
    assert detailed["meals"][0]["foods"] == [{"name": "Food A", "quantity": 125, "unit": "g", "kcal": 345.5,
                                              "protein": 7.5, "fat": 0, "carbohydrate": 23.2}]


def test_diet_separates_missing_fields_from_zero_and_keeps_signed_deficit():
    missing = {"localDate": 20261102, "overview": {"inTake": 0, "consumption": 0,
               "restingCalories": "NaN", "caloricDeficitGoal": 0}}
    surplus = {"localDate": 20261103, "overview": {"inTake": "1200", "consumption": "0",
               "restingCalories": "1000", "caloricDeficitGoal": -300}}

    left, right = diet_days([record(surplus), record(missing)], START, END, SHANGHAI)

    assert left == {"date": "2026-11-02", "intake_kcal": 0, "active_kcal": 0, "target_deficit_kcal": 0}
    assert right["total_expenditure_kcal"] == 1000
    assert right["calculated_deficit_kcal"] == -200
    assert right["target_deficit_kcal"] == -300


def test_diet_uses_local_date_and_keeps_latest_snapshot_of_each_day():
    newer = record({"localDate": 20261102, "overview": {"inTake": 200}}, modified=20)
    older = record({"localDate": 20261102, "overview": {"inTake": 999, "consumption": 110}}, modified=10)
    outside = record({"localDate": 20261101, "overview": {"inTake": 600}})
    outside["recordDay"] = 20261102
    invalid = {"recordDay": 20261102, "samplePoints": [{"key": "DIET_RECORD", "fieldsMetadata": "{"}]}
    local_timestamp = int(datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    fallback = {"startTime": local_timestamp, "samplePoints": [
        {"key": 600002699, "value": json.dumps({"overview": {"inTake": 300}})}
    ]}

    rows = diet_days([newer, older, outside, invalid, fallback], START, END, SHANGHAI)

    assert rows == [{"date": "2026-11-02", "intake_kcal": 200}, {"date": "2026-11-03", "intake_kcal": 300}]
