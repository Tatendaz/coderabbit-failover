#!/usr/bin/env python3
"""
Probe one pull request for CodeRabbit (@coderabbitai) review state and recommend a
next action for the post-PR review loop.

This is READ-ONLY against GitHub: it calls `gh api` to fetch comments/reviews and
prints a JSON status report. It never posts, edits, or merges anything — the skill's
agent does any posting via explicit `gh` commands. Local exception: it syncs parsed
rate-limit windows with the GLOBAL budget ledger (~/.claude/coderabbit/ledger.json,
see coderabbit_budget.py) — CodeRabbit's review pool is per developer across ALL
repos/PRs, so a window seen on any PR applies everywhere and vice versa.

Used by the coderabbit-failover skill's "post-PR review loop" so the polling/detection
logic is deterministic across sessions and doesn't get re-derived in chat. See
references/coderabbit-loop.md for how the output drives the loop.

Usage:
    python scripts/coderabbit_status.py --repo OWNER/REPO --pr NUMBER [--since ISO8601]
                                        [--bot-login coderabbit]
                                        [--no-activity-minutes 15]
                                        [--watch] [--watch-interval-seconds 75]
                                        [--watch-min-minutes 15] [--watch-grace-minutes 3]
                                        [--watch-max-minutes 30]

    python scripts/coderabbit_status.py --selftest   # validate decision logic offline

`--watch` moves the loop's polling INSIDE this script: it re-probes every interval and
exits only when the agent must act (`address`/`resume`/`review`), the PR is clean
(`done`, with the 15-min stale floor and 3-min fresh grace enforced internally), the
bot is silent (`no_activity`), or the 30-min ceiling is hit. Launch it with the Bash
tool's `run_in_background: true` so the session is free to do other work while it
watches — the process exiting is the signal to come back and handle the result.
Progress lines go to stderr; the final status JSON is the last thing on stdout.

Output: a JSON object on stdout (see references/coderabbit-loop.md for the schema and
the meaning of `action`; watch mode adds `watch_result` + `watch_elapsed_minutes`).
Exit codes:
    0  status probed successfully (read the `action` field for what to do)
    2  could not probe (gh missing, not authenticated, or API error) — see `error`
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# --- patterns -----------------------------------------------------------------

ACTIONABLE_RE = re.compile(r"actionable comments posted:\s*(\d+)", re.IGNORECASE)

# Clean-run variant (seen live 2026-07-02): when a review produces nothing, CodeRabbit
# edits the walkthrough to "No actionable comments were generated in the recent
# review. 🎉" and posts NO review object and NO numeric tally — treat it as tally 0,
# or the loop never reaches `done` on a clean PR.
CLEAN_NO_ACTIONABLE_RE = re.compile(
    r"no actionable comments were generated", re.IGNORECASE
)


def parse_actionable(body: str) -> int | None:
    """Actionable-comment count asserted by a body, or None if it asserts none."""
    m = ACTIONABLE_RE.search(body or "")
    if m:
        return int(m.group(1))
    if CLEAN_NO_ACTIONABLE_RE.search(body or ""):
        return 0
    return None

# Auto-review turned off for the whole repo -> `resume` won't help; need `review`.
DISABLED_RE = re.compile(
    r"auto[\s-]*reviews? (are|is)? ?disabled"
    r"|to trigger a single review, invoke the @?coderabbitai review",
    re.IGNORECASE,
)

# Reviews paused on this PR (manual pause or rate-limit) -> `resume`.
PAUSED_RE = re.compile(
    r"reviews?\s+(are\s+)?paused"
    r"|paused\s+reviews?"
    r"|review\s+paused",
    re.IGNORECASE,
)

# Every `@coderabbitai review` acknowledgment carries the boilerplate "This command
# is applicable only when automatic reviews are paused." — which PAUSED_RE matches,
# making a normal command ack read as a live pause (seen on Quant_Backtest_Platform
# PR #47: the loop kept recommending `resume` on an unpaused PR). Strip it first.
PAUSE_BOILERPLATE_RE = re.compile(
    r"applicable\s+only\s+when\s+automatic\s+reviews\s+are\s+paused",
    re.IGNORECASE,
)


def pause_asserted(body: str) -> bool:
    """True if a body asserts a live pause, ignoring command-ack boilerplate."""
    return bool(PAUSED_RE.search(PAUSE_BOILERPLATE_RE.sub("", body or "")))

# Generic "review skipped" — treat like disabled (needs an explicit review trigger).
SKIPPED_RE = re.compile(r"review\s+skipped", re.IGNORECASE)

# CodeRabbit's review budget hit. Posting `@coderabbitai resume`/`review` before the
# window frees just bounces, so the loop should wait it out instead. Live specimen
# (Quant_Backtest_Platform PR #47, 2026-07): the bot EDITS its comment in place to a
# "## Review limit reached" notice carrying the HTML marker "rate limited by
# coderabbit.ai" and the wait as "**Next review available in:** **21 minutes**".
# Older/documented phrasings ("Rate limit exceeded ... please wait 17 minutes and
# 42 seconds") are kept as fallbacks.
RATE_LIMIT_RE = re.compile(
    r"rate\s*limited\s+by\s+coderabbit"          # HTML marker in the edited comment
    r"|review\s+rate\s+limited"                  # command reply "⚠️ Action not completed" (live 2026-09-16)
    r"|review\s+limit\s+reached"                 # current notice heading
    r"|review\s+limit\s+is\s+currently\s+reached"  # command-reply variant (live 2026-07-26)
    r"|rate\s*limit\s*(?:exceeded|reached|hit)"
    r"|exceeded\s+the\s+rate\s+limit"
    r"|too\s+many\s+requests",
    re.IGNORECASE,
)
# Wait extraction: anchor on "wait"/"available in", tolerate markdown bold/colon
# noise (\W*), and require at least one numeric group to count (a bare "wait until
# the next included review" must not parse as zero wait).
RATE_WAIT_RE = re.compile(
    r"(?:wait|available\s+in)\W*(?:(\d+)\s*hours?)?\W*(?:and\s+)?"
    r"(?:(\d+)\s*min(?:ute)?s?)?\W*(?:and\s+)?(?:(\d+)\s*sec(?:ond)?s?)?",
    re.IGNORECASE,
)


def parse_rate_wait(body: str) -> tuple[int, int, int] | None:
    """First (hours, minutes, seconds) with any digits from a rate-limit body."""
    for m in RATE_WAIT_RE.finditer(body or ""):
        if any(m.groups()):
            h, mi, s = (int(g) if g else 0 for g in m.groups())
            return h, mi, s
    return None

# A human/agent command (NOT the bot) re-enabling review. These supersede an
# earlier pause/disabled signal so we don't recommend resuming what's already
# been resumed. Matched only against non-bot comments (CodeRabbit's own disabled
# notice contains "@coderabbitai review", which must not count as a request).
RESUME_CMD_RE = re.compile(r"@?coderabbit(?:ai)?\s+resume", re.IGNORECASE)
REVIEW_CMD_RE = re.compile(r"@?coderabbit(?:ai)?\s+(?:full\s+)?review", re.IGNORECASE)


# --- time helpers -------------------------------------------------------------

def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def newer(ts: datetime | None, ref: datetime | None) -> bool:
    """True if ts is strictly after ref. Unknown ts never counts as newer."""
    if ts is None or ref is None:
        return False
    return ts > ref


def active_signal(signal_ts: datetime | None, override_ts: datetime | None) -> bool:
    """True if a signal (e.g. a pause notice) is present and not superseded.

    `signal_ts` is when the pause/disabled state was last asserted; `override_ts`
    is when it was last countered (a `@coderabbitai resume` / `review` command).
    The signal is active iff it exists and nothing later overrode it.
    """
    if signal_ts is None:
        return False
    if override_ts is None:
        return True
    return signal_ts > override_ts


# --- gh plumbing --------------------------------------------------------------

def gh_json(path: str) -> object:
    """GET a gh api path with pagination; return parsed JSON. Raises RuntimeError."""
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", path],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise RuntimeError("gh CLI not found — install it and run `gh auth login`") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "unknown gh error").strip()
        raise RuntimeError(f"gh api {path} failed: {msg}")
    out = proc.stdout.strip()
    if not out:
        return []
    # With --paginate, multiple JSON arrays can be concatenated; join them.
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        merged: list = []
        decoder = json.JSONDecoder()
        idx = 0
        n = len(out)
        while idx < n:
            while idx < n and out[idx].isspace():
                idx += 1
            if idx >= n:
                break
            obj, end = decoder.raw_decode(out, idx)
            if isinstance(obj, list):
                merged.extend(obj)
            else:
                merged.append(obj)
            idx = end
        return merged


def author_login(item: dict) -> str:
    user = item.get("user") or {}
    return (user.get("login") or "").lower()


def gh_graphql(query: str, **variables: object) -> dict:
    """Run a GraphQL query via `gh api graphql`; return the parsed `data` object.

    Raises RuntimeError on transport / auth / GraphQL errors so callers can degrade
    gracefully rather than crash the whole probe.
    """
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        if val is None:
            continue  # omit nullable vars (e.g. first-page cursor)
        if isinstance(val, bool):
            cmd += ["-F", f"{key}={'true' if val else 'false'}"]
        elif isinstance(val, int):
            cmd += ["-F", f"{key}={val}"]
        else:
            cmd += ["-f", f"{key}={val}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not found — install it and run `gh auth login`") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "unknown gh error").strip()
        raise RuntimeError(f"gh api graphql failed: {msg}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api graphql returned non-JSON: {exc}") from exc
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'])}")
    return payload.get("data") or {}


FAILOVER_MARK = "review-failover"   # footer/marker text in backup-review comments

_THREADS_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          path
          line
          comments(first:1) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""


def count_unresolved_threads(repo: str, pr: int, bot_match: str) -> tuple[int, list]:
    """Count CodeRabbit-authored review threads still marked unresolved.

    The REST comments endpoint does NOT expose a thread's resolution state, so a
    PR can read "0 actionable comments" while several conversations sit unresolved
    in the GitHub UI. This queries GraphQL `reviewThreads.isResolved` to close that
    blind spot. Returns (count, detail) where detail is a list of {path, line} for
    each unresolved bot thread. Raises RuntimeError on API failure.
    """
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise RuntimeError(f"--repo must be OWNER/REPO, got {repo!r}") from exc

    bm = bot_match.lower()
    unresolved: list = []
    cursor = None
    while True:
        data = gh_graphql(_THREADS_QUERY, owner=owner, name=name, number=pr, cursor=cursor)
        threads = (((data.get("repository") or {}).get("pullRequest") or {})
                   .get("reviewThreads") or {})
        for node in threads.get("nodes") or []:
            if node.get("isResolved"):
                continue
            comments = (node.get("comments") or {}).get("nodes") or []
            author = ((comments[0].get("author") or {}).get("login") or "").lower() if comments else ""
            first_body = (comments[0].get("body") or "") if comments else ""
            # Backup-review (review-failover) threads count like CodeRabbit's: they are
            # posted by github-actions or the owner, so match the footer marker instead.
            if bm in author or FAILOVER_MARK in first_body:
                unresolved.append({"path": node.get("path"), "line": node.get("line")})
        page = threads.get("pageInfo") or {}
        if page.get("hasNextPage"):
            cursor = page.get("endCursor")
        else:
            break
    return len(unresolved), unresolved


# --- core: gather raw state, then decide --------------------------------------

def gather(repo: str, pr: int, bot_match: str) -> dict:
    """Fetch PR + comments + reviews + inline comments; reduce to a plain state dict.

    Separated from decide() so the decision logic can be unit-tested offline.
    """
    pr_meta = gh_json(f"repos/{repo}/pulls/{pr}")
    if not isinstance(pr_meta, dict):
        raise RuntimeError(f"unexpected response for PR {pr}")

    # HEAD commit of the PR — the anchor for "is the clean verdict for the CURRENT
    # commit?". A clean *review* carries the commit_id it reviewed (compared to this
    # sha); an edited-in-place summary comment has no commit_id, so we instead check
    # it was edited after HEAD was pushed — for which we need HEAD's commit time.
    head_sha = ((pr_meta.get("head") or {}).get("sha")) or None
    head_committed_at = None
    if head_sha:
        try:
            head_commit = gh_json(f"repos/{repo}/commits/{head_sha}")
            if isinstance(head_commit, dict):
                head_committed_at = (((head_commit.get("commit") or {})
                                      .get("committer") or {}).get("date"))
        except RuntimeError:
            head_committed_at = None  # degrade: freshness falls back to commit_id only

    # Backup review (review-failover): its latest commit status on HEAD, if any.
    failover_state = None
    if head_sha:
        try:
            for st_ in gh_json(f"repos/{repo}/commits/{head_sha}/statuses") or []:
                if isinstance(st_, dict) and st_.get("context") == "review-failover":
                    failover_state = st_.get("state")  # newest first
                    break
        except RuntimeError:
            failover_state = None

    issue_comments = gh_json(f"repos/{repo}/issues/{pr}/comments") or []
    reviews = gh_json(f"repos/{repo}/pulls/{pr}/reviews") or []
    inline = gh_json(f"repos/{repo}/pulls/{pr}/comments") or []

    bm = bot_match.lower()

    def is_bot(item: dict) -> bool:
        return bm in author_login(item)

    bot_comments = [c for c in issue_comments if is_bot(c)]
    bot_reviews = [r for r in reviews if is_bot(r)]
    bot_inline = [c for c in inline if is_bot(c)]

    # The single most recent bot conversation comment, for pause/disable detection.
    def ts_of(item: dict, *keys: str) -> datetime | None:
        for k in keys:
            t = parse_ts(item.get(k))
            if t:
                return t
        return None

    def ts_eff(item: dict) -> datetime | None:
        # Prefer last-edit time: CodeRabbit edits its summary comment in place each
        # review, so created_at would freeze it at first-post time and miss updates.
        return ts_of(item, "updated_at", "created_at")

    bot_comments_sorted = sorted(
        bot_comments, key=lambda c: ts_of(c, "created_at") or datetime.min.replace(tzinfo=timezone.utc)
    )
    bot_reviews_sorted = sorted(
        bot_reviews, key=lambda r: ts_of(r, "submitted_at", "created_at") or datetime.min.replace(tzinfo=timezone.utc)
    )

    latest_comment_body = bot_comments_sorted[-1]["body"] if bot_comments_sorted else ""
    latest_comment_body = latest_comment_body or ""

    latest_review = bot_reviews_sorted[-1] if bot_reviews_sorted else None
    latest_review_state = (latest_review.get("state") if latest_review else None)

    # Parse "Actionable comments posted: N" from the most recent place it appears
    # (CodeRabbit puts it in the walkthrough comment and/or the review body).
    actionable = None
    actionable_ts = None
    for item, *keys in (
        *[(c, "updated_at", "created_at") for c in bot_comments_sorted],
        *[(r, "submitted_at", "created_at") for r in bot_reviews_sorted],
    ):
        count = parse_actionable(item.get("body") or "")
        if count is not None:
            t = ts_of(item, *keys)
            if actionable_ts is None or (t and t >= actionable_ts):
                actionable = count
                actionable_ts = t

    # Edit time of the most recent bot ISSUE comment asserting a clean tally
    # ("Actionable comments posted: 0"). Confirms an edited-in-place summary
    # reflects the current HEAD (its edit landed after HEAD was pushed). Scans
    # every bot comment, not just the latest — the summary is usually the FIRST.
    clean_comment_ts = None
    for c in bot_comments_sorted:
        if parse_actionable(c.get("body") or "") == 0:
            t = ts_eff(c)
            if t and (clean_comment_ts is None or t > clean_comment_ts):
                clean_comment_ts = t

    # Max timestamp of any bot activity seen.
    all_bot_ts = [t for t in (
        [ts_of(c, "created_at") for c in bot_comments]
        + [ts_of(r, "submitted_at", "created_at") for r in bot_reviews]
        + [ts_of(c, "created_at") for c in bot_inline]
    ) if t]
    max_ts = max(all_bot_ts) if all_bot_ts else None

    # Unresolved review threads (GraphQL — REST can't see resolution state). Degrade
    # to "unknown" on failure (e.g. token scope) rather than crashing the probe.
    unresolved_threads = None
    unresolved_detail: list = []
    unresolved_error = None
    try:
        unresolved_threads, unresolved_detail = count_unresolved_threads(repo, pr, bot_match)
    except RuntimeError as exc:
        unresolved_error = str(exc)

    # Pause / disabled detection across ALL comments, not just the latest one: a
    # pause notice can be buried under later comments, so scanning only the newest
    # comment misses it. CodeRabbit's (bot) comments assert pause/disabled; non-bot
    # commands (`@coderabbitai resume` / `review`) counter them. A signal counts as
    # active only if nothing later superseded it.
    def max_ts_where(items, matcher, ts_keys, want_bot):
        best = None
        for it in items:
            if is_bot(it) != want_bot:
                continue
            if matcher(it.get("body") or ""):
                t = ts_of(it, *ts_keys)
                if t and (best is None or t > best):
                    best = t
        return best

    ic_k = ("created_at",)
    rv_k = ("submitted_at", "created_at")
    pause_ts = max(filter(None, [
        max_ts_where(issue_comments, pause_asserted, ic_k, True),
        max_ts_where(reviews, pause_asserted, rv_k, True),
        max_ts_where(inline, pause_asserted, ic_k, True),
    ]), default=None)
    disabled_ts = max(filter(None, [
        max_ts_where(issue_comments, DISABLED_RE.search, ic_k, True),
        max_ts_where(issue_comments, SKIPPED_RE.search, ic_k, True),
        max_ts_where(reviews, DISABLED_RE.search, rv_k, True),
        max_ts_where(reviews, SKIPPED_RE.search, rv_k, True),
        max_ts_where(inline, DISABLED_RE.search, ic_k, True),
    ]), default=None)
    resume_ts = max(filter(None, [
        max_ts_where(issue_comments, RESUME_CMD_RE.search, ic_k, False),
        max_ts_where(inline, RESUME_CMD_RE.search, ic_k, False),
    ]), default=None)
    review_cmd_ts = max(filter(None, [
        max_ts_where(issue_comments, REVIEW_CMD_RE.search, ic_k, False),
        max_ts_where(inline, REVIEW_CMD_RE.search, ic_k, False),
    ]), default=None)

    # Rate-limit notice: latest bot comment/review saying the hourly budget is hit,
    # plus the "wait N minutes and M seconds" it usually contains. Self-expiring —
    # decide() treats it as active only while now < rate_limited_until, so no
    # supersession bookkeeping is needed.
    rate_limit_notice_ts = None
    rate_limited_until = None
    for items, keys in ((issue_comments, ("updated_at", "created_at")),
                        (reviews, ("submitted_at", "created_at")),
                        (inline, ("updated_at", "created_at"))):
        for it in items:
            if not is_bot(it):
                continue
            body = it.get("body") or ""
            if not RATE_LIMIT_RE.search(body):
                continue
            t = ts_of(it, *keys)
            if t is None or (rate_limit_notice_ts and t <= rate_limit_notice_ts):
                continue
            rate_limit_notice_ts = t
            rate_limited_until = None
            wait = parse_rate_wait(body)
            if wait:
                h, mi, s = wait
                rate_limited_until = t + timedelta(hours=h, minutes=mi, seconds=s)

    # Sync with the GLOBAL budget ledger: CodeRabbit's review pool is per developer,
    # so a window reported on any PR applies to every PR in every repo. Push what
    # this PR's notice says into the ledger, then adopt the ledger's window when it
    # is later than (or absent from) this PR's own comments. Best-effort: the probe
    # must keep working even if the ledger module or file is unavailable.
    rate_limit_source = "notice" if rate_limited_until else None
    try:
        import coderabbit_budget as _budget
        _now = datetime.now(timezone.utc)
        with _budget.locked_ledger() as _led:
            if rate_limited_until and rate_limited_until > _now:
                _budget.apply_limit(_led, "pr", rate_limited_until, f"{repo}#{pr}", _now)
            _g = _budget.parse_ts(((_led.get("cooldowns") or {}).get("pr") or {}).get("until"))
        if _g and _g > _now and (rate_limited_until is None or _g > rate_limited_until):
            rate_limited_until = _g
            rate_limit_source = "ledger"
    except Exception:
        pass

    return {
        "pr_created_at": pr_meta.get("created_at"),
        "pr_state": pr_meta.get("state"),
        "head_sha": head_sha,
        "head_committed_at": head_committed_at,
        "failover_state": failover_state,
        "clean_comment_ts": clean_comment_ts.isoformat() if clean_comment_ts else None,
        "unresolved_threads": unresolved_threads,
        "unresolved_thread_detail": unresolved_detail,
        "unresolved_threads_error": unresolved_error,
        "paused": active_signal(pause_ts, resume_ts),
        "auto_review_disabled": active_signal(disabled_ts, review_cmd_ts),
        "pause_ts": pause_ts.isoformat() if pause_ts else None,
        "resume_request_ts": resume_ts.isoformat() if resume_ts else None,
        "review_request_ts": review_cmd_ts.isoformat() if review_cmd_ts else None,
        "rate_limit_notice_ts": rate_limit_notice_ts.isoformat() if rate_limit_notice_ts else None,
        "rate_limited_until": rate_limited_until.isoformat() if rate_limited_until else None,
        "rate_limit_source": rate_limit_source,
        "coderabbit_seen": bool(bot_comments or bot_reviews or bot_inline),
        "bot_inline_ts": [ts_of(c, "created_at").isoformat() for c in bot_inline if ts_of(c, "created_at")],
        "bot_review_events": [
            {
                "state": r.get("state"),
                "ts": (ts_of(r, "submitted_at", "created_at").isoformat()
                       if ts_of(r, "submitted_at", "created_at") else None),
                "actionable": (int(ACTIONABLE_RE.search(r.get("body") or "").group(1))
                               if ACTIONABLE_RE.search(r.get("body") or "") else None),
                "commit_id": r.get("commit_id"),
            }
            for r in bot_reviews_sorted
        ],
        "latest_review_state": latest_review_state,
        "latest_comment_body": latest_comment_body,
        "actionable_comments": actionable,
        "actionable_ts": actionable_ts.isoformat() if actionable_ts else None,
        "max_timestamp": max_ts.isoformat() if max_ts else None,
    }


def decide(state: dict, since: datetime | None, now: datetime, no_activity_minutes: float) -> dict:
    """Pure decision logic over a gathered state dict. No I/O — unit-testable."""
    created = parse_ts(state.get("pr_created_at"))
    since_eff = since or created
    max_ts = parse_ts(state.get("max_timestamp"))

    # New inline comments from the bot strictly after `since_eff`.
    new_inline = sum(
        1 for iso in state.get("bot_inline_ts", []) if newer(parse_ts(iso), since_eff)
    )
    # New bot reviews after `since_eff`, and whether any reported >0 actionable.
    new_reviews = 0
    new_reviews_actionable = 0
    for ev in state.get("bot_review_events", []):
        if newer(parse_ts(ev.get("ts")), since_eff):
            new_reviews += 1
            if (ev.get("actionable") or 0) > 0:
                new_reviews_actionable += 1

    # Pause/disabled are now computed in gather() across ALL comments (with
    # resume/review supersession), not regexed from the latest comment here.
    auto_disabled = bool(state.get("auto_review_disabled"))
    paused = bool(state.get("paused")) and not auto_disabled

    actionable = state.get("actionable_comments")
    unresolved = state.get("unresolved_threads")   # int, or None when GraphQL unknown
    approved = state.get("latest_review_state") == "APPROVED"
    # "Clean" is driven by unresolved threads when we know them: CodeRabbit's
    # actionable inline comments ARE its review threads, so 0 unresolved means they
    # were all handled — even if an older summary's "Actionable comments posted: N"
    # tally is stale and never got rewritten to 0 (which would otherwise pin the
    # loop at `wait` forever). We still require a review *verdict* to exist (a tally
    # was posted at some point, or APPROVED) so we don't call a PR clean while
    # CodeRabbit is mid-review and simply hasn't created its threads yet. When the
    # GraphQL thread count is unknown, fall back to the tally / approval.
    verdict_exists = (actionable is not None) or approved
    if unresolved is None:
        looks_clean = (actionable == 0) or approved
    else:
        looks_clean = verdict_exists and (unresolved == 0)

    # Freshness: does the clean verdict cover the CURRENT head commit? A bare `done`
    # can reflect a review of an EARLIER push (CodeRabbit hasn't re-reviewed the
    # latest commit yet) — the 15-min floor exists for exactly that. When the clean
    # verdict provably covers HEAD, the loop may conclude early (after a short grace).
    # Two independent proofs, drawn from ALL reviews/comments (not just the latest):
    #   (a) a review event whose commit_id == head_sha — combined with looks_clean
    #       (0 unresolved threads + a verdict on record) that means HEAD was reviewed
    #       and is clean, even when a clean RE-review omits the "Actionable: 0" tally
    #       (its review event then has actionable=None); or
    #   (b) an edited-in-place summary ("Actionable: 0") updated at/after the time
    #       HEAD was pushed (head_committed_at).
    # When neither holds (head info missing, or the clean signal has no commit_id and
    # predates HEAD), clean_is_fresh is False and the 15-min floor still applies.
    head_sha = state.get("head_sha")
    head_committed_at = parse_ts(state.get("head_committed_at"))
    reviewed_head = bool(head_sha) and any(
        ev.get("commit_id") == head_sha for ev in state.get("bot_review_events", [])
    )
    cct = parse_ts(state.get("clean_comment_ts"))
    summary_fresh = bool(cct and head_committed_at and cct >= head_committed_at)
    clean_is_fresh = bool(looks_clean and (reviewed_head or summary_fresh))

    # minutes since the PR opened (independent of `since`) — for the silence timer.
    ref_for_silence = created or since_eff or now
    minutes_since_pr = (now - ref_for_silence).total_seconds() / 60.0 if ref_for_silence else 0.0

    if not state.get("coderabbit_seen"):
        action = "no_activity" if minutes_since_pr >= no_activity_minutes else "wait"
        reason = ("no CodeRabbit activity on this PR after "
                  f"{minutes_since_pr:.1f} min" if action == "no_activity"
                  else "waiting for CodeRabbit's first review")
    elif auto_disabled:
        action = "review"
        reason = "auto-review disabled for this repo — trigger a one-off review"
    elif paused:
        action = "resume"
        reason = "CodeRabbit reviews appear paused on this PR"
    elif new_inline > 0 or new_reviews_actionable > 0:
        action = "address"
        reason = (f"{new_inline} new inline comment(s)"
                  + (f" and {new_reviews_actionable} new review(s) with actionable items"
                     if new_reviews_actionable else "")
                  + " since last check")
    elif isinstance(unresolved, int) and unresolved > 0:
        # No *new* activity, but conversations are still open. The loop isn't done
        # until each is fixed-or-replied AND resolved (resolveReviewThread).
        action = "address"
        reason = (f"{unresolved} unresolved CodeRabbit review thread(s) still open — "
                  "fix or reply, then resolve each thread")
    elif looks_clean:
        action = "done"
        reason = ("review approved, no unresolved review threads" if approved
                  else "no unresolved review threads and no new actionable comments")
    else:
        action = "wait"
        reason = "CodeRabbit active but nothing new actionable yet"

    # Rate-limited: a `resume`/`review` posted before the window frees just bounces
    # off the same limit, so downgrade to `wait` until rate_limited_until passes.
    # (Watch mode naps until then instead of burning polls.) `address`/`done` are
    # unaffected — fixing code or concluding never needs CodeRabbit's budget.
    rl_until = parse_ts(state.get("rate_limited_until"))
    rate_limited = bool(rl_until and rl_until > now)
    if rate_limited and action in ("resume", "review"):
        action = "wait"
        reason = (f"CodeRabbit rate-limited until {rl_until.isoformat()} — "
                  "resume/review would bounce; wait for the window to free")

    # The flip side: the window has FREED, and the notice says reviews do NOT
    # auto-resume ("After more reviews become available, a review can be triggered
    # using the @coderabbitai review command"). If no bot activity landed after the
    # window and nobody already re-triggered, recommend the one-off review — else
    # the PR sits at `wait` forever.
    if action == "wait" and rl_until and now >= rl_until:
        last_bot = parse_ts(state.get("max_timestamp"))
        req = parse_ts(state.get("review_request_ts"))
        if (last_bot is None or last_bot <= rl_until) and not (req and req > rl_until):
            action = "review"
            reason = ("CodeRabbit's rate-limit window has freed with no review since — "
                      "re-trigger with @coderabbitai review")

    # Backup review (review-failover, reviewer/local_reviewer.py). CodeRabbit on
    # hold must never leave a PR waiting: its status on HEAD stands in for CodeRabbit's
    # verdict (merge gate 2 in ~/.claude/CLAUDE.md). `address` always wins: open threads
    # (CodeRabbit's or the backup review's) must be handled first.
    fo = state.get("failover_state")
    # A notice with no stated wait ("Review rate limited.") sets no window, but if it
    # arrived after HEAD was pushed and CodeRabbit has not reviewed HEAD, HEAD is stuck.
    notice_ts = parse_ts(state.get("rate_limit_notice_ts"))
    limited_on_head = bool(notice_ts and head_committed_at and notice_ts >= head_committed_at
                           and not reviewed_head)
    if action != "address":
        if fo == "success":
            action = "done"
            reason = "backup review (review-failover) passed on this head; no unresolved threads"
            looks_clean = True
            clean_is_fresh = True
        elif fo == "failure":
            action = "address"
            reason = ("backup review (review-failover) found critical/high problems on this "
                      "head: fix, reply, resolve its threads, push")
        elif fo == "pending":
            action = "wait"
            reason = "backup review (review-failover) is running on this head"
        elif ((fo == "error" or rate_limited or limited_on_head)
              and action in ("wait", "review", "resume", "no_activity")):
            action = "failover"
            reason = ("CodeRabbit is rate-limited or on hold: start the backup review with "
                      "coderabbit_budget.py failover --repo <r> --pr <n>")

    return {
        "failover_state": fo,
        "coderabbit_seen": state.get("coderabbit_seen"),
        "new_inline_comments": new_inline,
        "new_reviews": new_reviews,
        "latest_review_state": state.get("latest_review_state"),
        "actionable_comments": actionable,
        "unresolved_threads": unresolved,
        "unresolved_thread_detail": state.get("unresolved_thread_detail", []),
        "unresolved_threads_error": state.get("unresolved_threads_error"),
        "paused": paused,
        "auto_review_disabled": auto_disabled,
        "rate_limited": rate_limited,
        "rate_limited_until": state.get("rate_limited_until"),
        "rate_limit_source": state.get("rate_limit_source"),
        "looks_clean": looks_clean,
        "clean_is_fresh": clean_is_fresh,
        "head_sha": state.get("head_sha"),
        "max_timestamp": state.get("max_timestamp"),
        "minutes_since_pr": round(minutes_since_pr, 1),
        "action": action,
        "reason": reason,
    }


# --- watch mode: poll internally, exit only when the agent must act ------------

def watch_step(result: dict, elapsed_min: float, clean_since_min: float | None,
               min_watch: float, grace: float, max_budget: float,
               rl_remaining_min: float | None = None) -> tuple[str, str | None, float | None]:
    """Decide whether one watch iteration exits or keeps polling. Pure — testable.

    Encodes the loop's time rules (see references/coderabbit-loop.md): exit at once
    on anything the agent must handle; a FRESH clean exits after `grace` minutes of
    continued quiet; a STALE clean holds the `min_watch` floor; everything stops at
    `max_budget`. `rl_remaining_min` is how long a known rate-limit window still has
    to run — when it can't free within the remaining budget, exit early so the agent
    can schedule a later relaunch instead of the watcher idling to the ceiling.

    Returns (verdict, watch_result, clean_since_min): verdict is "exit" or "poll";
    watch_result labels why we exited (None while polling); clean_since_min is the
    propagated grace-start marker (minutes into the watch the fresh clean appeared).
    """
    action = result.get("action")
    if action in ("address", "resume", "review", "no_activity", "error", "failover"):
        return "exit", f"action_{action}", None
    if action == "done":
        if result.get("clean_is_fresh"):
            if clean_since_min is None:
                clean_since_min = elapsed_min          # grace starts now
            if elapsed_min - clean_since_min >= grace:
                return "exit", "clean_fresh", clean_since_min
            if elapsed_min >= max_budget:
                return "exit", "budget_exhausted", clean_since_min
            return "poll", None, clean_since_min
        if elapsed_min >= min_watch:
            return "exit", "clean_stale_floor_met", None
        if elapsed_min >= max_budget:
            return "exit", "budget_exhausted", None
        return "poll", None, None
    # action == "wait"
    if elapsed_min >= max_budget:
        return "exit", "budget_exhausted", None
    if rl_remaining_min is not None and rl_remaining_min > (max_budget - elapsed_min):
        return "exit", "rate_limited_beyond_budget", None
    return "poll", None, None


def _watch(args: argparse.Namespace) -> int:
    """Run gather()+decide() on a loop until watch_step says exit.

    Designed to run as a BACKGROUND process (Bash run_in_background): progress goes
    to stderr, and the final status JSON is the last thing printed on stdout, so the
    agent handles the result when the process exits — no foreground polling.
    """
    start = datetime.now(timezone.utc)
    clean_since_min: float | None = None
    consecutive_errors = 0
    while True:
        now = datetime.now(timezone.utc)
        elapsed_min = (now - start).total_seconds() / 60.0
        try:
            state = gather(args.repo, args.pr, args.bot_login)
            consecutive_errors = 0
        except RuntimeError as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print(json.dumps({"error": str(exc), "action": "error",
                                  "watch_result": "action_error",
                                  "watch_elapsed_minutes": round(elapsed_min, 1),
                                  "repo": args.repo, "pr": args.pr}, indent=2))
                return 2
            print(f"[watch {elapsed_min:.1f}m] transient error "
                  f"({consecutive_errors}/3): {exc}", file=sys.stderr)
            time.sleep(args.watch_interval_seconds)
            continue

        result = decide(state, parse_ts(args.since), now, args.no_activity_minutes)

        rl_until = parse_ts(result.get("rate_limited_until"))
        rl_remaining_min = None
        if (result.get("action") == "wait" and rl_until and rl_until > now
                and result.get("failover_state") != "pending"):
            rl_remaining_min = (rl_until - now).total_seconds() / 60.0

        verdict, watch_result, clean_since_min = watch_step(
            result, elapsed_min, clean_since_min, args.watch_min_minutes,
            args.watch_grace_minutes, args.watch_max_minutes, rl_remaining_min)

        if verdict == "exit":
            out = {"repo": args.repo, "pr": args.pr, "now": now.isoformat(),
                   "since": args.since, "watch_result": watch_result,
                   "watch_elapsed_minutes": round(elapsed_min, 1), **result}
            print(json.dumps(out, indent=2))
            return 0

        print(f"[watch {elapsed_min:.1f}m] {result['action']} — {result['reason']}",
              file=sys.stderr)
        # Rate-limited: probing again before the window frees is pointless — nap
        # until just past it (never beyond the remaining budget; watch_step already
        # exited early if the window outlives the budget).
        sleep_s = args.watch_interval_seconds
        if rl_remaining_min is not None:
            remaining_budget_s = max(0.0, (args.watch_max_minutes - elapsed_min) * 60.0)
            sleep_s = max(sleep_s, min(rl_remaining_min * 60.0 + 5.0, remaining_budget_s))
        time.sleep(sleep_s)


# --- selftest (runs offline; no gh/network needed) ----------------------------

def _selftest() -> int:
    now = parse_ts("2026-05-30T12:00:00Z")
    created = "2026-05-30T11:00:00Z"

    def run(state, since):
        return decide(state, parse_ts(since), now, no_activity_minutes=5.0)["action"]

    def fresh(state, since):
        return decide(state, parse_ts(since), now, no_activity_minutes=5.0)["clean_is_fresh"]

    base = {
        "pr_created_at": created, "pr_state": "open", "coderabbit_seen": True,
        "bot_inline_ts": [], "bot_review_events": [], "latest_review_state": None,
        "latest_comment_body": "", "actionable_comments": None, "max_timestamp": None,
        "unresolved_threads": 0, "paused": False, "auto_review_disabled": False,
    }

    cases = []

    # 1. No activity yet, recently opened -> wait
    s = {**base, "coderabbit_seen": False, "pr_created_at": "2026-05-30T11:58:00Z"}
    cases.append(("fresh_no_activity", run(s, None), "wait"))

    # 2. No activity, opened long ago -> no_activity
    s = {**base, "coderabbit_seen": False}
    cases.append(("stale_no_activity", run(s, None), "no_activity"))

    # 3. New inline comments since last check -> address
    s = {**base, "bot_inline_ts": ["2026-05-30T11:45:00Z"], "max_timestamp": "2026-05-30T11:45:00Z"}
    cases.append(("new_inline", run(s, "2026-05-30T11:40:00Z"), "address"))

    # 4. New review reporting actionable>0 -> address
    s = {**base,
         "bot_review_events": [{"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z", "actionable": 3}],
         "latest_review_state": "COMMENTED", "actionable_comments": 3,
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("new_review_actionable", run(s, "2026-05-30T11:40:00Z"), "address"))

    # 5. Latest summary says 0 actionable -> done
    s = {**base, "latest_comment_body": "Actionable comments posted: 0",
         "actionable_comments": 0, "max_timestamp": "2026-05-30T11:50:00Z",
         "bot_review_events": [{"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z", "actionable": 0}]}
    cases.append(("clean_zero_actionable", run(s, "2026-05-30T11:49:00Z"), "done"))

    # 6. Approved review -> done
    s = {**base, "latest_review_state": "APPROVED",
         "bot_review_events": [{"state": "APPROVED", "ts": "2026-05-30T11:50:00Z", "actionable": None}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("approved", run(s, "2026-05-30T11:49:00Z"), "done"))

    # 7. Paused (computed by gather across all comments) -> resume
    s = {**base, "paused": True, "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("paused", run(s, "2026-05-30T11:40:00Z"), "resume"))

    # 8. Auto-review disabled for repo -> review (NOT resume)
    s = {**base, "auto_review_disabled": True, "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("auto_disabled", run(s, "2026-05-30T11:40:00Z"), "review"))

    # 9. Bot active (posted a walkthrough) but no review verdict yet, nothing new,
    #    0 unresolved -> wait (don't call clean before CodeRabbit reports a verdict)
    s = {**base, "bot_inline_ts": ["2026-05-30T11:30:00Z"],
         "max_timestamp": "2026-05-30T11:30:00Z"}
    cases.append(("active_no_verdict_yet", run(s, "2026-05-30T11:45:00Z"), "wait"))

    # 10. Disabled takes precedence over a concurrent paused signal
    s = {**base, "paused": True, "auto_review_disabled": True,
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("disabled_beats_paused", run(s, "2026-05-30T11:40:00Z"), "review"))

    # 11. REGRESSION: 0 actionable by count, but threads still unresolved -> address
    #     (the blind spot that let the loop declare "done" with open conversations).
    s = {**base, "latest_comment_body": "Actionable comments posted: 0",
         "actionable_comments": 0, "unresolved_threads": 2,
         "bot_review_events": [{"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z", "actionable": 0}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("clean_count_but_unresolved", run(s, "2026-05-30T11:49:00Z"), "address"))

    # 12. Approved review but a thread is still unresolved -> address (not done)
    s = {**base, "latest_review_state": "APPROVED", "unresolved_threads": 1,
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("approved_but_unresolved", run(s, "2026-05-30T11:49:00Z"), "address"))

    # 13. Unresolved count unknown (GraphQL failed) + 0 actionable -> done (degrade)
    s = {**base, "actionable_comments": 0, "unresolved_threads": None,
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("unknown_unresolved_degrades", run(s, "2026-05-30T11:49:00Z"), "done"))

    # 14. New inline comment still wins even if thread-count says 0 unresolved
    s = {**base, "bot_inline_ts": ["2026-05-30T11:45:00Z"], "unresolved_threads": 0,
         "max_timestamp": "2026-05-30T11:45:00Z"}
    cases.append(("new_inline_with_zero_unresolved", run(s, "2026-05-30T11:40:00Z"), "address"))

    # active_signal() — the supersession logic behind all-comments pause/disabled
    # detection (a pause is "active" only until a later resume/review command).
    P = parse_ts
    cases.append(("pause_active_when_no_resume",
                  active_signal(P("2026-05-30T11:50:00Z"), None), True))
    cases.append(("pause_superseded_by_later_resume",
                  active_signal(P("2026-05-30T11:50:00Z"), P("2026-05-30T11:55:00Z")), False))
    cases.append(("pause_after_resume_is_active_again",
                  active_signal(P("2026-05-30T12:00:00Z"), P("2026-05-30T11:55:00Z")), True))
    cases.append(("disabled_superseded_by_later_review",
                  active_signal(P("2026-05-30T11:50:00Z"), P("2026-05-30T11:55:00Z")), False))
    cases.append(("no_signal_is_inactive", active_signal(None, None), False))

    # 15. Stale "Actionable comments posted: 2" tally but all threads resolved -> done.
    #     Resolved threads ARE the handled actionable items; without this the loop
    #     would spin to the 30-min ceiling on a genuinely clean PR (the PR #9 case).
    s = {**base, "actionable_comments": 2, "unresolved_threads": 0,
         "latest_review_state": "COMMENTED", "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("stale_tally_all_resolved", run(s, "2026-05-30T11:51:00Z"), "done"))

    # 16. Same stale tally but a thread is still unresolved -> address (not done)
    s = {**base, "actionable_comments": 2, "unresolved_threads": 1,
         "latest_review_state": "COMMENTED", "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("stale_tally_one_unresolved", run(s, "2026-05-30T11:51:00Z"), "address"))

    # 17. clean_is_fresh: a clean review whose commit_id == HEAD -> fresh (early-exit OK)
    s = {**base, "head_sha": "abc123", "actionable_comments": 0, "unresolved_threads": 0,
         "bot_review_events": [{"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z",
                                "actionable": 0, "commit_id": "abc123"}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("fresh_review_matches_head", fresh(s, "2026-05-30T11:49:00Z"), True))

    # 18. Clean, but the only review's commit_id is an OLDER push -> NOT fresh (floor holds).
    #     The probe still reports `done`; the SKILL loop keeps the 15-min floor on it.
    s = {**base, "head_sha": "newsha", "actionable_comments": 0, "unresolved_threads": 0,
         "bot_review_events": [{"state": "APPROVED", "ts": "2026-05-30T11:50:00Z",
                                "actionable": None, "commit_id": "oldsha"}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("clean_but_review_is_stale_commit", fresh(s, "2026-05-30T11:49:00Z"), False))
    cases.append(("stale_commit_still_reports_done", run(s, "2026-05-30T11:49:00Z"), "done"))

    # 19. clean_is_fresh via an edited-in-place summary updated after HEAD was pushed
    s = {**base, "head_sha": "h", "head_committed_at": "2026-05-30T11:40:00Z",
         "actionable_comments": 0, "unresolved_threads": 0,
         "clean_comment_ts": "2026-05-30T11:45:00Z", "max_timestamp": "2026-05-30T11:45:00Z"}
    cases.append(("fresh_summary_after_head", fresh(s, "2026-05-30T11:44:00Z"), True))

    # 20. Summary clean tally last edited BEFORE the latest push -> NOT fresh
    s = {**base, "head_sha": "h", "head_committed_at": "2026-05-30T11:50:00Z",
         "actionable_comments": 0, "unresolved_threads": 0,
         "clean_comment_ts": "2026-05-30T11:45:00Z", "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("stale_summary_before_head", fresh(s, "2026-05-30T11:44:00Z"), False))

    # 21. Not clean (open threads) -> never fresh, even if a review commit matches HEAD
    s = {**base, "head_sha": "h", "unresolved_threads": 2,
         "bot_review_events": [{"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z",
                                "actionable": 0, "commit_id": "h"}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("not_clean_not_fresh", fresh(s, "2026-05-30T11:49:00Z"), False))

    # 22. Clean RE-review on HEAD that omits the "0 actionable" tally (COMMENTED,
    #     actionable=None) + 0 unresolved -> fresh. The live PR #44 case: the first
    #     review (oldsha) had actionable items; the clean re-review on HEAD carries no
    #     tally, and the old summary tally (1) is stale. reviewed_head + looks_clean.
    s = {**base, "head_sha": "newsha", "actionable_comments": 1, "unresolved_threads": 0,
         "latest_review_state": "COMMENTED",
         "bot_review_events": [
             {"state": "COMMENTED", "ts": "2026-05-30T11:40:00Z", "actionable": 1, "commit_id": "oldsha"},
             {"state": "COMMENTED", "ts": "2026-05-30T11:50:00Z", "actionable": None, "commit_id": "newsha"}],
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("fresh_clean_rereview_no_tally", fresh(s, "2026-05-30T11:51:00Z"), True))
    cases.append(("clean_rereview_reports_done", run(s, "2026-05-30T11:51:00Z"), "done"))

    # 23. Paused + rate-limit window still open -> wait (resume would bounce),
    #     but once the window has passed -> resume as usual.
    s = {**base, "paused": True, "rate_limited_until": "2026-05-30T12:20:00Z",
         "max_timestamp": "2026-05-30T11:50:00Z"}
    # Owner rule 2026-09-25: a rate-limited PR never just waits; it starts the backup review.
    cases.append(("rate_limited_starts_failover", run(s, "2026-05-30T11:40:00Z"), "failover"))

    # 23b. Backup review (review-failover) status on HEAD stands in for CodeRabbit.
    rl = {"rate_limited_until": "2026-05-30T12:20:00Z", "max_timestamp": "2026-05-30T11:50:00Z"}
    s = {**base, **rl, "failover_state": "success"}
    cases.append(("failover_success_is_done", run(s, "2026-05-30T11:40:00Z"), "done"))
    s = {**base, **rl, "failover_state": "failure"}
    cases.append(("failover_failure_addresses", run(s, "2026-05-30T11:40:00Z"), "address"))
    s = {**base, **rl, "failover_state": "pending"}
    cases.append(("failover_pending_waits", run(s, "2026-05-30T11:40:00Z"), "wait"))
    s = {**base, **rl, "failover_state": "error"}
    cases.append(("failover_error_retries", run(s, "2026-05-30T11:40:00Z"), "failover"))
    s = {**base, **rl, "failover_state": "success", "unresolved_threads": 2}
    cases.append(("failover_success_but_threads_addresses", run(s, "2026-05-30T11:40:00Z"), "address"))
    s = {**base, "failover_state": None, "coderabbit_seen": False,
         "rate_limited_until": "2026-05-30T12:20:00Z"}
    cases.append(("ledger_hold_no_review_yet_starts_failover", run(s, "2026-05-30T11:40:00Z"), "failover"))
    s = {**base, "rate_limit_notice_ts": "2026-05-30T11:45:00Z",
         "head_committed_at": "2026-05-30T11:30:00Z", "rate_limited_until": None}
    cases.append(("notice_without_wait_on_head_starts_failover", run(s, "2026-05-30T11:40:00Z"), "failover"))
    s = {**base, "rate_limit_notice_ts": "2026-05-30T11:20:00Z",
         "head_committed_at": "2026-05-30T11:30:00Z", "rate_limited_until": None}
    cases.append(("old_notice_before_head_ignored", run(s, "2026-05-30T11:40:00Z") != "failover", True))
    cases.append(("regex_matches_review_rate_limited",
                  bool(RATE_LIMIT_RE.search("<summary>Action not completed</summary> Review rate limited.")), True))
    s = {**base, "paused": True, "rate_limited_until": "2026-05-30T11:55:00Z",
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("rate_limit_expired_resumes", run(s, "2026-05-30T11:40:00Z"), "resume"))

    # 24. Rate limit never masks address — fixing code needs no CodeRabbit budget.
    s = {**base, "unresolved_threads": 1, "rate_limited_until": "2026-05-30T12:20:00Z",
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("rate_limited_still_addresses", run(s, "2026-05-30T11:51:00Z"), "address"))

    # watch_step() — the watcher's exit/poll rules (grace, floor, ceiling, rate limit).
    def ws(action, fresh_flag=False, elapsed=0.0, since=None, rl=None):
        v, wr, _ = watch_step({"action": action, "clean_is_fresh": fresh_flag},
                              elapsed, since, 15.0, 3.0, 30.0, rl)
        return v if wr is None else wr
    cases.append(("watch_address_exits", ws("address"), "action_address"))
    cases.append(("watch_resume_exits", ws("resume"), "action_resume"))
    cases.append(("watch_failover_exits", ws("failover"), "action_failover"))
    cases.append(("watch_wait_polls", ws("wait", elapsed=5.0), "poll"))
    cases.append(("watch_wait_hits_ceiling", ws("wait", elapsed=30.0), "budget_exhausted"))
    cases.append(("watch_stale_done_holds_floor", ws("done", elapsed=10.0), "poll"))
    cases.append(("watch_stale_done_floor_met", ws("done", elapsed=15.0), "clean_stale_floor_met"))
    cases.append(("watch_fresh_done_starts_grace", ws("done", True, elapsed=1.0), "poll"))
    cases.append(("watch_fresh_done_grace_held",
                  ws("done", True, elapsed=4.5, since=1.0), "clean_fresh"))
    cases.append(("watch_fresh_done_grace_running",
                  ws("done", True, elapsed=2.0, since=1.0), "poll"))
    cases.append(("watch_rl_within_budget_polls", ws("wait", elapsed=5.0, rl=10.0), "poll"))
    cases.append(("watch_rl_beyond_budget_exits",
                  ws("wait", elapsed=20.0, rl=25.0), "rate_limited_beyond_budget"))

    # 25. Rate-limit window freed + no bot activity since + nobody re-triggered ->
    #     review (the notice says reviews do NOT auto-resume). With bot activity
    #     after the window, or a review command already posted, stay `wait`.
    s = {**base, "rate_limited_until": "2026-05-30T11:55:00Z",
         "max_timestamp": "2026-05-30T11:50:00Z"}
    cases.append(("rl_expired_needs_retrigger", run(s, "2026-05-30T11:40:00Z"), "review"))
    s = {**base, "rate_limited_until": "2026-05-30T11:55:00Z",
         "max_timestamp": "2026-05-30T11:58:00Z"}
    cases.append(("rl_expired_bot_active_stays_wait", run(s, "2026-05-30T11:40:00Z"), "wait"))
    s = {**base, "rate_limited_until": "2026-05-30T11:55:00Z",
         "max_timestamp": "2026-05-30T11:50:00Z",
         "review_request_ts": "2026-05-30T11:56:00Z"}
    cases.append(("rl_expired_already_retriggered_waits", run(s, "2026-05-30T11:40:00Z"), "wait"))

    # Rate-limit notice parsing — pinned to the LIVE specimen (Quant_Backtest_Platform
    # PR #47, 2026-07: comment edited in place to "Review limit reached") plus the
    # older documented phrasing.
    live = ("<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
            "> ## Review limit reached\n"
            "> Otherwise, wait until the next included review is available.\n"
            "> **Next review available in:** **21 minutes**\n")
    cases.append(("rl_re_matches_live_marker", bool(RATE_LIMIT_RE.search(live)), True))
    cases.append(("rl_wait_parses_live_format", parse_rate_wait(live), (0, 21, 0)))
    legacy = "Rate limit exceeded. Please wait 17 minutes and 42 seconds before requesting another review."
    cases.append(("rl_re_matches_legacy", bool(RATE_LIMIT_RE.search(legacy)), True))
    cases.append(("rl_wait_parses_legacy", parse_rate_wait(legacy), (0, 17, 42)))
    # Command-reply variant (live specimen, promptups#4 2026-07-26): the ack to a
    # `@coderabbitai review` posted during a window says "limit is currently
    # reached", which the banner patterns above don't cover.
    reply = ("Your included review limit is currently reached under our Fair Usage "
             "Limits Policy. Your recent PR review activity is in the 95th percentile "
             "or higher among CodeRabbit users, so adaptive limits apply. This review "
             "may still proceed through usage-based billing if eligible. Your next "
             "included review will be available in 26 minutes.")
    cases.append(("rl_re_matches_command_reply", bool(RATE_LIMIT_RE.search(reply)), True))
    cases.append(("rl_wait_parses_command_reply", parse_rate_wait(reply), (0, 26, 0)))
    cases.append(("rl_wait_ignores_numberless", parse_rate_wait(
        "Otherwise, wait until the next included review is available."), None))
    cases.append(("rl_re_ignores_prose", bool(RATE_LIMIT_RE.search(
        "Our docs discuss rate limits in general.")), False))

    # pause_asserted() — the boilerplate in every review-command ack ("This command
    # is applicable only when automatic reviews are paused.") must NOT read as a live
    # pause (the false positive that kept recommending `resume` on PR #47), while a
    # genuine pause notice still must.
    cases.append(("pause_ignores_ack_boilerplate", pause_asserted(
        "Review triggered.\n> Note: CodeRabbit is an incremental review system and "
        "does not re-review already reviewed commits. This command is applicable "
        "only when automatic reviews are paused."), False))
    cases.append(("pause_matches_real_notice", pause_asserted(
        "Reviews are paused on this repository. Use @coderabbitai resume."), True))
    cases.append(("pause_matches_after_boilerplate_strip", pause_asserted(
        "This command is applicable only when automatic reviews are paused. "
        "Reviews are paused on this pull request."), True))

    # parse_actionable() — the numeric tally, the tally-less clean-run phrasing
    # (live specimen 2026-07-02: walkthrough edited to "No actionable comments were
    # generated in the recent review. 🎉" with no review object), and neither.
    cases.append(("actionable_numeric", parse_actionable("Actionable comments posted: 3"), 3))
    cases.append(("actionable_clean_phrase", parse_actionable(
        "No actionable comments were generated in the recent review. 🎉"), 0))
    cases.append(("actionable_absent", parse_actionable("Walkthrough updated."), None))

    ok = True
    for name, got, want in cases:
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[{flag}] {name}: got={got} want={want}")
    print("\nALL PASSED" if ok else "\nSOME FAILED")
    return 0 if ok else 1


# --- main ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CodeRabbit PR status probe (read-only).")
    ap.add_argument("--repo", help="OWNER/REPO, e.g. octocat/my-project")
    ap.add_argument("--pr", type=int, help="PR number")
    ap.add_argument("--since", help="ISO8601; only flag bot activity after this time")
    ap.add_argument("--bot-login", default="coderabbit",
                    help="substring to match CodeRabbit's bot login (default: coderabbit)")
    ap.add_argument("--no-activity-minutes", type=float, default=15.0,
                    help="if no bot activity after this many minutes, recommend stopping "
                         "(default 15 = the loop's minimum-watch floor)")
    ap.add_argument("--watch", action="store_true",
                    help="poll internally and exit only when the agent must act; run it "
                         "in the background (run_in_background) so the session isn't "
                         "stuck foreground-polling")
    ap.add_argument("--watch-interval-seconds", type=float, default=75.0,
                    help="seconds between probes in --watch mode (default 75)")
    ap.add_argument("--watch-min-minutes", type=float, default=15.0,
                    help="minimum watch before a STALE clean concludes (default 15)")
    ap.add_argument("--watch-grace-minutes", type=float, default=3.0,
                    help="quiet grace held after a FRESH clean before concluding (default 3)")
    ap.add_argument("--watch-max-minutes", type=float, default=30.0,
                    help="hard ceiling on one watch run (default 30)")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline decision-logic checks and exit")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.repo or args.pr is None:
        ap.error("--repo and --pr are required (or use --selftest)")

    if args.watch:
        return _watch(args)

    now = datetime.now(timezone.utc)
    try:
        state = gather(args.repo, args.pr, args.bot_login)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc), "action": "error",
                          "repo": args.repo, "pr": args.pr}, indent=2))
        return 2

    result = decide(state, parse_ts(args.since), now, args.no_activity_minutes)
    result = {"repo": args.repo, "pr": args.pr,
              "now": now.isoformat(), "since": args.since, **result}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
