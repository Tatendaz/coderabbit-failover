# coderabbit-failover

**One CodeRabbit review budget for all your agents, and a backup review when it runs out.** A Claude Code skill. A shared ledger books each review slot before an agent spends it. When CodeRabbit is rate-limited, a reviewer on your Mac reviews the PR instead.

[View on GitHub](https://github.com/Tatendaz/coderabbit-failover) [Install](#install)

What an agent runs when it wants a review (`budget` is `python3 <skill>/scripts/coderabbit_budget.py`):

```text
$ budget run --kind pr --repo OWNER/REPO --pr N -- \
    gh pr comment N -R OWNER/REPO --body "@coderabbitai review"
# exit 75 means hold. Do not wait for the window:
$ budget failover --repo OWNER/REPO --pr N
```

## Who it is for

People who run many coding agents against CodeRabbit. CodeRabbit limits reviews per developer, in one pool shared by every repo and PR, and parallel agents burn that pool fast.

I use it to let agents merge PRs on their own when CI is green and a real review passed on the head commit. The ledger stops two agents from spending the same slot. For open, same-repo PRs in repos it watches, the backup review runs instead of waiting for CodeRabbit's next window.

## How it works

- **One ledger.** `scripts/coderabbit_budget.py` keeps one file, `~/.claude/coderabbit/ledger.json`. `run` checks the budget and books the slot under a file lock, then runs your command. On hold it exits 75 and never runs it. If CodeRabbit answers with a rate-limit message, the slot is given back and the stated wait is recorded for every session.
- **A reviewer on your Mac.** `reviewer/local_reviewer.py` runs every 3 minutes under launchd. It starts on a CodeRabbit rate-limit comment, or on a `/open-code-review` comment from an owner, member or collaborator. `budget failover` posts that comment, and does not use CodeRabbit budget.
- **The backup review.** open-code-review in delegation mode with headless Claude Code (Opus 5.5, effort medium) on your subscription, in its own config folder: no user hooks, memory or CLAUDE.md, and the PR's settings, MCP and skills are ignored. Claude only reads the PR. The script posts inline comments, a summary, and a `review-failover` commit status.
- **Was the head reviewed?** `scripts/coderabbit_status.py` counts a review only if it ran on the current head commit: CodeRabbit's "Review completed" status, a "No actionable comments" comment whose range ends at the head, or `review-failover` success. The green stamp `@coderabbitai ignore` leaves does not count.

| `review-failover` status | Meaning |
|---|---|
| `pending` | Review running. |
| `success` | No critical or high findings. |
| `failure` | Critical or high findings. Fix and push. |
| `error` | Failed 3 times. Comment `/open-code-review` to retry. |

## Install

```bash
git clone https://github.com/Tatendaz/coderabbit-failover ~/.claude/skills/coderabbit-failover
python3 ~/.claude/skills/coderabbit-failover/scripts/coderabbit_budget.py selftest
```

Needs Python 3.10+ and an authenticated `gh`. The backup reviewer setup (macOS) is in the [README](https://github.com/Tatendaz/coderabbit-failover#set-up-the-backup-reviewer-macos).

## Status and limits

First public release, October 2026. No tagged releases yet.

- The backup reviewer is macOS only. It uses launchd and the Keychain. It runs while the Mac is awake and catches up on wake.
- It covers repos you own that are not archived, not forks, and were pushed this year. Fork PRs are skipped.
- The ledger and the status script each have an offline `selftest` (49 and 70 checks). The reviewer has `--dry-run` but no automated tests, and the repo has no CI.
- The backup review is one model's opinion. It catches a lot, not everything.
- CodeRabbit's limit messages change. If one stops being detected, add it to `RATE_RE`.

## Built by Tatenda Zhou

[More work](https://tatendaz.github.io/) · [Follow on X](https://x.com/realtatendazhou) · [LinkedIn](https://www.linkedin.com/in/tatendazhou)

Need help making AI coding agents reliable? [tatendaz@me.com](mailto:tatendaz@me.com?subject=Making%20AI%20coding%20agents%20reliable)

MIT licensed.
