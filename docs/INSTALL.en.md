# Agent installation contract

1. Clone this repository into a permanent local directory and read
   [`../INSTALL.md`](../INSTALL.md).
2. Ask the user to choose Haiku, Luna, or Grok and an optional fallback. When no
   other subscription exists, recommend Haiku from Claude and Luna from Codex.
3. Ask which projects or project roots should use memory. Never scan the whole
   home directory automatically.
4. Run `uv sync`, then `uv run python scripts/install.py` with the confirmed
   `--caller`, `--engine`, `--fallback`, `--root`, and `--project` values.
5. Run the separate-chat E2E check described in the Russian canonical guide.

In Codex Desktop, open the project, run `/hooks`, review the exact command and
hash from `.codex/hooks.json`, and mark it trusted. Without this manual step the
Desktop Stop hook does not run.

Use `install.py --dry-run` to inspect the plan without writes or provider calls.
Lifecycle commands are `manage.py status|doctor|pause|resume|upgrade|rollback|uninstall|uninstall-claude`.
Upgrade creates a managed backup; rollback accepts only `.cmc/backups/`; uninstall
`uninstall` removes the project Codex hook; `uninstall-claude` removes this
product's global Claude hooks. Both preserve memory and unrelated hooks.

Never silently skip a failed or unavailable step. Report `OK`, `PARTIAL`, or
`FAILED`; for every problem explain what failed, its impact, the exact recovery
action, and the test to repeat. Never commit personal memory, credentials,
machine paths, `daily/`, `wiki/`, `.cmc/`, `ACCESS.md`, or local configuration.
Only `.md` and `.txt` sources are accepted by `scripts/import_source.py`.

Tested: macOS, Python 3.12+, uv, authenticated Luna CLI. Linux/Windows and live
Haiku/Grok require separate verification. Extract a source archive into a new
versioned directory; never overwrite a dirty installation. Retain the old
directory and machine settings. Project upgrade backups include wiki articles.
`manage.py migrate-articles --project PATH` previews conservative legacy metadata;
add `--apply` only after review. Missing kind/status become hypothesis/proposed;
text and sources stay intact. Invalid sources block the whole batch. Repeat is
NOOP; `rollback --backup PATH` restores the managed snapshot. Pause concurrent
work during rollback. Public redistribution requires resolution of LICENSING.md.
