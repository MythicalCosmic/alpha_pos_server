# Alpha POS Server

Cloud back-office edition of Alpha POS. The repository pins the shared business
core as the `alpha_pos_core` Git submodule and adds server-only applications,
deployment configuration, and integration tests.

## Server applications

- `admins` — dashboards, analytics, users, shifts, treasury, Inkassa, exports,
  and admin APIs.
- `couriers` — courier identity, dispatch, location, settlement, and realtime
  rider APIs.
- `smartfood` — Telegram Mini App catalog, ordering, loyalty, support, and
  customer realtime APIs.
- `config` — server settings, URL routing, ASGI, and WSGI entry points.

Shared applications such as `base`, `core`, `cashbox`, `discounts`,
`fiscalization`, `hr`, `licensing`, `notifications`, `stock`, and sync are
provided by the pinned `alpha_pos_core` commit.

## Repository layout

```text
admins/                  Admin server application
couriers/                Courier server application
smartfood/               Customer ordering application
alpha_pos_core/          Pinned shared-core submodule
config/                  Django server configuration
deploy/                  Deployment and support-relay assets
postman/                 Generated manual API collection
tests/                   Repository and deployment tests
```

Each application keeps tests under its own `tests/` package, grouped by domain.
The server test run intentionally excludes the core submodule; run the core
suite from `alpha_pos_core` when changing shared code.

## Development

Clone with the submodule, create a virtual environment, and install the
development dependencies:

```bash
git clone --recurse-submodules <repository-url>
cd alpha_pos_server
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run server checks and tests:

```bash
python manage.py check
pytest
```

Production uses PostgreSQL and Redis through `docker-compose.yaml`. Runtime
secrets belong only in the ignored `.env`; no live credentials are stored in
the repository.

Validate and start the local Docker stack:

```bash
docker compose config --quiet
docker compose up --build
```

The ignored `.env` must be supplied out of band. Never copy SSH keys from the
workspace key directory into the application environment.

## Deployment

The container runs the ASGI application with Uvicorn workers and a Redis
channel layer. See [deploy/DEPLOY.md](deploy/DEPLOY.md) for first deployment,
continuous deployment, rollback, and support-relay setup.

Historical financial corrections require a reviewed, quiescent maintenance
window. Follow [the financial repair runbook](docs/operations/financial-repair.md)
without bypassing its evidence or synchronization gates.
