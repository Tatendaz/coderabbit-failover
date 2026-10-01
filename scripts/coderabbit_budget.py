#!/usr/bin/env python3
"""
Global CodeRabbit review-budget ledger (cross-repo, cross-session).

CodeRabbit rate-limits PR reviews per DEVELOPER over rolling windows — one pool
shared by every PR in every repo — and layers adaptive Fair-Usage limits on top
(sustained 7-day volume shrinks the hourly allowance, down to 1 review/hour,
one at a time). A session watching a single PR cannot see that shared pool, so
this script keeps a persistent ledger at ~/.claude/coderabbit/ledger.json that
every skill and session consults BEFORE firing anything that consumes a review,
and updates whenever CodeRabbit reports a limit.

What counts as a PR review trigger (kind): pr_open (a PR opened WITHOUT
`@coderabbitai ignore` in its description), push (a commit pushed to a PR with
auto-review active), manual_review / full_review (`@coderabbitai review` /
`full review` comments). CLI reviews (kind: cli) draw a SEPARATE per-developer
budget and are tracked independently.

Subcommands (all print JSON on stdout):
    status                          global view: cooldowns, counts, tier, queue
    run --kind pr|cli --repo OWNER/REPO [--pr N] [--as KIND] [--note TEXT] -- CMD...
                                    THE SAFE WAY TO SPEND A REVIEW. Under one lock:
                                    check the budget and book the slot. On hold it
                                    exits 75 and never runs CMD. Otherwise it runs
                                    CMD, passes its stdout through, and returns its
                                    exit code. A `rate_limit` error event in CMD's
                                    output records the cooldown and gives the slot
                                    back (the server refused, so nothing was spent).
                                    No separate record-trigger afterwards.
    check      --kind pr|cli        exit 0 = safe to trigger now; exit 1 = don't.
                                    Look-only unless --reserve. Never pipe it
                                    (`check | head && ...` hides the exit code);
                                    use `run` instead.
    check --reserve --repo OWNER/REPO [--pr N] [--as KIND] [--note TEXT]
                                    check and book the slot atomically (exit 0 =
                                    booked). No separate record-trigger afterwards.
    record-trigger --kind KIND --repo OWNER/REPO [--pr N] [--note TEXT]
    record-limit   --kind pr|cli (--wait TEXT | --until ISO) [--source TEXT]
                                    TEXT e.g. "26 minutes", "17 minutes and 42
                                    seconds", "1 hour", or bare minutes "26"
    queue-add  --repo OWNER/REPO --pr N [--reason TEXT]
    queue-list
    queue-pop  [--repo OWNER/REPO --pr N]
    failover   --repo OWNER/REPO --pr N [--dry-run]
                                    CodeRabbit on hold? Start the backup review
                                    instead of waiting: posts `/open-code-review`,
                                    which the owner's Mac picks up within ~3 min
                                    (reviewer/local_reviewer.py). Does NOT count
                                    against the CodeRabbit budget. Exit 3 = repo not
                                    covered (archived/fork/other owner): queue-add.
    selftest                        offline logic checks, uses a temp ledger

Exit codes: check → 0 go / 1 hold; run → 75 hold (CMD not run), else CMD's own code
(127 if CMD is not found); everything else → 0 ok / 2 usage-or-IO error. A hold also
prints one `HOLD ...` line on stderr, so it shows even when stdout is piped away.

The ledger directory can be overridden with $CODERABBIT_LEDGER_DIR (used by the
selftest so it never touches the real ledger).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

PR_KINDS = ("pr_open", "push", "manual_review", "full_review")
ALL_KINDS = PR_KINDS + ("cli", "failover")  # failover never counts as a CodeRabbit review

# Adaptive Fair-Usage escalation (docs.coderabbit.ai/management/plans#rate-limits,
# fetched 2026-07-26): PR reviews in the trailing 7 days -> reviews/hour.
PRO_TIERS = ((60, 1), (50, 2), (40, 3), (30, 4), (0, 5))
PRO_PLUS_TIERS = ((90, 1), (80, 2), (70, 3), (60, 4), (50, 5), (40, 6), (30, 8), (0, 10))

# Nominal per-developer hourly limits by plan: (pr, cli).
PLAN_NOMINAL = {
    "free": (1, 3),
    "oss": (1, 3),          # OSS tier is 1-10 by popularity; assume the floor
    "pro": (5, 5),
    "pro-plus": (10, 10),
    "enterprise": (12, 12),
}

WINDOW = timedelta(hours=1)
HISTORY = timedelta(days=7)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value):
    if not value:
        return None
    v = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


WAIT_RE = re.compile(
    r"(?:(\d+)\s*h(?:ou)?rs?)?\W*(?:and\s+)?"
    r"(?:(\d+)\s*min(?:ute)?s?)?\W*(?:and\s+)?"
    r"(?:(\d+)\s*sec(?:ond)?s?)?",
    re.IGNORECASE,
)


def parse_wait(text: str):
    """'26 minutes' / '17 minutes and 42 seconds' / '1 hour' / bare '26' -> timedelta."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():  # bare number = minutes (the banners always speak in minutes)
        return timedelta(minutes=int(text))
    for m in WAIT_RE.finditer(text):
        if any(m.groups()):
            h, mi, s = (int(g) if g else 0 for g in m.groups())
            return timedelta(hours=h, minutes=mi, seconds=s)
    return None


def tier_limit(plan: str, count_7d: int) -> int:
    """Hourly PR-review allowance for a plan given trailing-7-day volume."""
    plan = (plan or "pro").lower().replace("+", "-plus").replace("proplus", "pro-plus")
    if plan == "pro":
        tiers = PRO_TIERS
    elif plan == "pro-plus":
        tiers = PRO_PLUS_TIERS
    else:
        return PLAN_NOMINAL.get(plan, PLAN_NOMINAL["pro"])[0]
    for floor, limit in tiers:
        if count_7d >= floor:
            return limit
    return tiers[-1][1]


def cli_limit(plan: str) -> int:
    plan = (plan or "pro").lower().replace("+", "-plus").replace("proplus", "pro-plus")
    return PLAN_NOMINAL.get(plan, PLAN_NOMINAL["pro"])[1]


# --- ledger I/O ----------------------------------------------------------------

def ledger_dir() -> Path:
    override = os.environ.get("CODERABBIT_LEDGER_DIR")
    return Path(override) if override else Path.home() / ".claude" / "coderabbit"


def empty_ledger() -> dict:
    return {"version": 1, "triggers": [], "cooldowns": {}, "queue": []}


@contextmanager
def locked_ledger():
    """Yield the parsed ledger dict under an exclusive flock; caller mutates and we save."""
    d = ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "ledger.json"
    with open(d / "ledger.lock", "w") as lockfh:
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        try:
            ledger = empty_ledger()
            if path.exists():
                try:
                    loaded = json.loads(path.read_text() or "{}")
                    if isinstance(loaded, dict):
                        ledger.update({k: loaded.get(k, v) for k, v in empty_ledger().items()})
                except (json.JSONDecodeError, OSError) as exc:
                    bad = d / f"ledger.json.bad-{now_utc().strftime('%Y%m%dT%H%M%S')}"
                    try:
                        path.rename(bad)
                    except OSError:
                        pass
                    print(f"warning: ledger unreadable ({exc}); moved aside to {bad}",
                          file=sys.stderr)
            prune(ledger, now_utc())
            yield ledger
            fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".ledger-")
            with os.fdopen(fd, "w") as fh:
                json.dump(ledger, fh, indent=2)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lockfh, fcntl.LOCK_UN)


def prune(ledger: dict, now: datetime) -> None:
    """Drop triggers older than the 7-day history and expired cooldowns."""
    cutoff = now - HISTORY
    ledger["triggers"] = [
        t for t in ledger.get("triggers", [])
        if (parse_ts(t.get("ts")) or now) >= cutoff
    ]
    for kind in list(ledger.get("cooldowns", {})):
        until = parse_ts((ledger["cooldowns"][kind] or {}).get("until"))
        # keep expired cooldowns briefly visible (1h) for status/debugging, then drop
        if until is None or until < now - timedelta(hours=1):
            del ledger["cooldowns"][kind]


# --- pure decision logic (selftested) ------------------------------------------

def summarize(ledger: dict, plan: str, now: datetime) -> dict:
    triggers = ledger.get("triggers", [])

    def count(kinds, since):
        return sum(
            1 for t in triggers
            if t.get("kind") in kinds and (parse_ts(t.get("ts")) or now) >= since
        )

    pr_1h = count(PR_KINDS, now - WINDOW)
    pr_7d = count(PR_KINDS, now - HISTORY)
    cli_1h = count(("cli",), now - WINDOW)
    limit_pr = tier_limit(plan, pr_7d)
    limit_cli = cli_limit(plan)
    cd = ledger.get("cooldowns", {})
    pr_until = parse_ts((cd.get("pr") or {}).get("until"))
    cli_until = parse_ts((cd.get("cli") or {}).get("until"))
    return {
        "now": iso(now), "plan": plan,
        "pr_triggers_last_hour": pr_1h, "pr_triggers_last_7d": pr_7d,
        "cli_triggers_last_hour": cli_1h,
        "hourly_limit_pr": limit_pr, "hourly_limit_cli": limit_cli,
        "adaptive_tier_active": limit_pr < PLAN_NOMINAL.get(plan, PLAN_NOMINAL["pro"])[0],
        "pr_cooldown_until": iso(pr_until) if pr_until and pr_until > now else None,
        "cli_cooldown_until": iso(cli_until) if cli_until and cli_until > now else None,
        "pr_cooldown_source": (cd.get("pr") or {}).get("source"),
        "queue": ledger.get("queue", []),
    }


def decide_check(ledger: dict, kind: str, plan: str, now: datetime) -> dict:
    """Should a trigger of this kind fire now? Returns {'ok', 'reason', 'next_free_at', ...}."""
    s = summarize(ledger, plan, now)
    is_cli = kind == "cli"
    until = parse_ts(s["cli_cooldown_until"] if is_cli else s["pr_cooldown_until"])
    if until and until > now:
        return {"ok": False, "kind": kind,
                "reason": "CodeRabbit reported a rate-limit window that has not expired"
                          + (f" (source: {s['pr_cooldown_source']})" if not is_cli and s.get("pr_cooldown_source") else ""),
                "next_free_at": iso(until), **s}
    kinds = ("cli",) if is_cli else PR_KINDS
    used = s["cli_triggers_last_hour"] if is_cli else s["pr_triggers_last_hour"]
    limit = s["hourly_limit_cli"] if is_cli else s["hourly_limit_pr"]
    if used >= limit:
        in_window = sorted(
            parse_ts(t.get("ts")) for t in ledger.get("triggers", [])
            if t.get("kind") in kinds and parse_ts(t.get("ts"))
            and parse_ts(t.get("ts")) >= now - WINDOW
        )
        next_free = (in_window[0] + WINDOW) if in_window else now + WINDOW
        return {"ok": False, "kind": kind,
                "reason": f"{used}/{limit} {'CLI' if is_cli else 'PR'} review triggers already "
                          f"in the rolling hour (7-day volume {s['pr_triggers_last_7d']} -> "
                          f"limit {limit}/hr)",
                "next_free_at": iso(next_free), **s}
    return {"ok": True, "kind": kind, "reason": "budget available", "next_free_at": None, **s}


def apply_limit(ledger: dict, kind: str, until: datetime, source: str, now: datetime) -> dict:
    """Record a reported rate-limit window; the furthest-out window wins."""
    cd = ledger.setdefault("cooldowns", {})
    existing = parse_ts((cd.get(kind) or {}).get("until"))
    if existing is None or until > existing:
        cd[kind] = {"until": iso(until), "source": source, "recorded_at": iso(now)}
    return cd[kind]


def default_record_kind(kind: str) -> str:
    return "cli" if kind == "cli" else "manual_review"


def check_and_book(ledger: dict, kind: str, record_kind: str, repo: str, pr, note: str,
                   plan: str, now: datetime) -> dict:
    """Decide and, when the budget allows, book the slot in the same locked step.

    Booking at decision time (not after the review finishes) is what stops parallel
    sessions from all seeing the same free slot and overrunning the hourly cap.
    """
    out = decide_check(ledger, kind, plan, now)
    if out["ok"]:
        entry = {"ts": iso(now), "kind": record_kind, "repo": repo, "pr": pr,
                 "note": note or "", "booked": True}
        ledger["triggers"].append(entry)
        out = {**out, "reserved": entry}
    return out


def release_booking(ledger: dict, entry: dict) -> bool:
    """Give a booked slot back (the review never happened). Matches the exact entry."""
    triggers = ledger.get("triggers", [])
    for i, t in enumerate(triggers):
        if t == entry:
            del triggers[i]
            return True
    return False


def rate_limit_wait(line: str):
    """A CodeRabbit CLI NDJSON rate_limit error event -> its wait as a timedelta (or None)."""
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ev, dict) or ev.get("type") != "error" or ev.get("errorType") != "rate_limit":
        return None
    meta = ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}
    return parse_wait(str(meta.get("waitTime") or "")) or timedelta(minutes=5)


def hold_line(out: dict) -> str:
    return (f"HOLD: CodeRabbit {out.get('kind')} budget: {out.get('reason')}"
            f" (next free {out.get('next_free_at')}). Do NOT start the review.")


# --- subcommands ----------------------------------------------------------------

def cmd_status(args) -> int:
    with locked_ledger() as ledger:
        out = summarize(ledger, args.plan, now_utc())
        out["can_trigger_pr"] = decide_check(ledger, "pr", args.plan, now_utc())["ok"]
        out["can_trigger_cli"] = decide_check(ledger, "cli", args.plan, now_utc())["ok"]
    print(json.dumps(out, indent=2))
    return 0


RUN_HOLD_EXIT = 75  # EX_TEMPFAIL: the budget said hold, the command was not run


def _booking_error(args) -> str | None:
    """Why this booking request is malformed, or None."""
    if not args.repo:
        return "--repo OWNER/REPO is required to book a slot"
    record_kind = args.as_kind or default_record_kind(args.kind)
    if args.kind == "cli" and record_kind != "cli":
        return "--kind cli books a 'cli' trigger; drop --as"
    if args.kind == "pr" and record_kind not in PR_KINDS:
        return f"--as must be one of {PR_KINDS} for --kind pr"
    return None


def cmd_check(args) -> int:
    if args.reserve:
        err = _booking_error(args)
        if err:
            print(json.dumps({"error": err}))
            return 2
        with locked_ledger() as ledger:
            out = check_and_book(ledger, args.kind, args.as_kind or default_record_kind(args.kind),
                                 args.repo, args.pr, args.note, args.plan, now_utc())
    else:
        with locked_ledger() as ledger:
            out = decide_check(ledger, args.kind, args.plan, now_utc())
    print(json.dumps(out, indent=2))
    if not out["ok"]:
        print(hold_line(out), file=sys.stderr)
    return 0 if out["ok"] else 1


def cmd_run(args) -> int:
    argv = list(args.argv or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(json.dumps({"error": "run needs the review command after --"}))
        return 2
    err = _booking_error(args)
    if err:
        print(json.dumps({"error": err}))
        return 2
    with locked_ledger() as ledger:
        out = check_and_book(ledger, args.kind, args.as_kind or default_record_kind(args.kind),
                             args.repo, args.pr, args.note, args.plan, now_utc())
    if not out["ok"]:
        print(json.dumps(out, indent=2))
        print(hold_line(out), file=sys.stderr)
        return RUN_HOLD_EXIT
    entry = out["reserved"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        with locked_ledger() as ledger:
            release_booking(ledger, entry)
        print(f"run: could not start {argv[0]!r} ({exc}); slot given back", file=sys.stderr)
        return 127
    wait = None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if wait is None:
            wait = rate_limit_wait(line.strip())
    rc = proc.wait()
    if wait is not None:
        # CodeRabbit refused before reviewing, so the slot was not spent: give it back
        # and record the server's own window, which now blocks every session.
        limit_kind = "cli" if args.kind == "cli" else "pr"
        now = now_utc()
        with locked_ledger() as ledger:
            release_booking(ledger, entry)
            apply_limit(ledger, limit_kind, now + wait, args.note or args.repo, now)
        print(f"run: CodeRabbit refused ({limit_kind} rate limit, wait {wait}); "
              f"cooldown recorded, slot given back", file=sys.stderr)
    return rc


def cmd_record_trigger(args) -> int:
    if args.kind not in ALL_KINDS:
        print(json.dumps({"error": f"kind must be one of {ALL_KINDS}"}))
        return 2
    entry = {"ts": iso(args.at or now_utc()), "kind": args.kind, "repo": args.repo,
             "pr": args.pr, "note": args.note or ""}
    with locked_ledger() as ledger:
        ledger["triggers"].append(entry)
        out = {"recorded": entry, **summarize(ledger, args.plan, now_utc())}
    print(json.dumps(out, indent=2))
    return 0


def cmd_record_limit(args) -> int:
    now = now_utc()
    if args.until:
        until = parse_ts(args.until)
        if until is None:
            print(json.dumps({"error": f"could not parse --until {args.until!r}"}))
            return 2
    else:
        delta = parse_wait(args.wait or "")
        if delta is None:
            print(json.dumps({"error": f"could not parse --wait {args.wait!r}"}))
            return 2
        until = now + delta
    with locked_ledger() as ledger:
        cd = apply_limit(ledger, args.kind, until, args.source or "", now)
    print(json.dumps({"cooldown": cd, "kind": args.kind}, indent=2))
    return 0


def cmd_queue_add(args) -> int:
    with locked_ledger() as ledger:
        q = ledger.setdefault("queue", [])
        if not any(e.get("repo") == args.repo and e.get("pr") == args.pr for e in q):
            q.append({"repo": args.repo, "pr": args.pr,
                      "reason": args.reason or "", "queued_at": iso(now_utc())})
        out = {"queue": q}
    print(json.dumps(out, indent=2))
    return 0


def cmd_queue_list(args) -> int:
    with locked_ledger() as ledger:
        print(json.dumps({"queue": ledger.get("queue", [])}, indent=2))
    return 0


def cmd_queue_pop(args) -> int:
    with locked_ledger() as ledger:
        q = ledger.setdefault("queue", [])
        popped = None
        for i, e in enumerate(q):
            if args.repo and not (e.get("repo") == args.repo and e.get("pr") == args.pr):
                continue
            popped = q.pop(i)
            break
        out = {"popped": popped, "remaining": q}
    print(json.dumps(out, indent=2))
    return 0


def _gh(*args: str) -> tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "").strip() or (p.stderr or "").strip()


def cmd_failover(args) -> int:
    """Start the backup review (review-failover workflow) on one PR."""
    repo, pr = args.repo, args.pr
    # The backup reviewer runs on the owner's machine and covers every repo of
    # $REVIEW_FAILOVER_OWNER that is not archived and not a fork.
    rc, meta = _gh("repo", "view", repo, "--json", "owner,isArchived,isFork",
                   "--jq", r'"\(.owner.login) \(.isArchived) \(.isFork)"')
    owner = os.environ.get("REVIEW_FAILOVER_OWNER") or repo.split("/")[0]
    if rc != 0 or meta != f"{owner} false false":
        print(json.dumps({"ok": False, "reason": f"repo not covered by the backup reviewer ({meta[:120]})",
                          "hint": "queue-add instead"}))
        return 3
    rc, head = _gh("pr", "view", str(pr), "--repo", repo, "--json", "headRefOid,state",
                   "--jq", '.state + " " + .headRefOid')
    if rc != 0 or " " not in head:
        print(json.dumps({"ok": False, "reason": f"could not read PR: {head[:200]}"}))
        return 2
    state, sha = head.split(" ", 1)
    if state != "OPEN":
        print(json.dumps({"ok": False, "reason": f"PR is {state}"}))
        return 2
    rc, prev = _gh("api", f"repos/{repo}/commits/{sha}/statuses", "--jq",
                   '[.[] | select(.context=="review-failover")][0].state // ""')
    if rc == 0 and prev in ("pending", "success", "failure"):
        print(json.dumps({"ok": True, "skipped": True, "head": sha,
                          "reason": f"review-failover already {prev} on this head"}))
        return 0
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "would_comment": "/open-code-review", "head": sha}))
        return 0
    rc, out = _gh("pr", "comment", str(pr), "--repo", repo, "--body", "/open-code-review")
    if rc != 0:
        print(json.dumps({"ok": False, "reason": f"comment failed: {out[:200]}"}))
        return 2
    entry = {"ts": iso(now_utc()), "kind": "failover", "repo": repo, "pr": pr, "note": sha[:12]}
    with locked_ledger() as ledger:
        ledger["triggers"].append(entry)
        q = ledger.setdefault("queue", [])
        ledger["queue"] = [e for e in q if not (e.get("repo") == repo and e.get("pr") == pr)]
    print(json.dumps({"ok": True, "posted": out, "head": sha, "recorded": entry}, indent=2))
    return 0


# --- selftest -------------------------------------------------------------------

def _selftest() -> int:
    import shutil
    tmp = tempfile.mkdtemp(prefix="crbudget-test-")
    os.environ["CODERABBIT_LEDGER_DIR"] = tmp
    now = parse_ts("2026-07-26T12:00:00Z")
    cases = []

    def t(name, got, want):
        cases.append((name, got, want))

    # wait parsing
    t("wait_minutes", parse_wait("26 minutes"), timedelta(minutes=26))
    t("wait_min_sec", parse_wait("17 minutes and 42 seconds"), timedelta(minutes=17, seconds=42))
    t("wait_hour", parse_wait("1 hour and 5 minutes"), timedelta(hours=1, minutes=5))
    t("wait_bare", parse_wait("26"), timedelta(minutes=26))
    t("wait_garbage", parse_wait("wait until the next included review"), None)

    # tier tables
    t("pro_low", tier_limit("pro", 10), 5)
    t("pro_30", tier_limit("pro", 35), 4)
    t("pro_45", tier_limit("pro", 45), 3)
    t("pro_55", tier_limit("pro", 55), 2)
    t("pro_60", tier_limit("pro", 80), 1)
    t("proplus_85", tier_limit("pro+", 85), 2)
    t("free_flat", tier_limit("free", 100), 1)
    t("ent_flat", tier_limit("enterprise", 500), 12)

    def L(triggers=(), cooldowns=None, queue=()):
        return {"version": 1, "triggers": list(triggers),
                "cooldowns": cooldowns or {}, "queue": list(queue)}

    def trig(mins_ago, kind="manual_review"):
        return {"ts": iso(now - timedelta(minutes=mins_ago)), "kind": kind,
                "repo": "o/r", "pr": 1, "note": ""}

    # check: empty ledger -> ok
    t("empty_ok", decide_check(L(), "pr", "pro", now)["ok"], True)
    # active cooldown blocks pr, not cli
    cd = {"pr": {"until": iso(now + timedelta(minutes=20)), "source": "o/r#1",
                 "recorded_at": iso(now)}}
    t("cooldown_blocks_pr", decide_check(L(cooldowns=cd), "pr", "pro", now)["ok"], False)
    t("cooldown_pr_next_free", decide_check(L(cooldowns=cd), "pr", "pro", now)["next_free_at"],
      iso(now + timedelta(minutes=20)))
    t("cooldown_spares_cli", decide_check(L(cooldowns=cd), "cli", "pro", now)["ok"], True)
    # expired cooldown does not block
    cd2 = {"pr": {"until": iso(now - timedelta(minutes=1)), "source": "", "recorded_at": iso(now)}}
    t("expired_cooldown_ok", decide_check(L(cooldowns=cd2), "pr", "pro", now)["ok"], True)
    # hourly cap: 5 pr triggers in window blocks the 6th on pro (low 7d volume)
    five = [trig(i * 10) for i in range(5)]
    t("hourly_cap_blocks", decide_check(L(five), "pr", "pro", now)["ok"], False)
    t("hourly_cap_four_ok", decide_check(L(five[:4]), "pr", "pro", now)["ok"], True)
    # next_free = oldest in-window trigger + 1h
    t("hourly_next_free", decide_check(L(five), "pr", "pro", now)["next_free_at"],
      iso(parse_ts(five[4]["ts"]) + WINDOW))
    # adaptive: 60+ pr triggers in 7d -> limit 1; a single trigger in the hour blocks
    many = [trig(60 * 24 * 3 + i, "push") for i in range(65)] + [trig(30)]
    d = decide_check(L(many), "pr", "pro", now)
    t("adaptive_limit_1", d["hourly_limit_pr"], 1)
    t("adaptive_blocks", d["ok"], False)
    # cli budget independent of pr triggers
    t("cli_independent", decide_check(L(five), "cli", "pro", now)["ok"], True)
    cli6 = [trig(i * 5, "cli") for i in range(5)]
    t("cli_cap_blocks", decide_check(L(cli6), "cli", "pro", now)["ok"], False)
    # apply_limit: max wins
    led = L(cooldowns={"pr": {"until": iso(now + timedelta(minutes=30)), "source": "a",
                              "recorded_at": iso(now)}})
    apply_limit(led, "pr", now + timedelta(minutes=10), "b", now)
    t("limit_max_wins_keep", led["cooldowns"]["pr"]["until"], iso(now + timedelta(minutes=30)))
    apply_limit(led, "pr", now + timedelta(minutes=45), "c", now)
    t("limit_max_wins_extend", led["cooldowns"]["pr"]["until"], iso(now + timedelta(minutes=45)))
    # prune: old triggers dropped, recent kept
    led2 = L([trig(60 * 24 * 8), trig(5)])
    prune(led2, now)
    t("prune_drops_old", len(led2["triggers"]), 1)

    # check_and_book: the 5th cli slot books, the 6th holds (no double-booking race)
    led3 = L([trig(i * 5, "cli") for i in range(4)])
    first = check_and_book(led3, "cli", "cli", "o/r", None, "a", "pro", now)
    t("book_fifth_ok", first["ok"], True)
    t("book_fifth_recorded", len(led3["triggers"]), 5)
    t("book_sixth_holds", check_and_book(led3, "cli", "cli", "o/r", None, "b", "pro", now)["ok"], False)
    t("book_sixth_not_recorded", len(led3["triggers"]), 5)
    t("release_gives_back", release_booking(led3, first["reserved"]), True)
    t("release_count", len(led3["triggers"]), 4)
    # rate_limit event parsing (CodeRabbit CLI --agent NDJSON)
    ev = json.dumps({"type": "error", "errorType": "rate_limit", "message": "used all 5",
                     "metadata": {"waitTime": "2 minutes"}})
    t("rl_wait", rate_limit_wait(ev), timedelta(minutes=2))
    t("rl_other_error", rate_limit_wait(json.dumps({"type": "error", "errorType": "connection"})), None)
    t("rl_finding", rate_limit_wait(json.dumps({"type": "finding", "severity": "minor"})), None)
    t("rl_not_json", rate_limit_wait("plain text"), None)

    # run end to end on the temp ledger: book, hold without running, refund on rate_limit
    def run_cli(*cmd, note=""):
        ns = argparse.Namespace(argv=["--", *cmd], kind="cli", repo="o/r", pr=None,
                                as_kind=None, note=note, plan="pro")
        saved = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            return cmd_run(ns)
        finally:
            sys.stdout.close()
            sys.stdout = saved

    def ledger_cli_count():
        with locked_ledger() as led:
            return sum(1 for x in led["triggers"] if x.get("kind") == "cli")

    marker = Path(tmp) / "ran"
    t("run_ok_rc", run_cli(sys.executable, "-c", "print('review')"), 0)
    t("run_ok_booked", ledger_cli_count(), 1)
    t("run_passes_rc", run_cli(sys.executable, "-c", "import sys; sys.exit(3)"), 3)
    rl = ("import json; print(json.dumps({'type':'error','errorType':'rate_limit',"
          "'metadata':{'waitTime':'3 minutes'}}))")
    t("run_rl_rc", run_cli(sys.executable, "-c", rl), 0)
    t("run_rl_refunded", ledger_cli_count(), 2)
    with locked_ledger() as led:
        t("run_rl_cooldown", bool((led["cooldowns"].get("cli") or {}).get("until")), True)
    old_err = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        held = run_cli(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')")
    finally:
        sys.stderr.close()
        sys.stderr = old_err
    t("run_hold_rc", held, RUN_HOLD_EXIT)
    t("run_hold_never_runs", marker.exists(), False)
    t("run_hold_not_booked", ledger_cli_count(), 2)
    with locked_ledger() as led:
        led["cooldowns"] = {}
    t("run_missing_cmd_rc", run_cli("/nonexistent/coderabbit-xyz"), 127)
    t("run_missing_cmd_refunded", ledger_cli_count(), 2)

    ok = True
    for name, got, want in cases:
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[{flag}] {name}: got={got!r} want={want!r}")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PASSED" if ok else "\nSOME FAILED")
    return 0 if ok else 1


# --- main -----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Global CodeRabbit review-budget ledger.")
    ap.add_argument("--plan", default="pro",
                    help="free | oss | pro | pro-plus | enterprise (default pro)")
    sub = ap.add_subparsers(dest="cmd")

    def booking_args(p, required_repo):
        p.add_argument("--kind", choices=("pr", "cli"), default="pr")
        p.add_argument("--repo", required=required_repo)
        p.add_argument("--pr", type=int)
        p.add_argument("--as", dest="as_kind",
                       help="trigger kind to book for --kind pr (default manual_review)")
        p.add_argument("--note")

    sub.add_parser("status")
    p = sub.add_parser("check")
    booking_args(p, required_repo=False)
    p.add_argument("--reserve", action="store_true",
                   help="book the slot in the same locked step (needs --repo)")
    p = sub.add_parser("run", help="check, book and run the review command in one step")
    booking_args(p, required_repo=True)
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")
    p = sub.add_parser("record-trigger")
    p.add_argument("--kind", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int)
    p.add_argument("--note")
    p.add_argument("--at", type=parse_ts, help="ISO8601 backdate (for seeding history)")
    p = sub.add_parser("record-limit")
    p.add_argument("--kind", choices=("pr", "cli"), default="pr")
    p.add_argument("--wait")
    p.add_argument("--until")
    p.add_argument("--source")
    p = sub.add_parser("queue-add")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--reason")
    sub.add_parser("queue-list")
    p = sub.add_parser("queue-pop")
    p.add_argument("--repo")
    p.add_argument("--pr", type=int)
    p = sub.add_parser("failover")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("selftest")

    args = ap.parse_args()
    if args.cmd == "selftest":
        return _selftest()
    handlers = {
        "status": cmd_status, "check": cmd_check, "run": cmd_run,
        "record-trigger": cmd_record_trigger, "record-limit": cmd_record_limit,
        "queue-add": cmd_queue_add, "queue-list": cmd_queue_list,
        "queue-pop": cmd_queue_pop,
        "failover": cmd_failover,
    }
    if args.cmd not in handlers:
        ap.print_help()
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
