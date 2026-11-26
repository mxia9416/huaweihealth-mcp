import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from huawei_mcp.daily_extensions import get_extended_daily_health


ZONE = ZoneInfo("Asia/Shanghai")
START = date(2026, 10, 3)


def stamp(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp() * 1000)


class Cloud:
    def __init__(self):
        self.spo2_windows = []
        self.load_windows = []
        self.point_requests = []
        self.sequence_requests = []

    async def health_stat(self, first, last, types):
        self.spo2_windows.append((first, last, types))
        return {"bloodOxygenSaturationTotal": [
            {"recordDay": 20261002, "bloodOxygenSaturationBasic": {"avgSaturation": 92}},
            {"recordDay": 20261003, "bloodOxygenSaturationBasic": {"minSaturation": 92, "maxSaturation": 99,
                                                                  "avgSaturation": 95.5, "lastSaturation": 94}},
            {"recordDay": 20261004, "bloodOxygenSaturationBasic": {"avgSaturation": -1, "maxSaturation": 255}},
        ]}

    async def health_points_by_version(self, type_id, version):
        self.point_requests.append((type_id, version))
        point = {
            "value": json.dumps({"dailySleepDegree": 2, "dailySleepProblem": 4}),
            "fieldsMetadata": {
                "dailySleepInterpretParam": {
                    "dailyProblemDesc": "sleep_problem_04",
                    "dailyProblemDescInput": [{"val": 20, "trans": 1, "privateKey": "skip"}],
                    "dailyAdvice": [{"adviceCode": "sleep_advice_regular", "adviceType": 2, "adviceId": "skip"}],
                },
                "dailySleepTasks": {"dailyTask": [{"taskCode": "task_3", "taskType": 2, "taskId": "skip",
                                                  "dailyTaskCard": {"taskNum": 1, "taskTime": 240}}]},
                "noonSleepInfo": {"noonSleepTimeIntervalList": [
                    {"startTime": stamp(3, 13), "endTime": stamp(3, 13, 20), "timeZone": "UTC+08:00"},
                    {"startTime": stamp(3, 13), "endTime": stamp(3, 13, 20)},
                    {"startTime": stamp(4, 13), "endTime": stamp(4, 13, 10)},
                ]},
            },
        }
        return [{"startTime": stamp(2, 22), "endTime": stamp(3, 7), "samplePoints": [point]}]

    async def health_sequences_by_version(self, type_id, version):
        self.sequence_requests.append((type_id, version))
        night = {"startTime": stamp(2, 22), "endTime": stamp(3, 7), "summaryData": json.dumps({
            "avgBreathrate": 14, "minHrvBaseline": 28, "snoreFreq": 255, "minOxygenSaturation": -1,
            "wakeUpFeeling": 0, "notAllowlisted": "skip", "rdi": 3,
            "bedTime": stamp(2, 21, 40) // 1000, "fallAsleepTime": stamp(2, 22, 5) // 1000,
            "risingTime": stamp(3, 7, 20) // 1000, "wakeupTime": stamp(3, 7, 15) // 1000,
        }), "detailData": "encrypted sequence"}
        return [night, night, {"startTime": stamp(3, 22), "endTime": stamp(4, 7), "summaryData": {"avgHrv": 31}}]

    async def training_load_daily(self, first, last):
        self.load_windows.append((first, last))
        return {"data": {
            "20261002": [{"recordDay": 20261002, "fields": {"trainingLoads": 50}}],
            "20261003": [
                {"recordDay": 20261003, "fields": {"trainingLoads": 0, "bestTrainingLoadsFloor": 100,
                                                    "bestTrainingLoadsCeiling": 200}},
                {"recordDay": 20261003, "fields": {"aerobicTrainingLoads": 0}},
            ],
        }}


async def test_extended_metrics_are_opt_in_and_share_sleep_points():
    client = Cloud()
    out = await get_extended_daily_health(client, START, START, ["sleep_insights", "naps", "heart_rate"], ZONE)

    assert client.point_requests == [(500005, stamp(2, 0))]
    assert not client.spo2_windows and not client.load_windows and not client.sequence_requests
    assert set(out) == {"2026-10-03"}
    assert out["2026-10-03"]["naps"] == {
        "total_min": 20,
        "sessions": [{"start": "2026-10-03 13:00", "end": "2026-10-03 13:20", "duration_min": 20}],
    }
    assert out["2026-10-03"]["sleep_insights"] == {
        "degree_code": 2, "problem_code": 4, "problem_description_code": "sleep_problem_04",
        "description_inputs": [{"val": 20, "trans": 1}],
        "advice": [{"code": "sleep_advice_regular", "type": 2}],
        "tasks": [{"code": "task_3", "type": 2, "card": {"count": 1, "time_parameter": 240}}],
    }


async def test_sleep_details_retain_structured_summary_once_on_waking_day():
    client = Cloud()
    out = await get_extended_daily_health(client, START, START, ["sleep_details"], ZONE)

    assert client.sequence_requests == [(700013, stamp(2, 0))]
    assert out == {"2026-10-03": {"sleep_details": [{
        "start": "2026-10-02 22:05", "end": "2026-10-03 07:15", "breaths_per_min_avg": 14,
        "hrv_baseline_min_ms": 28, "wake_up_feeling_code": 0, "rdi": 3,
        "bed_time": "2026-10-02 21:40", "rise_time": "2026-10-03 07:20",
    }]}}


async def test_spo2_and_training_summary_filter_dates_and_preserve_real_zero():
    client = Cloud()
    end = START + timedelta(days=30)
    out = await get_extended_daily_health(client, START, end, ["spo2", "training_load"], ZONE)

    assert client.spo2_windows == [(20261003, 20261101, [16]), (20261102, 20261102, [16])]
    assert client.load_windows == [(20261003, 20261101), (20261102, 20261102)]
    assert out == {"2026-10-03": {
        "spo2": {"min_pct": 92, "max_pct": 99, "avg_pct": 95.5, "last_pct": 94},
        "training_load": {"load": 0, "aerobic_load": 0, "target_min": 100, "target_max": 200},
    }}


async def test_no_requested_extensions_performs_no_cloud_queries():
    client = Cloud()
    assert await get_extended_daily_health(client, START, START, ["heart_rate", "stress"], ZONE) == {}
    assert not client.spo2_windows and not client.load_windows and not client.point_requests and not client.sequence_requests
