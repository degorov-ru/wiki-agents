# Pre-rollout checklist

This checkout is product-only. Do not copy it over an active installation.

1. Run unit tests and synthetic E2E in a temporary project.
2. Confirm `engine.json` has an explicitly chosen primary and optional fallback.
3. Inspect `git status`; never package `daily/`, `wiki/`, `ACCESS.md`, `.cmc/`,
   `engine.json`, credentials, local hooks or test output.
4. Test install, repeat install, dry-run, pause/resume and uninstall in a clean
   profile before promising support. macOS is target; Linux and Windows remain unverified.
5. Owner authorizes the publication destination; retain upstream provenance and
   licensing notices without inventing a license grant.
