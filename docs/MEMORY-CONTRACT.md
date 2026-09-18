# Memory contract v1

`daily/` is append-only source. `wiki/` is derived material. A model receives
redacted input and returns JSON only: `noop` or a list of complete replacement
files. Code validates paths and article metadata, then writes only under `wiki/`.

Article frontmatter requires `title`, `kind`, `status`, `updated`, `sources`.
Kinds: `fact`, `decision`, `procedure`, `hypothesis`, `preference`. Statuses:
`proposed`, `active`, `disputed`, `superseded`, `cancelled`. A replacement links
to prior knowledge with `supersedes`; `superseded` requires `superseded_by`.
`cancelled` records withdrawal without a replacement. Q&A is derived, not
independent proof.

Retrieval keeps cancelled/superseded articles as labelled historical evidence:
excluding them could let incidental mentions in active pages revive withdrawn
decisions. Stale derived Q&A stays excluded. Legacy articles without a status
are presented as proposed, not automatically promoted to active.

Malformed model output, changed input snapshots, unsafe paths and invalid schema
leave source pending. Missing or invalid `engine.json` stops provider calls.
Redaction catches common credential assignments; it is not a promise to detect all secrets.
