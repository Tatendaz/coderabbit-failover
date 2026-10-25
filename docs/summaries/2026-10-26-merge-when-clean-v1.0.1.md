# Session: merge-when-clean v1.0.1 holds the zizmor config

**Branch:** security/merge-when-clean-v1.0.1
**Date:** 2026-10-26

## Prompt
> Backup reviews on the caller PRs: `.github/zizmor.yml` is not on the hold list.

## Steps
- Added `^(\.github/)?zizmor\.ya?ml$` to the protected paths in the reusable workflow.
- Added a test case and mutant. 108 tests pass.
