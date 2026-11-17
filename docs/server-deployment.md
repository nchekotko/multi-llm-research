# Server deployment

The deployed console uses `https://slavtrad.ru/signal-lab/` and a separate bearer token. Source is hosted on GitHub; credentials and research data are not.

The production service is a separate Docker container `signal-lab` on the existing reverse-proxy network. It has no host-published port. Nginx preserves `/signal-lab/`; `MULTILLM_ROOT_PATH=/signal-lab` identifies the external path. UI assets and API calls are relative to this prefix.

Runtime directory: `/root/services/signal-lab`. `app` is mounted read-only, `app/var` is writable. `server.env` is mode 0600. The image is pinned to an existing Python 3.12 image digest on this host, reusing its dependencies; deployment is separate from Economic Calendar rebuilds. Restart policy is `unless-stopped`; CPU, memory, PID and log limits are explicit.

Economic Calendar live volume is mounted read-only at `/economic/data`, its registry at `/economic/docs`. A small network-isolated `signal-lab-db-reader` helper holds SQLite `mode=ro`, `query_only` connections for live databases using WAL. Its volume permits SQLite to maintain coordination files (`-shm`/`-wal`); database content is never modified by its queries. The main application retains a read-only volume mount. No immutable shortcut is used against changing live data.

The server lacked `tradfi_1h.db`, so a consistent SQLite backup from the workstation is mounted separately at `/economic/tradfi_1h.db`. Operator config `tradfi_1h_path` selects that file; path containment checks still apply. The snapshot journal is converted to DELETE mode before mounting, so it requires no writable WAL coordination files. This hourly source is a dated snapshot and is not automatically updated by Economic Calendar collectors.

Existing AlgoPack datasets, studies, scanner results and research artifacts are copied; cached download pages are not transferred. `MULTILLM_MIN_FREE_BYTES=536870912` reserves 512 MiB before writing JSON artifacts. It is a guard, not a replacement for disk monitoring. Whole-market research may require more memory and disk than the current VPS can provide.

Operator checks: container health and restart count, HTTPS shell/assets, unauthenticated API 401, authenticated catalog/runs/overview, one bounded research run, existing Economic Calendar health. Never print provider keys or server.env into diagnostics.

Rollback routing: restore `/root/services/signal-lab/nginx.before.conf` to the existing Nginx configuration, run `docker exec ecbot-nginx nginx -t`, then reload. Stop only the `signal-lab` container if needed. Do not recreate the Economic Calendar containers or alter their data volumes.
