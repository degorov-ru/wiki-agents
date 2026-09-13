# Wiki Agents

[Русский](../README.md) · English · [Español](README.es.md)

Portable project memory for Claude Code and Codex. Conversation hooks write
useful context to daily logs; Haiku, Luna, or Grok compiles it into a linked
Markdown wiki. Personal memory and machine-specific paths are never shipped.

## Quick start

Send the repository link and this prompt to your coding agent:

> Install this memory system. Read `INSTALL.md`, ask me only the necessary
> questions, install and test everything. If anything is unavailable or fails,
> explain the problem plainly and tell me exactly how to fix it.

The installer selects a primary engine and optional fallback, discovers paths
on the recipient's machine, installs hooks, and runs real engine smoke tests.
The agent must then verify the full `chat → hook → daily → wiki` chain.

Full agent instructions: [INSTALL.md](INSTALL.en.md).
