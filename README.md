# Metabase Health Check

**Know when your Metabase changes.** A small, Docker-ready background service for Coolify that watches users, API-key metadata, the installed Metabase version, and new stable Metabase releases. Changes arrive by email.

No dashboard to maintain. No inbound ports. Just environment variables and a persistent volume.

```text
Metabase API ─────────────┐
  or application Postgres├── Poll → Compare → SQLite outbox → SMTP → Your team
GitHub stable releases ──┘
```

## What it watches

| Check | Alerts on | Source |
| --- | --- | --- |
| Users | Creation, removal, email/name changes, activation and administrator status | Metabase API or application PostgreSQL |
| API keys | Creation, removal, name/group/creator/timestamp changes exposed by the source | Metabase API or application PostgreSQL |
| Installed version | A change in the instance's version tag | Metabase session properties API |
| Stable releases | A change in GitHub's latest stable Metabase release | GitHub Releases API |

The first successful check of each source establishes a **quiet baseline**. Existing users and keys do not generate a flood of alerts. Subsequent changes are stored transactionally with the new snapshot, then emailed separately to each recipient. Failed recipients stay queued across restarts.

The installed version is compared numerically with Metabase’s published latest stable release on its official GitHub repository. OSS (`v0`) and Enterprise (`v1`) prefixes are normalized; prerelease and unfamiliar tags report an unknown comparison. Changes in the comparison generate alerts. The initial comparison appears in the startup status email. This compares release numbers, not edition-specific feature availability or supported upgrade paths. Login activity, passwords, arbitrary user attributes, and user group membership changes are not monitored.

## Deploy on Coolify

1. Push this repository to your GitHub organization repository. Give Coolify's GitHub App access to it.
2. In Coolify, add an application from that repository and select the **Dockerfile** build pack. Use `/Dockerfile` at the repository root.
3. Add the environment variables from [`.env.example`](.env.example). At minimum, fill in `METABASE_URL`, `METABASE_API_KEY`, `SMTP_HOST`, `SMTP_FROM`, and `NOTIFY_EMAILS`, plus your SMTP credentials.
4. Add persistent storage mounted at **`/data`**. The container runs as UID **10001**; bind-mounted directories must be writable by that user.
5. Deploy. No domain or exposed port is needed. This is a worker; use its Docker health check rather than an HTTP route check.
6. Check logs for successful `users`, `api_keys`, `version`, and `releases` checks. Make a reversible user-name change to verify delivery after the baseline has been created.

Keep one replica per state volume. Back up `/data` to preserve snapshots and pending mail. Losing the volume starts a new baseline. Changing instance URL or entity source requires a new state file; the service rejects accidental state reuse.

## Run with Docker Compose

```sh
cp .env.example .env
# Edit .env with your instance and SMTP settings.
docker compose up -d --build
docker compose logs -f monitor
```

To rebuild after pulling changes:

```sh
docker compose up -d --build
```

Compose reads `.env`; Coolify's Dockerfile deployment uses its environment settings directly. Do not commit your `.env`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `METABASE_URL` | required | Canonical `https` instance URL, including any subpath; no `/api` suffix |
| `METABASE_API_KEY` | required in API mode | Key assigned to the Administrators group for complete listings |
| `ENTITY_SOURCE` | `api` | `api` or `database` for user and API-key checks |
| `METABASE_DATABASE_URL` | empty | PostgreSQL application-database URI, used only in database mode |
| `POLL_INTERVAL_SECONDS` | `300` | Delay between completed polling cycles; minimum 30 |
| `CHECK_RELEASES` | `true` | Set `false` to disable GitHub release checks |
| `STATUS_INTERVAL_SECONDS` | `604800` | Status email on first run and every seven days; `0` disables |
| `STATE_PATH` | `/data/state.sqlite3` | Durable SQLite snapshots and outgoing notifications |
| `SMTP_HOST` | required | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port; defaults to 465 when security is `ssl` |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none` for an unauthenticated local relay |
| `SMTP_USERNAME` | empty | Optional SMTP login |
| `SMTP_PASSWORD` | empty | SMTP password |
| `SMTP_FROM` | required | Plain sender email address |
| `NOTIFY_EMAILS` | required | Comma-separated plain email addresses; duplicates removed |
| `ALLOW_INSECURE_URL` | `false` | Permit an `http` `METABASE_URL`, sending the API key in cleartext |
| `ALLOW_INSECURE_SMTP` | `false` | Permit `SMTP_USERNAME` with `SMTP_SECURITY=none`, sending the password in cleartext |
| `ALLOW_INSECURE_DB` | `false` | Permit a `METABASE_DATABASE_URL` without `sslmode=require` or stronger |

Example recipient list: `admin@example.com,ops@example.com`. This is a CSV environment value, not an uploaded file. Recipients receive individual messages and cannot see the other recipients. Queued messages retain the recipient list that was configured when the change was observed.

The three `ALLOW_INSECURE_*` flags each unlock one cleartext transport and default to off, so a misconfiguration fails at startup rather than leaking a credential. Turn one on only for a path you control end to end, such as a loopback SMTP relay.

## API mode and compatibility

Create a dedicated Metabase API key assigned to the **Administrators** group. A restricted key may fail checks or return incomplete visibility. The monitor only makes GET requests, but the key itself inherits its group's privileges.

The monitor uses:

- `GET /api/user/?status=all` with pagination, including deactivated users.
- `GET /api/api-key/` for key metadata.
- `GET /api/session/properties` for the installed version.
- `GET https://api.github.com/repos/metabase/metabase/releases/latest` for stable releases, without forwarding your Metabase credentials.

Metabase's API is unversioned. Verify these endpoints against **your instance's `/api/docs`** before relying on the monitor. Unexpected response shapes and failed checks preserve the previous baseline and are logged. Other checks continue. API-key rotation is detectable only if the returned metadata changes, such as `updated_at`; this service does not read or compare secret keys.

References: [Metabase API keys](https://www.metabase.com/docs/latest/people-and-groups/api-keys), [working with the API](https://www.metabase.com/learn/metabase-basics/administration/administration-and-operation/metabase-api), and [API changelog](https://www.metabase.com/docs/latest/developers-guide/api-changelog).

## Application database mode

If the API does not expose the needed metadata, configure an explicit database source:

```dotenv
ENTITY_SOURCE=database
METABASE_DATABASE_URL=postgresql://monitor:URL_ENCODED_PASSWORD@postgres:5432/metabase?sslmode=verify-full&sslrootcert=/data/ca.crt
```

The URL must set `sslmode` to `require`, `verify-ca`, or `verify-full`, otherwise startup fails. Prefer `verify-full` with an `sslrootcert`: `require` encrypts but validates no certificate, so it does not stop a machine-in-the-middle. libpq's own default, `prefer`, silently falls back to plaintext. To connect over a local socket or an already-encrypted tunnel, set `ALLOW_INSECURE_DB=true`.

This must be the **Metabase application database**, which stores accounts and settings, not a warehouse connected to Metabase for analytics. This implementation supports **PostgreSQL only**, not MySQL or H2. Version checks still use the instance URL; the API key is optional in this mode if session properties are publicly available.

Use a dedicated database account with `CONNECT`, schema `USAGE`, and only the necessary column-level `SELECT` grants. Queries run inside read-only transactions with a 15-second statement timeout. Required tables and columns:

```sql
GRANT CONNECT ON DATABASE metabase TO monitor;
GRANT USAGE ON SCHEMA public TO monitor;
GRANT SELECT (id, email, first_name, last_name, is_active, is_superuser, date_joined)
  ON public.core_user TO monitor;
GRANT SELECT (id, name, group_id, creator_id, created_at, updated_at)
  ON public.api_key TO monitor;
```

Create the role/password separately through your normal database administration process. These grants assume the `public` schema and the listed Metabase schema layout. Internal tables can change between versions; missing columns cause a failed check rather than a partial snapshot. Set the PostgreSQL role's search path appropriately if your tables are elsewhere.

Database mode is a deliberate switch, not an automatic fallback: alternating sources could otherwise create misleading change alerts. Password hashes, API-key hashes, salts, and session tokens are never selected. Key regeneration that leaves all selected metadata unchanged cannot be detected.

## Reliability and operational limits

- Polling observes differences between snapshots. A create-and-delete or change-and-revert between polls can be missed. This is **not a complete audit log**.
- SMTP delivery is at least once. A crash after SMTP accepts a message but before its acknowledgement is saved can cause a duplicate. A prolonged SMTP outage grows the durable outbox, so monitor disk usage.
- Failed checks and mail delivery appear in logs. The Docker health check becomes unhealthy after three polling intervals without a completely successful cycle (minimum 180 seconds). There are no immediate outage emails. The startup status email tests SMTP delivery.
- TLS certificates are verified. HTTP redirects are rejected to prevent credential forwarding; configure the final canonical Metabase URL. Responses over 1 MiB are rejected rather than parsed; pages hold at most 100 rows, so real responses stay far below that.
- Snapshots and mail contain account metadata, including email addresses. The state file is created mode `600`, and SQLite gives its journal the same mode. Restrict access to the volume and backups. Logs omit response bodies and raw exception messages to avoid credential leakage.
- Deactivating or rotating the monitoring key can stop API checks. GitHub outages/rate limiting can make the overall health check unhealthy; disable release checks for isolated deployments.

## Development

The API-mode service and tests use Python's standard library. PostgreSQL mode additionally needs the pinned driver in `requirements.txt`; the Docker image installs it.

```sh
python3 -m unittest discover -s tests -v
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Export settings into your shell; Python does not automatically load .env.
export STATE_PATH=./data/state.sqlite3
python monitor.py --once
```

`--once` establishes a baseline or processes one cycle and exits nonzero if any check or delivery fails. It can send real notification emails. `--healthcheck` only checks the local heartbeat.

## Weekly status email

Enabled by default, a status report is queued after the first polling cycle and every seven days thereafter, even when nothing changed. Set `STATUS_INTERVAL_SECONDS=604800` (or `0` to disable). The persisted schedule survives restarts; an overdue report runs on the next poll, without sending a backlog of missed weekly reports.

Reports contain the observation time, each check’s success/failure, current user and API-key counts when available, installed and published versions, upgrade comparison, and the number of messages queued before the report. Failed reads are shown as unavailable rather than reusing stale values. Status mail uses the same durable recipient-by-recipient retry queue as change alerts; a delayed email describes its original observation time.

A received report confirms the monitor ran and SMTP delivered it. A stopped container cannot email its own outage; use external monitoring if you need an alert when a report is missing.

Version numbering reference: [Metabase release versioning](https://www.metabase.com/docs/latest/developers-guide/versioning).

GitHub Actions runs the tests and builds the Docker image. Deployment against a live Metabase, PostgreSQL, and SMTP server is still required to confirm compatibility with your installation.
