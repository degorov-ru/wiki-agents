# AGENTS.md - Personal Knowledge Base Schema

> Adapted from [Andrej Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture.
> Instead of ingesting external articles, this system compiles knowledge from your own AI conversations.

## The Compiler Analogy

```
daily/          = source code    (your conversations - the raw material)
LLM             = compiler       (extracts and organizes knowledge)
wiki/      = executable     (structured, queryable knowledge base)
lint            = test suite     (health checks for consistency)
queries         = runtime        (using the knowledge)
```

You don't manually organize your knowledge. You have conversations, and the LLM handles the synthesis, cross-referencing, and maintenance.

---

## Architecture

### Layer 1: `daily/` - Conversation Logs (Immutable Source)

Daily logs capture what happened in your AI coding sessions. These are the "raw sources" - append-only, never edited after the fact.

```
daily/
├── 2026-04-01.md
├── 2026-04-02.md
├── ...
```

Each file follows this format:

```markdown
# Daily Log: YYYY-MM-DD

## Sessions

### Session (HH:MM) - Brief Title

**Context:** What the user was working on.

**Key Exchanges:**
- User asked about X, assistant explained Y
- Decided to use Z approach because...
- Discovered that W doesn't work when...

**Decisions Made:**
- Chose library X over Y because...
- Architecture: went with pattern Z

**Lessons Learned:**
- Always do X before Y to avoid...
- The gotcha with Z is that...

**Action Items:**
- [ ] Follow up on X
- [ ] Refactor Y when time permits
```

### Layer 2: `wiki/` - Compiled Knowledge (LLM-Owned)

The LLM owns this directory entirely. Humans read it but rarely edit it directly.

```
wiki/
├── index.md              # Master catalog - every article with one-line summary
├── log.md                # Append-only chronological build log
├── concepts/             # Atomic knowledge articles
├── connections/          # Cross-cutting insights linking 2+ concepts
└── qa/                   # Filed query answers (compounding knowledge)
```

### Layer 3: This File (AGENTS.md)

The schema that tells the LLM how to compile and maintain the knowledge base. This is the "compiler specification."

---

## Structural Files

### `wiki/index.md` - Master Catalog

A table listing every knowledge article. Query code searches catalog rows and
article text, including articles missing from the catalog. It sends at most six
articles within a fixed context budget to the model. A model reformulates only
the question into English and original-language search terms; code ranks the
full article texts with those terms. A bounded second pass selects up to six
articles from relevant excerpts of at most 24 candidates. Discovery never
depends on shortened catalog summaries or project-specific exceptions.

Format:

```markdown
# Knowledge Base Index

| Article | Summary | Compiled From | Updated |
|---------|---------|---------------|---------|
| [[concepts/supabase-auth]] | Row-level security patterns and JWT gotchas | daily/2026-04-02.md | 2026-04-02 |
| [[connections/auth-and-webhooks]] | Token verification patterns shared across Supabase auth and Stripe webhooks | daily/2026-04-02.md, daily/2026-04-04.md | 2026-04-04 |
```

### `wiki/log.md` - Build Log

Append-only chronological record of every compile, query, and lint operation.

Format:

```markdown
# Build Log

## [2026-04-01T14:30:00] compile | Daily Log 2026-04-01
- Source: daily/2026-04-01.md
- Articles created: [[concepts/nextjs-project-structure]], [[concepts/tailwind-setup]]
- Articles updated: (none)

## [2026-04-02T09:00:00] query | "How do I handle auth redirects?"
- Consulted: [[concepts/supabase-auth]], [[concepts/nextjs-middleware]]
- Filed to: [[qa/auth-redirect-handling]]
```

---

## Article Formats

### Concept Articles (`wiki/concepts/`)

One article per atomic piece of knowledge. These are facts, patterns, decisions, preferences, and lessons extracted from your conversations.

```markdown
---
title: "Concept Name"
kind: decision
status: active
aliases: [alternate-name, abbreviation]
tags: [domain, topic]
sources:
  - "daily/2026-04-01.md"
  - "daily/2026-04-03.md"
created: 2026-04-01
updated: 2026-04-03
---

# Concept Name

[2-4 sentence core explanation]

## Key Points

- [Bullet points, each self-contained]

## Details

[Deeper explanation, encyclopedia-style paragraphs]

## Related Concepts

- [[concepts/related-concept]] - How it connects

## Sources

- [[daily/2026-04-01.md]] - Initial discovery during project setup
- [[daily/2026-04-03.md]] - Updated after debugging session
```

### Connection Articles (`wiki/connections/`)

Cross-cutting synthesis linking 2+ concepts. Created when a conversation reveals a non-obvious relationship.

```markdown
---
title: "Connection: X and Y"
kind: fact
status: active
connects:
  - "concepts/concept-x"
  - "concepts/concept-y"
sources:
  - "daily/2026-04-04.md"
created: 2026-04-04
updated: 2026-04-04
---

# Connection: X and Y

## The Connection

[What links these concepts]

## Key Insight

[The non-obvious relationship discovered]

## Evidence

[Specific examples from conversations]

## Related Concepts

- [[concepts/concept-x]]
- [[concepts/concept-y]]
```

### Q&A Articles (`wiki/qa/`)

Filed answers from queries. Every complex question answered by the system can be permanently stored, making future queries smarter.

```markdown
---
title: "Q: Original Question"
kind: fact
status: active
updated: 2026-04-05
sources:
  - "daily/2026-04-05.md#m-20260405-example0000"
derived_from:
  - "[[concepts/article-1]]"
question: "The exact question asked"
consulted:
  - "concepts/article-1"
  - "concepts/article-2"
filed: 2026-04-05
---

# Q: Original Question

## Answer

[The synthesized answer with [[wikilinks]] to sources]

## Sources Consulted

- [[concepts/article-1]] - Relevant because...
- [[concepts/article-2]] - Provided context on...

## Follow-Up Questions

- What about edge case X?
- How does this change if Y?
```

---

## Core Operations

### 1. Compile (daily/ -> wiki/)

When processing a daily log:

1. Read the daily log file
2. Read `wiki/index.md` to understand current knowledge state
3. Read existing articles that may need updating
4. For each piece of knowledge found in the log:
   - If an existing concept article covers this topic: UPDATE it with new information, add the daily log as a source
   - If it's a new topic: CREATE a new `concepts/` article
5. If the log reveals a non-obvious connection between 2+ existing concepts: CREATE a `connections/` article
6. UPDATE `wiki/index.md` with new/modified entries
7. APPEND to `wiki/log.md`

**Important guidelines:**
- A daily log may produce zero or more articles; explicit no-op is valid
- Prefer updating existing articles over creating near-duplicates
- Use Obsidian-style `[[wikilinks]]` with full relative paths from wiki/
- Write in encyclopedia style - factual, concise, self-contained
- Every article must have YAML frontmatter
- Every article must link back to its source daily logs

### 2. Query (Ask the Knowledge Base)

1. Read `wiki/index.md` (the master catalog)
2. Select a bounded set of relevant articles from the index
3. Read those articles in full
4. Synthesize an answer with `[[wikilink]]` citations
5. Filed Q&A is derived material and requires a separate validated proposal

Selection uses weighted term matching over catalog rows and article text,
at most six articles, and a fixed context budget. It reports truncation;
no quality advantage over semantic retrieval is assumed.

Memory improvements must address the shared mechanism, not particular projects.
Do not add project names, article paths, or question-specific keyword exceptions
to retrieval logic. Verify changes with prior failures and additional topics.
Missing legacy status is not evidence that recorded facts are merely proposals;
retain explicit cancelled/proposed/disputed statuses and qualify unsupported claims.

### 3. Lint (Health Checks)

Structural and semantic checks run periodically:

1. **Broken links** - `[[wikilinks]]` pointing to non-existent articles
2. **Orphan pages** - Articles with zero inbound links from other articles
3. **Orphan sources** - Daily logs that haven't been compiled yet
4. **Stale articles** - Source daily log changed since article was last compiled
5. **Contradictions** - Conflicting claims across articles (requires LLM judgment)
6. **Schema and provenance** - required fields, states, sources, anchors and derived links

Output: a markdown report with severity levels (error, warning, suggestion).

---

## Conventions

- **Wikilinks:** Use Obsidian-style `[[path/to/article]]` without `.md` extension
- **Writing style:** Encyclopedia-style, factual, third-person where appropriate
- **Language:** Write Wiki titles, summaries and prose in Russian. Preserve identifiers,
  paths, code, source references and exact quoted messages. Never translate immutable daily logs.
- **Dates:** ISO 8601 (YYYY-MM-DD for dates, full ISO for timestamps in log.md)
- **File naming:** lowercase, hyphens for spaces (e.g., `supabase-row-level-security.md`)
- **Frontmatter:** Every article requires title, kind, status, updated, and sources
- **Sources:** Always link back to the daily log(s) that contributed to an article.
  Inherited references outside `daily/` and `sources/` may be retained only in
  `legacy_sources`; they remain explicitly unvalidated. New legacy references
  cannot be introduced, and canonical sources cannot be hidden in that field.

---

## Full Project Structure

```
llm-personal-kb/
|-- .gitignore                       # Excludes runtime state, temp files, caches
|-- AGENTS.md                        # This file - schema + full technical reference
|-- INSTALL.md                       # Portable installation instructions for an agent
|-- README.md                        # Concise overview + quick start
|-- pyproject.toml                   # Dependencies (at root so hooks can find it)
|-- daily/                           # "Source code" - conversation logs (immutable)
|-- wiki/                       # "Executable" - compiled knowledge (LLM-owned)
|   |-- index.md                     #   Master catalog - THE retrieval mechanism
|   |-- log.md                       #   Append-only build log
|   |-- concepts/                    #   Atomic knowledge articles
|   |-- connections/                 #   Cross-cutting insights linking 2+ concepts
|   |-- qa/                          #   Filed query answers (compounding knowledge)
|-- scripts/                         # CLI tools
|   |-- compile.py                   #   Compile daily logs -> knowledge articles
|   |-- query.py                     #   Ask questions (index-guided, no RAG)
|   |-- lint.py                      #   structural + optional contradiction checks
|   |-- flush.py                     #   Extract memories from conversations (background)
|   |-- config.py                    #   Path constants
|   |-- utils.py                     #   Shared helpers
|-- hooks/                           # Claude Code hooks
|   |-- session-start.py             #   Injects knowledge into every session
|   |-- session-end.py               #   Extracts conversation -> daily log
|   |-- pre-compact.py               #   Safety net: captures context before compaction
|-- reports/                         # Lint reports (gitignored)
```

---

## Hook System (Automatic Capture)

`scripts/install.py` merges hooks into the current user's
`~/.claude/settings.json`. That machine-local file is never distributed with this
repository. Hooks fire automatically when Claude Code is used in an initialized
project.

### `.claude/settings.json` Format

```json
{
  "hooks": {
    "SessionStart": [{ "matcher": "", "hooks": [{ "type": "command", "command": "/path/to/wiki-agents/.venv/bin/python3 /path/to/wiki-agents/hooks/session-start.py", "timeout": 15 }] }],
    "PreCompact": [{ "matcher": "", "hooks": [{ "type": "command", "command": "/path/to/wiki-agents/.venv/bin/python3 /path/to/wiki-agents/hooks/pre-compact.py", "timeout": 10 }] }],
    "SessionEnd": [{ "matcher": "", "hooks": [{ "type": "command", "command": "/path/to/wiki-agents/.venv/bin/python3 /path/to/wiki-agents/hooks/session-end.py", "timeout": 10 }] }]
  }
}
```

The installer uses the current Python interpreter and absolute hook paths,
with shell quoting, without launching uv. Empty `matcher` catches all events.

### Hook Details

**`session-start.py`** (SessionStart)
- Pure local I/O, no API calls, runs in under 1 second
- Reads `wiki/index.md` and the most recent daily log
- Outputs JSON to stdout: `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}`
- Claude sees the knowledge base index at the start of every session
- Max context: 20,000 characters

**`session-end.py`** (SessionEnd)
- Reads hook input from stdin (JSON with `session_id`, `transcript_path`, `cwd`)
- Copies the raw JSONL transcript to a temp file (no parsing in the hook - keeps it fast)
- Spawns `flush.py` as a fully detached background process
- Recursion guard: exits immediately if `CLAUDE_INVOKED_BY` env var is set

**`pre-compact.py`** (PreCompact)
- Same architecture as session-end.py
- Fires before Claude Code auto-compacts the context window
- Guards against empty `transcript_path` (known Claude Code bug #13668)
- Critical for long sessions: captures context before summarization discards it

**Why both PreCompact and SessionEnd?** Long-running sessions may trigger multiple auto-compactions before you close the session. Without PreCompact, intermediate context is lost to summarization before SessionEnd ever fires.

### Background Flush Process (`flush.py`)

Spawned by both hooks as a fully detached background process:
- **Windows:** `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` flags
- **Mac/Linux:** `start_new_session=True`

This ensures flush.py survives after Claude Code's hook process exits.

**What flush.py does:**
1. Sets `CLAUDE_INVOKED_BY=memory_flush` env var (prevents recursive hook firing)
2. Reads the pre-extracted conversation context from the temp `.md` file
3. Skips empty or exactly duplicated context using a content hash
4. Calls the configured Haiku, Luna, or Grok CLI without write tools
5. The selected engine decides what's worth saving - returns structured bullet points or `FLUSH_OK`
6. Appends result to `daily/YYYY-MM-DD.md`
7. Cleans up temp context only after success; failures remain for retry
8. **Scheduled auto-compilation:** If it's past 04:00 local time (`COMPILE_AFTER_HOUR = 4`) and any daily log is new or changed since its last compilation (hash comparison against `state.json`), spawns `compile.py` as another detached background process. On macOS, the installer registers a launchd queue check every five minutes for explicitly selected projects. This retries deferred and interrupted runs without another conversation. SessionStart remains an additional trigger. Each queue pass discovers initialized projects
under the configured project roots without following symlinks or traversing dependency
directories. The background queue starts one compilation at a time.

### JSONL Transcript Format

Claude Code stores conversations as `.jsonl` files. Messages are nested under a `message` key:

```python
entry = json.loads(line)
msg = entry.get("message", {})
role = msg.get("role", "")     # "user" or "assistant"
content = msg.get("content", "")  # string or list of content blocks
```

Content can be a string or a list of blocks (`{"type": "text", "text": "..."}` dicts).

---

## Script Details

### compile.py - The Compiler

Uses the engine selected during installation. `scripts/model_client.py` routes
the same prompt to Haiku, Luna, or Grok and invokes the configured fallback on
failure.

Existing article edits require an exact, unique source fragment. Similarity
thresholds must not substitute for exact matching, including for numbers or negations.
For sources touching many large articles, reduce `chunk_chars` in the canonical
project's `.cmc-config.json`; `CMC_CHUNK_CHARS` overrides it for a single run.

Local checkouts may use a regular `.cmc-project.json` containing
`{"project": "/absolute/path/to/canonical/project"}`. Hooks, compilation and queries resolve
this pointer before accessing memory. The target must have real `wiki/` and
`daily/` directories; pointer chains and symlinked managed directories are rejected.
Subdirectories inherit the nearest project pointer or initialized memory root.

- Selects relevant existing articles from the catalog before drafting changes, then
  supplies their complete text for exact fragment edits. Models return only edits
  and new articles; code owns metadata, the catalog and publication.
- One format is used for edits, with one correction attempt against the same bases.
  A separate content audit blocks invented claims, lost old facts and contradictions;
  omitted durable details are recorded as non-blocking `coverage_warnings`.
  One factual repair is allowed; unresolved errors leave the source pending.
- Verified chunks are cached only for identical sources, Wiki and compiler version.
  All chunks publish together. Unchanged compiled records are skipped; archive
  recompilation requires an explicit `--file` or `--all`. Historical rules must be
  labelled as history. A decision does not prove implementation.
- Incremental: tracks SHA-256 hashes of daily logs in `state.json`, skips unchanged files
- Primary-engine failures are reported and use the configured fallback;
  unrecoverable runs stay pending and retry later

**CLI:**
```bash
uv run python scripts/compile.py              # compile new/changed only
uv run python scripts/compile.py --all        # force recompile everything
uv run python scripts/compile.py --file daily/2026-04-01.md
uv run python scripts/compile.py --dry-run
```

### query.py - Index-Guided Retrieval

Uses index-term retrieval and loads at most a bounded set of matching articles. No RAG.

Selection uses weighted keyword matching over the catalog and article text.
Every question is reformulated into English and original-language terms before
an additional full-text ranking pass. A model then selects directly relevant
articles from bounded excerpts; the selected order is preserved. No model needs
the entire catalog to discover candidates. The answer model sees only selected articles and must
say when that context is insufficient. Primary articles with invalid metadata
remain discoverable with an explicit unvalidated label; invalid derived Q&A is excluded.
Code restricts answer citations to the exact supplied articles, resolving a shortened slug
only when it identifies one supplied article unambiguously. An independent grounding pass
checks claims and event dates against that context before delivery or filing. One repair
is allowed; verifier failures and repeated unsupported answers produce an explicit refusal.
Article update dates and the current date are never proof of an event date.

**CLI:**
```bash
uv run python scripts/query.py "What auth patterns do I use?"
uv run python scripts/query.py "What's my error handling strategy?" --file-back
```

`--file-back` explicitly saves a derived Q&A article. The model still has no
filesystem tools: code validates every cited article is active, copies its
primary sources, records `derived_from`, and applies the normal proposal transaction.

### lint.py - Health Checks

Checks:

| Check | Type | Catches |
|-------|------|---------|
| Broken links | Structural | `[[wikilinks]]` to non-existent articles |
| Orphan pages | Structural | Articles with zero inbound links |
| Orphan sources | Structural | Daily logs not yet compiled |
| Stale articles | Structural | Source logs changed since compilation |
| Article schema | Structural | Invalid kind/status/source/supersession/derived metadata |
| Contradictions | LLM | Conflicting claims across articles |

**CLI:**
```bash
uv run python scripts/lint.py                    # all checks
uv run python scripts/lint.py --structural-only  # skip LLM check (free)
```

Reports saved to `reports/lint-YYYY-MM-DD.md`.

---

## State Tracking

`.cmc/state.json` tracks:
- `ingested` - map of daily log filenames to SHA-256 hashes, compilation timestamps, and costs
- `query_count` - total queries run
- `last_lint` - timestamp of most recent lint
- `total_cost` - cumulative API cost

`.cmc/last-flush.json` tracks recent content hashes and the last session.

Both are gitignored and regenerated automatically.

---

## Dependencies

`pyproject.toml` (at project root):
- Claude, Codex, or Grok CLI - selected during installation for LLM-backed work
- `python-dotenv>=1.0.0` - Environment variable management
- `tzdata>=2024.1` - Timezone data
- Python 3.12+, managed by [uv](https://docs.astral.sh/uv/)

The selected CLI must already be installed and signed in. Credential storage is
owned by that CLI and is never read or copied by this product.

---

## Usage reporting

Provider usage and price vary by CLI and account. Adapter-reported cost is stored
when available; unavailable cost remains `unknown`, never an assumed zero.

---

## Customization

### Additional Article Types

Add directories like `people/`, `projects/`, `tools/` to `wiki/`. Define the article format in this file (AGENTS.md) and update `utils.py`'s `list_wiki_articles()` to include them.

### Obsidian Integration

The knowledge base is pure markdown with `[[wikilinks]]` - works natively in Obsidian. Point a vault at `wiki/` for graph view, backlinks, and search.

### Scaling Beyond Index-Guided Retrieval

If measured retrieval misses become unacceptable, add a tested search layer before
the model. The current implementation already bounds articles and context size.

<!-- wiki-agents:memory-start -->
## Project memory

Before substantial work, read `wiki/index.md`, the latest file in `daily/`,
and relevant pages under `wiki/`. Keep stable facts traceable to `daily/` or
`sources/`; never store credentials in project memory.
<!-- wiki-agents:memory-end -->
