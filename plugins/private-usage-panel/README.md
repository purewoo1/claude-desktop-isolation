# Private usage panel

Local mod v0.2.1 for Claude Code 2.1.309, installed only in the isolated Desktop home.
Draws the account's five-hour and weekly usage, extra server-reported windows,
and reset countdowns. It is not a per-conversation context meter or an exact
token/billing ledger.

Uses only `session.usage()` (without a breakdown request), clock, command and UI
APIs. It reads no credentials, conversations or files, starts no processes,
makes no HTTP/model requests and never compacts or modifies prompts. The host
app decides pane placement: this is not a patch to the native session rail.

Default: a compact band above the prompt, leaving the native Context rail intact.
On reload it closes only its own old v0.1 pane. `/quota-bar` toggles the band.
`/quota-panel` manually toggles a detailed pane, which occupies the right-hand
pane area and can temporarily hide Context; it is never opened automatically.
Missing values show unknown; passed reset times show stale data, not zero.
Readings follow what the engine last received, so idle figures may lag.
Version 0.2.1 awaits the async clock API in both UI sites; seventeen offline
tests cover the countdown text and its minute-by-minute redraw.

Validation: `claude plugin validate PATH`; offline tests: `claude plugin test PATH`.
Disable: `claude plugin disable private-usage-panel@skills-dir` in the same home.
For complete rollback move the installed `~/.claude/skills/private-usage-panel`
directory out of that skills directory and start a new session.
