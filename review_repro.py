"""Part B evidence — reproduces every finding in REVIEW.md against tool.py.

Runs with no network at all: the upstream is faked with respx. The fake mirrors
the real frankfurter.dev behaviour, which was measured first with curl:

    GET /v1/latest?base=EUR&symbols=EUR   -> 404 {"message": "bad currency pair"}
    GET /v1/latest?base=EUR&symbols=XXX   -> 404 {"message": "not found"}
    GET /v1/2030-01-01?base=EUR&symbols=TRY -> 404 {"message": "not found"}
    GET /v1/1990-01-01?base=EUR&symbols=TRY -> 404 {"message": "not found"}
    GET /v1/2026-08-30?base=EUR&symbols=TRY -> 200 {"date": "2026-08-28", ...}
                                                     ^ a Sunday was asked for;
                                                       the upstream answers with
                                                       the Friday it actually
                                                       published, and says so.

Each finding is printed and then asserted, so this file is not a narration: if a
finding stops reproducing, the script fails. That is the intended signal.

    ./test.sh          # installs .venv from requirements.txt
    .venv/bin/python review_repro.py
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import respx
from fastapi.testclient import TestClient

import tool

TODAY = str(date.today())
FRIDAY_RATE = 56.1718      # what the upstream published on 2026-08-28
LATEST_RATE = 47.1234      # what the upstream publishes "now"


class FakeUpstream:
    """Stands in for api.frankfurter.dev. `latest_rate` is mutable so that cache
    staleness can be demonstrated without any timing games."""

    def __init__(self) -> None:
        self.latest_rate = LATEST_RATE
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path.rsplit("/", 1)[-1]
        base = request.url.params.get("base")
        symbol = request.url.params.get("symbols")

        if symbol == base:
            return httpx.Response(404, json={"message": "bad currency pair"})
        if symbol not in {"TRY", "JPY", "USD"}:
            return httpx.Response(404, json={"message": "not found"})
        if path == "latest":
            return self._ok(base, symbol, "2026-09-01", self.latest_rate)
        if path > "2026-12-31" or path < "1999-01-04":
            return httpx.Response(404, json={"message": "not found"})
        # A weekend or holiday: the upstream backfills to the last publication
        # and reports that date. tool.py never reads it.
        return self._ok(base, symbol, "2026-08-28", FRIDAY_RATE)

    @staticmethod
    def _ok(base: str, symbol: str, on: str, rate: float) -> httpx.Response:
        return httpx.Response(
            200, json={"amount": 1.0, "base": base, "date": on, "rates": {symbol: rate}}
        )


def heading(letter: str, title: str) -> None:
    print(f"\n{letter}. {title}\n{'-' * (len(title) + 3)}")


def call(client: TestClient, **params) -> tuple[int, dict]:
    tool._cache.clear()
    response = client.get("/tools/convert", params=params)
    return response.status_code, response.json()


def main() -> None:
    upstream = FakeUpstream()
    with respx.mock:
        respx.route(host="api.frankfurter.dev").mock(side_effect=upstream)
        client = TestClient(tool.app)

        # ------------------------------------------------------------------
        heading("A", "A rate for a date that has no rate, stamped with that date")
        status, body = call(client, amount=250, from_="EUR", to="TRY", on="2030-01-01")
        print(f"   asked for  : 2030-01-01 (no rate exists, and none ever will)")
        print(f"   answered   : HTTP {status}  rate_date={body['rate_date']!r}  rate={body['rate']}")
        print(f"   the rate is: today's, fetched from /latest after the 404")
        print(f"   -> the customer is told a 2030 rate. There is no error to notice.")
        assert status == 200
        assert body["rate_date"] == "2030-01-01"
        assert body["rate"] != 0.0

        # ------------------------------------------------------------------
        heading("B", "Friday's rate presented as Sunday's")
        status, body = call(client, amount=250, from_="EUR", to="TRY", on="2026-08-30")
        print(f"   upstream said : date=2026-08-28  rate={FRIDAY_RATE}")
        print(f"   service says  : rate_date={body['rate_date']!r}  rate={body['rate']}")
        print(f"   -> same number, wrong day. The model cannot tell the customer")
        print(f"      which day the rate is from, because it is not told either.")
        assert body["rate_date"] == "2026-08-30"

        # ------------------------------------------------------------------
        heading("C", "An unknown currency answers zero, with HTTP 200")
        status, body = call(client, amount=250, from_="EUR", to="XXX")
        print(f"   HTTP {status}  {json.dumps(body)}")
        print(f"   -> 'ECB via frankfurter.dev' attributes the zero to the ECB.")
        assert status == 200 and body["rate"] == 0.0 and body["result"] == 0.0

        # ------------------------------------------------------------------
        heading("D", "Converting a currency to itself answers zero")
        status, body = call(client, amount=250, from_="EUR", to="EUR")
        print(f"   HTTP {status}  result={body['result']}")
        print(f"   -> 250 EUR is reported as 0 EUR. Nothing about this request is")
        print(f"      exotic; it is the first thing anyone types by accident.")
        assert status == 200 and body["result"] == 0.0

        # ------------------------------------------------------------------
        heading("E", "The cache key has no date in it")
        tool._cache.clear()
        first = client.get("/tools/convert", params={"amount": 1, "from_": "EUR", "to": "TRY", "on": "2026-08-28"}).json()
        before = len(upstream.requests)
        second = client.get("/tools/convert", params={"amount": 1, "from_": "EUR", "to": "TRY", "on": "2026-09-01"}).json()
        print(f"   call 1 (2026-08-28): rate={first['rate']}  rate_date={first['rate_date']!r}")
        print(f"   call 2 (2026-09-01): rate={second['rate']}  rate_date={second['rate_date']!r}")
        print(f"   upstream requests during call 2: {len(upstream.requests) - before}")
        print(f"   -> one rate, two dates. The second date was never looked up.")
        assert first["rate"] == second["rate"]
        assert first["rate_date"] != second["rate_date"]
        assert len(upstream.requests) == before

        # ------------------------------------------------------------------
        heading("F", "The cache never expires, so 'latest' freezes for good")
        tool._cache.clear()
        stale = client.get("/tools/convert", params={"amount": 1, "from_": "EUR", "to": "TRY"}).json()
        upstream.latest_rate = 99.9999          # the ECB publishes a new rate
        fresh = client.get("/tools/convert", params={"amount": 1, "from_": "EUR", "to": "TRY"}).json()
        print(f"   before the ECB publishes: rate={stale['rate']}")
        print(f"   upstream now publishes  : 99.9999")
        print(f"   service still answers   : rate={fresh['rate']}")
        print(f"   -> every request for the rest of the process lifetime is stale.")
        assert stale["rate"] == fresh["rate"]
        upstream.latest_rate = LATEST_RATE

        # ------------------------------------------------------------------
        heading("G", "The rate is rounded to 2 decimals before it is multiplied")
        status, body = call(client, amount=250, from_="EUR", to="TRY")
        correct = round(250 * LATEST_RATE, 2)
        print(f"   upstream rate : {LATEST_RATE}")
        print(f"   service rate  : {body['rate']}")
        print(f"   correct result: {correct}      service result: {body['result']}")
        print(f"   error         : {correct - body['result']:.2f} TRY on 250 EUR ({abs(correct - body['result']) / correct * 100:.3f}%)")
        print(f"   -> this is the README's own worked example; it expects {correct}.")
        assert body["rate"] == round(LATEST_RATE, 2)
        assert body["result"] != correct

        # ------------------------------------------------------------------
        heading("H", "The documented query parameters are silently ignored")
        upstream.requests.clear()
        tool._cache.clear()
        response = client.get(
            "/tools/convert",
            params={"amount": 100, "from": "USD", "to": "JPY", "date": "2020-01-01"},
        )
        body = response.json()
        print(f"   request  : ?amount=100&from=USD&to=JPY&date=2020-01-01  (the README's shape)")
        print(f"   upstream : {upstream.requests[0]}")
        print(f"   answer   : HTTP {response.status_code}  from={body['from']!r}  rate_date={body['rate_date']!r}")
        print(f"   -> 'from' and 'date' bind to nothing, so the defaults win. The")
        print(f"      customer asked about USD in 2020 and was answered about EUR today.")
        assert response.status_code == 200
        assert body["from"] == "EUR"
        assert body["rate_date"] == TODAY
        assert "base=EUR" in upstream.requests[0]

        # ------------------------------------------------------------------
        heading("I", "amount is never validated")
        for amount in ("-250", "0", "1e400"):
            status, body = call(client, amount=amount, from_="EUR", to="TRY")
            print(f"   amount={amount:<8} HTTP {status}  result={body['result']}")
        print(f"   -> a negative amount converts happily; 1e400 overflows to")
        print(f"      infinity and is serialised as JSON null.")

        # ------------------------------------------------------------------
        heading("!", "Checked and NOT a defect: the client does have a timeout")
        timeout = httpx.AsyncClient().timeout
        print(f"   httpx {httpx.__version__}: AsyncClient() -> {timeout}")
        print(f"   -> a bare AsyncClient() is not unbounded; httpx defaults to 5s.")
        print(f"      The real point is narrower: the fallback path issues two")
        print(f"      sequential requests, so the worst case is 10s, and nothing")
        print(f"      in tool.py states either number.")
        assert timeout.read == 5.0

    print("\nAll findings reproduced.")


if __name__ == "__main__":
    main()
