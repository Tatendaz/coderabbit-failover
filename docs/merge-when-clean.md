# Merge when clean (reusable workflow)

A repo calls the shared job from `.github/workflows/merge-when-clean.yml`. Pin the full commit
sha of a `merge-when-clean-v*` tag:

```yaml
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
    uses: Tatendaz/coderabbit-failover/.github/workflows/reusable-merge-when-clean.yml@<sha> # merge-when-clean-v1.0.0
    # Private repos on self-hosted runners:
    # with:
    #   runs-on: '["self-hosted","macOS","ARM64"]'
```

The file's header lists what it merges and what it skips. An optional
`.github/merge-hold-paths` (one regex per line) holds more paths for the owner.
Run the tests with `pip install --require-hashes -r tests/requirements.txt && python -m pytest tests`.

The tests run in the `Self-tests` workflow on Ubuntu and macOS. Each case also has a mutant, a
small break in the script, and `test_case_catches_mutant` checks that the case fails on it.
Set `MWC_BASH=/bin/bash` to run the cases with another bash.
