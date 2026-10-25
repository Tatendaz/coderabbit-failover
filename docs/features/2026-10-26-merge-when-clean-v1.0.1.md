# Feature: merge-when-clean v1.0.1 holds the zizmor config

**Branch:** security/merge-when-clean-v1.0.1
**Date:** 2026-10-26

## Summary
The shared job no longer merges a PR that touches `.github/zizmor.yml`, `.github/zizmor.yaml`,
`zizmor.yml` or `zizmor.yaml`. The owner merges those.

## Why
Each caller's `.github/zizmor.yml` holds the one exception to sha pinning (the tag pin on this
workflow). The backup review on Tatendaz/Tatendaz#19 and Tatendaz/yapui#19 found that an
opted-in PR could widen that exception and merge itself.

## What changed
- One more pattern in the built-in list of paths the owner merges.
- New test case `zizmor_config_skipped`, with its mutant.

## Notes
Release: tag `merge-when-clean-v1.0.1`, move `merge-when-clean-v1`.
