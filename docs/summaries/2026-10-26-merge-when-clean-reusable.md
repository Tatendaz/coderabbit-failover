# Session: one shared, tested merge-when-clean workflow

**Branch:** security/merge-when-clean-reusable
**Date:** 2026-10-26

## Prompt
> Build one shared, tested merge-when-clean reusable workflow in Tatendaz/coderabbit-failover,
> then replace the copy in each of the 24 listed consumer repos with a short pinned caller.

## Steps
- Built the reusable workflow from template v7 (sha256 `a5520977…`). Only the own-check filter
  and the protected path list changed; a test checks that.
- Wrote `tests/merge_when_clean/fake_gh.py` and `tests/merge_when_clean/test_merge_when_clean.py`. Each case runs the extracted
  script with `bash --noprofile --norc -e -o pipefail`, and each has a mutant it must catch.
- Pinned pytest by hash in `tests/merge_when_clean/requirements.txt` and added a CI job to `selftest.yml`.
- actionlint and zizmor (strict, online) clean.

## Decisions
- Coordinator change 2026-10-26: callers pin `@merge-when-clean-v1` (moving major tag), not a
  sha, with a zizmor `ref-pin` policy for `Tatendaz/coderabbit-failover/*` only. Release steps
  went into `docs/merge-when-clean.md`.
- Concurrency lives in the reusable job, not in the caller: two holders of the same group
  deadlock.
- The status-success `if` and the triggers stay in the caller, so a non-success status never
  starts a run.
