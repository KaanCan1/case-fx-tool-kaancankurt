"""Currency conversion against the ECB reference rates published by frankfurter.dev.

The rule this module exists to enforce: a rate is only ever reported next to the
date the upstream says it belongs to. When the ECB published nothing for the day
that was asked for, the answer carries the earlier publication date it actually
came from, and the caller is told both dates. Nothing here ever falls back to a
different day's rate while claiming the asked-for one.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# The ECB publishes once per working day at around 16:00 CET, so "today" and
# "in the future" are decided in the ECB's own timezone rather than the server's.
ECB_TIMEZONE = ZoneInfo("Europe/Berlin")

# The euro reference rate series starts on the first trading day of 1999.
SERIES_START = date(1999, 1, 4)

DEFAULT_UPSTREAM = "https://api.frankfurter.dev"
SOURCE = "ECB via frankfurter.dev"

# A published rate for a past day never changes, so it is cached for good. Today
# and "latest" can still change when the ECB publishes, so they expire quickly.
TTL_SETTLED = None
TTL_UNSETTLED = 600.0
TTL_CURRENCIES = 86_400.0

TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=2.0)


class ConversionError(Exception):
    """A refusal with a machine code, a readable sentence and an HTTP status."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _reject(code: str, message: str, status: int = 400) -> ConversionError:
    return ConversionError(code, message, status)


class _Cache:
    """Tiny in-process cache. Keys carry the date, so two dates never share an entry."""

    def __init__(self) -> None:
        self._items: dict[Any, tuple[float | None, Any]] = {}

    def get(self, key: Any) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self._items[key]
            return None
        return value

    def put(self, key: Any, value: Any, ttl: float | None) -> None:
        self._items[key] = (None if ttl is None else time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._items.clear()


cache = _Cache()
_client: httpx.AsyncClient | None = None


def upstream_base() -> str:
    """Read on every call so the process can be pointed at a fake upstream."""
    return os.environ.get("FX_UPSTREAM_BASE", DEFAULT_UPSTREAM).rstrip("/")


def today() -> date:
    return datetime.now(ECB_TIMEZONE).date()


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get(path: str, params: dict[str, str] | None = None) -> Any:
    """One upstream GET. Every failure mode becomes upstream_unavailable."""
    url = f"{upstream_base()}/v1/{path}"
    try:
        response = await _http().get(url, params=params)
    except httpx.HTTPError as exc:
        raise _reject(
            "upstream_unavailable",
            f"The exchange rate service could not be reached ({type(exc).__name__}).",
            502,
        ) from exc

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise _reject(
            "upstream_unavailable",
            f"The exchange rate service answered with HTTP {response.status_code}.",
            502,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise _reject(
            "upstream_unavailable",
            "The exchange rate service returned a response that was not JSON.",
            502,
        ) from exc


async def supported_currencies() -> dict[str, str]:
    cached = cache.get("currencies")
    if cached is not None:
        return cached
    payload = await _get("currencies")
    if not isinstance(payload, dict) or not payload:
        raise _reject(
            "upstream_unavailable",
            "The exchange rate service did not return a usable currency list.",
            502,
        )
    cache.put("currencies", payload, TTL_CURRENCIES)
    return payload


@dataclass(frozen=True)
class Quote:
    rate: Decimal
    rate_date: date


async def fetch_quote(source: str, target: str, asked: date | None) -> Quote:
    """Fetch one rate. `rate_date` is always the date the upstream reports, never
    the date that was asked for."""
    path = "latest" if asked is None else asked.isoformat()
    key = (source, target, path)

    cached = cache.get(key)
    if cached is not None:
        return cached

    payload = await _get(path, {"base": source, "symbols": target})
    if payload is None:
        raise _reject(
            "no_rate_available",
            f"The ECB has published no {source}/{target} rate for {path}.",
            404,
        )

    raw_rate = (payload.get("rates") or {}).get(target)
    raw_date = payload.get("date")
    if raw_rate is None or raw_date is None:
        raise _reject(
            "no_rate_available",
            f"The ECB has published no {source}/{target} rate for {path}.",
            404,
        )

    try:
        rate = Decimal(str(raw_rate))
        rate_date = date.fromisoformat(str(raw_date))
    except (InvalidOperation, ValueError) as exc:
        raise _reject(
            "upstream_unavailable",
            "The exchange rate service returned a rate or date that could not be read.",
            502,
        ) from exc

    # The upstream may answer an unpublished day with an earlier publication —
    # that is expected and is reported as rate_date. Answering with a *later*
    # date is not, and we will not pass it on.
    if asked is not None and rate_date > asked:
        raise _reject(
            "upstream_unavailable",
            f"The exchange rate service answered {asked.isoformat()} with a rate "
            f"dated {rate_date.isoformat()}, which is later than the date asked for.",
            502,
        )

    quote = Quote(rate=rate, rate_date=rate_date)
    settled = asked is not None and asked < today()
    cache.put(key, quote, TTL_SETTLED if settled else TTL_UNSETTLED)
    return quote


def _validate_amount(amount: float) -> Decimal:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise _reject("invalid_amount", "amount must be a number.") from exc
    if not value.is_finite():
        raise _reject("invalid_amount", "amount must be a finite number.")
    if value <= 0:
        raise _reject("invalid_amount", "amount must be greater than zero.")
    return value


def _validate_currency(value: str, field: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise _reject(
            "invalid_currency",
            f"{field} must be a three-letter currency code, for example EUR.",
        )
    return code


def _validate_date(asked: date | None) -> date | None:
    if asked is None:
        return None
    if asked > today():
        raise _reject(
            "date_in_future",
            f"{asked.isoformat()} is in the future; no rate has been published for it.",
        )
    if asked < SERIES_START:
        raise _reject(
            "date_out_of_range",
            f"The ECB series starts on {SERIES_START.isoformat()}; "
            f"{asked.isoformat()} is before it.",
        )
    return asked


async def convert(
    amount: float, source: str, target: str, asked: date | None
) -> dict[str, Any]:
    """Convert `amount` from `source` to `target`, for `asked` or the latest rate."""
    quantity = _validate_amount(amount)
    source = _validate_currency(source, "from")
    target = _validate_currency(target, "to")
    if source == target:
        raise _reject(
            "same_currency",
            "from and to are the same currency; the ECB publishes no rate for a "
            "currency against itself.",
        )
    asked = _validate_date(asked)

    known = await supported_currencies()
    for code, field in ((source, "from"), (target, "to")):
        if code not in known:
            raise _reject(
                "unknown_currency",
                f"{field}={code} is not a currency the ECB publishes a rate for.",
            )

    quote = await fetch_quote(source, target, asked)
    result = (quantity * quote.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "amount": float(quantity),
        "from": source,
        "to": target,
        "rate": float(quote.rate),
        "result": float(result),
        "rate_date": quote.rate_date.isoformat(),
        "asked_date": (asked or today()).isoformat(),
        "source": SOURCE,
    }
