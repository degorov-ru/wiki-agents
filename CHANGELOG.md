# Changelog

## 0.2.2 — 2026-09-18

- Match compound identifiers and their word parts, including mixed-language
  queries such as cookie-баннера, without removing exact identifier matches.
- Add a regression proving relevant evidence is not hidden by an incidental
  metrics-id match. No additional model call or dependency is required.

## Unreleased 0.2.1

- Keep article bodies, not the full index, in the bounded answer context.
- Retain labelled cancellation/supersession evidence in retrieval; never treat
  historical decisions or unclassified legacy claims as current facts.
- Make a fresh initializer byte-identical on repeat, including AGENTS.md.
- Check inbound links in one pass instead of rereading the wiki for every page.
- Preview legacy metadata migration; preserve text/sources, mark unclassified
  legacy content as proposed hypotheses, refuse invalid sources without writes.
- Include article snapshots in managed upgrade/migration backups and rollback.

## Unreleased 0.2.0

- Explicit providers, read-only model proposals and validated writes.
- Transaction recovery, queue deduplication and controlled purge.
- Knowledge states, source anchors, bounded retrieval and source import.
- Status, doctor, pause, resume, upgrade, rollback and uninstall commands.
- Portable release checks and synthetic full-path tests.
