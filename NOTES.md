# Notes

## Decisions

**When the ECB published no rate for the day asked**, the endpoint answers with the
earlier rate and says which day it is from: `rate_date` is the date the upstream
reports, `asked_date` is the date the caller asked for. When they differ the agent
can say "this is Friday's rate" instead of implying it is Sunday's. I chose
answering over refusing because the upstream already hands us the real date, so
honesty costs nothing here, and refusing would leave a customer with no number when
a usable one exists.

The invariant is narrower than "handle weekends": a rate is only ever printed next
to the date the upstream says it belongs to, and no path substitutes another day's.
When no rate exists for the day asked or any day before it, the answer is
`no_rate_available` — never a fall back to `latest`.

**Future dates are refused before the upstream is called.** I measured what
Frankfurter does with them and it is inconsistent: tomorrow through roughly two
weeks out returns today's rate, and beyond that it 404s. The cutoff is not
documented, so I do not depend on it. "Today" is decided in `Europe/Berlin`, the
ECB's own timezone, not the server's.

**`from` equal to `to` is refused.** Returning `1.0` is arithmetically fine, but
there is no ECB publication behind it, so `rate_date` would have no honest value.

**Unknown currencies are checked against the upstream's own currency list**, so
`unknown_currency` stays distinguishable from "no rate that day".

**The rate is never rounded, only the result is** — half-up, two decimals, via
`Decimal`. Rounding the rate first puts a systematic error into every conversion;
on EUR/USD it is about 0.19%.

**The cache is keyed by `(from, to, date)`.** A past day's rate is settled and kept
indefinitely; today and `latest` expire after ten minutes, because the ECB publishes
around 16:00 CET and an answer given before that is not final.

## With another day

- Keep money in integer minor units end to end. `Decimal` becomes `float` at the
  JSON boundary, which lets float representation back in at the last moment.
- Move the cache out of the process. It is an unbounded dict, so a caller
  enumerating dates and pairs would grow it without limit — but an in-process LRU
  would cap the memory and not the real problem, which is that every worker keeps
  its own copy and every restart discards it.
- A scheduled contract test against the real Frankfurter, so a change in its
  behaviour — like that future-date window — is caught by us and not by a customer.
- Log the upstream's `date` with every answer, so a disputed conversion can be
  reconstructed later.

## AI tools

Claude Code, for the whole task, as a pair rather than a generator. It wrote most of
the code; I set the direction, made the decisions above and checked the work. What I
insisted on was verifying every review finding against the actual line rather than
accepting a plausible-sounding list.

Measuring the real upstream with `curl` before designing anything was its initiative,
not mine. I took the habit on: it is what turned the future-date window from a guess
into a measurement, and why the fake upstream in the tests behaves like the real one.
`review_repro.py` came out of the same habit — it reproduces each review finding and
asserts it, so a finding that stops being true fails loudly.

## One thing the AI got wrong

In its first pass over `tool.py` it reported "no timeout — if the upstream hangs, the
request hangs forever". It sounded right and I nearly took it.

I asked it to verify each finding on the line instead of from memory. It installed
`httpx` and constructed the client: `httpx.AsyncClient()` defaults to
`Timeout(timeout=5.0)`. Not unbounded at all. The finding left the ranked list for
"things that look suspicious but are fine", keeping the narrower point that does
survive: the fallback path makes two sequential requests, so the worst case is about
ten seconds, and no number is stated anywhere in the file.

That is why `review_repro.py` asserts instead of narrates. A confident sentence about
code is not evidence about code.
