# Merge when clean (reusable workflow)

`.github/workflows/reusable-merge-when-clean.yml` merges an open PR once it is clean on its head
commit: CI green, a real review on that commit, and no open thread. Its header lists what it
merges and what it skips. An optional `.github/merge-hold-paths` in the calling repo (one regex
per line) holds more paths for the owner.

## Call it from a repo

`.github/workflows/merge-when-clean.yml`:

```yaml
name: Merge when clean
on:
  schedule:
    - cron: '23,53 * * * *'
  status: {}
  pull_request_review:
    types: [submitted]
  workflow_dispatch: {}
permissions:
  contents: write
  pull-requests: write
  checks: read
  statuses: read
jobs:
  merge:
    if: github.event_name != 'status' || github.event.state == 'success'
    uses: Tatendaz/coderabbit-failover/.github/workflows/reusable-merge-when-clean.yml@merge-when-clean-v1
    # Private repos on self-hosted runners:
    # with:
    #   runs-on: '["self-hosted","macOS","ARM64"]'
```

Do not add a `merge-when-clean` concurrency group to the caller. The shared job holds that
group, and two holders of one group deadlock.

The caller pins the moving major tag `merge-when-clean-v1`, not a sha. With a sha pin, every fix
would need one Dependabot PR per repo. Those PRs change a workflow file, which `GITHUB_TOKEN`
cannot merge, so a person would merge each one. The tag lives in this repo, so only people with
write access here can move it. Third-party actions stay pinned to a sha.

zizmor's `unpinned-uses` audit wants a sha by default. Allow a tag pin for this one workflow path
in the calling repo's `.github/zizmor.yml`:

```yaml
rules:
  unpinned-uses:
    config:
      policies:
        "Tatendaz/coderabbit-failover/.github/workflows/reusable-merge-when-clean.yml": ref-pin
```

Other paths in this repo, and every other `uses:`, still fall under zizmor's implicit
`"*": hash-pin`.

The tag is a moving ref with write permissions in every caller. Anyone who can move it changes
what runs in all of them. Only the owner can push to this repo. A tag ruleset that blocks
updates and deletes of `merge-when-clean-v*` for everyone but the owner would make that explicit.

## Release a fix

Each fix is one merge and one tag move. Every caller picks it up on its next run.

1. Change the workflow and its tests in one PR. CI must be green and the PR reviewed. The
   job never merges a PR that touches this file, so the owner merges it.
2. Tag the merge commit with the next version, then move the major tag to it:

   ```bash
   git fetch origin && git switch main && git pull --ff-only
   sha=$(git rev-parse HEAD)          # the merge commit of the fix
   git tag merge-when-clean-v1.0.1 "$sha"
   git tag -f merge-when-clean-v1 "$sha"
   git push origin merge-when-clean-v1.0.1
   git push --force origin merge-when-clean-v1
   ```

3. Check one repo: `gh workflow run merge-when-clean.yml -R Tatendaz/<repo>`, then read the
   run log.

To roll back, move `merge-when-clean-v1` back to the previous `merge-when-clean-v1.x.y` commit
and push it with `--force`.

A change that callers must act on (a new required input, other permissions, other triggers)
gets a new major tag, `merge-when-clean-v2`. Callers move to it in their own PRs.

## Tests

```bash
pip install --require-hashes -r tests/merge_when_clean/requirements.txt
python -m pytest tests/merge_when_clean
```

Each case extracts the `run:` script from the workflow, puts a fake `gh`
(`tests/merge_when_clean/fake_gh.py`) first on PATH, and runs the script with
`bash --noprofile --norc -e -o pipefail`, the way GitHub runs `shell: bash`. Each case also has
a mutant, a small break in the script, and `test_case_catches_mutant` checks that the case fails
on it. Set `MWC_BASH=/bin/bash` to run the cases with another bash. They run in the `Self-tests`
workflow on Ubuntu and macOS.
