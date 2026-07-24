# LMIO toilet service

The toilet service coordinates toilet and support requests during a contest.
CMS only validates contestant browser sessions. Olimp-control owns the contestant
roster, computer assignments, class catalog, and physical layouts. The toilet
service owns its operator accounts, class-to-toilet mappings, queues, locks,
alerts, sessions, rate limits, and append-only audit trail.

CMS contestants and toilet operators use completely independent authentication
paths. A CMS-authenticated userid must also exist in the identifier-only
contestant roster synchronized from Olimp-control; CMS authentication never
creates a local contestant.

## What is implemented

- CMS-authoritative student authentication using existing password-login
  cookies, including IP-restriction validation, cookie refresh relay,
  multi-contest cookie probing, and student WebSocket authentication. Without a
  CMS login cookie, Toilet returns unauthenticated and does not probe CMS.
- Toilet-local bcrypt-backed administrator/proctor accounts with independently
  managed roles and immutable session scope snapshots.
- Student-only roster synchronization from Olimp-control. Current computer,
  location, and physical-layout data is fetched live and is never mirrored in
  the toilet database.
- Live lookup of the contestant's single current computer and its class for every
  toilet request. Olimp-control owns and enforces the one-to-one mapping.
- Atomic toilet locks. Missing or incomplete mappings conservatively lock every
  configured toilet and create a staff alert; malformed historical
  multi-computer snapshots are handled defensively the same way.
- FIFO scheduling with capacity, overlap fairness, pending-request cancellation,
  proctor-only active return handling, and live scoped WebSocket updates.
- Serialized SQLite writes using `BEGIN IMMEDIATE`, foreign keys, WAL, a busy
  timeout, and database constraints.
- Safe runtime toilet changes and class-to-toilet reassignment. Stable IDs keep
  renames harmless; pending queues are reconciled; active allocations are
  preserved unless their toilet is deleted.
- Fixed-window CMS-probe, assignment-lookup, student, session-issuance, and
  operator-login rate limits; CSRF protection; expiring sessions; role/scope
  enforcement; and SQLite-trigger-enforced append-only audit rows.
- Scoped and all-class proctor pages with clickable live physical layouts,
  request dialogs, alerts, and returns, plus a separate admin page for toilets,
  operators, class-to-toilet mappings, and audit controls.
- English-default contestant localization with Lithuanian basics. Locale resolution
  uses an explicit `lang`/`language` query parameter, then the CMS `language`
  cookie, then the persisted toilet choice.
- A configurable optional-request list that is disabled by default. Toilet
  requests are the only always-available flow; an unset or empty
  `TOILET_GENERAL_REQUEST_TYPES` hides the assistance form and the proctor
  assistance view entirely and rejects every non-toilet request type
  server-side. `paper` ("Additional paper") is the one supported opt-in value.
- Staff-initiated toilet requests. Selecting a computer in the class layout opens
  a dialog whose first action queues that computer's contestant, using the same
  live Control assignment lookup, conservative routing, and audit trail as a
  contestant request. The acting proctor is the audited actor and may only queue
  contestants inside their own class scope.

## Staff-only operation

`TOILET_STUDENT_UI_ENABLED` is `false` by default, so the toilet root serves a
short bilingual notice instead of the contestant app and proctors queue toilet
breaks on a contestant's behalf. Leave CMS `toilet_url` empty while it is off so
contestants are not offered a link to a disabled page.

The contestant REST and WebSocket handlers stay in place behind that flag, and
the contestant pages, translations, and support-request form remain in the code
and unit tests. Re-enabling the contestant surface is
`TOILET_STUDENT_UI_ENABLED=true` plus a CMS `toilet_url`, and should follow
verifying CMS student login and every contestant translation for the countries
taking part.

The detailed behavioral contract is in
[`IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md). The CMS authentication
boundary and proxy requirements are in [`toilet-auth.md`](../toilet-auth.md).

## Local development

From the workspace root, create a virtual environment and install the pinned
dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r toilet2\dev-requirements.txt
```

Run the repository's local CMS integration stack to develop against real CMS
cookies and real Olimp-control roster, assignment, and layout data:

```powershell
.\cms-test.ps1 rebuild
.\cms-test.ps1 smoke
```

The toilet is at <http://localhost:8080/toilet/>. A separate loopback-only
development UI at <http://localhost:8082/> has two service-local account forms:
a full Olimp-control administrator and a toilet operator. CMS contestants are
loaded independently from the checked-in CSV through
`cmsImportContestantsCSV`; the CMS AdminWebServer remains linked for ordinary
administration but exposes no companion write API. The fixture materializes
student sessions through real CMS login. Classes, computers, toilets, and
mappings remain in their native interfaces. The toilet process itself never
seeds a default identity or password. The helper's trust boundary, gated
bootstrap, and removal instructions are in
[`DEVELOPMENT_UI.md`](../DEVELOPMENT_UI.md).

For a manually configured environment, set every required value from
`toilet2/.env.sample` and run one factory-created worker:

```powershell
.\.venv\Scripts\python.exe -m uvicorn toilet2.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

Operators use `/operator/login`; those
credentials and permissions exist only in the toilet database. Alternate
same-origin clients can read authenticated identity, role/scope, locale, and the
CSRF token from `GET /api/session`.

There is deliberately no default operator password. Provision the first local
administrator explicitly; the CLI prompts twice and never accepts a password on
the command line:

```powershell
.\.venv\Scripts\python.exe -m toilet2.manage_operator toilet-admin `
  --database-url sqlite:///C:/absolute/path/to/toilet2.db `
  --display-name "Toilet administrator" --admin
```

For gated automation, `--password-env NAME` reads an explicitly named environment
variable. Use `--proctor` with either repeated `--class-scope UUID` or
`--all-classes`. After bootstrap, authenticated administrators manage accounts
through the admin UI/API. `--keep-password` updates an existing account without
rotating its password.

Configuration is read from environment variables; `.env` files are not loaded by
the application itself. [`toilet2/.env.sample`](.env.sample) documents every
setting. Use an absolute `TOILET_DATABASE_URL` in a service deployment.

## Production services

### 1. Olimp-control

Set a random `CTRL_TOILET_AUTH_KEY` of at least 32 UTF-8 bytes, distinct from
the managed-computer `CTRL_AUTH_KEY`, run the Django migrations, and expose the
application over HTTPS or a private authenticated network:

```powershell
python manage.py migrate
```

In the Olimp-control UI:

1. Import the contestant CSV independently from the CMS import.
2. Assign each computer to a class/location and place it in the physical grid.
3. Create the strict one-to-one contestant-computer mappings. Remove an
   existing mapping before assigning either side to a different mapping.
4. Keep Control staff accounts for Control administration only. Toilet
   administrator/proctor accounts are created in the toilet service.

The signed service contract is documented in
[`olimp-control-srv/TOILET_API.md`](../olimp-control-srv/TOILET_API.md).

### 2. CMS

Deploy the included CMS patch. It adds the read-only authenticated endpoint
`/api/toilet-auth` (or `/<contest>/api/toilet-auth` in multi-contest mode). The
endpoint returns the CMS-authenticated username and contest and deliberately uses
the ordinary ContestWebServer authentication machinery.

### 3. Toilet service

Start with [`toilet2/.env.sample`](.env.sample), then at minimum set:

```text
TOILET_DATABASE_URL=sqlite:////absolute/path/to/toilet2.db
TOILET_ROOT_PATH=/toilet
TOILET_PUBLIC_ORIGIN=https://contest.example.org
TOILET_COOKIE_SECURE=true
TOILET_CMS_BASE_URL=http://127.0.0.1:8888
TOILET_CMS_CONTESTS=contest-name
TOILET_CONTROL_BASE_URL=https://control.internal.example.org
TOILET_CONTROL_AUTH_KEY=<same dedicated key as Olimp-control>
```

For multiple simultaneously served CMS contests, list all contest names in
`TOILET_CMS_CONTESTS` and set `TOILET_CMS_MULTI_CONTEST=true`.

Always run exactly one Uvicorn worker using `toilet2.main:create_app --factory`.
SQLite serializes across connections too, but queue notifications and the
in-process mutation coordinator are intentionally single-process. Startup
initializes/migrates the schema, purges expired sessions, and synchronizes the
student roster once. Administrators can repeat that student sync explicitly.
Class names/order, computer placement, and assigned students are read from
Olimp-control on each relevant HTTP/WebSocket request; only the class UUID to
toilet mapping is local. During upgrade, an unmatched synthetic legacy class UUID
is remapped only when its UUID exactly matches the migration's deterministic
UUID5 formula and its saved name has one exact unique match in the live class
catalog. Ambiguous, missing, non-migration, and conflicting mappings are left
untouched and reported visibly in the admin page and audit log.

### Docker Compose deployment

The production Docker setup runs one unprivileged Toilet2 container and stores
the SQLite database, WAL, and SHM files together in the persistent
`lmio-toilet-data` Docker volume. From the standalone `toilet2` repository root:

```bash
cp .env.ec2.sample .env
chmod 600 .env
sudoedit .env

sudo docker compose config
sudo docker compose build
sudo docker compose run --rm --no-deps toilet2 \
  python -m toilet2.manage_operator toilet-admin \
  --display-name "Toilet administrator" --admin
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs -f toilet2
```

The operator command prompts twice for a password and initializes the same
database volume used by the service. There is no separate Toilet2 migration
command: the provisioning CLI and application startup initialize or upgrade the
SQLite schema. Startup also attempts a roster synchronization, but a Control
failure is logged and does not fail the process health check. Confirm the sync
explicitly from the Toilet administrator page.

The EC2 sample keeps `TOILET_CONTROL_BASE_URL=https://ctrl.lmio.lt` so TLS
verifies the deployed certificate, while Compose resolves that name to
`172.31.47.173` inside the container. Prefer Route 53 private DNS over the
included static mapping when available. The Control proxy and Django
`ALLOWED_HOSTS` must accept `ctrl.lmio.lt`. Set
`TOILET_FORWARDED_ALLOW_IPS` to the immediate trusted CMS proxy address or the
narrowest applicable network.

The container publishes port 8000 because the CMS proxy is on another instance.
The Toilet EC2 security group must allow that port only from the CMS proxy
security group. Do not expose it publicly. Continue to serve the browser-facing
application only at `/toilet/` on the CMS HTTPS origin.

The named volume survives image rebuilds, container replacement, and ordinary
`docker compose down`. Never run `docker compose down -v` unless intentionally
deleting all Toilet2 state. Keep Docker data on persistent encrypted EBS and use
a SQLite-aware online backup while the service is running; do not copy only the
live `toilet.db` file because committed data may still be in its WAL.

To update the service after taking a backup:

```bash
git pull
sudo docker compose up --build -d
sudo docker compose logs --tail=100 toilet2
```

The direct process-level health probe is:

```bash
curl -fsS http://127.0.0.1:8000/operator/login >/dev/null
```

Run exactly one Compose service instance and never scale the `toilet2` service;
the mutation coordinator and WebSocket fanout are intentionally single-process.

## Reverse proxy requirements

Mount the app on the same browser origin as CWS so CMS cookies are sent to it. The
proxy must:

- strip `/toilet` before forwarding while the app has
  `TOILET_ROOT_PATH=/toilet`;
- proxy WebSocket upgrades for `/toilet/ws/`;
- preserve the browser Host/scheme used by `TOILET_PUBLIC_ORIGIN`;
- pass the actual immediate client address to the app; and
- allow the app to contact CWS.

The toilet service sends that client address to CWS as `X-Forwarded-For`. Increase
CMS `num_proxies_used` for the additional toilet-service-to-CWS hop and verify this
with the real proxy chain before a contest. Do not expose the Olimp-control HMAC
endpoint over plaintext on an untrusted network: signatures provide integrity,
not confidentiality.

## Operations and data rules

- Use the admin page to create/rename/delete toilets, manage local operators,
  map live classes to toilets, reload the live class list, synchronize students,
  and inspect audit history. Class creation/rename/deletion and physical layout
  changes stay in Olimp-control.
- New local operator usernames use the URL-safe
  `[A-Za-z0-9_][A-Za-z0-9_.@+-]*` form. A legacy account with another form is
  marked in the admin page and remains deletable there so it can be recreated
  with a safe username.
- Scoped proctors see and act only on requests intersecting their assigned classes.
  Explicit all-class proctors use `/proctor-all` and receive every class in REST
  and staff-WebSocket state.
- A capacity decrease below active use is rejected with HTTP 409.
- A proctor queues a toilet break by selecting the contestant's computer in the
  class layout. A contestant who is already queued cannot be queued twice, and a
  proctor cannot queue a contestant whose live class falls outside their scope.
- Students can cancel pending requests only. An active visit can be released only
  by an audited proctor return, because the student may already have left.
- Deleting an in-use toilet atomically demotes affected requests, clears invalid
  locks/mappings, creates an urgent alert and audit event, rebuilds conservative
  pending locks, and reschedules.
- Class display metadata and layouts are live Olimp-control data. Public class
  UUIDs, local toilet row IDs, stored request snapshots, and audit history are not
  rewritten by a Control rename.
- Back up the SQLite database using a SQLite-aware online-backup method while the
  service is running. Do not edit it directly during a contest.
- Rate-limit buckets cap repeated rejection counts and audit the first rejection in
  each actor/window instead of creating an unbounded audit row per repeated 429.
- Student/admin/proctor state reads, polling, and WebSocket heartbeats are read-only
  and do not append audit rows. A CMS companion session is audited once when issued,
  not on every state refresh.
- The admin audit page defaults to configuration changes and incidents. Request
  lifecycle and session/system detail remain available in separate views, while the
  underlying database keeps the complete append-only history.
- Repeated student-roster synchronization failures are coalesced until recovery,
  which is recorded once. Live class/layout failures do not mutate local mappings.

## Tests

The toilet suite creates isolated temporary SQLite databases:

```powershell
python -m pytest toilet2\tests -q
```

The Olimp-control suite uses Django's isolated test database:

```powershell
Set-Location olimp-control-srv
python manage.py test ctrl
python manage.py check
python manage.py makemigrations ctrl --check --dry-run
```

Local tests mock the exact CMS endpoint and cookie behavior. The root Docker
environment additionally runs real CMS/nginx end-to-end authentication smoke
tests and verifies the separate API-backed development UI; see
[`CMS_TESTING.md`](../CMS_TESTING.md) and
[`DEVELOPMENT_UI.md`](../DEVELOPMENT_UI.md). A full production proxy chain and
CMS grading stack remain deployment gates.
