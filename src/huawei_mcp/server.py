"""MCP-сервер Huawei Health: только чтение (активность, сон, пульс, стресс, тренировки).

Переменные окружения:
  HUAWEI_TOKEN_FILE  файл пары токенов (по умолчанию /data/tokens.json), создаёт huawei-mcp-login
  HUAWEI_TZ          часовой пояс аккаунта для дат и времени (по умолчанию Europe/Moscow)
  HUAWEI_OWNER_HEIGHT  рост владельца (см) в профиле весов: какие взвешивания его (весами пользуется семья)
  HUAWEI_ENABLED_TOOLS  список инструментов через запятую (пусто — все); остальные не публикуются,
                        чтобы не занимать контекст модели
  MCP_HOST / MCP_PORT  адрес Streamable HTTP (0.0.0.0:8000, путь /mcp)
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import protocol as p
from .client import AuthRequired, HuaweiClient, HuaweiError
from .daily_extensions import EXTENDED_METRICS, get_extended_daily_health
from .diet import DIET_RECORD_TYPE, diet_days
from .samples import SAMPLE_TYPES, health_samples

TZ = ZoneInfo(os.getenv("HUAWEI_TZ", "Europe/Moscow"))
OWNER_HEIGHT = int(os.getenv("HUAWEI_OWNER_HEIGHT") or 0) or None
MAX_RANGE_DAYS = 366
WORKOUT_RANGE_DAYS = 62
STAT_WINDOW_DAYS = 30
MAX_OUTPUT_CHARS = 60_000
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

mcp = FastMCP(
    "Huawei Health",
    instructions=(
        "Read-only access to the user's Huawei Health data (the Health app's own cloud, synced from "
        "the Huawei scale, watch/band and phone). Dates are YYYY-MM-DD in the account's time zone. "
        "Start weekly reports with daily summaries. If a specific date needs investigation because of a "
        "potential concern, unexpected value, or the user's question, use get_health_samples for that date "
        "and metric. Fetch detail pages only as needed and check coverage before drawing conclusions."
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8000")),
    stateless_http=True,
    json_response=True,
)

client = HuaweiClient(Path(os.getenv("HUAWEI_TOKEN_FILE", "/data/tokens.json")))


# ----------------------------------------------------------------------------- helpers

def clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {k: clean(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [clean(v) for v in obj if v not in (None, "", [], {})]
    return obj


def dump(obj: Any, *, require_complete: bool = False) -> str:
    text = json.dumps(clean(obj), ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > MAX_OUTPUT_CHARS:
        if require_complete:
            raise ValueError("The result is too large. Query a shorter period or request fewer details.")
        text = text[:MAX_OUTPUT_CHARS] + f"... [обрезано: ответ {len(text)} символов, сузьте период]"
    return text


def date_range(start_date: str, end_date: str, max_days: int = MAX_RANGE_DAYS) -> tuple[date, date]:
    try:
        s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("Даты в формате YYYY-MM-DD") from exc
    if e < s:
        raise ValueError("end_date раньше start_date")
    if (e - s).days > max_days:
        raise ValueError(f"Диапазон не больше {max_days} дней")
    return s, e


async def guarded(coro):
    try:
        return await coro
    except AuthRequired as e:
        client.forget()
        raise RuntimeError(
            f"Huawei Health: требуется вход ({e}). Администратору: заново выполнить `huawei-mcp-login` в контейнере сервера."
        ) from e
    except HuaweiError as e:
        raise RuntimeError(str(e)) from e


# ----------------------------------------------------------------------------- tools

@mcp.tool(annotations=READ_ONLY)
async def get_daily_activity(start_date: str, end_date: str) -> str:
    """Daily activity totals per day: steps, distance, calories, active/walking minutes,
    active hours, moderate-to-high intensity minutes, step goal. Up to 366 days."""
    s, e = date_range(start_date, end_date)
    days: dict[str, dict] = {}
    for a, b in p.windows(s, e, STAT_WINDOW_DAYS):
        answer = await guarded(client.sports_stat(a, b))
        days.update(p.daily_activity(answer.get("sportStat") or []))
    rows = []
    for d in sorted(days):
        r = dict(days[d])
        r.pop("sport_type", None)
        goal = r.pop("step_goal", None)
        dist = r.pop("distance_m", None)
        r["distance_km"] = round(dist / 1000, 2) if dist else None
        # 0 в облаке = «нет данных» (например, дни до подключения устройства)
        r = {k: v for k, v in r.items() if k == "date" or v}
        if len(r) > 1:
            rows.append({**r, "step_goal": goal})
    return dump(rows) if rows else "За период нет данных активности"


@mcp.tool(annotations=READ_ONLY)
async def get_daily_health(start_date: str, end_date: str, metrics: list[str] | None = None) -> str:
    """Daily health metrics. Defaults to sleep, heart_rate, stress, and intensity.

    sleep: total/deep/light/REM/awake minutes, score, efficiency, latency, wake-ups, avg HR, HRV, SpO2,
    fell asleep / woke up time (a night belongs to the day of waking up);
    heart_rate: resting, min, max; stress: avg/min/max score; intensity: moderate-to-high minutes.
    Additional metrics can be requested explicitly: spo2 (independent daily blood oxygen), sleep_details
    (breathing, snoring and personal baselines), naps, sleep_insights (Huawei codes/parameters), training_load.
    Up to 366 days.
    """
    names = metrics or list(p.HEALTH_TYPES)
    supported = set(p.HEALTH_TYPES) | set(EXTENDED_METRICS)
    bad = [m for m in names if m not in supported]
    if bad:
        raise ValueError(f"Неизвестные metrics: {bad}; доступны {sorted(supported)}")
    s, e = date_range(start_date, end_date)
    days: dict[str, dict] = {}
    types = [p.HEALTH_TYPES[m] for m in names if m in p.HEALTH_TYPES]
    if types:
        for a, b in p.windows(s, e, STAT_WINDOW_DAYS):
            answer = await guarded(client.health_stat(a, b, types))
            for d, fam in p.health_by_day(answer, TZ).items():
                days.setdefault(d, {}).update({k: v for k, v in fam.items() if k in names})
    extra = [m for m in names if m in EXTENDED_METRICS]
    if extra:
        additions = await guarded(get_extended_daily_health(client, s, e, extra, TZ))
        for day, metrics_for_day in additions.items():
            days.setdefault(day, {}).update(metrics_for_day)
    rows = [{"date": d, **days[d]} for d in sorted(days) if days[d]]
    return dump(rows, require_complete=bool(extra)) if rows else "За период нет данных"


@mcp.tool(annotations=READ_ONLY)
async def get_health_samples(date: str, metric: str, offset: int = 0, limit: int = 200) -> str:
    """On-demand timestamped measurements for one account-local date (YYYY-MM-DD).

    metric is heart_rate, hrv (RMSSD), spo2 (the value for each record's measurement window), or emotion.
    Use daily summaries first; request a specific date
    when a potential concern, unexpected pattern, or user question needs investigation. Samples are observations,
    and their counts are not durations. This tool does not diagnose conditions. offset/limit paginate the sorted
    records, with at most 500 per response. Follow next_offset only when needed; pages reflect current cloud data.
    """
    if metric not in SAMPLE_TYPES:
        raise ValueError(f"metric must be one of {sorted(SAMPLE_TYPES)}")
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 500")
    day, _ = date_range(date, date)
    type_id = SAMPLE_TYPES[metric]
    if metric == "emotion":
        raw = await guarded(client.health_points_by_version(type_id, p.history_cursor(day, TZ)))
    else:
        start = datetime.combine(day, datetime.min.time(), TZ)
        next_day = start + timedelta(days=1)
        raw = await guarded(client.health_samples_by_time(
            type_id, int(start.timestamp() * 1000), int(next_day.timestamp() * 1000) - 1))
    rows = health_samples(raw, metric, day, TZ)
    stop = min(offset + limit, len(rows))
    return dump({"date": date, "metric": metric, "timezone": str(TZ), "total_records": len(rows),
                 "offset": offset, "returned_records": len(rows[offset:stop]),
                 "next_offset": stop if stop < len(rows) else None,
                 "records": rows[offset:stop]}, require_complete=True)


@mcp.tool(annotations=READ_ONLY)
async def get_diet(start_date: str, end_date: str, include_foods: bool = False) -> str:
    """Huawei diet diary by local date. Returns intake, activity and resting expenditure, and the target deficit.

    The calculated deficit is active + resting expenditure - intake and requires all three values. Set
    include_foods=true for meals and individual foods, including the recorded units and nutrient values.
    Use shorter ranges for detailed meals. Days without a diary are omitted.
    """
    s, e = date_range(start_date, end_date)
    records = await guarded(client.health_points_by_version(DIET_RECORD_TYPE, p.history_cursor(s, TZ)))
    rows = diet_days(records, s, e, TZ, include_foods)
    return dump(rows, require_complete=True) if rows else "За период нет данных о питании"


@mcp.tool(annotations=READ_ONLY)
async def get_workouts(start_date: str, end_date: str, min_minutes: int = 3) -> str:
    """Saved workouts, including cycling and strength training, by local start date.

    Returns start/end, duration, distance, steps, separate active and total calories, and average/min/max heart rate
    when Huawei provides them. The legacy kcal field is active calories. Unknown sports keep their numeric code.
    Sessions shorter than min_minutes (default 3) are excluded. Up to 62 days."""
    s, e = date_range(start_date, end_date, WORKOUT_RANGE_DAYS)
    records = await guarded(client.motion_paths(p.history_cursor(s, TZ)))
    sessions = p.motion_path_workouts(records, s, e, TZ, min_minutes)
    return dump(sessions) if sessions else "За период тренировок нет"


@mcp.tool(annotations=READ_ONLY)
async def get_body_composition(start_date: str, end_date: str, detailed: bool = False,
                               all_people: bool = False) -> str:
    """Smart-scale measurements (Huawei Scale): weight, BMI, body fat % and kg, fat-free mass,
    skeletal muscle, water, protein, bone mass, visceral fat level, BMR, body age/score,
    waist-hip ratio, heart rate; plus the change over the period.

    detailed=true adds per-segment fat/muscle (arms, legs, trunk) and raw impedances.
    By default only the account owner's measurements; all_people=true also returns other family
    members' and unrecognized weigh-ins (each item has the scale profile: height, age, gender).
    Measurements without full_analysis were taken without the hand electrodes (weight only). Up to 366 days.
    """
    s, e = date_range(start_date, end_date)
    records = await guarded(client.by_time(
        lambda a, b: client.health_data(a, b, p.WEIGHT_TYPE), p.windows(s, e, p.BY_TIME_WINDOW_DAYS)))
    items = p.body_measurements(records, TZ, detailed=detailed,
                                owner_height=None if all_people else OWNER_HEIGHT,
                                include_unassigned=all_people)
    if not items:
        return "За период взвешиваний нет"
    if not all_people:
        for m in items:
            m.pop("profile", None)
    return dump({"measurements": items, "trend": p.body_trend(items) if not all_people else None})


@mcp.tool(annotations=READ_ONLY)
async def get_devices() -> str:
    """Huawei devices bound to the account (watch, band, scale, phone)."""
    answer = await guarded(client.bind_devices())
    data = {k: v for k, v in answer.items() if k not in ("resultCode", "resultDesc")}
    return dump(data) if clean(data) else "Привязанных устройств нет"


@mcp.tool(annotations=READ_ONLY)
async def get_login_status() -> str:
    """Whether the Huawei Health login is valid and how many days remain until a browser re-login is needed."""
    try:
        t = client.tokens
    except AuthRequired as e:
        return f"Входа нет: {e}. Администратору: `huawei-mcp-login`."
    days_left = int((t.refresh_expires_at - time.time()) // 86400)
    return dump({"logged_in": True, "region": t.region, "relogin_in_days": days_left,
                 "relogin_after": datetime.fromtimestamp(t.refresh_expires_at, TZ).date().isoformat()})


# ----------------------------------------------------------------------------- filter

def filter_tools(server: FastMCP, enabled: str | None) -> None:
    """Оставить только инструменты из списка через запятую; пустой список — все.
    Опечатка в имени — ошибка на старте, а не молча пропавший инструмент."""
    wanted = {n.strip() for n in (enabled or "").split(",") if n.strip()}
    if not wanted:
        return
    have = {t.name for t in server._tool_manager.list_tools()}
    unknown = wanted - have
    if unknown:
        raise SystemExit(f"HUAWEI_ENABLED_TOOLS: неизвестные инструменты {sorted(unknown)}; есть {sorted(have)}")
    for name in have - wanted:
        server.remove_tool(name)


filter_tools(mcp, os.getenv("HUAWEI_ENABLED_TOOLS"))


# ----------------------------------------------------------------------------- http

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    # процесс жив; облако Huawei не дёргаем
    try:
        client.tokens
        return JSONResponse({"status": "ok", "auth": True})
    except AuthRequired:
        return JSONResponse({"status": "ok", "auth": False})


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
