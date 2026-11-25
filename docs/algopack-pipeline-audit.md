# AlgoPack pipeline audit — 2026-11-25

## Scope and reproduction

Audited the local checkout's path from HTTP response through table validation,
snapshot files and the persisted job queue to the JSON consumed by the Data page.
The checkout already contained unrelated funding changes; these were not modified.

All new scenarios use synthetic responses through `httpx.MockTransport`, temporary
storage, the real downloader and queue, and FastAPI's TestClient. They require no
MOEX credentials or market-data license. No live MOEX response, production service,
or browser rendering was verified. The tests establish client behavior under these
inputs, not that MOEX actually returns malformed responses.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_algopack_pipeline.py tests/test_algopack.py tests/test_console.py -q
```

## Confirmed and fixed

| Trigger | Previous behavior | Fix |
| --- | --- | --- |
| HTTP 200 with `data: null`, `{}`, `""`, `false` or `0` | Falsy values ended pagination; the job completed and produced an empty snapshot manifest. | Require an array before testing for the empty-page terminator. |
| Duplicate column names, non-string column names, or non-array rows | `dict(zip(...))` could overwrite values or interpret strings as rows. | Validate unique nonempty string column names and array rows with matching lengths. |
| Whole-market April 1 request returns April 2 rows, followed by an empty April 2 response | Rows passed the global April 1–3 range check and the snapshot completed. | Check each row against the particular requested day as well as the overall range. |

The initial regression run had 10 failures and one passing happy-path test.
After the fix and added edge cases, all 33 focused tests pass (14 new scenarios).
The full local suite passes: 225 passed, 1 skipped, with 3 dependency deprecation
warnings. Ruff passes for the changed Python files, and `git diff --check` passes.
The happy path checks a 503 retry, pagination using actual row counts, persisted
content and metadata, console/API visibility, snapshot reuse without HTTP calls,
and rejection of modified product content by `read_dataset`. A valid empty array
still produces a successful zero-row snapshot; that is not proof of market coverage.

## Dependency attribution

The reproduced defects belong to `application/algopack.py`. HTTPX correctly returns
the JSON values; the application decides how to interpret the provider's table.
Pydantic validates the download request, not this provider response. FastAPI exposes
the saved job and manifest as instructed. There is no demonstrated upstream defect
or justified upstream issue from these scenarios.

Environment: Python tests with HTTPX 0.28.1, FastAPI 0.163.0, Starlette 1.10.0,
Pydantic 2.14.3 and pytest 9.2.0. TestClient emits a dependency deprecation warning;
that warning is not evidence of a library bug.

## Economic Calendar reuse check

Searched the local `Economic-Calendar-telegram-bot` repository for AlgoPack,
TradeStats and related clients. Inspected `src/services/moex_algo_service.py` and
`src/services/sentiment_core/blocks/moex_algopack.py`: the inspected code handles
heatmaps, FUTOI/HI2 and sentiment with `requests`. It is not a drop-in replacement
for this historical FO pagination and immutable-snapshot contract. Reused the
existing Signal Lab downloader; no collector, data source or database was added.
Economic Calendar services were not contacted or changed.

## Remaining usability and integrity boundaries

- Download diagnostics now use allowlisted error codes and actionable Russian text
  (see the follow-up below). Other job types retain their existing diagnostics.
- Raw pages are cached before semantic table validation. Retrying a rejected page
  reuses that page; there is no selective refresh workflow yet.
- Console and dataset-list endpoints read manifests without rechecking product
  hashes. The visible saved-snapshot badge is not a current integrity check;
  `read_dataset` performs that check when data is consumed. This audit did not
  change the UI or add expensive full-file checks to each overview request.
- Existing completed snapshots are not retroactively revalidated by this patch.

These are application follow-ups, not upstream bug claims. A useful open-source
contribution starts with this reproducible regression patch and its documented
scope; external contributions should follow a separately reproduced dependency issue.


## Follow-up: actionable download errors

Download jobs now persist a stable `error_code` and a fixed Russian `error` message.
The existing Tasks page already renders `error` as escaped text; no frontend change
is required. Messages distinguish missing configuration/credentials, rejected access
(401/403), rate limits (429), provider outages, timeouts/network failures, malformed
responses, wrong scope, duplicate rows, pagination limits, damaged cache/snapshots,
locked downloads, disk space and filesystem permissions. Unknown errors use a safe
fallback asking the user to provide the job ID.

Messages never interpolate exception text, response bodies, URLs, tokens or paths.
Cached-response failures explicitly avoid promising that an ordinary retry will fix
corrupt or invalid data. Saved records from earlier failed jobs are not rewritten.
The UI wording changes apply to newly executed jobs.

The Economic Calendar implementations inspected above expose provider-specific
logging/optional results, not this persisted console-job error contract. The change
extends the existing Signal Lab downloader and queue; no external service is changed.
Tests exercise HTTP and transport failures through the queue and console API,
configuration errors, cache/snapshot corruption and storage errors, including
secret-bearing exception and response text. Browser rendering and live MOEX access
remain outside this verification.
