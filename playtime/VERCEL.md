# One-minute sampling on Vercel

This adapter runs the existing API counter on Vercel, not on GitHub Actions.
It does not launch VRChat, enable the calendar or change Unity. By the owner's
request, this deployment starts a **new empty counter** and never imports old hours.
The retired world's Redis state and GitHub checkpoint archive were subsequently
removed at the owner's explicit request. Active hours are preserved.

The target is the published world `wrld_bf7ec577-6b45-4aa2-839d-376b8a366c21`,
as requested by the owner, across all instances visible to the bot. World IDs scope
separate encrypted checkpoints; changing this setting never merges another world's
hours or edits a Unity scene's Blueprint ID.
The owner requested removal of the previous counter. Operator-only action
`forget-previous-world` removes only that retired world's Redis state/previous
snapshot under its own lease, refuses the active world, and never deletes Unity
assets, the VRChat world, credentials, or the external scheduler task.
Deploying code alone does **not** enable the schedule. Production remains disabled
unless explicitly configured, initialized and verified. Do not enable paid services
or enter passwords/TOTP seeds into source, chat, scheduler URLs or logs.

## Runtime

- `POST /api/playtime`: one sample, authenticated by `Authorization: Bearer <CRON_SECRET>`.
- External scheduler: **every minute** (`* * * * *`, all minutes/hours/days).
  For cron-job.org use POST, that Authorization header, no request body, and no
  `X-Playtime-Action` header. Disable automatic job disabling on repeated failures;
  enable failure notifications. A paused login must remain visibly unhealthy.
- `GET /playtime.json` (also `/api/playtime`): public sanitized leaderboard only.
  Reading the report never logs in or polls VRChat. Public responses cache for 60s.
- `POST /api/playtime`, `X-Playtime-Action: resume`: one operator-requested login,
  requiring the **different** `PLAYTIME_ADMIN_SECRET` bearer token. Never give that
  token/header to the scheduler. It still honors Retry-After and the minute gate.
- `POST /api/playtime`, `X-Playtime-Action: initialize`: operator-only creation of
  an empty counter on the deployed production host. Uses the admin bearer token,
  works while polling is disabled, never calls VRChat and refuses existing state.
  This avoids downloading sensitive production variables for local initialization.
- Operator actions `schedule` / `schedule-status` configure and inspect only this
  bot's cron-job.org task. They require the admin token and `CRONJOB_API_KEY` in
  production. The API key stays in Vercel; the scheduler receives only `CRON_SECRET`.
  Enabling requires two recent successful polls, reuses an existing managed task,
  and refuses duplicates or an unrelated task. The job is POST once per minute;
  no response bodies or administrator credentials are saved by the scheduler.

An encrypted Upstash Redis checkpoint retains the authenticated session across
invocations. A 330-second ownership-checked lease prevents overlapping workers;
Vercel hard timeout is 300s. A minute marker is persisted before external effects.
Repeated triggers in the same UTC minute are skipped. A slow invocation may skip
the next minute: no overlapping requests or invented attendance to compensate.
Sampling normally has an 18-second API budget, leaving room for state persistence
within cron-job.org's 30-second HTTP timeout. Network stalls/manual authentication
can still exceed that timeout; monitor last successful sample, not only HTTP triggers.

The scheduler **does not password-login every minute**. Normal polls reuse cookies.
The owner enabled `auto_recover_session` in `playtime/config.json`: only a known
401 missing-credentials/unauthorized response during session verification, with
exactly one auth cookie actually sent and no Basic login header, marks recovery
eligible. It runs on a later minute and never bootstraps a new/unknown session.

Automatic recovery is limited to one attempt per six hours, counting manual login
timestamps too. The attempt and a hard-stop latch are saved before credentials are
sent. Failure, timeout/crash, rejected new session, email challenge, 403 or 429
prevents another automatic login until a successful operator resume. Server
Retry-After always wins; no IP switching or repeated attempts to bypass rejection.
Operator `recovery-status` returns eligibility, the safe attempt counter, method,
timestamp and cooldown, never credentials. Set `auto_recover_session` to false to
disable the feature. Vercel Hobby outbound IPs remain dynamic; this mechanism does
not guarantee uninterrupted attendance or make sessions permanent.

Hours remain estimates from two consecutive successful sightings. The existing
600-second maximum observation gap is unchanged. Rate-limit backoff remains at
least one hour and honors a longer server Retry-After. Requests remain spaced 2s.
The previous checkpoint is retained atomically alongside the latest one. Missing,
corrupt or wrong-account checkpoints stop checks instead of initializing zeroes.

## Configuration (production only)

Vercel project: import this repository, Python runtime, no frontend build command.
`vercel.json` pins `framework: null` (Other) and an empty build command so the
file-based `api/playtime.py` handler is not mistaken for a framework application
requiring a root ASGI/WSGI entrypoint. Do not select the Python framework preset.
Keep Fluid Compute enabled. Add variables securely in Vercel, scoped to Production:

- `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`: an owned durable Redis database,
  not an anonymous temporary database. Use its primary REST endpoint.
  Vercel Marketplace's `KV_REST_API_URL` / `KV_REST_API_TOKEN` pair is also supported
  automatically if neither explicit Upstash variable is configured. Never use the
  read-only token or mix credentials from two databases.
- `VRC_USERNAME`, `VRC_PASSWORD`, `VRC_TOTP_SECRET`, `VRC_CONTACT`.
- `VRC_SESSION_KEY`: a new Fernet encryption key for this new counter. Keep it backed
  up securely and do not rotate it after initialization without a recovery plan.
  Do not replace the existing GitHub counter's key.
- `CRON_SECRET`: independent random ASCII secret, at least 32 characters.
- `PLAYTIME_ADMIN_SECRET`: different random ASCII secret, at least 32 characters.
- `PLAYTIME_ENABLED=false` until cutover checks are ready; then `true` and redeploy.

`VERCEL_ENV=production` is supplied by Vercel. Preview deployments cannot poll.
GitHub credentials are NOT needed for normal execution on Vercel. Public report
GETs and sampling consume storage/function quotas; verify actual monthly usage
before promising that a deployment is free. One-minute scheduling is ~43,200
invocations per 30 days, plus monitoring/report readers.

## External publication clock

When GitHub's native cron does not fire, operator action `publication-schedule`
configures a separate cron-job.org task for the existing public Pages publisher.
It uses a dedicated fine-grained `GH_REPORT_DISPATCH_TOKEN` stored as a Vercel
Production Secret: owner `korichneviykakiish`, only `5ul-calendar-feed`, Actions
read/write. Do not use the operator's broad Git credential or VRChat credentials.

Setup verifies the account, repository, active workflow and one dispatch before
creating a disabled job, confirming its identity, and enabling it. Existing managed
jobs are reused; incomplete listings, duplicates or changed identities stop setup.
Only this dedicated GitHub token is stored in the publication job's HTTPS request
header. Its request is a fixed `POST` to `publish-vercel-report.yml`, body
`{"ref":"main"}`, every minute with no expiry date. It cannot sample VRChat or reset hours.
An existing five-minute task is upgraded in place only when its saved job ID matches;
the change never creates a duplicate or takes over an unrelated job.
New tasks notify the scheduler account email on failure/automatic disabling;
reconfiguration preserves existing notification preferences. Renew the limited
token before its chosen expiration, then reconfigure the same task.

`publication-status` exposes only safe numeric history. HTTP 204 means GitHub
accepted a dispatch, not that Pages finished publishing. Verify actual successful
Actions runs and the public Pages `updated` timestamp. The redundant native
`on.schedule` entry is removed, leaving manual/external dispatch enabled. Both
tasks run once per minute; sampling failures notify the cron-job.org account email.

### Month-long operation

Neither task has an end date; no month-end reset is performed. Counters and the
previous checkpoint have no TTL. Only temporary leases expire. The dedicated GitHub
token must outlive the intended period. Vercel/VRChat uptime, outbound IP stability,
session acceptance, GitHub queue delays and provider quotas are not guaranteed.

Fenced saves use MGET/MSET instead of four separate inner commands. Native DELEX
IFEQ releases only the owner's lease; older backends safely fall back to Lua.
Operator `storage-status` tests native support using a random absent key, without
creating data or touching any live lease. With native release, a normal sample is
approximately nine billable Redis commands including Lua inner calls, plus one
for a public read: about 432,000 commands per 30 days (446,400 per 31). This excludes
extra clients, management and other uses of the same database; monitor actual usage
against the provider's current plan. No paid plan is enabled automatically.

## Safe cutover

1. Run local unit tests and deploy **disabled**. Keep the current service and data.
2. Pause old GitHub sampling and wait for active jobs to finish before logging in
   on the new host. Do not delete or migrate old hours, checkpoints or reports.
   Do not run two hosts against the same bot account.
3. Initialize using `python -m playtime.serverless --initialize-empty` from a trusted
   operator environment with `VRC_SESSION_KEY`, `VRC_USERNAME` and the two Upstash
   variables. No GitHub token is needed and no old data is read. This creates empty
   state only if it does not already exist; repeating initialization cannot erase
   hours subsequently earned on Vercel. Keep the new encryption key and an encrypted
   off-provider backup securely; never commit either.
4. Confirm the public report contains an empty leaderboard and no last-success
   timestamp before enabling production. Perform exactly one manual `resume` on
   the deployed host. If challenged, stop and complete
   the requested account verification; do not enable repeated login attempts.
5. In a later minute, perform a normal `sample` using the saved session. Only after
   this succeeds create/enable the external one-minute schedule. Observe at least
   two scheduled successful samples and incoming request acceptance.
6. Switch report consumers deliberately to the new verified URL. Check VRChat's
   String Loading trusted-domain rules first: a new Vercel domain is not assumed
   equivalent to the currently trusted GitHub Pages domain. Keeping the old URL
   needs a separate verified publication bridge; this adapter does not provide one.
   `publish-vercel-report.yml` provides that bridge for the existing Pages URL:
   it copies only the public sanitized JSON, without credentials or API sampling,
   once per minute through the external task. GitHub execution/deployment delays affect display
   freshness, not the counter's one-minute operation. Calendar remains unchanged.

The retired world's hours were not imported. The old GitHub sampling workflow
has been replaced with public-report publication only: enabling it cannot create
a second VRChat sampler. The calendar's workflow remains separately disabled.

## Verification

`python -m unittest discover -v`

Tests cover 60-second accounting, duplicate minutes, leases, stale writes, missing
storage, pre-request checkpoints, bounded automatic recovery, authentication isolation,
rate-limit delays, request budgets, public data hygiene and preview safety.
Live Redis scripting, deployment, credentials, scheduler and VRChat access still need
real checks; local tests alone cannot confirm any of those external services.
