# Relay — independent 3X-UI reseller console

Self-hosted FastAPI/Jinja application for **MHSanaei/3x-ui v3.8.5**, using its central client API. A dedicated SQLite database owns reseller identity, permissions, sessions, operations and cumulative sales. The master panel owns VPN configuration and telemetry.

**Status:** implemented and tested locally with version-specific API fixtures. It has not been connected to your panel or installed on your VPS. Read [verification and remaining limits](docs/VERIFICATION.md) before production rollout.

- [English installation guide](docs/INSTALL.en.md)
- [راهنمای نصب فارسی](docs/INSTALL.fa.md)
- [Verified API contract](docs/API-3.8.5.md)
- [Architecture and recovery](docs/ARCHITECTURE.md)
- [Security and deployment limits](docs/VERIFICATION.md)

## Local development

Python 3.10+; tests were run with Python 3.14 on Windows. Linux production targets are documented in the installer.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

Copy `.env.example` to a protected local `.env`. For local HTTP only set `APP_ENV=development`, `APP_ORIGIN=http://127.0.0.1:8095`, and `APP_DB` to an absolute, dedicated `state/app.db`. Configure the real master URL/token to use the integration. There is no implicit demo adapter in production.

```powershell
.venv\Scripts\python -m reseller.manage bootstrap --env .env
.venv\Scripts\python -m reseller.manage serve --env .env
```

There are no default production credentials. Bootstrap prompts without echo; restarting never changes passwords. First login requires a password change.

## VPS installation

Upload this project (excluding `.reference`, `.venv`, test state and secrets) into a staging directory on the VPS. Then:

```bash
sudo bash ./install.sh --dry-run
sudo bash ./install.sh
```

For an explicit private SSH tunnel during evaluation:

```bash
sudo bash ./install.sh --tunnel-http --origin http://localhost:8095 --port 8095
```

The installer performs actual dependency, dedicated-user, database, bootstrap and systemd setup. It binds to loopback by default. It does not touch the existing panel service, database, certificates, firewall or inbound settings during installation.

An explicit compatibility mode can publish Relay like 3X-UI at `http://VPS_IP:PORT/web-path/`: `sudo bash ./install.sh --public-http --origin http://VPS_IP:8095/relay-secret/ --port 8095`. This binds to all interfaces and uses plaintext HTTP; restrict the port at the VPS firewall whenever possible. HTTPS remains the recommended public deployment.

## Project layout

`reseller/security.py`: Argon2id, session rotation, persistent rate limiting.
`reseller/domain.py`: ownership, grants, ledger, reservations, reconciliation.
`reseller/xui.py`: bounded, TLS-verified, server-only v3.8.5 API adapter.
`reseller/web.py`, `templates`, `static`, `i18n.py`: session-authenticated bilingual UI.
`reseller/migrations`: ordered database schema (application ID and version checked).
`deploy/ops.py`: installation, update, backup, restore, status, uninstall.
`tests`: accounting, authorization, integration and isolated maintenance tests.

The scripts have no published download location. Do not invent a remote installer URL. A release helper creates a portable source archive and SHA-256 for trusted transfer; no remote script is piped into a root shell.
