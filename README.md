# Wiki Agents

Русский · [English](docs/README.en.md) · [Español](docs/README.es.md)

**Your AI conversations compile themselves into a searchable knowledge base.**

Архитектура основана на [LLM Knowledge Base Карпати](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), но источником служат разговоры с Claude Code и Codex. Хуки сохраняют важные решения и знания в ежедневные журналы, а выбранный движок — Haiku, Luna или Grok — собирает из них связанную wiki. Поиск работает через простой индекс без векторной базы.

Установщик предлагает выбрать Haiku (Claude CLI), Luna (Codex CLI) или Grok и
при желании резервный движок.

## Быстрый старт

Для установки на другом компьютере начни со [START-HERE.md](START-HERE.md):
там готовое сообщение агенту, границы доступа и критерии успешной установки.
Происхождение кода и сведения о лицензии: [LICENSING.md](LICENSING.md).

Отправь агенту ссылку на репозиторий и этот текст:

> "Установи систему памяти по этой ссылке. Прочитай INSTALL.md, задай мне только
> необходимые вопросы, выполни установку и тестирование. Если что-то не работает
> или недоступно, объясни проблему простым языком и скажи, как её исправить."

Агент следует [INSTALL.md](INSTALL.md). Установщик сам определяет локальные пути,
добавляет хуки и проверяет выбранные движки. Личная память и пути автора в
репозиторий не входят.

Установщик делает реальный smoke-тест каждого движка и выводит `OK`, `PARTIAL`
или `FAILED` с простой инструкцией исправления. Затем агент выполняет E2E-тест
через отдельный чат по `INSTALL.md`.

From there, your conversations start accumulating. After 04:00 local time, the next session start or flush automatically triggers compilation of changed daily logs into wiki articles. If the computer was off at 04:00, the next session start acts as the fallback trigger. You can also run `uv run python scripts/compile.py` manually at any time.

## How It Works

```
Conversation -> Claude SessionEnd/PreCompact or Codex Stop hooks -> flush.py extracts knowledge
    -> daily/YYYY-MM-DD.md -> compile.py -> wiki/concepts/, connections/, qa/
        -> SessionStart hook injects index into next session -> cycle repeats
```

- **Hooks** capture conversations automatically (Claude session end/pre-compaction and Codex Stop)
- **flush.py** calls the configured memory engine to decide what's worth saving, retains failed inputs for retry, and after 04:00 triggers compilation automatically
- **compile.py** validates a read-only model proposal, then code writes organized articles
- **query.py** answers questions using index-guided retrieval (no RAG needed at personal scale)
- **lint.py** checks links, sources, schema, staleness, and optional contradictions

## Key Commands

```bash
uv run python scripts/compile.py                    # compile new daily logs
uv run python scripts/query.py "question"            # ask the knowledge base
uv run python scripts/lint.py                        # run health checks
uv run python scripts/lint.py --structural-only      # free structural checks only
uv run python scripts/manage.py status --project /path/to/project
uv run python scripts/manage.py doctor --project /path/to/project
uv run python scripts/manage.py pause                # pause this checkout's hooks
uv run python scripts/manage.py resume
```

Set `CMC_PROJECT_ROOTS` to an OS-path-separated list of project roots. Machine-local
roots, runtime logs, `.cmc/`, `daily/`, and generated wiki content must not be committed
to a shared compiler repository.

## Why No Vector Database?

The default query path uses deterministic index-term matching and sends no more
than six current articles within a fixed context budget. This keeps the product
simple; it does not claim to outperform semantic retrieval. Add another retrieval
layer only after measured misses justify it.

## Supported installation and updates

Tested platform: macOS with Python 3.12+, `uv`, and an authenticated Luna CLI.
Linux/Windows and live Haiku/Grok operation remain unverified. CLI hook testing
does not establish Desktop hook trust; follow [INSTALL.md](INSTALL.md).

Use a new versioned directory for updates, never overwrite a dirty installation.
Keep the previous directory and machine configuration until the pilot passes.
`manage.py upgrade` backs up project instructions, hooks, index/log and articles;
`rollback --backup` restores that snapshot. Uninstall removes hooks, not memory.
Legacy metadata migration is explicit and preview-first; see INSTALL.md.

Distribution artifact: source-only `tar.gz` built by `scripts/release_check.py
--build /path/to/package.tar.gz`. Extract into a permanent directory, run
`uv sync`, then install as described above. Machine settings and project memory
are excluded. See [LICENSING.md](LICENSING.md) for provenance and licensing status.

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the technical reference: article formats, hook
architecture, script internals, known platform limits, and customization options.
