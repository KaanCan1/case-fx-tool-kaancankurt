# fx-tool

One HTTP endpoint an AI agent can call as a tool, converting an amount between
two currencies using the ECB reference rates published by
[frankfurter.dev](https://frankfurter.dev).

It is built around one rule: **a rate is only ever reported next to the date the
upstream says it belongs to.** When the answer cannot be given honestly, the
service refuses instead of guessing — a wrong number is worse than no number.

## Run

```sh
./run.sh                       # listens on 8080
PORT=9000 ./run.sh             # or wherever
```

`run.sh` creates `.venv` and installs `requirements.txt` on first use.
The upstream comes from `FX_UPSTREAM_BASE` (default `https://api.frankfurter.dev`)
and is read on every request, so the process can be pointed at a fake without a
restart. The `/v1` path segment is appended by this service, not taken from the
variable.

## Test

```sh
./test.sh
```

29 tests, no network. Every upstream call is intercepted inside the process, so
`FX_UPSTREAM_BASE` may point at a closed port — or nowhere at all. Verified by
running the suite with every non-loopback socket and DNS lookup blocked.

## The endpoint

```
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

`date` is optional; without it the most recent publication is used.

```json
{
  "amount": 250.0,
  "from": "EUR",
  "to": "TRY",
  "rate": 56.1718,
  "result": 14042.95,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

`rate_date` is the day the rate the service used was actually published — taken
from the upstream's own `date` field, never assumed. `asked_date` is what the
caller asked for. **When they differ, the rate is from an earlier day than the
one requested**, and the agent can say so to the customer.

Every failure returns a non-2xx status and only this:

```json
{ "error": "same_currency", "message": "from and to are the same currency; ..." }
```

No refusal ever carries a `rate` or a `result` field, so there is no number for a
caller to mistake for an answer.

## What it does in each case

| Case | What happens | Code | Status |
|---|---|---|---|
| The ECB published no rate that day (weekend, holiday) | Answers with the last published rate, `rate_date` set to the day it really came from and `asked_date` to the day requested | — | 200 |
| No rate exists for that day or any earlier one | Refuses; never substitutes another day's rate | `no_rate_available` | 404 |
| `date` is in the future | Refused before any upstream call | `date_in_future` | 400 |
| `date` is before 1999-01-04, when the series starts | Refused before any upstream call | `date_out_of_range` | 400 |
| `date` is not a calendar date | Refused | `invalid_date` | 400 |
| Currency code is not three letters | Refused | `invalid_currency` | 400 |
| Currency code is well-formed but the ECB does not publish it | Refused, checked against the upstream's own currency list | `unknown_currency` | 400 |
| `from` and `to` are the same | Refused — there is no ECB publication to date the answer to | `same_currency` | 400 |
| Upstream is slow, refuses the connection, returns 5xx, or returns something that is not JSON | Refused | `upstream_unavailable` | 502 |
| Upstream returns a rate dated *later* than the day asked for | Refused; the answer is not passed on | `upstream_unavailable` | 502 |
| `amount` missing, zero, negative, or not finite | Refused | `invalid_amount` | 400 |
| `amount` has ten decimal places | Accepted; the result is rounded to 2 decimals, the rate never is | — | 200 |
| A query parameter is rejected in a way none of the rows above covers | Refused | `invalid_request` | 400 |
| Anything unforeseen | Refused, not absorbed into a result | `internal_error` | 500 |

## Notes

- **Rounding.** The rate is passed through at full precision and only the final
  result is rounded, half-up, to 2 decimals, using `Decimal`. Rounding the rate
  first would put a systematic error into every conversion.
- **Caching.** Keyed by `(from, to, date)`, so two dates can never share an entry.
  A past day's rate is settled and cached indefinitely; today and `latest` expire
  after 10 minutes, because the ECB publishes around 16:00 CET and an answer given
  before that is not final. Repeating a question does not re-ask the upstream.
- **Dates.** "Today" and "in the future" are decided in `Europe/Berlin`, the
  timezone the ECB publishes in, not the server's.
- **Future dates are rejected locally.** The upstream answers dates up to roughly
  two weeks ahead with today's rate, and 404s beyond that; the cutoff is
  undocumented, so this service does not rely on it.
- **Timeouts** are explicit: 2s connect, 4s read.

## Also in this repository

`tool.py` is the AI-written version of the same service that came with the case;
[`REVIEW.md`](REVIEW.md) is the review of it, and `review_repro.py` reproduces
every finding in that review against a faked upstream (`python review_repro.py`).
