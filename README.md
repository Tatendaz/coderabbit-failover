# coderabbit-failover

A Claude Code skill for people who run many coding agents against CodeRabbit.

- **Budget ledger.** CodeRabbit rate-limits reviews per developer, across all repos. One
  ledger, shared by every session, books each review slot before it is spent.
- **Backup review.** When CodeRabbit is rate-limited, a reviewer on your Mac reviews the
  PR with Claude Code and [open-code-review](https://www.npmjs.com/package/@alibaba-group/open-code-review)
  and posts a `review-failover` commit status. A PR never waits for a review window.

I use it to let agents merge PRs on their own when CI is green and a real review passed
on the head commit.

## What's in it

| Path | What |
|---|---|
| `SKILL.md` | The rules the agent follows. |
| `scripts/coderabbit_budget.py` | The ledger: `run`, `check`, `record-limit`, `failover`, queue, `selftest`. |
| `scripts/coderabbit_status.py` | Reads a PR and says if CodeRabbit really reviewed the head. |
| `reviewer/local_reviewer.py` | The backup reviewer. |
| `launchd/review-failover.plist.template` | Runs the reviewer every 3 minutes. |

## Install the skill

```bash
git clone https://github.com/Tatendaz/coderabbit-failover ~/.claude/skills/coderabbit-failover
python3 ~/.claude/skills/coderabbit-failover/scripts/coderabbit_budget.py selftest
```

Needs Python 3.10+ and an authenticated `gh`.

## Set up the backup reviewer (macOS)

1. Install open-code-review where the reviewer finds it:

   ```bash
   mkdir -p ~/.review-failover/.tools && cd ~/.review-failover/.tools
   npm install @alibaba-group/open-code-review@1.12.9
   ```

2. Put a Claude Code token in the Keychain. It reviews on your subscription, not API credits.

   ```bash
   claude setup-token
   security add-generic-password -U -a "$USER" -s claude-code-oauth-token -w
   ```

3. Try it. Nothing is posted with `--dry-run`.

   ```bash
   REVIEW_FAILOVER_OWNER=<your-github-user> python3 reviewer/local_reviewer.py --dry-run
   ```

4. Run it every 3 minutes: fill in `launchd/review-failover.plist.template`, copy it to
   `~/Library/LaunchAgents/local.review-failover.plist`, then
   `launchctl load ~/Library/LaunchAgents/local.review-failover.plist`.

### Settings

| Env var | Default | What |
|---|---|---|
| `REVIEW_FAILOVER_OWNER` | (required) | GitHub user or org to watch. |
| `REVIEW_FAILOVER_HOME` | `~/.review-failover` | State, cache, tools. |
| `REVIEW_FAILOVER_KEYCHAIN` | `claude-code-oauth-token` | Keychain item with the token. |
| `REVIEW_FAILOVER_NOTIFY` | none | A command that takes one message, called after 3 failed tries. |
| `CODERABBIT_LEDGER_DIR` | `~/.claude/coderabbit` | Where the ledger lives. |

### What starts a backup review

- CodeRabbit posts a rate-limit comment on a PR.
- An owner, member or collaborator comments `/open-code-review` (what `budget failover` posts).

It only covers repos you own that are not archived and not forks. Fork PRs are skipped.
It runs only while the Mac is awake, and catches up on wake.

The reviewing Claude can read only the PR checkout, and run only `ocr delegate`,
`git diff`, `git show` and `git log` (never with `--output` or `--no-index`). Its
findings are posted publicly, so a prompt hidden in PR code must not reach other files
on the Mac.

| Status | Meaning |
|---|---|
| `pending` | Review running. |
| `success` | No critical or high findings. |
| `failure` | Critical or high findings. Fix and push. |
| `error` | Failed 3 times. Comment `/open-code-review` to retry. |

## Limits

- The backup review is one model's opinion. It catches a lot, not everything.
- CodeRabbit's limit messages change. If one stops being detected, add it to `RATE_RE`.

MIT license.
