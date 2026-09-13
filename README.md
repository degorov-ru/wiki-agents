# LLM Personal Knowledge Base

Русский · [English](docs/README.en.md) · [Español](docs/README.es.md)

**Your AI conversations compile themselves into a searchable knowledge base.**

Архитектура основана на [LLM Knowledge Base Карпати](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), но источником служат разговоры с Claude Code и Codex. Хуки сохраняют важные решения и знания в ежедневные журналы, а выбранный движок — Haiku, Luna или Grok — собирает из них связанную wiki. Поиск работает через простой индекс без векторной базы.

Установщик предлагает выбрать Haiku (Claude CLI), Luna (Codex CLI) или Grok и
при желании резервный движок.

## Быстрый старт

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
- **compile.py** turns daily logs into organized concept articles with cross-references (triggered automatically or run manually)
- **query.py** answers questions using index-guided retrieval (no RAG needed at personal scale)
- **lint.py** runs 7 health checks (broken links, orphans, contradictions, staleness)

## Key Commands

```bash
uv run python scripts/compile.py                    # compile new daily logs
uv run python scripts/query.py "question"            # ask the knowledge base
uv run python scripts/query.py "question" --file-back # ask + save answer back
uv run python scripts/lint.py                        # run health checks
uv run python scripts/lint.py --structural-only      # free structural checks only
```

Set `CMC_PROJECT_ROOTS` to an OS-path-separated list of project roots. Machine-local
roots, runtime logs, `.cmc/`, `daily/`, and generated wiki content must not be committed
to a shared compiler repository.

## Why No RAG?

Karpathy's insight: at personal scale (50-500 articles), the LLM reading a structured `index.md` outperforms vector similarity. The LLM understands what you're really asking; cosine similarity just finds similar words. RAG becomes necessary at ~2,000+ articles when the index exceeds the context window.

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, cross-platform details, costs, and customization options. AGENTS.md is designed to give an AI agent everything it needs to understand, modify, or rebuild the system.
