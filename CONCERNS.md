# Remaining deployment decisions and risks

The earlier application-level concerns (open proctor access, development admin
checkbox, missing CSRF/rate limits/audit, free-text class identity, and unprotected
SQLite queue races) are addressed by the current implementation. The remaining
items require deployment context or an organizer policy decision.

## Required before the first real contest

- Run an end-to-end smoke test against the deployed CMS and reverse proxy. Verify
  password login, IP restrictions, hidden/disabled contestants, expired cookies,
  refreshed `Set-Cookie`, multi-contest routing if used, the student WebSocket
  handshake, and logout cookie clearing. Verify explicitly that a request without
  a CMS login cookie never reaches the CMS identity endpoint.
- Validate CMS `num_proxies_used` against the real proxy chain. A wrong value can
  make CMS evaluate the toilet server or proxy address instead of the contestant
  computer's address.
- Run only one Uvicorn worker and supervise/restart it as one unit. Moving to
  multiple application processes needs a shared event bus and a cross-process
  coordinator, not only SQLite locking.
- Use HTTPS for all browser traffic, including toilet-local operator passwords.
  Rotate the dedicated toilet-to-Control HMAC key independently from the
  managed-computer key.
- Decide and test backup/restore, database retention, disk monitoring, clock
  synchronization, and an incident procedure for temporary CMS or Olimp-control
  outages.
- Load-test the venue's expected number of clients and WebSockets on the actual
  hardware/network. SQLite is intentionally retained for this single-site,
  single-writer design.

## Organizer policy still to decide

- No current computer, a computer without a class, a missing class-to-toilet
  mapping, or an assignment-service failure uses the all-toilets fallback and
  tells the student to ask a proctor. Malformed historical payloads containing
  several computers are handled by the same conservative fallback and staff
  alert. Decide whether staff need a separate escalation channel in addition to
  the in-app alert.
- Physical layouts and current student locations deliberately have no local
  mirror. During a Control outage existing queue actions remain available, but
  proctors cannot reload the physical layout until Control recovers.
- Active allocations survive ordinary class mapping changes because a student may
  already have left. Deleting the actual toilet is the exceptional operation that
  demotes them and raises an urgent alert. Staff training should make that behavior
  explicit.
- Manual return timestamps are audited with the operator identity. Organizers
  should decide who may use this feature and what time bounds, if any, should be
  enforced beyond parsing a valid timestamp.
- Define how long completed requests, alerts, audit rows, sessions, and rate-limit
  buckets must be retained. Audit rows are append-only by design; archival should
  be a deliberate offline operation on a copied/closed database.

## Possible later improvements

- Browser notifications/sound for a student's "go now" transition.
- External metrics and alerting for service health, queue length, blocked fallback
  requests, clock skew, disk space, and authentication-service availability.
- A server-side session revalidation policy for long-lived operator sessions if
  staff-role revocation must take effect faster than the configured session TTL.
- PostgreSQL plus a shared coordinator/event bus if the service ever needs multiple
  processes or sites.
