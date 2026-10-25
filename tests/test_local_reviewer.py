"""Offline tests for the backup reviewer's worker pool. No network, no gh, no claude.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_reviewer", ROOT / "reviewer/local_reviewer.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)


class Recorder:
    """A fake review_pr that sleeps and records when each review starts and ends."""

    def __init__(self, delay=0.3, results=None, errors=None):
        self.delay = delay
        self.results = results or {}
        self.errors = errors or {}
        self.mu = threading.Lock()
        self.spans: list[tuple[str, int, float, float]] = []
        self.calls: list[tuple[str, int, bool]] = []
        self.hooks: dict = {}

    def __call__(self, repo, pr, manual, dry):
        t0 = time.monotonic()
        with self.mu:
            self.calls.append((repo, pr, manual))
        hook = self.hooks.get((repo, pr))
        if hook:
            hook()
        time.sleep(self.delay)
        with self.mu:
            self.spans.append((repo, pr, t0, time.monotonic()))
        err = self.errors.get((repo, pr))
        if err:
            raise err
        return self.results.get((repo, pr), "done")

    def max_parallel(self) -> int:
        events = sorted([(s, 1) for _, _, s, _ in self.spans] + [(e, -1) for _, _, _, e in self.spans])
        cur = best = 0
        for _, d in events:
            cur += d
            best = max(best, cur)
        return best


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rf-test-"))
        self.patches = [
            mock.patch.object(lr, "OWNER", "o"),
            mock.patch.object(lr, "STATE_FILE", self.tmp / ".state/local.json"),
            mock.patch.object(lr, "LOCK_FILE", self.tmp / ".state/local.lock"),
            mock.patch.object(lr, "STATE_LOCK", self.tmp / ".state/state.lock"),
            mock.patch.object(lr, "REPO_LOCKS", self.tmp / ".state/repo-locks"),
            mock.patch.object(lr, "CONCURRENCY", 3),
            mock.patch.object(lr, "POLL_EVERY", 60),
            mock.patch.object(lr, "active_repos", lambda st: {"a", "b", "c", "d", "e"}),
            mock.patch.object(lr, "repos_with_open_prs", lambda: {"a", "b", "c", "d", "e"}),
            mock.patch.object(lr, "set_status", mock.Mock()),
            mock.patch.object(lr, "gh_json", mock.Mock(return_value={"headRefOid": "h"})),
            mock.patch.object(lr, "log", lambda msg: None),
        ]
        for p in self.patches:
            p.start()
        self.notify = mock.Mock()
        mock.patch.object(lr, "notify", self.notify).start()
        self.addCleanup(mock.patch.stopall)
        self.triggers: dict[str, list] = {}
        mock.patch.object(lr, "find_triggers", lambda repo, since: self.triggers.get(repo, [])).start()

    def queue(self, *items):
        lr.save_state({"queue": [{"repo": r, "pr": n, "manual": True, "attempts": 0,
                                  "key": f"{r}#{n}"} for r, n in items],
                       "last_poll_ok": lr.iso(lr.now())})

    def state(self) -> dict:
        return json.loads(lr.STATE_FILE.read_text())

    def trigger(self, repo, pr, cid, manual=True):
        self.triggers.setdefault(repo, []).append(
            {"repo": repo, "pr": pr, "manual": manual, "key": f"{repo}#{cid}@t"})


class Concurrency(Base):
    def test_different_repos_run_in_parallel(self):
        rec = Recorder()
        self.queue(("a", 1), ("b", 1), ("c", 1))
        with mock.patch.object(lr, "review_pr", rec):
            t0 = time.monotonic()
            self.assertEqual(lr.main([]), 0)
            took = time.monotonic() - t0
        self.assertEqual(rec.max_parallel(), 3)
        self.assertLess(took, 0.8)  # serial would be >= 0.9
        self.assertEqual(self.state()["queue"], [])

    def test_same_repo_runs_one_after_the_other(self):
        rec = Recorder(delay=0.2)
        self.queue(("a", 1), ("a", 2), ("b", 1))
        with mock.patch.object(lr, "review_pr", rec):
            lr.main([])
        a = sorted((s, e) for r, _, s, e in rec.spans if r == "a")
        self.assertEqual(len(a), 2)
        self.assertGreaterEqual(a[1][0], a[0][1])  # second starts after first ends
        self.assertEqual(rec.max_parallel(), 2)    # a#1 and b#1 overlapped
        self.assertEqual(self.state()["queue"], [])

    def test_pool_size_caps_parallel_reviews(self):
        rec = Recorder(delay=0.15)
        self.queue(("a", 1), ("b", 1), ("c", 1), ("d", 1), ("e", 1))
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "CONCURRENCY", 2):
            lr.main([])
        self.assertEqual(rec.max_parallel(), 2)
        self.assertEqual(len(rec.spans), 5)

    def test_concurrency_env(self):
        for raw, want in (("5", 5), ("0", 1), ("99", 8), ("x", 3)):
            with mock.patch.dict(lr.os.environ, {"REVIEW_FAILOVER_CONCURRENCY": raw}):
                self.assertEqual(lr._concurrency(), want)
        with mock.patch.dict(lr.os.environ, {}, clear=True):
            self.assertEqual(lr._concurrency(), 3)

    def test_repo_lock_blocks_a_second_review_of_the_same_repo(self):
        # A --once-pr run and a worker must not share one cache clone at the same time.
        rec = Recorder(delay=0.2)
        with mock.patch.object(lr, "review_pr", rec):
            ts = [threading.Thread(target=lr.review_locked,
                                   args=({"repo": "a", "pr": n, "manual": True}, False))
                  for n in (1, 2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(rec.max_parallel(), 1)

    def test_once_pr_does_not_need_the_run_lock(self):
        rec = Recorder(delay=0)
        lr.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(lr.LOCK_FILE, "w") as held, mock.patch.object(lr, "review_pr", rec):
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(lr.main(["--once-pr", "o/a#7"]), 0)
        self.assertEqual(rec.calls, [("a", 7, True)])


class StateAndTriggers(Base):
    def test_state_write_waits_for_the_state_lock(self):
        lr.STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        done = threading.Event()
        with open(lr.STATE_LOCK, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            t = threading.Thread(target=lambda: (lr.save_state({"x": 1}), done.set()))
            t.start()
            self.assertFalse(done.wait(0.3), "save_state wrote while another holder had the lock")
            fcntl.flock(held, fcntl.LOCK_UN)
        self.assertTrue(done.wait(2))
        t.join()
        self.assertEqual(self.state(), {"x": 1})

    def test_parallel_state_writes_stay_valid(self):
        errs = []

        def writer(n):
            try:
                for i in range(40):
                    lr.save_state({"w": n, "i": i, "pad": "x" * 2000})
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertIn("w", self.state())

    def test_state_saved_after_each_review_not_only_at_the_end(self):
        rec = Recorder(delay=0.05)
        rec_slow = Recorder(delay=0.6)
        seen_during = {}

        def fake(repo, pr, manual, dry):
            if repo == "a":
                return rec(repo, pr, manual, dry)
            time.sleep(0.3)  # a#1 is finished by now, b#1 is still running
            seen_during["queue"] = [(x["repo"], x["pr"]) for x in self.state()["queue"]]
            return rec_slow(repo, pr, manual, dry)
        self.queue(("a", 1), ("b", 1))
        with mock.patch.object(lr, "review_pr", fake):
            lr.main([])
        self.assertEqual(seen_during["queue"], [("b", 1)])

    def test_trigger_posted_during_a_running_batch_is_saved_and_reviewed(self):
        rec = Recorder(delay=0.1)
        saved_while_running = {}

        def slow_a():
            # Post a new trigger in another repo, then outlive one poll interval.
            self.trigger("b", 9, "c9")
            time.sleep(0.5)
            saved_while_running["queue"] = [(x["repo"], x["pr"]) for x in self.state()["queue"]]
        rec.hooks[("a", 1)] = slow_a
        rec.hooks[("b", 9)] = lambda: time.sleep(0.8)  # still queued when a#1 looks
        self.trigger("a", 1, "c1")
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "POLL_EVERY", 0.2):
            lr.main([])
        self.assertIn(("b", 9), saved_while_running["queue"])
        self.assertEqual(sorted((r, n) for r, n, _ in rec.calls), [("a", 1), ("b", 9)])
        self.assertEqual(self.state()["queue"], [])

    def test_trigger_for_the_pr_under_review_runs_it_again(self):
        rec = Recorder(delay=0.05)
        rec.hooks[("a", 1)] = lambda: (self.trigger("a", 1, f"c{len(rec.calls)}"),
                                       time.sleep(0.4) if len(rec.calls) == 1 else None)
        self.trigger("a", 1, "c0")
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "POLL_EVERY", 0.2):
            lr.main([])
        self.assertGreaterEqual(len(rec.calls), 2)
        self.assertEqual(self.state()["queue"], [])

    def test_head_moved_during_review_queues_the_new_head(self):
        rec = Recorder(delay=0, results={("a", 1): "moved"})
        self.queue(("a", 1))
        with mock.patch.object(lr, "review_pr", rec):
            lr.main([])
        q = self.state()["queue"]
        self.assertEqual([(x["repo"], x["pr"], x["manual"]) for x in q], [("a", 1, False)])
        self.assertEqual(len(rec.calls), 1)  # the new head waits for the retry delay
        self.assertIn("not_before", q[0])

    def test_review_pr_reports_moved_when_head_changes(self):
        heads = iter(["old", "new"])

        def gh(*args, inp=None):
            if args[:2] == ("pr", "view") and "state,isCrossRepository,headRefOid,baseRefName" in args:
                return {"state": "OPEN", "isCrossRepository": False,
                        "headRefOid": next(heads), "baseRefName": "main"}
            if args[:2] == ("pr", "view"):
                return {"headRefOid": next(heads)}
            return None
        with mock.patch.object(lr, "gh_json", gh), \
                mock.patch.object(lr, "prepare_worktree", lambda *a: self.tmp), \
                mock.patch.object(lr, "drop_worktree", lambda *a: None), \
                mock.patch.object(lr, "run_claude", lambda *a: {"findings": []}), \
                mock.patch.object(lr, "post_results", lambda *a: ("success", {"critical": 0})):
            self.assertEqual(lr.review_pr("a", 1, True, False), "moved")


class Failures(Base):
    def test_claude_rate_limit_backs_off_without_an_attempt(self):
        until = lr.now() + timedelta(minutes=30)
        rec = Recorder(delay=0, errors={("a", 1): lr.RateLimited("limit", until)})
        self.queue(("a", 1), ("b", 1))
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "CONCURRENCY", 1):
            lr.main([])
        st = self.state()
        self.assertEqual([(x["repo"], x["attempts"]) for x in st["queue"]], [("a", 0), ("b", 0)])
        self.assertEqual(st["claude_backoff_until"], lr.iso(until))
        self.assertEqual(rec.calls, [("a", 1, True)])  # b waits for the backoff
        self.notify.assert_not_called()

    def test_shorter_limit_does_not_cut_a_longer_backoff(self):
        far = lr.now() + timedelta(hours=3)
        lr.save_state({"queue": [{"repo": "a", "pr": 1, "manual": True, "attempts": 0, "key": "a"}],
                       "claude_backoff_until": lr.iso(far), "last_poll_ok": lr.iso(lr.now())})
        st = lr.load_state()
        fut = mock.Mock()
        fut.result.side_effect = lr.RateLimited("limit", lr.now() + timedelta(minutes=5))
        lr.finish(st, st["queue"][0], fut, [])
        self.assertEqual(st["claude_backoff_until"], lr.iso(far))

    def test_long_burst_push_names_a_few_and_counts_the_rest(self):
        errs = {(f"repo-with-a-long-name-{n}", 1): RuntimeError("boom") for n in range(12)}
        rec = Recorder(delay=0, errors=errs)
        lr.save_state({"queue": [{"repo": r, "pr": 1, "manual": True, "attempts": 2, "key": r}
                                 for r, _ in errs], "last_poll_ok": lr.iso(lr.now())})
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "CONCURRENCY", 8):
            lr.main([])
        msg = self.notify.call_args[0][0]
        self.assertLessEqual(len(msg), 190)
        self.assertRegex(msg, r"^Backup review failed 3x on 12 PRs: .+, \+\d+ more$")

    def test_killed_run_leaves_a_retry(self):
        calls = []
        lr.save_state({"queue": [{"repo": "a", "pr": 1, "manual": False, "attempts": 0, "key": "a",
                                  "running_since": lr.iso(lr.now())}],
                       "last_poll_ok": lr.iso(lr.now())})
        with mock.patch.object(lr, "review_pr", lambda r, n, m, d: calls.append(m) or "done"):
            lr.main([])
        self.assertEqual(calls, [True])  # skips its own stale `pending` status
        self.assertEqual(self.state()["queue"], [])

    def test_running_review_is_marked_in_saved_state(self):
        seen = {}

        def fake(repo, pr, manual, dry):
            seen["q"] = self.state()["queue"]
            return "done"
        self.queue(("a", 1))
        with mock.patch.object(lr, "review_pr", fake):
            lr.main([])
        self.assertIn("running_since", seen["q"][0])

    def test_worktree_is_created_under_base_not_tmp(self):
        cmds = []

        def fake_run(cmd, cwd=None, env=None, inp=None, timeout=300):
            cmds.append(cmd)
            return 0, "", ""
        (self.tmp / "cache/a/.git").mkdir(parents=True)
        stale = self.tmp / "wts/a/rf-9-old"
        other = self.tmp / "wts/a-b/rf-1-live"  # repo "a-b" must not be swept by repo "a"
        stale.mkdir(parents=True)
        other.mkdir(parents=True)
        with mock.patch.object(lr, "run", fake_run), \
                mock.patch.object(lr, "CACHE", self.tmp / "cache"), \
                mock.patch.object(lr, "WORKTREES", self.tmp / "wts"):
            wt = lr.prepare_worktree("a", 1, "h")
        self.assertEqual(wt.parent, self.tmp / "wts/a")
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())
        add = next(c for c in cmds if "add" in c)
        self.assertIn("core.symlinks=false", add)

    def test_claude_limit_backoff_is_capped(self):
        e = lr.claude_limit("Claude AI usage limit reached|9999999999")
        self.assertLessEqual(e.until, lr.now() + timedelta(hours=6, seconds=5))

    def test_rate_limit_backoff_doubles_and_gives_up_only_after_a_day(self):
        rec = Recorder(delay=0, errors={("a", 1): lr.RateLimited("limit")})
        self.queue(("a", 1))
        waits = []
        with mock.patch.object(lr, "review_pr", rec):
            for _ in range(6):
                lr.main([])
                st = self.state()
                (item,) = st["queue"]
                self.assertEqual(item["attempts"], 0)
                waits.append(lr.parse(item["not_before"]) - lr.now())
                st.pop("claude_backoff_until")
                item.pop("not_before")
                lr.save_state(st)
            mins = [round(w.total_seconds() / 60) for w in waits]
            self.assertEqual(mins, [15, 30, 60, 120, 240, 360])
            st = self.state()
            st["queue"][0]["limited_since"] = lr.iso(lr.now() - timedelta(hours=25))
            lr.save_state(st)
            lr.main([])
        (item,) = self.state()["queue"]
        self.assertEqual(item["attempts"], 1)  # a day of limits: now it counts

    def test_claude_limit_detection(self):
        epoch = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
        e = lr.claude_limit(f"Claude AI usage limit reached|{epoch}")
        self.assertIsInstance(e, lr.RateLimited)
        self.assertEqual(int(e.until.timestamp()), epoch)
        e = lr.claude_limit('API Error: 429 {"type":"rate_limit_error"}')
        self.assertIsNone(e.until)  # no reset time: the caller backs off
        self.assertIsNone(lr.claude_limit(f"usage limit reached|{epoch - 7 * 3600 * 2}").until)
        self.assertIsNone(lr.claude_limit("claude rc=1: error_max_turns"))

    def test_run_claude_raises_rate_limited(self):
        out = json.dumps({"is_error": True, "result": "You've hit your limit · resets 3pm"})
        with mock.patch.object(lr, "run", side_effect=[(0, "tok", ""), (0, "", ""), (0, "", ""),
                                                        (1, out, "")]), \
                mock.patch.object(lr, "CLAUDE_HOME", self.tmp / "ch"):
            with self.assertRaises(lr.RateLimited):
                lr.run_claude(self.tmp, "a", 1, "h", "main")

    def test_failure_waits_before_retry_and_counts(self):
        rec = Recorder(delay=0, errors={("a", 1): RuntimeError("boom")})
        self.queue(("a", 1))
        logs = []
        with mock.patch.object(lr, "review_pr", rec), mock.patch.object(lr, "log", logs.append):
            lr.main([])
            lr.main([])  # the next launchd run, before the retry delay
        self.assertEqual(len(rec.calls), 1)
        self.assertNotIn("another run is active; exit", logs)  # the second run really ran
        (item,) = self.state()["queue"]
        self.assertEqual(item["attempts"], 1)
        self.assertIn("not_before", item)

    def test_retry_of_an_auto_item_skips_its_own_pending_status(self):
        calls = []

        def fake(repo, pr, manual, dry):
            calls.append(manual)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "done"
        lr.save_state({"queue": [{"repo": "a", "pr": 1, "manual": False, "attempts": 0, "key": "a"}],
                       "last_poll_ok": lr.iso(lr.now())})
        with mock.patch.object(lr, "review_pr", fake):
            lr.main([])
            st = self.state()
            st["queue"][0]["not_before"] = lr.iso(lr.now() - timedelta(seconds=1))
            lr.save_state(st)
            lr.main([])
        self.assertEqual(calls, [False, True])
        self.assertEqual(self.state()["queue"], [])

    def test_burst_of_final_failures_sends_one_push(self):
        errs = {(r, 1): RuntimeError("boom") for r in "abc"}
        rec = Recorder(delay=0, errors=errs)
        lr.save_state({"queue": [{"repo": r, "pr": 1, "manual": True, "attempts": 2,
                                  "key": r} for r in "abc"], "last_poll_ok": lr.iso(lr.now())})
        with mock.patch.object(lr, "review_pr", rec):
            lr.main([])
        self.assertEqual(self.notify.call_count, 1)
        msg = self.notify.call_args[0][0]
        self.assertIn("3 PRs", msg)
        self.assertEqual(self.state()["queue"], [])


class Sandbox(Base):
    def test_parallel_reviews_each_read_only_their_own_worktree_and_cache(self):
        cmds = []
        mu = threading.Lock()

        def fake_run(cmd, cwd=None, env=None, inp=None, timeout=300):
            if cmd[0] == "security":
                return 0, "tok", ""
            if cmd[0] == "claude":
                t0 = time.monotonic()
                time.sleep(0.2)
                with mu:
                    cmds.append((cwd, cmd, t0, time.monotonic()))
                return 0, json.dumps({"structured_output": {"findings": []}}), ""
            return 0, "", ""

        def fake_review(repo, pr, manual, dry):
            wt = self.tmp / f"wt-{repo}"
            wt.mkdir(exist_ok=True)
            lr.run_claude(wt, repo, pr, "h", "main")
            return "done"
        self.queue(("a", 1), ("b", 1))
        with mock.patch.object(lr, "run", fake_run), mock.patch.object(lr, "review_pr", fake_review), \
                mock.patch.object(lr, "CLAUDE_HOME", self.tmp / "ch"), \
                mock.patch.object(lr, "CACHE", self.tmp / "cache"):
            lr.main([])
        self.assertEqual(len(cmds), 2)
        (_, _, s1, e1), (_, _, s2, e2) = cmds
        self.assertLess(max(s1, s2), min(e1, e2), "the two reviews did not overlap")
        for cwd, cmd, _, _ in cmds:
            repo = Path(cwd).name.removeprefix("wt-")
            settings = json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]
            self.assertEqual(settings["filesystem"]["allowRead"][:2],
                             [str(cwd), str(self.tmp / "cache" / repo)])
            self.assertFalse(settings["allowUnsandboxedCommands"])
            self.assertIn("Bash(git *--no-index*)", cmd[cmd.index("--disallowedTools") + 1])


if __name__ == "__main__":
    unittest.main()
