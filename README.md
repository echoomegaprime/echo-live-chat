# Echo Live Chat

Echo Live Chat is ECHO's tenant-safe, embeddable support-chat runtime. This
repository preserves the original Worker contract while moving execution and
state to the private FORGE cluster.

FORGE's rescue pipeline had already staged all eleven D1 application exports
as nullable text tables. Ten are empty and `widgets` contains one recovered
row. The schema gate preserves those tables under `legacy_*_text_v1`, revokes
runtime access to them, imports the widget into the typed runtime table, and
creates a labeled structural parent because the rescued row has no tenant id.

## Runtime

- FastAPI on loopback port `8465`
- Public browser boundary at `https://live-chat.echo-op.com`
- PostgreSQL schema `cf_echo_live_chat`
- Dedicated `echo-live-chat` operating-system and database roles
- Immutable releases under `/opt/echo-live-chat/releases`
- A read-only self-bind of `/opt/echo-live-chat/current`, matching the canonical migration auditor contract
- Five-minute, single-flight maintenance through a systemd timer
- Secrets loaded from root-owned systemd credentials; Stripe API and webhook signing values are isolated and none are stored here

The strict compatibility contract contains 37 unique non-generic method/path
pairs. Operational `/health`, `/status`, and `/diagnostics` routes are attached
dynamically so the canonical migration auditor can attest the exact Worker
surface independently of the health plane.

## Security boundaries

Administrative endpoints are a platform-operator surface: one dedicated,
root-managed bearer intentionally has cross-tenant authority, and every call
must select an existing tenant with `X-Tenant-ID`. That credential is never
issued to tenants or browsers. Browser visitors use `/v/init` to receive an
opaque, short-lived session scoped to exactly one tenant, widget, visitor,
conversation, and origin. Every subsequent visitor read or write validates
that scope and repeats the ownership predicate in PostgreSQL.

Widget CORS is allowlist-based. Stripe webhooks verify the raw request body,
timestamped signature, and event id before any mutation. AI calls use fixed
private endpoints, bounded timeouts, and graceful degradation. Logs contain
request shape and timing only—never chat text, visitor identity, headers,
prompts, or upstream response bodies.

## Local verification

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
python -m py_compile app.py live_chat_core.py smoke_live.py
```

The local suite covers exact route parity, credential fail-closed behavior,
origin boundaries, webhook signatures, credential separation, source
provenance, redaction, and deployment contracts. The staging and production
smoke then exercise all 37 routes against real PostgreSQL, including
cross-origin visitor-session rejection, cross-tenant object isolation, CORS,
oversized-body rejection, update-field validation, and semantic delete/rating
checks.

## Deployment

Deployment is staging-first and atomic:

```bash
python3 register_public_route.py
sudo ./deploy_echo_live_chat.sh
sudo ./prove_rollback.sh
```

The gate checks both immutable source hashes, compiles and tests the release,
applies the additive schema, boots the candidate on `8466`, runs the live smoke,
then atomically promotes it. Production acceptance runs through loopback and
the public tunnel hostname. A red production smoke restores the prior symlink,
unit files, and verified health. `prove_rollback.sh` deliberately exercises
that recovery path without leaving the failed candidate active.

After deployment, run the canonical FORGE migration auditor and require the
`echo-live-chat` row to be `migrated`, `healthy`, and `37/37/37` with coverage
`1.0`:

```bash
sudo python3 /home/forge/cf-migration-audit/audit_rollup.py \
  --release-root /opt/echo-live-chat
```

Do not infer completion from this README or from a local test result.

## Provenance and state

`migration_contract.json` records the canonical inventory digest and the
strict recovered-bundle digest as distinct artifacts. The immutable rescue
index records ten empty application exports and one non-empty widget export.
The preserved FORGE rescue table supplied that widget row; the typed migration
imports it with a labeled structural tenant and generates only a replacement
public key. It does not fabricate customer content.
