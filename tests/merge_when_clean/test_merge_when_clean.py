"""Tests for .github/workflows/reusable-merge-when-clean.yml.

Each case pulls the `run:` script out of the workflow, puts a fake `gh` (fake_gh.py)
first on PATH, and runs the script the way GitHub runs `shell: bash`:
`bash --noprofile --norc -e -o pipefail`. Set MWC_BASH to try another bash.

Every case also names one small break in the script (a mutant). test_case_catches_mutant
applies it and checks the case then fails, so a case that cannot fail is caught.
"""
import json
import re
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORKFLOW = ROOT / ".github/workflows/reusable-merge-when-clean.yml"
FAKE_GH = HERE / "fake_gh.py"
REPO = "Tatendaz/demo"
OWNER = "Tatendaz"


def extract_script(text):
    """The block scalar under `run: |` of the "Merge every clean PR" step."""
    lines = text.splitlines()
    step = next(i for i, l in enumerate(lines) if l.strip() == "- name: Merge every clean PR")
    start = next(i for i in range(step, len(lines)) if lines[i].strip() == "run: |")
    indent, body = None, []
    for line in lines[start + 1:]:
        if not line.strip():
            body.append("")
            continue
        ind = len(line) - len(line.lstrip())
        if indent is None:
            indent = ind
        if ind < indent:
            break
        body.append(line[indent:])
    return "\n".join(body).rstrip("\n") + "\n"


SCRIPT = extract_script(WORKFLOW.read_text())


# ---------- fixtures: one repo, a few PRs, all clean unless a case changes them ----------

def make_pr(number, **kw):
    pr = {
        "number": number,
        "author": OWNER,
        "ref": f"security/fix-{number}",
        "fork": False,
        "draft": False,
        "labels": [],
        "title": f"security: fix {number}",
        "body": "",
        "sha": f"{number:040x}",
        "mergeable_state": "clean",
        "files": [["README.md", ""]],
        "changed_files": None,  # None = len(files)
        "checks": [check("CI")],
        "statuses": [status("CodeRabbit", "success", "coderabbitai[bot]", "Review completed")],
        "decision": None,
        "opinions": [],
        "threads": [],
        "threads_more": False,
    }
    pr.update(kw)
    return pr


def check(name, status_="completed", conclusion="success", started="2026-10-26T10:00:00Z"):
    return {"name": name, "status": status_, "conclusion": conclusion, "started_at": started}


def status(context, state, login, description="", created="2026-10-26T10:00:00Z"):
    return {"context": context, "state": state, "creator": {"login": login},
            "description": description, "created_at": created}


def dependabot_pr(number, title, body, **kw):
    return make_pr(number, author="dependabot[bot]", ref=f"dependabot/npm_and_yarn/x-{number}",
                   title=title, body=body, **kw)


def routes_for(prs, hold=None, overrides=None):
    """hold: None = file missing (404), a string = its content, a dict = a raw route."""
    r = {f"repos/{REPO}": {"json": {"default_branch": "main"}}}
    r[f"repos/{REPO}/pulls"] = {"pages": [[{
        "number": p["number"],
        "draft": p["draft"],
        "user": {"login": p["author"]},
        "head": {"ref": p["ref"], "repo": {"full_name": "someone/fork" if p["fork"] else REPO}},
        "labels": [{"name": n} for n in p["labels"]],
    } for p in prs]]}
    if hold is None:
        r[f"repos/{REPO}/contents/.github/merge-hold-paths"] = {"error": "gh: Not Found (HTTP 404)"}
    elif isinstance(hold, dict):
        r[f"repos/{REPO}/contents/.github/merge-hold-paths"] = hold
    else:
        r[f"repos/{REPO}/contents/.github/merge-hold-paths"] = {"raw": hold}
    for p in prs:
        n, sha = p["number"], p["sha"]
        changed = len(p["files"]) if p["changed_files"] is None else p["changed_files"]
        r[f"repos/{REPO}/pulls/{n}"] = {"json": {
            "number": n, "title": p["title"], "body": p["body"], "user": {"login": p["author"]},
            "labels": [{"name": l} for l in p["labels"]], "head": {"sha": sha},
            "changed_files": changed, "mergeable_state": p["mergeable_state"]}}
        r[f"repos/{REPO}/pulls/{n}/files"] = {"pages": [[
            {"filename": f, "previous_filename": prev or None} for f, prev in p["files"]]]}
        # Two check runs per page, so a read without --paginate misses some.
        pages = [p["checks"][i:i + 2] for i in range(0, len(p["checks"]), 2)] or [[]]
        r[f"repos/{REPO}/commits/{sha}/check-runs"] = {"pages": [
            {"total_count": len(p["checks"]), "check_runs": page} for page in pages]}
        r[f"repos/{REPO}/commits/{sha}/statuses"] = {"pages": [p["statuses"]]}
        r[f"merge:{n}"] = {}
        r[f"graphql:{n}"] = {"json": {"data": {"repository": {"pullRequest": {
            "reviewDecision": p["decision"],
            "latestOpinionatedReviews": {"nodes": [{"state": s} for s in p["opinions"]]},
            "reviewThreads": {"pageInfo": {"hasNextPage": p["threads_more"]},
                              "nodes": [{"isResolved": t} for t in p["threads"]]}}}}}}
    r.update(overrides or {})
    return r


class Result:
    def __init__(self, rc, out, calls):
        self.rc, self.out, self.calls = rc, out, calls
        self.merged = [int(c[2]) for c in calls if c[:2] == ["pr", "merge"]]
        self.merge_calls = [c for c in calls if c[:2] == ["pr", "merge"]]

    def __repr__(self):
        return f"rc={self.rc} merged={self.merged}\n{self.out}"


class Runner:
    def __init__(self, tmp_path, script):
        self.tmp = tmp_path
        self.script = tmp_path / "merge.sh"
        self.script.write_text(script)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f'#!/bin/sh\nexec python3 "{FAKE_GH}" "$@"\n')
        gh.chmod(0o755)
        self.bindir = bindir

    def __call__(self, prs, hold=None, overrides=None):
        routes = self.tmp / "routes.json"
        log = self.tmp / "calls.log"
        routes.write_text(json.dumps(routes_for(prs, hold, overrides)))
        log.write_text("")
        env = dict(os.environ, PATH=f"{self.bindir}:{os.environ['PATH']}", REPO=REPO, OWNER=OWNER,
                   GH_TOKEN="fake", FAKE_GH_ROUTES=str(routes), FAKE_GH_LOG=str(log))
        bash = os.environ.get("MWC_BASH", "bash")
        p = subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", str(self.script)],
                           env=env, capture_output=True, text=True, timeout=60)
        calls = [json.loads(l) for l in log.read_text().splitlines() if l]
        return Result(p.returncode, p.stdout + p.stderr, calls)


# ---------- cases ----------

CASES = {}


def case(old, new):
    """Register a case and the mutant (replace `old` with `new` in the script) it must catch."""
    def deco(fn):
        CASES[fn.__name__] = (fn, old, new)
        return fn
    return deco


def assert_not_merged(r, rc=0, says=None):
    assert r.rc == rc, r
    assert r.merged == [], r
    if says:
        assert says in r.out, r


# The happy path

@case('gh pr merge "$pr" --repo', 'echo gh pr merge "$pr" --repo')
def clean_pr_merges(run):
    r = run([make_pr(5)])
    assert r.rc == 0, r
    assert r.merged == [5], r
    assert "#5: merged" in r.out, r


@case('--match-head-commit "$sha"', '')
def merge_pins_the_head_commit(run):
    r = run([make_pr(5)])
    assert r.merge_calls, r
    call = r.merge_calls[0]
    assert "--match-head-commit" in call, r
    assert call[call.index("--match-head-commit") + 1] == f"{5:040x}", r
    assert "--squash" in call and "--delete-branch" in call, r


@case('sort -n <<<"$prs"', 'cat <<<"$prs"')
def oldest_pr_first(run):
    r = run([make_pr(12), make_pr(3)])
    assert r.rc == 0, r
    assert r.merged == [3, 12], r


@case('[ -z "$prs" ] && { echo "no open PRs to check"; exit 0; }', '')
def no_open_prs_exits_0(run):
    r = run([])
    assert r.rc == 0, r
    assert "no open PRs to check" in r.out, r
    assert not any(c[:2] == ["api", f"repos/{REPO}/contents/.github/merge-hold-paths?ref=main"]
                   for c in r.calls), r


# Which PRs are in scope

@case('(.head.ref | startswith(\\"security/\\"))', 'true')
def non_security_owner_pr_skipped(run):
    r = run([make_pr(5, ref="feat/thing")])
    assert_not_merged(r, says="no open PRs to check")


@case(' or any(.labels[]; .name==\\"automerge\\")', '')
def owner_pr_labelled_automerge_merges(run):
    r = run([make_pr(5, ref="feat/thing", labels=["automerge"])])
    assert r.merged == [5], r


@case('(.user.login==\\"$OWNER\\"', '(true')
def other_author_pr_skipped(run):
    r = run([make_pr(5, author="mallory")])
    assert_not_merged(r, says="no open PRs to check")


@case('select(.draft==false) | ', '')
def draft_pr_skipped(run):
    r = run([make_pr(5, draft=True)])
    assert_not_merged(r, says="no open PRs to check")


@case('| select(.head.repo.full_name==\\"$REPO\\")', '')
def fork_pr_skipped(run):
    r = run([make_pr(5, fork=True)])
    assert_not_merged(r, says="no open PRs to check")


@case('*,needs-owner,*|', '')
def needs_owner_label_skipped(run):
    r = run([make_pr(5, labels=["needs-owner"])])
    assert_not_merged(r, says="#5: skip (label)")


@case('|*,do-not-merge,*', '')
def do_not_merge_label_skipped(run):
    r = run([make_pr(5, labels=["do-not-merge"])])
    assert_not_merged(r, says="#5: skip (label)")


@case("grep -q 'Tatendaz/ideas-inbox#17'", "grep -q 'no-such-marker'")
def night_shift_pr_skipped(run):
    r = run([make_pr(5, body="Nightly batch. Tracked in Tatendaz/ideas-inbox#17.")])
    assert_not_merged(r, says="#5: skip (Night Shift PR)")


# Dependabot major / minor

@case('if [ "$author" = "dependabot[bot]" ] && is_major', 'if false && is_major')
def dependabot_major_title_skipped(run):
    r = run([dependabot_pr(5, "Bump left-pad from 1.3.0 to 2.0.0",
                           "Bumps [left-pad](https://x) from 1.3.0 to 2.0.0.")])
    assert_not_merged(r, says="#5: skip (Dependabot major update")


@case('[ "${a%%.*}" = 0 ] &&', 'false &&')
def dependabot_zero_x_minor_counts_as_major(run):
    r = run([dependabot_pr(5, "Bump tiny from 0.3.1 to 0.4.0",
                           "Bumps [tiny](https://x) from 0.3.1 to 0.4.0.")])
    assert_not_merged(r, says="#5: skip (Dependabot major update")


@case('[ "$seen" = no ]', 'false')
def dependabot_without_versions_counts_as_major(run):
    r = run([dependabot_pr(5, "Bump the stuff", "Some text with no versions.")])
    assert_not_merged(r, says="#5: skip (Dependabot major update")


@case('is_major <<<"$title"$\'\\n\'"$summary"', 'is_major <<<"$title"$\'\\n\'"$body"')
def dependabot_minor_with_major_release_notes_merges(run):
    body = ("Bumps [left-pad](https://x) from 1.3.0 to 1.4.0.\n"
            "<details><summary>Release notes</summary>\n"
            "<p>See the guide for moving from 1.x to 2.x before the next major.</p>\n"
            "</details>")
    r = run([dependabot_pr(5, "Bump left-pad from 1.3.0 to 1.4.0", body)])
    assert r.merged == [5], r


GROUP_TITLE = "Bump the npm-minor group across 1 directory with 3 updates"


@case("| awk '{print $2, $4}')", "| awk '{print $2, $4}' | head -n 1)")
def dependabot_group_with_one_major_skipped(run):
    body = ("Bumps the npm-minor group with 3 updates: [a](https://x), [b](https://x) and [c](https://x).\n\n"
            "Updates `a` from 1.0.0 to 1.1.0\n"
            "Updates `b` from 2.4.0 to 3.0.0\n"
            "Updates `c` from 0.1.0 to 0.1.1\n")
    r = run([dependabot_pr(5, GROUP_TITLE, body)])
    assert_not_merged(r, says="#5: skip (Dependabot major update")


@case("grep -E '^(Updates `|Bumps \\[)'", "grep -E '^(Bumps \\[)'")
def dependabot_group_all_minor_merges(run):
    body = ("Bumps the npm-minor group with 2 updates: [a](https://x) and [c](https://x).\n\n"
            "Updates `a` from 1.0.0 to 1.1.0\n"
            "Updates `c` from 0.1.0 to 0.1.1\n")
    r = run([dependabot_pr(5, GROUP_TITLE, body)])
    assert r.merged == [5], r


# Files the owner merges

@case('if [ "$rc" != 1 ]; then echo "#$pr: skip (touches files', 'if false; then echo "#$pr: skip (touches files')
def protected_path_skipped(run):
    for path in ["firestore.rules", "app/storage.rules", "database.rules.json", "db/migrations/001.sql",
                 "terraform/main.tf", "infra/net.tf", ".github/workflows/merge-when-clean.yml",
                 ".github/merge-hold-paths"]:
        r = run([make_pr(5, files=[["README.md", ""], [path, ""]])])
        assert_not_merged(r, says="#5: skip (touches files the owner merges")


@case('|^(\\.github/)?zizmor\\.ya?ml$', '')
def zizmor_config_skipped(run):
    # It holds the one exception to sha pinning, so a PR must not loosen it on its own.
    for path in [".github/zizmor.yml", ".github/zizmor.yaml", "zizmor.yml"]:
        r = run([make_pr(5, files=[[path, ""]])])
        assert_not_merged(r, says="#5: skip (touches files the owner merges")


@case('workflows/(reusable-)?merge', 'workflows/merge')
def reusable_workflow_path_skipped(run):
    r = run([make_pr(5, files=[[".github/workflows/reusable-merge-when-clean.yml", ""]])])
    assert_not_merged(r, says="#5: skip (touches files the owner merges")


@case('"\\(.filename)\\t\\(.previous_filename // "")"', '"\\(.filename)\\t"')
def rename_away_from_protected_path_skipped(run):
    r = run([make_pr(5, files=[["rules/old-firestore.rules.bak", "firestore.rules"]])])
    assert_not_merged(r, says="#5: skip (touches files the owner merges")


@case(' || [ "$(grep -c . <<<"$files")" != "$changed" ]', '')
def file_list_count_mismatch_not_merged(run):
    r = run([make_pr(5, changed_files=2)])
    assert_not_merged(r, says="#5: file list incomplete (2 changed)")


# The hold file

@case('grep -Eq -f <(printf \'%s\\n\' "$hold") <<<"$paths"; rc=$?', 'rc=1')
def hold_path_match_skipped(run):
    r = run([make_pr(5, files=[["docs/guide.md", ""]])], hold="# held by the owner\n^docs/\n")
    assert_not_merged(r, says="#5: skip (matches .github/merge-hold-paths")
    assert "hold paths: ^docs/" in r.out, r


@case("hold=$(grep -vE '^[[:space:]]*(#|$)' <<<\"$hold\" || true)", "true")
def hold_comments_and_blank_lines_ignored(run):
    # As regexes, "# x|README" would match README.md and a blank line matches every path.
    r = run([make_pr(5)], hold="# x|README\n\n^infra/\n")
    assert r.merged == [5], r


@case('(grep $rc); merging nothing"; exit 1', '(grep $rc); merging nothing"; exit 0')
def bad_hold_regex_exits_1(run):
    r = run([make_pr(5)], hold="^docs/(\n")
    assert_not_merged(r, rc=1, says="bad regex in .github/merge-hold-paths")


@case('$(head -c 200 "$err"); merging nothing"; exit 1', '$(head -c 200 "$err"); merging nothing"; exit 0')
def unreadable_hold_file_exits_1(run):
    r = run([make_pr(5)], hold={"error": "gh: Server Error (HTTP 500)"})
    assert_not_merged(r, rc=1, says="could not read .github/merge-hold-paths")


# Checks and statuses

@case('[ "$pending" != 0 ] || ', '')
def pending_check_not_merged(run):
    r = run([make_pr(5, checks=[check("CI"), check("e2e", "in_progress", None)])])
    assert_not_merged(r, says="#5: not clean yet")


@case('[ "$failed" != 0 ] || ', '')
def failed_check_not_merged(run):
    r = run([make_pr(5, checks=[check("CI"), check("lint", conclusion="failure")])])
    assert_not_merged(r, says="#5: not clean yet")


@case('if [ "$total" = 0 ]; then', 'if false; then')
def zero_checks_not_merged(run):
    r = run([make_pr(5, checks=[])])
    assert_not_merged(r, says="#5: no CI checks on this commit yet")


@case('select(.name!="Merge when clean" and (.name | endswith(" / Merge when clean") | not))',
      'select(.name!="Merge when clean")')
def own_merge_job_check_ignored(run):
    own = [check("Merge when clean / Merge when clean", "in_progress", None),
           check("merge / Merge when clean", "queued", None)]
    r = run([make_pr(5, checks=[check("CI")] + own)])
    assert r.merged == [5], r


@case('gh api --paginate "repos/$REPO/commits/$sha/check-runs', 'gh api "repos/$REPO/commits/$sha/check-runs')
def failed_check_on_second_page_not_merged(run):
    checks = [check("build"), check("lint"), check("e2e", conclusion="failure")]
    r = run([make_pr(5, checks=checks)])
    assert_not_merged(r, says="checks=3 pending=0 failed=1")


@case('map(max_by(.started_at))', 'map(min_by(.started_at))')
def latest_check_run_per_name_counts(run):
    checks = [check("CI", conclusion="failure", started="2026-10-26T09:00:00Z"),
              check("CI", conclusion="success", started="2026-10-26T10:00:00Z")]
    r = run([make_pr(5, checks=checks)])
    assert r.merged == [5], r


@case('[ "$bad" != 0 ] || ', '')
def other_status_not_green_not_merged(run):
    st = make_pr(5)["statuses"] + [status("ci/vercel", "pending", "vercel[bot]")]
    r = run([make_pr(5, statuses=st)])
    assert_not_merged(r, says="#5: not clean yet")


@case('map(sort_by(.created_at) | last)', 'map(sort_by(.created_at) | first)')
def latest_status_per_context_counts(run):
    st = [status("CodeRabbit", "pending", "coderabbitai[bot]", "Review in progress", "2026-10-26T09:00:00Z"),
          status("CodeRabbit", "success", "coderabbitai[bot]", "Review completed", "2026-10-26T10:00:00Z")]
    r = run([make_pr(5, statuses=st)])
    assert r.merged == [5], r


@case('if [ "$(jq -r .mergeable_state <<<"$j")" = "dirty" ]', 'if [ "$(jq -r .mergeable_state <<<"$j")" = "never" ]')
def merge_conflicts_not_merged(run):
    r = run([make_pr(5, mergeable_state="dirty")])
    assert_not_merged(r, says="#5: merge conflicts")


# Was it really reviewed?

@case('*"Review completed"*) reviewed=yes', '*) reviewed=yes')
def coderabbit_review_skipped_is_not_a_review(run):
    st = [status("CodeRabbit", "success", "coderabbitai[bot]", "Review skipped")]
    r = run([make_pr(5, statuses=st)])
    assert_not_merged(r, says="reviewed=no")


@case('case "$cr" in "success coderabbitai[bot] "', 'case "$cr" in "success "*" "')
def coderabbit_status_from_another_user_is_not_a_review(run):
    st = [status("CodeRabbit", "success", "mallory", "Review completed")]
    r = run([make_pr(5, statuses=st)])
    assert_not_merged(r, says="reviewed=no")


@case('[ "$fo" = "success $OWNER" ] && reviewed=yes', 'true')
def owner_backup_review_success_counts(run):
    st = [status("review-failover", "success", OWNER, "no critical/high findings")]
    r = run([make_pr(5, statuses=st)])
    assert r.merged == [5], r


@case('[ "$fo" = "success $OWNER" ]', '[ "${fo%% *}" = success ]')
def backup_review_from_another_user_is_not_a_review(run):
    st = [status("review-failover", "success", "mallory", "looks fine")]
    r = run([make_pr(5, statuses=st)])
    assert_not_merged(r, says="reviewed=no")


@case(' || [ "$fo_open" = yes ]', '')
def backup_review_failure_blocks_after_coderabbit_completed(run):
    st = [status("CodeRabbit", "success", "coderabbitai[bot]", "Review completed"),
          status("review-failover", "failure", OWNER, "2 high findings")]
    r = run([make_pr(5, statuses=st)])
    assert_not_merged(r, says="#5: not clean yet")


@case('[ "$changes" != 0 ] || ', '')
def changes_requested_with_null_review_decision_blocks(run):
    r = run([make_pr(5, decision=None, opinions=["APPROVED", "CHANGES_REQUESTED"])])
    assert_not_merged(r, says="decision=NONE, changes requested=1")


@case('[ "$decision" = CHANGES_REQUESTED ] || ', '')
def review_decision_changes_requested_blocks(run):
    r = run([make_pr(5, decision="CHANGES_REQUESTED")])
    assert_not_merged(r, says="#5: not clean yet")


@case(' || [ "$unresolved" != 0 ]', '')
def unresolved_thread_blocks(run):
    r = run([make_pr(5, threads=[True, False])])
    assert_not_merged(r, says="unresolved threads=1")


@case('[ "$more" != false ] || ', '')
def more_than_100_threads_blocks(run):
    r = run([make_pr(5, threads=[True] * 100, threads_more=True)])
    assert_not_merged(r, says="(more=true)")


# Reads that fail turn the run red

@case('|| { echo "could not read the default branch; merging nothing"; exit 1; }', '|| true')
def default_branch_read_failure_exits_1(run):
    r = run([make_pr(5)], overrides={f"repos/{REPO}": {"error": "gh: Bad credentials (HTTP 401)"}})
    assert_not_merged(r, rc=1, says="could not read the default branch")


@case('echo "could not list open PRs; merging nothing"; exit 1', 'echo "could not list open PRs; merging nothing"; exit 0')
def pr_list_read_failure_exits_1(run):
    r = run([make_pr(5)], overrides={f"repos/{REPO}/pulls": {"error": "gh: API rate limit exceeded (HTTP 403)"}})
    assert_not_merged(r, rc=1, says="could not list open PRs")


def _one_read_fails(run, route):
    prs = [make_pr(3), make_pr(7)]
    r = run(prs, overrides={route.format(n=3, sha=prs[0]["sha"]): {"error": "gh: Server Error (HTTP 502)"}})
    assert r.rc == 1, r
    assert r.merged == [7], r
    assert "some PRs could not be read" in r.out, r


@case('echo "#$pr: could not read the PR"; read_failed=1;', 'echo "#$pr: could not read the PR";')
def per_pr_read_failure_exits_1(run):
    _one_read_fails(run, "repos/" + REPO + "/pulls/{n}")


@case('echo "#$pr: could not read the file list"; read_failed=1;', 'echo "#$pr: could not read the file list";')
def file_list_read_failure_exits_1(run):
    _one_read_fails(run, "repos/" + REPO + "/pulls/{n}/files")


@case('echo "#$pr: could not read checks"; read_failed=1;', 'echo "#$pr: could not read checks";')
def check_runs_read_failure_exits_1(run):
    _one_read_fails(run, "repos/" + REPO + "/commits/{sha}/check-runs")


@case('echo "#$pr: could not read statuses"; read_failed=1;', 'echo "#$pr: could not read statuses";')
def statuses_read_failure_exits_1(run):
    _one_read_fails(run, "repos/" + REPO + "/commits/{sha}/statuses")


@case('echo "#$pr: could not read reviews"; read_failed=1;', 'echo "#$pr: could not read reviews";')
def reviews_read_failure_exits_1(run):
    _one_read_fails(run, "graphql:{n}")


@case('&& echo "#$pr: merged" || echo "#$pr: GitHub refused the merge"', '; echo "#$pr: merged"')
def refused_merge_is_reported(run):
    r = run([make_pr(5)], overrides={"merge:5": {"error": "GraphQL: Head branch was modified"}})
    assert r.rc == 0, r
    assert "#5: GitHub refused the merge" in r.out, r
    assert "#5: merged" not in r.out, r


# ---------- runners ----------

@pytest.mark.parametrize("name", list(CASES))
def test_case(name, tmp_path):
    fn, _, _ = CASES[name]
    fn(Runner(tmp_path, SCRIPT))


@pytest.mark.parametrize("name", list(CASES))
def test_case_catches_mutant(name, tmp_path):
    fn, old, new = CASES[name]
    assert SCRIPT.count(old) == 1, f"mutant for {name} must match the script exactly once: {old!r}"
    with pytest.raises(AssertionError):
        fn(Runner(tmp_path, SCRIPT.replace(old, new)))


def test_workflow_is_reusable_and_pins_nothing_from_the_caller():
    text = WORKFLOW.read_text()
    assert "\n  workflow_call:\n" in text
    assert "runs-on: ${{ fromJSON(inputs.runs-on) }}" in text
    assert "default: '\"ubuntu-latest\"'" in text
    # The script is inline: no checkout, no `uses:` step, so callers need nothing else.
    job = text.split("\njobs:", 1)[1].split("run: |", 1)[0]
    assert not re.search(r"^\s*(-\s*)?uses:", job, re.M)


def test_script_matches_template_v7_except_known_changes():
    """The logic is template v7; only the own-check filter and the protected path list changed.

    v1.0.1 added the zizmor config to the protected paths."""
    template = HERE / "fixtures/merge-when-clean-v7.yml"
    v7 = extract_script(template.read_text())
    v7 = v7.replace('select(.name!="Merge when clean")',
                    'select(.name!="Merge when clean" and (.name | endswith(" / Merge when clean") | not))')
    v7 = v7.replace(r'^\.github/workflows/merge-when-clean\.yml$|',
                    r'^\.github/workflows/(reusable-)?merge-when-clean\.yml$|')
    v7 = v7.replace(r'|^\.github/merge-hold-paths$' + "'",
                    r'|^\.github/merge-hold-paths$|^(\.github/)?zizmor\.ya?ml$' + "'")
    assert v7 == SCRIPT
