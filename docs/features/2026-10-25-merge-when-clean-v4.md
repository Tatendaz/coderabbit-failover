# Feature: merge-when-clean template v6

**Branch:** security/merge-when-clean-v4
**Date:** 2026-10-25

## Summary
Update `.github/workflows/merge-when-clean.yml` to the shared template v6 (sha256 `af7e7bd9…`), unchanged.

## Why
v6 holds every PR when `.github/merge-hold-paths` has a bad regex, blocks on any reviewer's latest CHANGES_REQUESTED review, and drops `tr | grep -q` pipes that could turn a match into a pass under `pipefail`. Without a hold-paths file it acts like v2.
