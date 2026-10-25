# Session: zizmor fails on unparseable workflows

**Branch:** security/zizmor-strict-2026-10-25
**Date:** 2026-10-25

## Prompt
> Security sweep 2026-10-25, hardening follow-up: apply CodeRabbit's zizmor findings from
> model-picker#10 to the other repos.

## Steps
- Local zizmor 1.31.1: a broken workflow next to valid ones exits 0 by default, 1 with
  `--strict-collection`. Online mode + strict exits 0 on this repo.
