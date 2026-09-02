# Review of tool.py

The service answers every question it is asked. That is the problem: it returns
HTTP 200 for a rate that does not exist, a currency that does not exist, and an
upstream that is down. The first three findings are one habit seen from three
angles — the shape of the answer is fixed before it is known whether there is one.

Line numbers are `tool.py`. Every finding is reproduced by `review_repro.py`,
which runs with no network; the letters under **Verify** are its sections.

## 1. The rate's date is echoed back, not read (`tool.py:30`, `tool.py:44`)

The rate's date is reported as `str(on or date.today())` — the caller's own
question, handed back. The upstream reply carries a `date` field saying what the
rate actually is; it is never read. Ask for Sunday 2026-08-30 and the upstream
answers with Friday's rate and says `date: 2026-08-28`, but the service reports
`rate_date: 2026-08-30`. Ask for 2030-01-01 and the upstream 404s, the fallback at
lines 36-40 fetches `/latest`, and today's rate comes back stamped `rate_date: 2030-01-01`.

**Customer:** the number is real and the day is wrong, so nothing looks broken.
The model states the asked-for date with full confidence, because it was not told
otherwise. When the customer reconciles that figure against their own records,
neither they nor we can explain the gap — we never recorded what we used.

**Verify:** A, B. Serve `{"date": "2026-08-28", ...}` for a request asking about
2026-08-30 and assert `rate_date` is 2026-08-28. Against the real upstream: ask
for any Sunday and compare `rate_date` with the upstream's `date`.

## 2. Every failure is answered with a rate of zero, at HTTP 200 (`tool.py:71-81`)

The bare `except` swallows everything and returns a well-formed body with
`rate: 0.0`, `result: 0.0` and `source: "ECB via frankfurter.dev"`, attributing
the zero to the ECB. The only trace is a `print`. This is not an exotic path:
`EUR` to `EUR` reaches it, and the customer is told 250 EUR is 0 EUR. So does any
unknown code, any upstream 500, and any response that is not JSON.

**Customer:** zero is a number, not an error. A model reading `result: 0.0` with a
200 has nothing to react to and reports it as the answer. Refusing would have cost
one unanswered question; this costs a customer who was told something false.

**Verify:** C, D. Or just `curl '<host>/tools/convert?amount=250&from_=EUR&to=EUR'`
against the real upstream — no fake needed for this one.

## 3. The cache key has no date, and nothing expires (`tool.py:28`, `tool.py:43`)

The key is `f"{base}-{target}"`. The first answer for a pair is returned for every
later question about that pair, whatever date is asked, without consulting the
upstream. Line 43 also caches the rate fetched by the line-39 fallback, so one bad
date poisons the pair for the life of the process.

**Customer:** a historical query silently returns some other day's rate — and
finding 1 asserts the wrong date next to it. Because nothing expires, a process
started before the ECB's 16:00 CET publication quotes yesterday indefinitely: the
longer we stay up, the more wrong we get, which is the opposite of how staleness
is usually noticed.

**Verify:** E, F. Ask two different dates and assert the second call hits the
upstream; change the fake's rate between two `latest` calls and assert the answer
moves.

## 4. The rate is rounded before it is multiplied (`tool.py:60`)

`round(rate, 2)` is applied to the rate itself, so the error is multiplied by the
amount. This is wrong on every successful conversion — the common path — and worst
on major pairs, whose rates are small. At today's rates EUR/USD 1.1578 becomes
1.16 (0.19% off) and EUR/GBP 0.8587 becomes 0.86 (0.15%): on 100,000 EUR that is
220 USD or 130 GBP of invented money. The brief's own example expects 11780.85;
this returns 11780.00.

**Verify:** G. Assert `rate` equals the upstream's rate exactly and that `result`
is `amount * rate` from the unrounded value.

## Also, briefly

- **Neither `FX_UPSTREAM_BASE` nor `PORT` is read (`tool.py:18`).** Not ranked,
  because it is the one finding that harms us rather than a customer — in
  production the hardcoded host is the correct host. But it means the service
  cannot be pointed at a fake upstream, so none of the four findings above can be
  covered in CI. It is cheap, and it is what makes the rest testable.
- **The documented parameters do not exist (`tool.py:48`).** The brief specifies
  `from` and `date`; the code takes `from_` and `on`, so a caller written against
  the docs has both silently ignored and gets EUR to TRY, today. Whether that
  reaches a customer depends on who writes the caller — an agent whose schema is
  generated from this function uses the real names and never notices. A contract
  violation with latent harm, not an active defect. (H)
- **`amount` is unvalidated (`tool.py:48`).** Negatives convert happily; `1e400`
  overflows to infinity and serialises as `result: null`. (I)
- **Errors have no machine-readable shape.** Nothing distinguishes "no rate
  published" from "upstream down" — and by finding 2 neither is detectable anyway.

## The one I would fix before shipping tonight

**Finding 1.** Read the upstream's `date` and report it; stop answering at all when
there is no rate for the date asked.

Finding 2 is tempting — one `except` block, a smaller diff, and the more
embarrassing screenshot. I would still fix 1 first. A zero is absurd, and absurd
output has a chance of being caught: by the model, the customer, or us in a log. A
correct rate under the wrong date is plausible, and plausible wrong output is
caught by nobody. It is also the specific thing the brief says the endpoint must
never do, and it is the root of three of these four findings — fixing the date
handling closes the weekend case, the future-date case, and half of the cache one.

## Things that look suspicious but are fine

- **`httpx.AsyncClient()` with no timeout (`tool.py:23`).** I expected an unbounded
  client. It is not: httpx defaults to 5s on connect, read, write and pool, which I
  confirmed by constructing one. The narrower true point is that the line-39
  fallback adds a second sequential request, so the worst case is ~10s and neither
  number is stated anywhere. Make it explicit rather than call it a bug.
- **The client is built at import time, outside a lifespan hook (`tool.py:23`).**
  This looks like the classic wrong-event-loop mistake. It is not — httpx binds
  lazily and the app serves correctly. The real loose end is that it is never closed.
- **A missing `amount` returns 422 (`tool.py:48`).** Looked unhandled; FastAPI
  validates before the handler and rejects it clearly. The body is FastAPI's shape
  rather than the brief's, which belongs with the error-shape point above — but the
  behaviour is right, and it is the one input in this file that is refused.
