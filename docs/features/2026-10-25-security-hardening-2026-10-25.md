# Feature: Supply-chain hardening

**Branch:** security/hardening-2026-10-25
**Date:** 2026-10-25

## Summary
Slower, safer action updates, a workflow security scan, and an auto-merge job.

## What changed
- `.github/dependabot.yml` (new): `github-actions` only, monthly, `cooldown: default-days: 7`,
  `open-pull-requests-limit: 3`, minor and patch grouped. Security updates ignore the cooldown.
- `.github/workflows/zizmor.yml` (new): scans `.github/workflows` on PRs and pushes to `main`,
  fails on high findings. zizmor 1.31.1 from hash-pinned wheels.
- `.github/workflows/merge-when-clean.yml` (new, shared template): merges Dependabot PRs and
  the owner's `security/*` or `automerge`-labelled PRs once CI is green, a real review ran on
  the head and 0 threads are open.

## Notes
No package-manager cooldown: the scripts use only the Python standard library.
