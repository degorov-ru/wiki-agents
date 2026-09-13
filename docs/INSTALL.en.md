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

Never silently skip a failed or unavailable step. Report `OK`, `PARTIAL`, or
`FAILED`; for every problem explain what failed, its impact, the exact recovery
action, and the test to repeat. Never commit personal memory, credentials,
machine paths, `daily/`, `wiki/`, `.cmc/`, `ACCESS.md`, or local configuration.
