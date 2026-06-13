# alpha_pos_server

The **cloud back-office** edition (Docker). Consumes `alpha_pos_core` as a submodule +
editable install, and adds only its own server-side apps.

## Owns

- `admins` — analytics, dashboards, role/user admin, audit, exports, treasury/inkassa
  consolidation. **Order WRITE endpoints are not mounted here** (the server doesn't take
  orders); only read/analytics views.
- `hr` — departments, payroll, contracts, leave, performance, expenses, cash ledger.
  (HR *tables* also exist on local for the AUTO_POS attendance row, but the UI is here.)

## From `core` (shared)

`base` + sync engine, `stock`, `discounts`, `cashbox`, `fiscalization`, `licensing`,
`notifications` — installed as apps so their **tables** exist and sync, even where the
UI is local-only.

## Edition specifics

- **ASGI:** gunicorn-WSGI → **uvicorn workers** (`channels` + `channels-redis`).
- **Channel layer:** **Redis** (`channels_redis`) — multiple workers share groups.
- **DB:** Postgres (existing `docker-compose.yaml` Postgres service).
- Websocket consumers: `SyncIngestConsumer` (cloud side of WS sync), `DashboardConsumer`,
  `AlertsConsumer`, `CashierControlConsumer` (server side of lock/remove).

## Status

`admins`, `hr` + deploy chain copied. Next: `config/settings.py` (extends
`core.alpha_pos_core.settings_base`, `EDITION=server`), `config/asgi.py`
(`ProtocolTypeRouter`), wire core as a submodule, `manage.py check`.
