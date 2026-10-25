# Session: parallel backup reviews

**Branch:** feat/parallel-reviews
**Date:** 2026-10-25

## Prompt
> Build issue Tatendaz/coderabbit-failover#8 (parallel backup reviews), get the PR merged
> clean, deploy it to the live reviewer safely, verify parallel runs on real PRs.

## Steps
- Split `main()` into `poll()`, `run_queue()` and `finish()`. Workers only run reviews;
  the main thread alone changes the queue.
- Added `file_lock()`, a state lock in `save_state()`, per-repo locks, `RateLimited`,
  `claude_limit()`, and a `moved` result from `review_pr()`.
- Wrote 19 tests. Each one fails against the old logic (checked with a shim that gives the
  old file the new names).

## Decisions
- One long-lived run with a poll loop, not detached workers: launchd never starts a second
  copy of a running job, so the running copy has to poll for itself.
- `--once-pr` no longer takes the run lock, so a manual review can run beside the
  scheduled one. The repo lock still keeps them off one cache clone.
