# Opt-in playtime report

This is a new counter. It does not import any old leaderboard or player hours.
The existing calendar and the Unity scene are unchanged.

## Player-facing rules

- Use a dedicated VRChat bot account. Its profile must explain the counter before enabling it.
- Sending the bot a friend request opts into a public leaderboard of name and estimated time in 5ul.
- The bot accepts incoming friend requests, but never sends unsolicited requests or messages.
- Removing the bot stops future counting. Existing totals remain until an administrator removes them.
- Hidden locations, unavailable API data and long observation gaps do not earn time.
- AFK presence counts: API presence does not prove active gameplay.
- No history of other worlds, private instance links, avatars or profile details is retained.

## Operation

The active counter runs on Vercel, triggered once per minute by cron-job.org.
The original GitHub sampler is deleted. `publish-vercel-report.yml` only copies
the sanitized public result to the existing trusted GitHub Pages URL; it receives
no VRChat credentials and never logs in. A separate cron-job.org task dispatches
publication every minute, independently of the minute API sampling. GitHub's
native schedule is removed. Queues and Pages deployment can still delay visibility.
Unity keeps its existing URL. The old encrypted checkpoints are not imported.

For the separately deployed one-minute Vercel adapter, see [VERCEL.md](VERCEL.md).
It uses persistent encrypted state and an external scheduler, not GitHub Actions.
The GitHub workflow cannot log in or sample, even when enabled manually.

`publish-vercel-report` is a short scheduled report job, not a continuously running game client.
The external schedule has no end date and does not rely on a personal computer.
Treat totals as estimates, not authoritative exact attendance or anti-cheat.

Two successful sightings in the configured world accrue the actual elapsed time, capped by
`max_gap_seconds` (600). A gap beyond that threshold or a failed/partial sample breaks continuity.
Leaving and returning between sightings cannot be detected. Short visits can be missed.
After a failure, two new successful sightings are required before time accrues again.
Changing rooms within the same world is eligible. Stable VRChat IDs survive display-name changes.

Online friends are read in pages of 100 with a two-second pause between API calls. At most
20 incoming friend requests are accepted per run. Notifications are scanned locally for requests
in up to five pages of 100: VRChat's deprecated `type` filter is not relied on. All pages needed
for that batch are read before accepting, so accepting cannot shift the next page's offset.
Hidden requests are not accepted; duplicate IDs are accepted at most once. A withdrawn request
(404) is skipped, but authentication, rate-limit and other errors stop the batch without retry.
The log records counts only, including whether the 500-notification scan cap was reached.
Older requests beyond that cap require administrator review; notifications are never deleted.
Failed, duplicate or incomplete friend-list pages never become a valid empty snapshot.
Requests are bounded; there are no retry loops.

Normal Vercel samples reuse the encrypted authenticated session. A password/TOTP login
requires an explicit operator `resume`, never an automatic retry after rejection. No game
client or always-on personal computer is needed. Vercel Hobby does not guarantee a fixed
outbound IP; an expired or rejected session may still require manual recovery.

## Storage and publication

- Upstash Redis holds authenticated-encrypted state using the Vercel `VRC_SESSION_KEY`.
- Checkpoints contain totals, last observations, the session and run idempotency markers.
- A previous checkpoint is retained atomically; leases prevent overlapping/stale writes.
- The retired world's Redis state and GitHub release snapshots were explicitly deleted.
- Active hours are never cleared by deployment, publication, sampling failure or initialization.
- Missing/corrupt established storage or an account/world mismatch stops the job without resetting totals.
- The encryption key must be backed up securely. Replacing it makes existing checkpoints unreadable.
- `docs/playtime.json`: public top 120, in the existing Unity `updated` / `top` format.
- Public data contains only rank, sanitized display name and seconds, plus report status.
- Pages and Git commits share the calendar's concurrency lock to avoid competing publishers.
- The original gist leaderboard is not read or overwritten.

Monitor Vercel, Redis and GitHub usage: code and state are small, but quotas and repository
history are not unlimited. Do not use perpetual Actions jobs or chained restarts to simulate
24/7 hosting. GitHub only publishes a static project report.

## Configuration and activation

The owner's requested published world is explicitly configured in `playtime/config.json`:
`wrld_bf7ec577-6b45-4aa2-839d-376b8a366c21`. All visible instances of that world
are eligible. This is not inferred from the local Unity scene's Blueprint ID or the
bot's own location. Friends whose location is hidden cannot be counted.
Configure runtime credentials only in Vercel Production as described in [VERCEL.md](VERCEL.md).
The GitHub publisher receives none of these credentials. `VRC_TOTP_SECRET` is the permanent
authenticator seed, not a six-digit code; never put either in inputs, source, logs or chat.

1. Verify the configured world, public opt-in notice and durable encrypted storage.
2. Initialize only an absent counter; existing state must not be overwritten.
3. Request one manual `resume` on Vercel and verify a subsequent normal sample in a later minute.
   Stop on a challenge or cooldown; do not automatically retry authentication.
4. Enable the external minute task only after two fresh successful samples; reuse its saved
   identity instead of creating duplicates. Monitor `schedule-status` and the actual last success.
5. GitHub Actions `publish-vercel-report` -> `Run workflow` publishes the current Vercel public JSON.
   This never logs in, samples VRChat or recreates the retired GitHub checkpoint archive.
6. Verify external task history, the corresponding successful Actions runs and the Pages
   `updated` timestamp. A successful manual publication is not evidence of an external clock.

Unity retains `https://korichneviykakiish.github.io/5ul-calendar-feed/playtime.json`.
Do not enable the old Actions `PLAYTIME_ENABLED` variable: it is no longer used for sampling.

On a rejected login, additional email confirmation or invalid secrets, sampling pauses
pending manual recovery. On 429 it stores a
cooldown of at least one hour, increasing up to a day, and respects a longer server Retry-After.
The cooldown is checked before sending a password or TOTP. This is our backoff policy, not a promise about when VRChat
lifts a restriction. No IP switching to bypass a rejection.
Old totals remain intact during errors. The report's `updated` is the last complete sample, not
the last attempted run. `status` exposes staleness to monitoring without publishing credentials.

## Checks

`python -m unittest discover -v`

Tests use mock sessions and synthetic players. They never contact VRChat or require secrets.
Live validation still requires a working bot session and consenting friends in the target world.
