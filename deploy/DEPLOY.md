# Server deploy — Alpha POS cloud + Control Center (IP-only, auto-HTTPS)

Two Docker Compose stacks on one Linux server, behind a Caddy reverse proxy that
gets **real Let's Encrypt HTTPS** with no domain via **nip.io**:

| Service | URL |
|---|---|
| Alpha POS cloud backend | `https://pos.<IP>.nip.io` |
| POS Control Center | `https://control.<IP>.nip.io` |

`<IP>` = your server's public IP, e.g. `203.0.113.10` → `pos.203.0.113.10.nip.io`.
nip.io resolves that name to the IP automatically — nothing to register.

## Prerequisites
- Linux server with Docker + Docker Compose v2.
- Ports **80 and 443 open** to the internet (Caddy needs 80/443 for the cert
  challenge + serving). The app ports (8000) are **not** exposed publicly.
- A GitHub login that can read both repos.

## Steps (run on the SERVER)

```bash
# 1. Get both repos side by side
#    (alpha_pos deploy bundle + prelaunch fixes live on the prelaunch-fixes branch)
cd ~
git clone -b prelaunch-fixes https://github.com/MythicalCosmic/alpha_pos.git
git clone https://github.com/MythicalCosmic/pos_control.git

# 2. Deploy (replace with your real public IP)
cd ~/alpha_pos/deploy
chmod +x deploy.sh
./deploy.sh 203.0.113.10
```

`deploy.sh` generates `.env` files (with fresh secrets), the Caddyfile, and the
compose overrides, then builds and starts **alpha_pos**, **pos_control**, and
**caddy**. It prints the one-time finishing commands (license the cloud, create
admins) — run those, then verify:

```bash
curl -fsS https://pos.<IP>.nip.io/healthz      # -> ok
curl -fsSI https://control.<IP>.nip.io/ | head -1
```

> First HTTPS hit can take ~30s while Caddy obtains certificates. If it fails,
> check `docker compose logs caddy` in `~/alpha_pos/deploy/caddy` — the usual
> cause is port 80/443 not reachable from the internet.

## Point a desktop POS at these servers

In the desktop control panel → **Configuration**:
- `LICENSE_CONTROL_CENTER_URL = https://control.<IP>.nip.io`
- `CLOUD_SYNC_URL = https://pos.<IP>.nip.io`
- `CLOUD_SYNC_TOKEN = ` (the token deploy.sh printed)
- `SYNC_ENABLED = True`

Then **License & Subscription → Register** (after you create a tenant in the
control center), and run a sale to test sync.

## Updating after a code change
```bash
cd ~/alpha_pos   && git pull && docker compose -f docker-compose.yaml -f docker-compose.edge.yml up -d --build
cd ~/pos_control && git pull && docker compose -f docker-compose.yaml -f docker-compose.edge.yml up -d --build
```

## Useful
```bash
# logs
cd ~/alpha_pos && docker compose -f docker-compose.yaml -f docker-compose.edge.yml logs -f web
# restart everything
cd ~/alpha_pos/deploy && ./deploy.sh <IP>
```

Secrets live only in the generated `.env` files (gitignored). Re-running
`deploy.sh` preserves existing secrets so the database keeps working.

## Continuous deployment (auto-redeploy on push)

Instead of `git pull && up --build` by hand, let CI build the image and let the
server pull it automatically.

**How it works**

1. Push to `main` (or a `v*` tag) → `.github/workflows/deploy.yml` builds the
   Docker image and pushes it to GHCR as
   `ghcr.io/<owner>/<repo>:latest` (+ `:sha-<commit>`, + `:vX.Y` for tags).
2. On the server, **Watchtower** (in `deploy/docker-compose.cd.yml`) polls GHCR
   every 5 min, pulls a changed `:latest`, and recreates the `web` container.
3. The container entrypoint runs `migrate` on every start, so schema changes
   apply themselves.

**One-time server setup**

```bash
# a) Authenticate Docker to GHCR so Watchtower can pull the image.
#    Use a GitHub PAT (classic) with read:packages, or skip this if you make
#    the GHCR package Public (Repo → Packages → package → Settings → Visibility).
echo <PAT_WITH_read:packages> | docker login ghcr.io -u <github-user> --password-stdin

# b) Pin the image in the alpha_pos .env (owner/repo lowercased):
echo 'WEB_IMAGE=ghcr.io/mythicalcosmic/alpha_pos:latest' >> ~/alpha_pos/.env

# c) Bring the stack up with the CD overlay (NOTE: no --build — pull the image):
cd ~/alpha_pos && docker compose \
  -f docker-compose.yaml -f docker-compose.edge.yml -f deploy/docker-compose.cd.yml up -d
```

From then on, a merge to `main` redeploys within the poll interval — nothing to
run on the server.

**Force an immediate update** (don't wait for the poll):
```bash
cd ~/alpha_pos && docker compose \
  -f docker-compose.yaml -f docker-compose.edge.yml -f deploy/docker-compose.cd.yml \
  pull web && docker compose \
  -f docker-compose.yaml -f docker-compose.edge.yml -f deploy/docker-compose.cd.yml up -d web
```

**Roll back** to a known-good build:
```bash
# set the pinned SHA tag, then up -d
sed -i 's#^WEB_IMAGE=.*#WEB_IMAGE=ghcr.io/mythicalcosmic/alpha_pos:sha-<commit>#' ~/alpha_pos/.env
cd ~/alpha_pos && docker compose \
  -f docker-compose.yaml -f docker-compose.edge.yml -f deploy/docker-compose.cd.yml up -d web
```

Watchtower only updates containers labelled
`com.centurylinklabs.watchtower.enable=true` (just `web`); db, redis and caddy
are left untouched.

## Self-redeploy without CI (no GitHub Actions / no registry)

If GitHub Actions isn't available (billing lock, private-repo minutes, etc.),
skip the image pipeline entirely: a small systemd timer on the server polls git
and rebuilds when the deploy branch moves. You `git push`, the server redeploys
itself — no Actions, no GHCR, no Watchtower.

**Files:** `deploy/auto_redeploy.sh` + `deploy/systemd/alpha-pos-autodeploy.{service,timer}`.

**Install (once, on the server):**
```bash
cd ~/alpha_pos && git checkout main && git pull
chmod +x deploy/auto_redeploy.sh

# Edit the unit if you don't deploy as root from /root/alpha_pos:
#   User=, Environment=ALPHA_DIR=, ExecStart= and DEPLOY_BRANCH=
sudo cp deploy/systemd/alpha-pos-autodeploy.service /etc/systemd/system/
sudo cp deploy/systemd/alpha-pos-autodeploy.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpha-pos-autodeploy.timer

# Test the poll immediately:
sudo systemctl start alpha-pos-autodeploy.service
journalctl -u alpha-pos-autodeploy.service -n 30 --no-pager
```

From then on: **push to `main` → within ~3 min the server fast-forwards and runs
`docker compose … up -d --build`; migrations run on container start.** Check the
timer with `systemctl list-timers | grep autodeploy`.

Safe by design: fast-forward only (won't clobber server-side commits — it bails
loudly if the branch diverged), a flock guard prevents overlapping deploys, and
the gitignored `.env` / `docker-compose.edge.yml` are left untouched. To pause
auto-deploy: `sudo systemctl disable --now alpha-pos-autodeploy.timer`.

This and the CI+Watchtower path (above) are alternatives — run one or the other,
not both.
