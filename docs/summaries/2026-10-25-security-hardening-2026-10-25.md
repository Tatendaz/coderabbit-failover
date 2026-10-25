# Session: Supply-chain hardening

**Branch:** security/hardening-2026-10-25
**Date:** 2026-10-25

## Prompts
1. "After you finish the sweep for all your repos, read HARDENING.md and do it for each of
   your repos: Dependabot config with 7-day cooldown, package-manager cooldown, CI lockdown,
   zizmor job."
2. "Add the auto-merge job from templates/merge-when-clean.yml to every repo you harden."

## Steps taken
- Validated `dependabot.yml` against the SchemaStore schema.
- actionlint clean; zizmor 1.31.1 clean at high.
