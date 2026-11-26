"""Async-клиент облака Huawei Health с самоподдерживающейся парой токенов.

Токен облако принимает только «тремя способами сразу»: в JSON-теле (`token`), в
`Authorization: Bearer` и в `x-token`. Ротация убивает предыдущий access-токен и отдаёт
тот же refresh-токен — поэтому новая пара сразу пишется на диск. Ответ 1002/1004
значит, что пару уже ротировало приложение на телефоне: ротируем и повторяем один раз.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from . import protocol as p

USER_AGENT = "Mozilla/5.0 (Linux; Android 12) huawei-mcp/0.1"


class HuaweiError(RuntimeError):
    """Ошибка облака Huawei Health."""


class AuthRequired(HuaweiError):
    """Нет токенов или refresh-токен мёртв — нужен вход в браузере."""


@dataclass
class Tokens:
    access_token: str | None = None
    refresh_token: str | None = None
    uid: str | None = None
    site_id: Any = None
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    region: str = "ru"

    @property
    def hosts(self) -> dict[str, str]:
        return p.REGIONS[self.region]

    @property
    def refresh_dead(self) -> bool:
        return bool(self.refresh_expires_at and time.time() > self.refresh_expires_at)

    def needs_rotation(self) -> bool:
        if not self.refresh_token or self.refresh_dead:
            return False
        return not self.access_token or time.time() > self.expires_at - p.TOKEN_LIVE_SLACK

    def apply(self, answer: dict) -> None:
        """Ответ obtain/refresh (ThirdPartyLoginInfo) -> состояние пары."""
        access = answer.get("accessToken")
        if not access:
            raise AuthRequired("в ответе нет accessToken")
        self.access_token = str(access)
        expire_ms = p.to_float(answer.get("accessTokenExpireTime"))  # epoch, мс
        self.expires_at = expire_ms / 1000 if expire_ms else time.time() + 1800
        new_refresh = answer.get("refreshToken")
        # ротация возвращает тот же refresh-токен: его 180 дней считаются от входа
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = str(new_refresh)
            self.refresh_expires_at = time.time() + p.REFRESH_TOKEN_TTL
        elif not self.refresh_expires_at:
            self.refresh_expires_at = time.time() + p.REFRESH_TOKEN_TTL
        if answer.get("uid"):
            self.uid = str(answer["uid"])
        if answer.get("siteId") is not None:
            self.site_id = answer["siteId"]

    @classmethod
    def load(cls, path: Path) -> Tokens | None:
        try:
            raw = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        tokens = cls(**known)
        return tokens if tokens.refresh_token and tokens.region in p.REGIONS else None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f)
        tmp.replace(path)


def _ensure(answer: dict, what: str) -> dict:
    code = p.result_code(answer)
    if p.is_ok(code):
        return answer
    if p.code_int(code) in p.FATAL_CODES:
        raise AuthRequired(f"{what}: {code} {p.result_desc(answer)}")
    raise HuaweiError(f"{what}: resultCode {code} — {p.result_desc(answer)}")


class HuaweiClient:
    def __init__(self, token_file: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        self.token_file = token_file
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=30, transport=transport,
                                       headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    # ── токены ───────────────────────────────────────────────────────────
    @property
    def tokens(self) -> Tokens:
        if self._tokens is None:
            self._tokens = Tokens.load(self.token_file)
        if self._tokens is None:
            raise AuthRequired("нет токенов Huawei Health")
        if self._tokens.refresh_dead:
            raise AuthRequired("refresh-токену больше 180 дней")
        return self._tokens

    def forget(self) -> None:
        self._tokens = None

    async def _raw(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        try:
            r = await self._http.post(url, json=body, headers={"x-request-id": str(uuid.uuid4()), **headers})
        except httpx.HTTPError as e:
            raise HuaweiError(f"облако Huawei недоступно: {type(e).__name__}") from e
        try:
            answer = r.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise HuaweiError(f"{url}: HTTP {r.status_code}, ответ не JSON")
        # часть контроллеров кладёт вердикт в тело 4xx — тогда решает resultCode
        if r.status_code >= 400 and p.is_ok(p.result_code(answer)):
            raise HuaweiError(f"{url}: HTTP {r.status_code}")
        return answer

    def _session_headers(self, uid: str | None) -> dict[str, str]:
        h = {"x-ts": str(int(time.time() * 1000)), "x-version": p.APP_VERSION}
        if uid:
            h["x-huid"] = str(uid)
        return h

    async def _session_call(self, tokens: Tokens, path: str, body: dict) -> dict:
        url = tokens.hosts["session"] + path
        answer = await self._raw(url, body, self._session_headers(tokens.uid))
        # сохранённый uid мог остаться от другого аккаунта — повторяем без него
        if p.code_int(p.result_code(answer)) == p.BAD_HUID_CODE and tokens.uid:
            tokens.uid = None
            answer = await self._raw(url, body, self._session_headers(None))
        return _ensure(answer, path)

    async def exchange_code(self, code: str, region: str) -> Tokens:
        """Одноразовый код из браузера -> пара токенов (сохраняется в файл)."""
        tokens = Tokens(region=region, refresh_expires_at=time.time() + p.REFRESH_TOKEN_TTL)
        tokens.apply(await self._session_call(
            tokens, p.OBTAIN_PATH, {"authorizationCode": code, "appId": p.HMS_APP_ID}))
        tokens.save(self.token_file)
        self._tokens = tokens
        return tokens

    async def rotate(self) -> None:
        tokens = self.tokens
        tokens.apply(await self._session_call(
            tokens, p.REFRESH_PATH, {"refreshToken": tokens.refresh_token, "appId": p.HMS_APP_ID}))
        tokens.save(self.token_file)

    # ── данные ───────────────────────────────────────────────────────────
    def _data_headers(self, t: Tokens) -> dict[str, str]:
        h = {"Authorization": f"Bearer {t.access_token}", "x-token": t.access_token or "",
             "x-token-type": str(p.TOKEN_TYPE), "x-version": p.APP_VERSION}
        if t.uid:
            h["x-huid"] = str(t.uid)
        return h

    def _data_body(self, t: Tokens, extra: dict) -> dict:
        body: dict[str, Any] = {"token": t.access_token or "", "tokenType": p.TOKEN_TYPE,
                                "appId": p.APP_ID, "ts": int(time.time() * 1000), "source": 1}
        if t.site_id is not None:
            body["siteId"] = t.site_id
        return {**body, **extra}

    async def post(self, path: str, extra: dict) -> dict:
        async with self._lock:  # ротация не должна идти параллельно
            t = self.tokens
            if t.needs_rotation():
                await self.rotate()
            url = t.hosts["data"] + path
            answer = await self._raw(url, self._data_body(t, extra), self._data_headers(t))
            code = p.code_int(p.result_code(answer))
            if code in p.AUTH_FAILED_CODES:
                await self.rotate()
                answer = await self._raw(url, self._data_body(t, extra), self._data_headers(t))
            elif code == p.BAD_HUID_CODE and t.uid:
                t.uid = None
                answer = await self._raw(url, self._data_body(t, extra), self._data_headers(t))
            return _ensure(answer, path)

    async def sports_stat(self, start: int, end: int) -> dict:
        return await self.post(p.READ_PATHS["sports_stat"], {"startTime": start, "endTime": end, "dataSource": 2})

    async def health_stat(self, start: int, end: int, types: list[int]) -> dict:
        return await self.post(p.READ_PATHS["health_stat"], {
            "startTime": start, "endTime": end, "types": types, "deviceCode": 0, "dataSource": 2})

    async def sports_detail(self, start: int, end: int, sport_types: tuple[int, ...]) -> dict:
        return await self.post(p.READ_PATHS["sports_detail"], {
            "startTime": start, "endTime": end, "queryType": 1, "dataType": 2, "sportTypes": list(sport_types)})

    async def motion_paths(self, version: int) -> list[dict]:
        return await self._by_version("motion_paths", version, {}, "workout records")

    async def health_points_by_version(self, type_id: int, version: int) -> list[dict]:
        return await self._by_version("health_points_by_version", version, {"type": type_id}, "health records")

    async def health_sequences_by_version(self, type_id: int, version: int) -> list[dict]:
        return await self._by_version("health_sequences_by_version", version, {"type": type_id, "subTypes": []}, "health sequences")

    async def training_load_daily(self, start_day: int, end_day: int) -> dict:
        return await self.post(p.READ_PATHS["training_load_daily"], {
            "startDay": start_day, "endDay": end_day, "type": 300010, "dataType": 2, "deviceCode": 0})

    async def _by_version(self, route: str, version: int, params: dict, data_name: str) -> list[dict]:
        """Read records and deletions in modification-cursor order."""
        records: dict[tuple, dict] = {}
        for _ in range(100):
            page = await self.post(p.READ_PATHS[route], {**params, "version": version, "dataType": 2, "deviceCode": 0})
            current = page.get("detailInfos")
            deleted = page.get("deleteInfos")
            if any(items is not None and not isinstance(items, list) for items in (current, deleted)):
                raise HuaweiError(f"Huawei {data_name} returned an invalid page")
            try:
                next_version = int(page["currentVersion"])
            except (KeyError, TypeError, ValueError, OverflowError):
                if not current and not deleted:
                    return list(records.values())
                raise HuaweiError(f"Huawei {data_name} returned an invalid cursor") from None
            if next_version <= version:
                if not current and not deleted:
                    return list(records.values())
                raise HuaweiError(f"Huawei {data_name} cursor did not advance")

            for row in current or []:
                if isinstance(row, dict):
                    records[p.cloud_record_key(row)] = row
            for tombstone in deleted or []:
                if not isinstance(tombstone, dict):
                    continue
                identifiers = {field: p.cloud_record_id(tombstone.get(field)) for field in ("recordId", "dataId")}
                identifiers = {field: value for field, value in identifiers.items() if value is not None}
                if identifiers:
                    records = {key: row for key, row in records.items() if not any(
                        p.cloud_record_id(row.get(field)) == value for field, value in identifiers.items())}
            version = next_version
        raise HuaweiError(f"Huawei {data_name} exceeded the pagination limit")

    async def health_data(self, start: int, end: int, type_id: int) -> dict:
        return await self.post(p.HEALTH_DATA_PATH, {
            "startTime": start, "endTime": end, "type": type_id, "queryType": 1, "dataType": 2})

    async def health_samples_by_time(self, type_id: int, start: int, end: int) -> list[dict]:
        answer = await self.post(p.HEALTH_DATA_PATH, {
            "startTime": start, "endTime": end, "type": type_id,
            "queryType": 2, "dataType": 2, "deviceCode": 0})
        rows = answer.get("detailInfos")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise HuaweiError("Huawei health samples returned an invalid response")
        return rows

    async def bind_devices(self) -> dict:
        return await self.post(p.READ_PATHS["bind_devices"], {})

    async def by_time(self, fetch: Callable[[int, int], Any], windows) -> list[dict]:
        """*ByTime/getHealthData по окнам ≤10 дней, повторы между окнами отбрасываются."""
        records, seen = [], set()
        for a, b in windows:
            answer = await fetch(a, b)
            for rows in (answer.get("data") or {}).values():
                for rec in rows or []:
                    if not isinstance(rec, dict):
                        continue
                    # у записей здоровья dataId всегда 0 — ключ только по dataId склеивал
                    # все измерения в одно; повторы между окнами отсекаем по полному ключу
                    key = (rec.get("dataId") or None, rec.get("recordId"), rec.get("startTime"), rec.get("type"))
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(rec)
        return records

    async def aclose(self) -> None:
        await self._http.aclose()
