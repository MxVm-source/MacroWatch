# MacroWatch cleanup execution
Approved scope: docs/superpowers/specs/2026-11-06-cleanup.md.
Native implementation on existing isolated branch jj-us-briefing; user approved
cleanup/deploy and public/manual vs private/JJ destination roles explicitly.

Implemented three-job entrypoint, private-only command allowlist, retired crypto
jobs even when old crypto flags remain set. Public automation defaultmanualonly.
Traditional-market close recap at actual close+10m, conditional sourced quotes;
Monday remaining-week calendar. Retargeted existing statement filter to macro.
One-time role-verified description updates; persistence prevents future repeated
edits or monthly activation spam. Old module files remain in git, not scheduled.

Checks: full61 tests pass, compileall and diff whitespacecheck pass. Live local
Yahoo quotes for latest completed session parsed. Independent review found
plural/substr crypto-filter bypass and concurrent manual/scheduled duplicate race;
both reproduced with failing tests then fixed, full suite green. Failed Telegram
sends retain retry eligibility; private/public bot description edits only after
identity resolution. Main worker never places trades or reads old Bitget accounts.
Known BLS networkblock retained; manual-only cache updater has no schedule.

Deployment and runtime description/source/delivery verification pending.
