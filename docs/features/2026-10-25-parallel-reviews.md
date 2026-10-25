# Feature: parallel backup reviews

**Branch:** feat/parallel-reviews
**Date:** 2026-10-25

## Summary
The backup reviewer runs up to 3 reviews at once (`REVIEW_FAILOVER_CONCURRENCY`), one per
repo, and keeps polling for new triggers while they run. Closes #8.

## Why
It reviewed one PR at a time. In the 2026-10-25 security sweep, 103 reviews ran back to
back for 145 minutes, and a PR triggered at the start of a batch waited 20 to 30 minutes.
A `/open-code-review` posted during a batch was not seen until the batch ended.

## What changed
- A thread pool runs the reviews. A per-repo file lock keeps two reviews (or a `--once-pr`
  run) off one cache clone.
- The run polls every 2 minutes while reviews run and saves state after each poll and each
  finished review, under a state lock.
- A trigger for a PR under review runs it again when that review ends. A head that moved
  during the review is reviewed 3 minutes later.
- A Claude usage or rate-limit error pauses new reviews until the reset time (if none is
  given: 15 minutes, doubling, 6 hours at most). It counts as a failed attempt only after
  24 hours of limits on that PR.
- A review in flight is marked in state before it starts. A run that was killed mid-review
  leaves the mark, and the next run retries past its own `pending` status.
- A failed review waits 3 minutes before its next try. All PRs that fail for good in one
  run go out in one phone push.
- `tests/test_local_reviewer.py`: 27 offline tests, run in the selftest workflow.

## Notes
The read sandbox from #2 is kept: each review's `allowRead` is its own worktree, its own repo
cache, and the OCR tools. Worktrees moved from `$TMPDIR` to `BASE/.worktrees`, inside the
blocked `~/`. In `$TMPDIR` a review could read a parallel review's worktree with
`git diff <dir> <dir>` (git falls back to no-index for paths outside the repo).
