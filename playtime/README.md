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

For the separately deployed one-minute Vercel adapter, see [VERCEL.md](VERCEL.md).
It uses persistent encrypted state and an external scheduler, not GitHub Actions.
The GitHub schedule below is unchanged until a verified cutover.

`publish-playtime` is a short scheduled report job, not a continuously running game client.
The schedule targets every five minutes, away from the top of the hour. GitHub can delay or
drop scheduled runs. Treat totals as estimates, not authoritative exact attendance or anti-cheat.

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

The GitHub workflow explicitly enables `VRC_AUTH_PER_RUN=true`: each eligible run creates one
fresh session using the bot password and a current TOTP code, samples on that same runner, then
invalidates only that session with `PUT /logout`. A previous runner's IP-bound cookies are not
restored. No game client or always-on personal computer is needed. The schedule is still subject
to GitHub delays and VRChat authentication restrictions; this is not a promise of uninterrupted service.

## Storage and publication

- `playtime-state-v1` release: authenticated-encrypted checkpoints using `VRC_SESSION_KEY`.
- Checkpoints contain totals, last observations and run idempotency markers. Per-run login mode
  does not retain the disposable runner session in the checkpoint.
- The two newest checkpoints and one per day for up to seven observed days are retained.
- Each new checkpoint is uploaded and verified before old files are deleted.
- Checkpoints are replaceable release assets, not encrypted blobs committed every five minutes.
- Missing/corrupt established storage or an account/world mismatch stops the job without resetting totals.
- The encryption key must be backed up securely. Replacing it makes existing checkpoints unreadable.
- `docs/playtime.json`: public top 120, in the existing Unity `updated` / `top` format.
- Public data contains only rank, sanitized display name and seconds, plus report status.
- Pages and Git commits share the calendar's concurrency lock to avoid competing publishers.
- The original gist leaderboard is not read or overwritten.

The code and state are small, but Actions runtime and repository history are not unlimited.
Monitor GitHub usage. Private repositories consume included minutes; do not use perpetual jobs
or chained restarts to simulate 24/7 hosting. This workflow publishes a static project report.

## Configuration and activation

World ID comes from the current Unity scene blueprint and is in `playtime/config.json`.
The workflow reuses repository secrets `VRC_USERNAME`, `VRC_PASSWORD`, `VRC_TOTP_SECRET`,
`VRC_CONTACT`, and `VRC_SESSION_KEY`. The password and TOTP seed are scoped to the sampling
step's environment; they are never passed in workflow inputs, logs, or the public report.

1. Run `publish-playtime` with `action=publish` to initialize an empty counter and publish the report.
   This does not send any VRChat request.
2. Configure the five secrets above. `VRC_TOTP_SECRET` is the permanent authenticator seed,
   not a six-digit code. Never put passwords or codes in workflow inputs, issues, source, or chat.
3. Run `publish-playtime` with `action=resume`. It permits one login and sample even after a
   previous authentication failure, but never overrides a rate-limit deadline. Confirm a second,
   separate `action=sample` run works before enabling the schedule.
4. Confirm `status=ok`, the world ID, opt-in profile text, and the published report. Only then set
   the Actions repository variable `PLAYTIME_ENABLED=true` to enable scheduled samples.
5. Point Unity's Playtime board at the new Pages `playtime.json` when ready. This setup does not
   edit the world or replace its current leaderboard automatically.

`action=publish` only rebuilds a report from saved totals. In the GitHub workflow, `sample` does
one fresh login, sample and logout. `resume` permits a manual recovery attempt, not a retry loop.
Without `VRC_AUTH_PER_RUN=true`, the old cookie-only mode remains available for a stable host.

On a rejected login, additional email confirmation, invalid secrets, or failed session cleanup,
automatic logins stop pending manual recovery. On 429 it stores a
cooldown of at least one hour, increasing up to a day, and respects a longer server Retry-After.
The cooldown is checked before sending a password or TOTP. A rate-limited sample does not even
send a logout request afterwards. This is our backoff policy, not a promise about when VRChat
lifts a restriction. No IP switching to bypass a rejection.
Old totals remain intact during errors. The report's `updated` is the last complete sample, not
the last attempted run. `status` exposes staleness to monitoring without publishing credentials.

## Checks

`python -m unittest discover -v`

Tests use mock sessions and synthetic players. They never contact VRChat or require secrets.
Live validation still requires a working bot session and consenting friends in the target world.
