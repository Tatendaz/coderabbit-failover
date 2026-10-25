# Feature: zizmor fails on unparseable workflows

**Branch:** security/zizmor-strict-2026-10-25
**Date:** 2026-10-25

## Summary
The zizmor job now runs with `--strict-collection` and the read-only `github.token`.

## Why
By default zizmor warns and skips a workflow it cannot parse, so the job passed without
scanning that file (seen during the 2026-10-25 hardening). The token turns on the online
audits: impostor commits and known-vulnerable actions. Found by CodeRabbit on
Tatendaz/model-picker#10.
