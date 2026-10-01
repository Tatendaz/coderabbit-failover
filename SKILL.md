---
name: coderabbit-failover
description: Spend CodeRabbit reviews through a shared budget ledger, and start a local backup review (Claude Code + open-code-review) when CodeRabbit is rate-limited, so a PR never waits for a review window. Use before opening a PR, pushing to a PR with auto-review, posting `@coderabbitai review`, running `coderabbit review`, or when CodeRabbit says it is rate-limited.
---

# CodeRabbit budget + backup review

CodeRabbit limits reviews per developer, in one pool shared by every repo and PR.
Parallel agents burn that pool fast. This skill does two things:

1. `scripts/coderabbit_budget.py` keeps one ledger (`~/.claude/coderabbit/ledger.json`)
   and books a review slot before anything spends one.
2. `reviewer/local_reviewer.py` runs a backup review on your Mac when CodeRabbit is on hold.

`budget` below means `python3 <this skill>/scripts/coderabbit_budget.py`.

## Rules

- **Spend a review only through `budget run`.** It checks and books the slot under a lock,
  so two sessions cannot take the same slot:
  `budget run --kind pr|cli --repo OWNER/REPO [--pr N] -- <command>`
- **Never** gate with `budget check | head && ...`. The pipe hides the exit code.
- **Exit 75 = hold.** For a PR, do not wait. Run
  `budget failover --repo OWNER/REPO --pr N`. It posts `/open-code-review` and the backup
  reviewer picks it up within ~3 minutes. It does not use CodeRabbit budget.
- **Exit 3 from failover** (archived repo, fork, other owner): `budget queue-add` it, and
  fire it after `next_free_at`. Fire one queued review per freed window, never a batch.
- **On every rate-limit message** from CodeRabbit:
  `budget record-limit --kind pr --wait "<stated wait>" --source "OWNER/REPO#N"`.
- A trigger made outside `budget run`: `budget record-trigger` it.
- `@coderabbitai rate limit` as a PR comment is a free check of remaining capacity.
- New PRs: end the description with `@coderabbitai ignore`, then request one budgeted
  `@coderabbitai review` when the PR is ready.

## Is the PR really reviewed?

Use `scripts/coderabbit_status.py --repo OWNER/REPO --pr N` (add `--watch` in the
background). A review counts only if it ran on the **current head commit**:

- CodeRabbit: its status went `pending` then `success` "Review completed", or a
  "No actionable comments" comment whose commit range ends at the head.
- Backup: a `review-failover` commit status of `success` on the head.

These are **not** a review: the green stamp `@coderabbitai ignore` leaves, a `success`
that says "Review rate limited", an empty bot review.

`review-failover` = `failure` means critical/high findings: fix, push, review the new head.

## Backup reviewer

See `README.md` for setup. It reviews with headless Claude Code in its own config folder
(no user hooks, memory or CLAUDE.md; PR settings, MCP and skills ignored). Claude can only
read the PR. The script posts inline comments, a summary, and the commit status.
