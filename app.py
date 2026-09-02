"""HTTP surface for the conversion tool.

One endpoint, and one error shape. Every refusal — ours or FastAPI's own request
validation — leaves as {"error": ..., "message": ...} with a non-2xx status, so a
caller never has to parse two different failure formats.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import fx

# Which error code a failed query parameter maps to.
_FIELD_ERRORS = {
    "amount": ("invalid_amount", "amount must be a number greater than zero."),
    "from": ("invalid_currency", "from must be a three-letter currency code, for example EUR."),
    "to": ("invalid_currency", "to must be a three-letter currency code, for example TRY."),
    "date": ("invalid_date", "date must be a calendar date in YYYY-MM-DD form."),
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await fx.close()


app = FastAPI(
    title="fx-tool",
    version="1.0",
    summary="Convert an amount between two currencies using ECB reference rates.",
    lifespan=lifespan,
)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


@app.exception_handler(fx.ConversionError)
async def _handle_refusal(_: Request, exc: fx.ConversionError) -> JSONResponse:
    return _error(exc.code, exc.message, exc.status)


@app.exception_handler(RequestValidationError)
async def _handle_bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    for problem in exc.errors():
        field = str(problem["loc"][-1])
        if field in _FIELD_ERRORS:
            code, message = _FIELD_ERRORS[field]
            if problem.get("type") == "missing":
                message = f"{field} is required."
            return _error(code, message, 400)
    return _error("invalid_request", "The request could not be understood.", 400)


@app.exception_handler(Exception)
async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # Nothing is answered with a number we are not sure of, so an unforeseen
    # failure is reported as a failure rather than absorbed into a result.
    return _error(
        "internal_error",
        "The conversion could not be completed because of an unexpected error.",
        500,
    )


@app.get("/tools/convert")
async def convert(
    amount: float = Query(description="How much to convert, greater than zero."),
    source: str = Query(alias="from", description="Currency to convert from, e.g. EUR."),
    target: str = Query(alias="to", description="Currency to convert to, e.g. TRY."),
    asked: date | None = Query(
        default=None,
        alias="date",
        description=(
            "ECB publication date to use, YYYY-MM-DD. Defaults to the most recent "
            "publication. If the ECB published nothing that day, the answer carries "
            "the earlier date it really came from in rate_date."
        ),
    ),
) -> dict[str, Any]:
    return await fx.convert(amount=amount, source=source, target=target, asked=asked)
