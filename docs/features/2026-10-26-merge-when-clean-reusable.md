# Feature: one shared, tested merge-when-clean workflow

**Branch:** security/merge-when-clean-reusable
**Date:** 2026-10-26

## Summary
Add `.github/workflows/reusable-merge-when-clean.yml`, a `workflow_call` version of the
merge-when-clean job (template v7), and `tests/test_merge_when_clean.py`, which runs its
script against a fake `gh` in CI.

## Why
Each repo had its own copy of the job. It needed 6 fixes in one day, and each fix was copied
into about 25 repos by hand, with no tests. Now repos call this one file, pinned to a commit
sha, and Dependabot bumps the pin.

## What changed
- The script is template v7, with two changes for `workflow_call`:
  - The job's own check run is now named `<caller job> / Merge when clean`, so the filter
    also skips names that end in ` / Merge when clean`.
  - `.github/workflows/reusable-merge-when-clean.yml` joins the paths the owner merges.
- Input `runs-on` (JSON, default `"ubuntu-latest"`). Private repos pass
  `'["self-hosted","macOS","ARM64"]'`.
- The job sets its own permissions and holds the `merge-when-clean` concurrency group.
  Callers must not use that group name.
- 51 cases, each with a mutant (a small break in the script) that the case must catch.
  They run on Ubuntu and macOS, and on macOS also with `/bin/bash` 3.2.

## Notes
Tag the merge commit `merge-when-clean-v1.0.0`. Callers pin that sha.
