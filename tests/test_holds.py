"""The hold (core/holds.py): cards, decisions, retries, expiry, notification
and installation at start-up. Ported from passdesk-pro's tests when the
hold moved into core (docs/decisions.md, 2026-10-21)."""

import calendar
import io
import json
import os
import threading
import time
import unittest
import urllib.error
from unittest import mock

from passdesk.core import approvals as core_approvals
from passdesk.core import config, holds, notify, plugins, protocol
from passdesk.core.errors import Denied, Queued
from passdesk.tools import git

from . import test_git
from .base import POLICY, Base, _FakeHTTPResponse

CARD = {"tool": "git_push", "principal": "developer", "repo": "acme/widgets", "branch": "factory/x", "base": "main",
        "head": "abc123", "commits": 2, "shortstat": "1 file changed", "file_count": 1, "files": ["a.py"],
        "bundle_sha256": "f" * 64, "secret_scan": "clean"}
PR_CARD = {"tool": "github_create_pr", "principal": "developer", "repo": "acme/widgets", "head": "factory/x",
           "base": "main", "title": "T", "body": "", "draft": True}
HELD = ["git_push", "github_create_pr", "gitlab_create_mr"]


class HoldBase(Base):
    def setUp(self):
        super().setUp()
        self.policy = {"approvals": {"tools": list(HELD), "hold_seconds": 2, "expire_seconds": 60}}
        self.patches = [
            mock.patch.object(holds, "load_policy", lambda: self.policy),
            mock.patch.object(holds, "POLL_SECONDS", 0.05),
        ]
        for p in self.patches:
            p.start()
        self.sent = []
        # The fixture's notifier records the card id synchronously; tests of
        # the real one replace it.
        self.patches.append(mock.patch.object(notify, "send_async", lambda card, cid, m: self.sent.append(cid)))
        self.patches[-1].start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        core_approvals.set_hook(None)
        super().tearDown()

    def write_webhook(self, url: str) -> None:
        path = config.SECRETS_DIR / notify.CREDENTIAL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url + "\n")
        os.chmod(path, 0o600)

    def audit_records(self):
        try:
            lines = (config.STATE / "audit.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines]

    def audit_events(self):
        return [r.get("event") for r in self.audit_records()]

    def write_pending(self, card, cid, nonce, expires):
        path = config.STATE / "approvals" / "pending" / f"{cid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**card, "id": cid, "nonce": nonce, "created": expires - 400,
                                     "expires": expires}))
        return path

    def decide(self, cid, verdict="approve", approver="daron", reason="", delay=0.0, nonce=None):
        def write():
            time.sleep(delay)
            n = nonce
            if n is None:  # as the console does: echo the pending card's nonce
                try:
                    n = json.loads((config.STATE / "approvals" / "pending" / f"{cid}.json").read_text()).get("nonce")
                except (OSError, ValueError):
                    n = None
            p = config.STATE / "approvals" / "decisions" / f"{cid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"id": cid, "nonce": n, "decision": verdict, "approver": approver,
                                     "reason": reason, "ts": time.time()}))
        if delay:
            threading.Thread(target=write, daemon=True).start()
        else:
            write()


class HoldTests(HoldBase):
    def test_card_id_is_stable_across_volatile_fields(self):
        a = holds.card_id(CARD)
        b = holds.card_id({**CARD, "shortstat": "different", "files": []})
        c = holds.card_id({**CARD, "bundle_sha256": "0" * 64})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)

    def test_held_call_is_denied_with_the_id_and_a_pending_card(self):
        with self.assertRaises(Denied) as cm:
            holds.hook(CARD)
        cid = holds.card_id(CARD)
        self.assertIn(cid, str(cm.exception))
        self.assertIn("retry this exact call", str(cm.exception))
        pending = json.loads((config.STATE / "approvals" / "pending" / f"{cid}.json").read_text())
        self.assertEqual(pending["repo"], "acme/widgets")
        self.assertEqual(self.sent, [cid])
        self.assertEqual(self.audit_events(), ["approval-requested"])
        # A retry while still pending: no second notification, same hold.
        with self.assertRaises(Denied):
            holds.hook(CARD)
        self.assertEqual(self.sent, [cid])

    def test_approval_during_the_hold_lets_the_write_proceed(self):
        cid = holds.card_id(CARD)
        self.decide(cid, delay=0.3)
        started = time.monotonic()
        holds.hook(CARD)  # returns: approved
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-granted"])
        done = json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())
        self.assertEqual((done["decision"], done["approver"]), ("approve", "daron"))
        self.assertFalse((config.STATE / "approvals" / "pending" / f"{cid}.json").exists())
        self.assertFalse((config.STATE / "approvals" / "decisions" / f"{cid}.json").exists())

    def test_decision_made_between_calls_is_honoured_on_retry(self):
        with self.assertRaises(Denied):
            holds.hook(CARD)
        self.decide(holds.card_id(CARD))
        holds.hook(CARD)  # the retry goes through
        self.assertEqual(self.audit_events()[-1], "approval-granted")

    def test_gitlab_merge_requests_are_held_like_pull_requests(self):
        mr = {**PR_CARD, "tool": "gitlab_create_mr", "repo": "gitlab.acme.net/platform/api"}
        self.assertIn("gitlab_create_mr", holds.SUGGESTED_TOOLS)
        with self.assertRaises(Denied) as cm:
            holds.hook(mr)
        self.assertIn("held for approval", str(cm.exception))
        # The same fields identify it as a PR's do: a retry is the same card,
        # a different title is a different one.
        self.assertEqual(holds.card_id(mr), holds.card_id(dict(mr)))
        self.assertNotEqual(holds.card_id(mr), holds.card_id({**mr, "title": "other"}))
        self.assertNotEqual(holds.card_id(mr), holds.card_id(PR_CARD))
        self.assertNotEqual(holds.card_id({**mr, "body_sha256": "a" * 64}),
                            holds.card_id({**mr, "body_sha256": "b" * 64}))
        self.decide(holds.card_id(mr))
        holds.hook(mr)  # the retry goes through
        self.assertEqual(self.audit_events()[-1], "approval-granted")

    def test_decline_is_a_denial_with_the_reason(self):
        cid = holds.card_id(PR_CARD)
        with self.assertRaises(Denied):
            holds.hook(PR_CARD)  # held
        self.decide(cid, "decline", approver="sam", reason="wrong base branch")
        with self.assertRaises(Denied) as cm:
            holds.hook(PR_CARD)  # the retry hears the decision
        self.assertEqual(str(cm.exception), "declined by sam: wrong base branch")
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-declined"])

    def test_stale_decision_for_an_earlier_card_is_ignored(self):
        cid = holds.card_id(CARD)
        p = config.STATE / "approvals" / "decisions" / f"{cid}.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"id": cid, "nonce": "an-earlier-card", "decision": "approve", "approver": "old",
                                 "ts": time.time() + 3600}))  # even a future timestamp does not help
        with self.assertRaises(Denied) as cm:
            holds.hook(CARD)
        self.assertIn("held for approval", str(cm.exception))
        self.assertFalse(p.exists())

    def test_expired_card_is_replaced_and_its_decision_voided(self):
        cid = holds.card_id(CARD)
        pend = config.STATE / "approvals" / "pending" / f"{cid}.json"
        pend.parent.mkdir(parents=True)
        pend.write_text(json.dumps({**CARD, "id": cid, "created": time.time() - 500, "expires": time.time() - 100}))
        self.decide(cid)  # decided after it expired: void
        with self.assertRaises(Denied):
            holds.hook(CARD)
        self.assertEqual(self.audit_events(), ["approval-expired", "approval-requested"])
        self.assertGreater(json.loads(pend.read_text())["expires"], time.time())

    def test_expired_card_with_unused_approval_is_recorded(self):
        self.policy["approvals"]["hold_seconds"] = 0  # no decision will ever arrive for the fresh card
        cid = holds.card_id(CARD)
        pend = self.write_pending(CARD, cid, "n1", time.time() - 100)
        self.decide(cid, "approve", approver="daron", nonce="n1")
        with self.assertRaises(Denied):
            holds.hook(CARD)  # the agent never retried before it expired
        expired = next(r for r in self.audit_records() if r["event"] == "approval-expired")
        self.assertEqual(expired["decision"], "approve")
        self.assertEqual(expired["approver"], "daron")
        self.assertEqual(self.audit_events(), ["approval-expired", "approval-requested"])
        self.assertGreater(json.loads(pend.read_text())["expires"], time.time())  # a fresh card
        done = json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())
        self.assertEqual(done["decision"], "expired")
        self.assertEqual(done["approver"], "daron")

    def test_expired_card_with_unused_decline_is_recorded(self):
        self.policy["approvals"]["hold_seconds"] = 0
        cid = holds.card_id(CARD)
        self.write_pending(CARD, cid, "n1", time.time() - 100)
        self.decide(cid, "decline", approver="sam", reason="too risky", nonce="n1")
        with self.assertRaises(Denied):
            holds.hook(CARD)
        expired = next(r for r in self.audit_records() if r["event"] == "approval-expired")
        self.assertEqual(expired["decision"], "decline")
        self.assertEqual(expired["approver"], "sam")
        self.assertEqual(expired["reason"], "too risky")
        done = json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())
        self.assertEqual((done["decision"], done["approver"]), ("expired", "sam"))

    def test_expired_card_with_no_decision_has_no_decision_fields(self):
        self.policy["approvals"]["hold_seconds"] = 0
        cid = holds.card_id(CARD)
        self.write_pending(CARD, cid, "n1", time.time() - 100)
        with self.assertRaises(Denied):
            holds.hook(CARD)
        expired = next(r for r in self.audit_records() if r["event"] == "approval-expired")
        for key in ("decision", "approver", "reason", "decided"):
            self.assertNotIn(key, expired)
        done = json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())
        self.assertEqual(done["decision"], "expired")
        self.assertNotIn("approver", done)

    def test_expired_card_with_mismatched_nonce_decision_is_not_recorded(self):
        self.policy["approvals"]["hold_seconds"] = 0
        cid = holds.card_id(CARD)
        self.write_pending(CARD, cid, "n1", time.time() - 100)
        self.decide(cid, "approve", approver="daron", nonce="an-earlier-card")
        with self.assertRaises(Denied):
            holds.hook(CARD)
        expired = next(r for r in self.audit_records() if r["event"] == "approval-expired")
        self.assertNotIn("decision", expired)
        self.assertFalse((config.STATE / "approvals" / "decisions" / f"{cid}.json").exists())

    def test_sweep_expires_other_pending_cards_exactly_once(self):
        self.policy["approvals"]["hold_seconds"] = 0
        other_cid = holds.card_id(PR_CARD)
        other_pend = self.write_pending(PR_CARD, other_cid, "n2", time.time() - 100)
        with self.assertRaises(Denied):
            holds.hook(CARD)  # a different card's held call; its sweep catches the other one
        events = [r["event"] for r in self.audit_records() if r.get("id") == other_cid]
        self.assertEqual(events, ["approval-expired"])
        self.assertFalse(other_pend.exists())
        holds._sweep_expired(time.time())  # a later held call's sweep must not find it again
        events_after = [r["event"] for r in self.audit_records() if r.get("id") == other_cid]
        self.assertEqual(events_after, ["approval-expired"])

    def test_sweep_skips_pending_with_malformed_expires(self):
        # A malformed expires value in another pending file must not break
        # the sweep or the held call that runs it.
        self.policy["approvals"]["hold_seconds"] = 0
        bad_cid = holds.card_id(PR_CARD)
        bad_pend = config.STATE / "approvals" / "pending" / f"{bad_cid}.json"
        bad_pend.parent.mkdir(parents=True, exist_ok=True)
        bad_pend.write_text(json.dumps({**PR_CARD, "id": bad_cid, "nonce": "n-bad", "created": time.time(),
                                        "expires": "x"}))  # invalid: not a number
        with self.assertRaises(Denied):
            holds.hook(CARD)  # a different card's held call; its sweep must skip the bad one
        self.assertTrue(bad_pend.exists())  # bad file still there, skipped
        self.assertEqual(self.audit_events(), ["approval-requested"])  # no error from bad expires

    def test_expire_pending_ignores_a_card_already_replaced_with_a_fresh_nonce(self):
        # A stale caller (the sweep's snapshot, taken outside the lock, or a
        # retry's "existing" read) must not act on a card that was already
        # replaced by the time it runs: it must re-read and compare nonces.
        cid = holds.card_id(CARD)
        old_nonce = "old-nonce"
        pend = self.write_pending(CARD, cid, old_nonce, time.time() - 100)
        fresh = {**CARD, "id": cid, "nonce": "new-nonce", "created": time.time(), "expires": time.time() + 60}
        pend.write_text(json.dumps(fresh))  # replaced in between, as another thread would do
        holds._expire_pending(cid, old_nonce, time.time())  # the stale caller's call: a no-op
        self.assertEqual(self.audit_events(), [])
        self.assertEqual(json.loads(pend.read_text())["nonce"], "new-nonce")
        self.assertFalse((config.STATE / "approvals" / "done" / f"{cid}.json").exists())

    def test_consume_after_the_card_expired_meanwhile_is_denied_without_auditing(self):
        # The decision was read outside the lock; by the time _consume runs
        # under the lock, the card may have already expired and been filed
        # (and audited) elsewhere. Applying the decision anyway would grant
        # or decline a card that no longer exists, and audit it twice.
        cid = holds.card_id(CARD)
        nonce = "n1"
        pend = self.write_pending(CARD, cid, nonce, time.time() + 60)
        pending_snapshot = json.loads(pend.read_text())
        decision = {"id": cid, "nonce": nonce, "decision": "approve", "approver": "daron", "reason": "",
                    "ts": time.time()}
        pend.unlink()  # expired and filed elsewhere between the read and this call
        with self.assertRaises(Denied) as cm:
            holds._consume(decision, pending_snapshot, cid)
        self.assertIn("retry", str(cm.exception))
        self.assertEqual(self.audit_events(), [])
        self.assertFalse((config.STATE / "approvals" / "done" / f"{cid}.json").exists())

    def test_ensure_pending_itself_records_an_expired_card(self):
        # Single-threaded, direct call: exercises _ensure_pending's own
        # expiry branch without going through the sweep in hook().
        cid = holds.card_id(CARD)
        self.write_pending(CARD, cid, "n1", time.time() - 100)
        self.decide(cid, "approve", approver="daron", nonce="n1")
        pending = holds._ensure_pending(CARD, cid, 60, time.time())
        self.assertGreater(pending["expires"], time.time())
        self.assertNotEqual(pending["nonce"], "n1")
        expired = next(r for r in self.audit_records() if r["event"] == "approval-expired")
        self.assertEqual(expired["decision"], "approve")
        self.assertEqual(expired["approver"], "daron")

    def test_tools_not_in_policy_are_not_held(self):
        self.policy["approvals"]["tools"] = ["github_create_pr"]
        self.assertIsNone(holds.hook(CARD))  # git_push passes straight through
        self.assertEqual(self.audit_events(), [])

    def test_a_pending_card_with_an_unreadable_expiry_is_discarded_not_wedged(self):
        self.policy["approvals"]["hold_seconds"] = 0
        cid = holds.card_id(CARD)
        pend = config.STATE / "approvals" / "pending" / f"{cid}.json"
        pend.parent.mkdir(parents=True)
        pend.write_text(json.dumps({**CARD, "id": cid, "nonce": "n1", "created": time.time(), "expires": None}))
        self.decide(cid, nonce="n1")  # a decision for the broken card is voided with it
        with self.assertRaises(Denied) as cm:
            holds.hook(CARD)  # a fresh card, not a crash
        self.assertIn("held for approval", str(cm.exception))
        self.assertEqual(self.audit_events(), ["approval-discarded", "approval-requested"])
        fresh = json.loads(pend.read_text())
        self.assertNotEqual(fresh["nonce"], "n1")
        self.assertGreater(fresh["expires"], time.time())
        self.assertFalse((config.STATE / "approvals" / "decisions" / f"{cid}.json").exists())

    def test_odd_seconds_values_fall_back_to_the_defaults(self):
        for bad in ("abc", None, [1], True, {"x": 1}):
            hold, expire, _ = holds.settings({"approvals": {"hold_seconds": bad, "expire_seconds": bad}})
            self.assertEqual((hold, expire), (45, 86400), bad)
        self.assertEqual(holds.settings({"approvals": {"hold_seconds": 7.9, "expire_seconds": 100}})[:2], (7, 100))

    def test_expiry_allows_long_windows_up_to_a_week(self):
        for given, used in ((86400, 86400), (3 * 86400, 3 * 86400), (7 * 86400, 7 * 86400),
                            (30 * 86400, 7 * 86400), (10, 60), (-5, 60)):
            self.assertEqual(holds.settings({"approvals": {"expire_seconds": given}})[1], used, given)

    def test_settings_default_to_off(self):
        self.assertEqual(holds.settings({}), (45, 86400, []))
        self.assertEqual(holds.settings({"approvals": {"tools": []}})[2], [])
        self.assertEqual(holds.settings({"approvals": {"tools": "git_push"}})[2], [])  # not a list: off
        self.assertEqual(holds.settings({"approvals": {"tools": ["git_push", 3, ""]}})[2], ["git_push"])
        self.assertFalse(holds.enabled({}))
        self.assertTrue(holds.enabled({"approvals": {"tools": ["git_push"]}}))


class InstallTests(HoldBase):
    def test_nothing_listed_installs_no_hook(self):
        self.assertFalse(holds.install({}))
        self.assertFalse(holds.install({"approvals": {"tools": [], "hold_seconds": 5}}))
        self.assertFalse(core_approvals.hook_installed())
        self.assertEqual(self.audit_events(), [])
        core_approvals.require(CARD)  # the approval point passes straight through

    def test_listed_tools_install_the_hook_and_record_it(self):
        self.assertTrue(holds.install(self.policy))
        self.assertTrue(core_approvals.hook_installed())
        records = self.audit_records()
        self.assertEqual([r["event"] for r in records], ["approvals-enabled"])
        self.assertEqual(records[0]["tools"], HELD)
        self.assertEqual((records[0]["hold_seconds"], records[0]["expire_seconds"]), (2, 60))
        cid = holds.card_id(CARD)
        self.decide(cid, delay=0.3)
        core_approvals.PRINCIPAL.set("developer")
        core_approvals.require(dict(CARD))  # held, then approved
        self.assertEqual(self.audit_events(), ["approvals-enabled", "approval-requested", "approval-granted"])

    def test_the_server_installs_it_before_plugins(self):
        # protocol.main: the hold goes on first, so a plugin's hook (if any)
        # replaces it rather than the other way round.
        order = []
        with mock.patch.object(protocol.config, "validate", lambda: None), \
             mock.patch.object(protocol.config, "WORKSPACE", config.WORKSPACE), \
             mock.patch.object(protocol.config, "STATE", config.STATE), \
             mock.patch.object(protocol.config, "MIRRORS", config.STATE / "mirrors"), \
             mock.patch.object(protocol.policy, "load_policy", lambda: self.policy), \
             mock.patch.object(protocol.holds, "install", lambda pol: order.append(("holds", pol)) or True), \
             mock.patch.object(protocol.plugins, "load", lambda: order.append(("plugins", None)) or []), \
             mock.patch.object(protocol, "make_server", side_effect=OSError("stop here")), \
             self.assertRaises(SystemExit):
            protocol.main()
        self.assertEqual(order, [("holds", self.policy), ("plugins", None)])

    def test_the_retired_pro_plugin_is_refused_by_name(self):
        config.PLUGINS = "passdesk_pro"
        self.addCleanup(setattr, config, "PLUGINS", "")
        with self.assertRaises(ImportError) as cm:
            plugins.load()
        self.assertIn("[approvals].tools", str(cm.exception))
        tools, problems = plugins.declared_tools()
        self.assertEqual(tools, {})
        self.assertEqual(problems[0][0], "passdesk_pro")


class FakeStop:
    """A stand-in for the sweeper's stop event: wait() returns the next of
    `answers` at once (True stops the loop), runs `on_wait` first, and
    records each timeout, so the timer is tested without sleeping."""

    def __init__(self, answers, on_wait=None):
        self.answers = list(answers)
        self.on_wait = on_wait
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.on_wait:
            self.on_wait()
        return self.answers.pop(0) if self.answers else True


class IdleSweepTests(HoldBase):
    """Expired cards leave pending/ with no held call: once at start-up and
    on the sweeper's timer (doomtickle/passdesk-ops#141)."""

    def setUp(self):
        super().setUp()
        core_approvals.set_hook(holds.hook)
        self.cid = holds.card_id(CARD)
        self.pending = config.STATE / "approvals" / "pending" / f"{self.cid}.json"
        self.done = config.STATE / "approvals" / "done" / f"{self.cid}.json"

    def run_sweeper(self, stop, clock=time.time, interval=None):
        thread = holds.start_sweeper(interval=interval, clock=clock, stop=stop)
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def expired_events(self, cid):
        return [r for r in self.audit_records() if r.get("event") == "approval-expired" and r.get("id") == cid]

    def test_start_up_sweep_files_an_expired_card_with_its_late_decision(self):
        now = time.time()
        self.write_pending(CARD, self.cid, "n1", now - 10)
        self.decide(self.cid, verdict="decline", reason="too late")  # arrived, never applied
        live_cid = holds.card_id(PR_CARD)
        live = self.write_pending(PR_CARD, live_cid, "n2", now + 600)
        self.run_sweeper(FakeStop([True]))
        self.assertFalse(self.pending.exists())
        self.assertFalse((config.STATE / "approvals" / "decisions" / f"{self.cid}.json").exists())
        done = json.loads(self.done.read_text())
        self.assertEqual((done["decision"], done["nonce"], done["approver"], done["reason"]),
                         ("expired", "n1", "daron", "too late"))
        [event] = self.expired_events(self.cid)
        self.assertEqual((event["decision"], event["approver"]), ("decline", "daron"))
        self.assertTrue(live.exists())  # not yet expired: untouched
        self.assertEqual(self.expired_events(live_cid), [])

    def test_the_timer_files_a_card_that_expires_while_idle_exactly_once(self):
        start = time.time()
        self.write_pending(CARD, self.cid, "n1", start + 100)
        seen = []
        stop = FakeStop([False, False, False, True], on_wait=lambda: seen.append(self.pending.exists()))
        clock = iter([start, start + 50, start + 150, start + 210]).__next__
        self.run_sweeper(stop, clock=clock)
        # start-up (not yet expired), tick 1 (not yet), tick 2 (expired), tick 3 (already gone)
        self.assertEqual(seen, [True, True, False, False])
        self.assertEqual(stop.timeouts, [holds.SWEEP_SECONDS] * 4)
        self.assertEqual(len(self.expired_events(self.cid)), 1)
        self.assertEqual(json.loads(self.done.read_text())["decision"], "expired")

    def test_the_interval_can_be_set(self):
        stop = FakeStop([True])
        self.run_sweeper(stop, interval=7)
        self.assertEqual(stop.timeouts, [7])

    def test_nothing_runs_when_holds_are_off_or_a_plugin_replaced_the_hook(self):
        self.write_pending(CARD, self.cid, "n1", time.time() - 10)
        for installed in (None, lambda card: None):
            core_approvals.set_hook(installed)
            self.assertIsNone(holds.start_sweeper(stop=FakeStop([True])))
            holds.sweep_idle()
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.audit_events(), [])

    def test_a_failing_sweep_is_logged_and_the_timer_keeps_going(self):
        self.write_pending(CARD, self.cid, "n1", time.time() - 10)
        real = holds._sweep_expired
        calls = []

        def flaky(now):
            calls.append(now)
            if len(calls) == 1:
                raise PermissionError("/state/approvals/pending: secret-looking detail")
            real(now)

        err = io.StringIO()
        with mock.patch.object(holds, "_sweep_expired", flaky), mock.patch("sys.stderr", err):
            self.run_sweeper(FakeStop([False, True]))
        self.assertEqual(len(calls), 2)  # start-up (failed), then the first tick
        self.assertIn("the expiry sweep failed: PermissionError", err.getvalue())
        self.assertNotIn("secret-looking", err.getvalue())  # by type only
        self.assertFalse(self.pending.exists())
        self.assertEqual(len(self.expired_events(self.cid)), 1)

    def test_a_failing_patch_prune_never_escapes(self):
        err = io.StringIO()
        with mock.patch.object(core_approvals, "prune_patches", side_effect=OSError("x")), \
             mock.patch("sys.stderr", err):
            holds.sweep_idle()
        self.assertIn("pruning patches failed: OSError", err.getvalue())

    def test_a_held_call_survives_a_failing_sweep(self):
        self.policy["approvals"]["hold_seconds"] = 0
        with mock.patch.object(holds, "_sweep_expired", side_effect=OSError("disk full")), \
             mock.patch("sys.stderr", io.StringIO()), self.assertRaises(Denied) as cm:
            holds.hook(CARD)
        self.assertIn("held for approval", str(cm.exception))
        self.assertTrue(self.pending.exists())

    def test_the_idle_sweep_prunes_the_patch_only_an_expired_card_named(self):
        now = time.time()
        old = now - core_approvals.PATCH_KEEP_SECONDS - 60
        expired_sha = core_approvals.store_patch(b"expired card's patch\n")
        live_sha = core_approvals.store_patch(b"live card's patch\n")
        for sha in (expired_sha, live_sha):
            os.utime(core_approvals.patches_dir() / f"{sha}.patch", (old, old))
        self.write_pending({**CARD, "patch_sha256": expired_sha}, self.cid, "n1", now - 10)
        live_cid = holds.card_id(PR_CARD)
        self.write_pending({**PR_CARD, "patch_sha256": live_sha}, live_cid, "n2", now + 600)
        holds.sweep_idle(now)
        self.assertFalse((core_approvals.patches_dir() / f"{expired_sha}.patch").exists())
        self.assertTrue((core_approvals.patches_dir() / f"{live_sha}.patch").exists())
        self.assertEqual(json.loads(self.done.read_text())["patch_sha256"], expired_sha)

    def test_the_server_starts_the_sweeper_after_plugins_and_before_serving(self):
        order = []

        class FakeServer:
            def serve_forever(self):
                order.append("serve")

        with mock.patch.object(protocol.config, "validate", lambda: None), \
             mock.patch.object(protocol.config, "WORKSPACE", config.WORKSPACE), \
             mock.patch.object(protocol.config, "STATE", config.STATE), \
             mock.patch.object(protocol.config, "MIRRORS", config.STATE / "mirrors"), \
             mock.patch.object(protocol.policy, "load_policy", lambda: self.policy), \
             mock.patch.object(protocol.holds, "install", lambda pol: order.append("holds") or True), \
             mock.patch.object(protocol.plugins, "load", lambda: order.append("plugins") or []), \
             mock.patch.object(protocol, "make_server", lambda: FakeServer()), \
             mock.patch.object(protocol.holds, "start_sweeper", lambda: order.append("sweeper")):
            protocol.main()
        self.assertEqual(order, ["holds", "plugins", "sweeper", "serve"])


ISO_UTC = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"


class HeldOutcomeTests(HoldBase):
    """An approved hold tells the tool who approved it and when, so its
    result can say the write was held (doomtickle/passdesk-ops#141)."""

    def pending(self, cid):
        return config.STATE / "approvals" / "pending" / f"{cid}.json"

    def test_approval_during_the_hold_returns_the_approver_and_time(self):
        cid = holds.card_id(CARD)
        self.decide(cid, approver="daron", delay=0.3)
        outcome = holds.hook(CARD)
        self.assertEqual(set(outcome), {"id", "approver", "decided", "waited_seconds"})
        self.assertEqual((outcome["id"], outcome["approver"]), (cid, "daron"))
        self.assertRegex(outcome["decided"], ISO_UTC)
        self.assertIsInstance(outcome["waited_seconds"], int)
        self.assertLessEqual(outcome["waited_seconds"], 2)

    def test_a_retry_after_the_decision_reports_how_long_the_write_waited(self):
        cid = holds.card_id(CARD)
        with self.assertRaises(Denied):
            holds.hook(CARD)
        card = json.loads(self.pending(cid).read_text())
        card["created"] -= 600  # first held ten minutes ago
        self.pending(cid).write_text(json.dumps(card))
        p = config.STATE / "approvals" / "decisions" / f"{cid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        decided = card["created"] + 125
        p.write_text(json.dumps({"id": cid, "nonce": card["nonce"], "decision": "approve", "approver": "sam",
                                 "reason": "", "ts": decided}))
        outcome = holds.hook(CARD)
        self.assertEqual(outcome["approver"], "sam")
        self.assertEqual(outcome["waited_seconds"], 125)
        self.assertEqual(outcome["decided"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(decided)))

    def test_an_odd_decision_time_counts_as_now(self):
        cid = holds.card_id(PR_CARD)
        with self.assertRaises(Denied):
            holds.hook(PR_CARD)
        card = json.loads(self.pending(cid).read_text())
        p = config.STATE / "approvals" / "decisions" / f"{cid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"id": cid, "nonce": card["nonce"], "decision": "approve", "approver": "",
                                 "ts": "yesterday"}))
        before = time.time()
        outcome = holds.hook(PR_CARD)
        self.assertEqual(outcome["approver"], "unknown")
        self.assertRegex(outcome["decided"], ISO_UTC)
        self.assertGreaterEqual(calendar.timegm(time.strptime(outcome["decided"], "%Y-%m-%dT%H:%M:%SZ")),
                                int(before) - 1)

    def test_a_non_finite_or_out_of_range_time_counts_as_now(self):
        # Python's json reads 1e999 as inf and accepts NaN; gmtime raises on
        # those and on 1e20. The approval is filed and audited by then, so a
        # crash would lose the write and a retry would need a second approval.
        for raw in ("1e999", "-1e999", "NaN", "1e20"):
            for field in ("ts", "created"):
                with self.subTest(raw=raw, field=field):
                    cid = holds.card_id(PR_CARD)
                    with self.assertRaises(Denied):
                        holds.hook(PR_CARD)
                    pending = self.pending(cid)
                    card = json.loads(pending.read_text())
                    ts = time.time()
                    if field == "created":
                        pending.write_text(json.dumps({**card, "created": 0}).replace('"created": 0', f'"created": {raw}'))
                    p = config.STATE / "approvals" / "decisions" / f"{cid}.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps({"id": cid, "nonce": card["nonce"], "decision": "approve",
                                             "approver": "daron", "ts": 0}).replace('"ts": 0', f'"ts": {raw if field == "ts" else ts}'))
                    before = time.time()
                    outcome = holds.hook(PR_CARD)
                    self.assertEqual(outcome["approver"], "daron")
                    self.assertRegex(outcome["decided"], ISO_UTC)
                    self.assertGreaterEqual(calendar.timegm(time.strptime(outcome["decided"], "%Y-%m-%dT%H:%M:%SZ")),
                                            int(min(before, ts)) - 1)
                    self.assertGreaterEqual(outcome["waited_seconds"], 0)
                    self.assertFalse(pending.exists())

    def test_require_passes_the_outcome_on_and_the_note_names_it(self):
        core_approvals.set_hook(holds.hook)
        cid = holds.card_id(CARD)
        self.decide(cid, approver="daron", delay=0.2)
        outcome = core_approvals.require(dict(CARD))
        self.assertEqual(core_approvals.HELD_ID.get(), cid)
        note = core_approvals.held_note(outcome)
        self.assertRegex(note, rf"^Held for approval \(id {cid}\); approved by \"daron\" at "
                               r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ after \d+ s\.$")

    def test_a_write_the_policy_does_not_hold_gets_no_outcome(self):
        core_approvals.set_hook(holds.hook)
        core_approvals.HELD_ID.set("")
        self.policy["approvals"]["tools"] = ["github_create_pr"]
        self.assertIsNone(core_approvals.require(dict(CARD)))
        self.assertEqual(core_approvals.HELD_ID.get(), "")
        self.assertEqual(core_approvals.with_held_note("Pushed.", None), "Pushed.")


class CardRedactionTests(HoldBase):
    """A credential-looking string in a card field must not reach the
    webhook summary or the pending card file."""

    def test_leaked_token_in_pr_title_does_not_reach_webhook_or_pending_card(self):
        token = "ghp_" + "a" * 36
        card = {**PR_CARD, "title": f"Fix: rotate {token} now"}
        core_approvals.set_hook(holds.hook)
        core_approvals.PRINCIPAL.set("developer")
        captured = {}

        def fake_send_async(sent_card, cid, minutes):
            captured["cid"] = cid
            captured["text"] = notify.summary(sent_card, cid, minutes)
        self.patches[-1].stop()  # replace the fixture's no-op notifier with this spy
        with mock.patch.object(notify, "send_async", fake_send_async), self.assertRaises(Denied):
            core_approvals.require(card)
        pending = json.loads(
            (config.STATE / "approvals" / "pending" / f"{captured['cid']}.json").read_text())
        self.assertNotIn(token, pending["title"])
        self.assertNotIn(token, captured["text"])
        self.assertIn("Fix: rotate", pending["title"])  # the title itself survives, just scrubbed


class NotifyTests(HoldBase):
    def setUp(self):
        super().setUp()
        self.patches[-1].stop()  # real notify.send_async for these

    def test_no_webhook_credential_means_no_send(self):
        self.assertFalse(notify.send(CARD, "abc", 30))
        self.assertIsNone(notify.webhook_url())

    def test_a_non_https_webhook_is_ignored(self):
        self.write_webhook("http://hooks.example.invalid/abc")
        self.assertIsNone(notify.webhook_url())

    def test_a_webhook_with_userinfo_is_refused_before_urllib_can_echo_it(self):
        # urllib raises InvalidURL quoting the netloc, password included,
        # for a URL like this; it must never get that far.
        self.write_webhook("https://user:s3cret-pw@ntfy.example.invalid/topic")
        self.assertIsNone(notify.webhook_url())
        self.assertFalse(notify.usable_url("https://@host/"))
        self.assertFalse(notify.usable_url("https:///path"))
        self.assertTrue(notify.usable_url("https://hooks.example.invalid/abc"))

    def test_an_http_client_exception_is_logged_by_type_only(self):
        import http.client
        url = "https://hooks.example.invalid/abc"
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open",
                               side_effect=http.client.BadStatusLine(f"garbage from {url}")):
            self.assertFalse(notify.send(CARD, "abc", 30, url=url))
        record = self.audit_records()[-1]
        self.assertEqual(record["event"], "approval-notify-failed")
        self.assertEqual(record["reason"], "BadStatusLine")
        self.assertNotIn(url, json.dumps(record))

    def test_the_thread_never_raises(self):
        # Even an exception type nothing anticipated must not reach
        # threading.excepthook (whose traceback would print the URL).
        self.write_webhook("https://hooks.example.invalid/abc")
        import http.client
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open", side_effect=http.client.InvalidURL("x")):
            seen = []
            with mock.patch.object(threading, "excepthook", lambda args: seen.append(args)):
                notify.send_async(CARD, "abc", 30).join(5)
        self.assertEqual(seen, [])
        self.assertEqual(self.audit_events(), ["approval-notify-failed"])

    def test_summary_names_the_essentials(self):
        text = notify.summary(CARD, "abc", 30)
        for piece in ("developer", "factory/x", "acme/widgets", "2 commit(s)", "abc", "30 min", "approve abc"):
            self.assertIn(piece, text)
        self.assertIn("draft pull request", notify.summary(PR_CARD, "x", 5))

    def test_post_failure_is_logged_not_raised(self):
        self.write_webhook("https://hooks.example.invalid/abc")
        import urllib.error
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open", side_effect=urllib.error.URLError("no route")):
            self.assertFalse(notify.send(CARD, "abc", 30))
        self.assertEqual(self.audit_events(), ["approval-notify-failed"])

    def test_successful_post_carries_the_text_under_each_key(self):
        self.write_webhook("https://hooks.example.invalid/abc")
        seen = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=0):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return Resp()
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open", fake_open):
            self.assertTrue(notify.send(CARD, "abc", 30))
        self.assertEqual(seen["url"], "https://hooks.example.invalid/abc")
        self.assertEqual(seen["body"]["text"], seen["body"]["content"])
        self.assertEqual(seen["body"]["text"], seen["body"]["message"])

    def test_a_queued_writes_outcome_is_posted_as_one_line(self):
        self.write_webhook("https://hooks.example.invalid/abc")
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=0):
            seen.append(json.loads(req.data)["text"])
            return Resp()
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open", fake_open):
            notify.send_result_async(CARD, "abc", "push of factory/x to acme/widgets", True,
                                     "Pushed 2 commit(s) to acme/widgets:factory/x (abc123).\nok").join(5)
            notify.send_result_async(CARD, "abc", "push of factory/x to acme/widgets", False,
                                     "Push rejected by GitHub:\n<untrusted>...").join(5)
        self.assertEqual(seen, [
            "Passdesk approval abc: the gateway made the approved push of factory/x to acme/widgets: "
            "Pushed 2 commit(s) to acme/widgets:factory/x (abc123).",
            "Passdesk approval abc: the approved push of factory/x to acme/widgets FAILED: Push rejected by GitHub: "
            "(factory approvals show abc)"])
        self.assertLessEqual(len(notify.result_summary("abc", "x", True, "y" * 5000)), notify.MAX_SUMMARY_LEN)

    def test_the_hold_does_not_wait_for_the_webhook(self):
        # A webhook that hangs for longer than the hold must not stretch
        # the held call: the POST runs on its own thread.
        self.write_webhook("https://hooks.example.invalid/abc")
        self.policy["approvals"]["hold_seconds"] = 0
        release = threading.Event()
        started = threading.Event()

        def slow_open(req, timeout=0):
            started.set()
            release.wait(5)
            raise OSError("gave up")
        with mock.patch.object(notify.NO_REDIRECT_OPENER, "open", slow_open):
            t0 = time.monotonic()
            with self.assertRaises(Denied):
                holds.hook(CARD)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertTrue(started.wait(2))
            release.set()
            for th in threading.enumerate():
                if th.name.startswith("notify-"):
                    th.join(2)
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-notify-failed"])


if __name__ == "__main__":
    unittest.main()


# --- the approve-later queue (doomtickle/passdesk-ops#91) ----------------------

JOB = {"repo": "acme/widgets", "branch": "factory/x", "base": "main", "head": "abc123",
       "bundle_sha256": "f" * 64, "commits": 2}


class QueueBase(HoldBase):
    """Core's hook installed, a call that waits no time, and a completer for
    git_push that records what it is asked to write."""

    def setUp(self):
        super().setUp()
        self.policy["approvals"]["hold_seconds"] = 0
        core_approvals.set_hook(holds.hook)
        token = core_approvals.PRINCIPAL.set("developer")
        self.addCleanup(core_approvals.PRINCIPAL.reset, token)
        self.writes = []
        self.write_result = "Pushed 2 commit(s) to acme/widgets:factory/x (abc123)."
        self.write_delay = 0.0
        self.write_error = None

        def complete(job, card, policy):
            self.writes.append((job, card))
            time.sleep(self.write_delay)
            if self.write_error:
                raise self.write_error
            return self.write_result
        deferred = mock.patch.dict(core_approvals._deferred, {"git_push": complete})
        deferred.start()
        self.addCleanup(deferred.stop)
        self.results = []
        results = mock.patch.object(notify, "send_result_async",
                                    lambda card, cid, what, ok, result: self.results.append((cid, ok, result)))
        results.start()
        self.addCleanup(results.stop)
        self.cid = holds.card_id(CARD)

    def path(self, kind, cid=None):
        return config.STATE / "approvals" / kind / f"{cid or self.cid}.json"

    def read(self, kind, cid=None):
        return json.loads(self.path(kind, cid).read_text())

    def call(self, card=CARD, job=JOB):
        """The tool's side: require with a job, as git_push calls it."""
        card = {k: v for k, v in card.items() if k != "principal"}
        return core_approvals.require(card, job=job)

    def queue(self):
        with self.assertRaises(Queued) as cm:
            self.call()
        return cm.exception


class QueueTests(QueueBase):
    def test_undecided_after_the_hold_is_queued_as_a_result_not_a_refusal(self):
        q = self.queue()
        self.assertEqual((q.cid, q.state), (self.cid, "queued"))
        self.assertTrue(q.text.startswith(f"Queued for approval (id {self.cid}); nothing has been sent yet."), q.text)
        self.assertIn("push of factory/x to acme/widgets", q.text)
        self.assertIn("Report it as queued, not done.", q.text)
        self.assertIn(f"factory approvals approve {self.cid}", q.text)
        rec = self.read("queued")
        self.assertEqual((rec["id"], rec["nonce"], rec["tool"], rec["job"]),
                         (self.cid, self.read("pending")["nonce"], "git_push", JOB))
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-queued"])
        self.assertEqual(core_approvals.HELD_ID.get(), self.cid)
        self.assertEqual(self.writes, [])

    def test_approval_inside_the_hold_is_unchanged(self):
        self.policy["approvals"]["hold_seconds"] = 2
        self.decide(self.cid, delay=0.3)
        outcome = self.call()
        self.assertEqual((outcome["id"], outcome["approver"]), (self.cid, "daron"))
        self.assertEqual(self.writes, [])  # the tool writes, in the call
        self.assertFalse(self.path("queued").exists())
        self.assertNotIn("queued", self.read("done"))
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-granted"])

    def test_approval_makes_the_queued_write_once_and_files_the_result(self):
        self.queue()
        self.decide(self.cid, approver="daron")
        holds.sweep_idle()
        [(job, card)] = self.writes
        self.assertEqual(job, JOB)
        self.assertEqual((card["id"], card["head"], card["bundle_sha256"]), (self.cid, "abc123", "f" * 64))
        done = self.read("done")
        self.assertEqual((done["queued"], done["decision"], done["approver"], done["write"], done["result"]),
                         (True, "approve", "daron", "done", self.write_result))
        self.assertIsInstance(done["finished"], float)
        self.assertNotIn("job", done)
        for kind in ("pending", "decisions", "queued"):
            self.assertFalse(self.path(kind).exists(), kind)
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-queued", "approval-granted",
                                               "approval-write-done"])
        granted = self.audit_records()[2]
        self.assertTrue(granted["queued"])
        self.assertEqual(self.results, [(self.cid, True, self.write_result)])
        holds.sweep_idle()
        holds.run_queued()
        self.assertEqual(len(self.writes), 1)

    def test_a_retry_while_queued_reports_the_same_id_and_queues_nothing_more(self):
        first = self.queue()
        nonce = self.read("queued")["nonce"]
        second = self.queue()
        self.assertEqual((second.cid, second.state), (first.cid, "queued"))
        self.assertTrue(second.text.startswith(f"Queued for approval (id {self.cid})"))
        self.assertEqual(self.read("queued")["nonce"], nonce)
        self.assertEqual(self.sent, [self.cid])  # notified once
        self.assertEqual(self.audit_events().count("approval-queued"), 1)
        self.assertEqual(self.writes, [])

    def test_a_retry_after_approval_makes_the_write_in_that_call_once(self):
        self.queue()
        self.decide(self.cid, approver="daron")
        with self.assertRaises(Queued) as cm:
            self.call()
        self.assertEqual(cm.exception.state, "completed")
        lines = cm.exception.text.splitlines()
        self.assertEqual(lines[0], self.write_result)
        self.assertRegex(lines[-1], rf'^Held for approval \(id {self.cid}\); approved by "daron" at '
                                    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ after \d+ s; written by the gateway at "
                                    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\.$")
        holds.sweep_idle()
        with self.assertRaises(Queued):
            self.call()
        self.assertEqual(len(self.writes), 1)

    def test_a_retry_after_the_gateway_made_the_write_reports_its_result(self):
        self.queue()
        self.decide(self.cid)
        holds.sweep_idle()
        for _ in range(2):
            with self.assertRaises(Queued) as cm:
                self.call()
            self.assertEqual((cm.exception.state, cm.exception.text.splitlines()[0]), ("completed", self.write_result))
            self.assertIn("written by the gateway", cm.exception.text)
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.path("pending").exists())  # no new card

    def test_after_the_result_window_the_same_call_is_a_new_request(self):
        self.queue()
        self.decide(self.cid)
        holds.sweep_idle()
        done = self.read("done")
        self.path("done").write_text(json.dumps({**done, "finished": time.time() - 61}))
        q = self.queue()  # expire_seconds is 60 in this fixture
        self.assertEqual(q.state, "queued")
        self.assertTrue(self.path("pending").exists())

    def test_a_retry_while_the_gateway_is_writing_says_so(self):
        self.queue()
        self.decide(self.cid)
        self.write_delay = 0.5
        runner = threading.Thread(target=holds.run_queued)
        runner.start()
        deadline = time.monotonic() + 5
        while not self.writes and time.monotonic() < deadline:
            time.sleep(0.01)
        with self.assertRaises(Queued) as cm:
            self.call()
        runner.join(5)
        self.assertEqual(cm.exception.state, "queued")
        self.assertTrue(cm.exception.text.startswith(f"Approved (id {self.cid}); the gateway is making this write"))
        self.assertEqual(len(self.writes), 1)

    def test_a_decline_is_recorded_nothing_is_written_and_a_retry_hears_it(self):
        self.queue()
        self.decide(self.cid, verdict="decline", approver="daron", reason="wrong branch")
        holds.sweep_idle()
        self.assertEqual(self.writes, [])
        done = self.read("done")
        self.assertEqual((done["decision"], done["write"], done["queued"]), ("decline", "skipped", True))
        self.assertEqual(self.audit_events()[-1], "approval-declined")
        self.assertTrue(self.audit_records()[-1]["queued"])
        self.assertEqual(self.results, [])
        for _ in range(2):
            with self.assertRaises(Denied) as cm:
                self.call()
            self.assertEqual(str(cm.exception), f"declined by daron: wrong branch (approval {self.cid}, queued)")
        self.assertEqual(self.writes, [])

    def test_expiry_is_recorded_nothing_is_written_and_a_late_approval_does_nothing(self):
        self.queue()
        holds.sweep_idle(time.time() + 61)
        done = self.read("done")
        self.assertEqual((done["decision"], done["write"], done["queued"]), ("expired", "skipped", True))
        self.assertFalse(self.path("queued").exists())
        expired = self.audit_records()[-1]
        self.assertEqual((expired["event"], expired["queued"]), ("approval-expired", True))
        self.decide(self.cid, nonce=done["nonce"])  # too late: no card to apply it to
        holds.sweep_idle()
        self.assertEqual(self.writes, [])
        q = self.queue()  # the same call again: a fresh card, queued again
        self.assertEqual(q.state, "queued")
        self.assertNotEqual(self.read("pending")["nonce"], done["nonce"])

    def test_a_decision_exactly_as_the_hold_ends_is_applied_in_the_call_once(self):
        real = holds._queue

        def decided_meanwhile(pending, cid, job):
            self.decide(cid)
            return real(pending, cid, job)
        with mock.patch.object(holds, "_queue", decided_meanwhile):
            outcome = self.call()
        self.assertEqual(outcome["id"], self.cid)  # approved: the tool writes, in the call
        self.assertFalse(self.path("queued").exists())
        holds.sweep_idle()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.audit_events(), ["approval-requested", "approval-granted"])

    def test_a_decision_just_after_queueing_is_made_by_the_queue_once(self):
        real = holds._queue

        def decided_after(pending, cid, job):
            queued = real(pending, cid, job)
            self.decide(cid)
            return queued
        with mock.patch.object(holds, "_queue", decided_after), self.assertRaises(Queued):
            self.call()
        self.write_delay = 0.2
        racers = [threading.Thread(target=holds.run_queued) for _ in range(3)]
        racers.append(threading.Thread(target=lambda: self.assertRaises(Queued, self.call)))
        for t in racers:
            t.start()
        for t in racers:
            t.join(5)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.audit_events().count("approval-write-done"), 1)

    def test_a_failed_write_is_recorded_and_reported_once_then_a_new_request(self):
        self.write_error = Denied("factory/x changed in the broker's mirror after the push was queued")
        self.queue()
        self.decide(self.cid)
        holds.sweep_idle()
        done = self.read("done")
        self.assertEqual((done["write"], done["result"]),
                         ("failed", "factory/x changed in the broker's mirror after the push was queued"))
        failed = self.audit_records()[-1]
        self.assertEqual((failed["event"], failed["id"]), ("approval-write-failed", self.cid))
        self.assertEqual(self.results, [(self.cid, False, done["result"])])
        with self.assertRaises(Denied) as cm:
            self.call()
        self.assertIn(f"the gateway could not make the approved write (id {self.cid}): factory/x changed",
                      str(cm.exception))
        self.assertEqual(self.queue().state, "queued")  # reported once; this call asks again

    def test_a_crashing_completer_is_recorded_by_the_runner_not_raised(self):
        self.write_error = RuntimeError("boom")
        self.queue()
        self.decide(self.cid)
        holds.sweep_idle()
        self.assertEqual((self.read("done")["write"], self.read("done")["result"]), ("failed", "RuntimeError: boom"))

    def test_a_tampered_card_or_job_writes_nothing(self):
        cases = {
            "card": lambda: self.path("pending").write_text(json.dumps({**self.read("pending"), "head": "def456"})),
            "job": lambda: self.path("queued").write_text(json.dumps({**self.read("queued"), "tool": "github_create_pr"})),
            "job-shape": lambda: self.path("queued").write_text(json.dumps({**self.read("queued"), "job": "x"})),
        }
        for name, tamper in cases.items():
            with self.subTest(name):
                for kind in ("done", "pending", "queued", "decisions"):
                    self.path(kind).unlink(missing_ok=True)
                self.queue()
                tamper()
                self.decide(self.cid)
                holds.sweep_idle()
                done = self.read("done")
                self.assertEqual(done["write"], "failed")
                self.assertIn("does not match", done["result"])
                self.assertEqual(self.audit_events()[-1], "approval-write-failed")
        self.assertEqual(self.writes, [])

    def test_a_job_with_no_matching_card_is_dropped_and_recorded(self):
        self.queue()
        self.path("queued").write_text(json.dumps({**self.read("queued"), "nonce": "other"}))
        self.decide(self.cid)
        holds.sweep_idle()
        self.assertFalse(self.path("queued").exists())
        self.assertEqual(self.audit_records()[-1]["event"], "approval-write-failed")
        self.assertIn("no matching pending card", self.audit_records()[-1]["reason"])
        self.assertEqual(self.writes, [])

    def test_a_card_with_an_unreadable_expiry_writes_nothing(self):
        self.queue()
        self.path("pending").write_text(json.dumps({**self.read("pending"), "expires": "soon"}))
        self.decide(self.cid)
        holds.run_queued()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.audit_records()[-1]["event"], "approval-discarded")
        for kind in ("pending", "queued", "decisions"):
            self.assertFalse(self.path(kind).exists(), kind)

    def test_a_restart_while_queued_still_makes_the_write_on_approval(self):
        self.queue()
        self.decide(self.cid)
        # A new process: nothing in memory, the files as they were.
        with mock.patch.object(holds, "_running", set()):
            holds.start_sweeper(stop=FakeStop([True])).join(5)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.read("done")["write"], "done")

    def test_a_restart_past_the_expiry_files_the_queued_card_as_expired(self):
        self.queue()
        later = time.time() + 120
        holds.start_sweeper(clock=lambda: later, stop=FakeStop([True])).join(5)
        self.assertEqual(self.writes, [])
        self.assertEqual((self.read("done")["decision"], self.read("done")["write"]), ("expired", "skipped"))

    def test_a_write_cut_off_by_a_restart_is_filed_as_failed_never_retried(self):
        self.queue()
        self.decide(self.cid)
        events, work = holds._claim(self.cid, time.time())  # claimed, then the process died
        self.assertIsNotNone(work)
        self.assertEqual(self.read("done")["write"], "running")
        with mock.patch.object(holds, "_running", set()):
            holds.start_sweeper(stop=FakeStop([True])).join(5)
        done = self.read("done")
        self.assertEqual(done["write"], "failed")
        self.assertIn("may or may not have reached the forge", done["result"])
        self.assertEqual(self.writes, [])
        with self.assertRaises(Denied) as cm:
            self.call()
        self.assertIn("may or may not have reached the forge", str(cm.exception))

    def test_a_call_without_a_job_keeps_the_retry_hold(self):
        with self.assertRaises(Denied) as cm:
            self.call(job=None)
        self.assertIn("then retry this exact call", str(cm.exception))
        self.assertFalse(self.path("queued").exists())

    def test_a_tool_with_no_completer_keeps_the_retry_hold(self):
        issue = {"tool": "github_create_issue", "repo": "acme/widgets", "title": "T", "body": "", "labels": []}
        self.policy["approvals"]["tools"].append("github_create_issue")
        with self.assertRaises(Denied) as cm:
            self.call(card=issue, job={"repo": "acme/widgets"})
        self.assertIn("then retry this exact call", str(cm.exception))

    def test_nothing_is_queued_or_run_under_a_plugins_hook_or_with_holds_off(self):
        self.queue()
        self.decide(self.cid)
        for installed in (None, lambda card: None):
            core_approvals.set_hook(installed)
            holds.sweep_idle()
            self.assertIsNone(holds.start_sweeper(stop=FakeStop([True])))
            self.assertIsNone(self.call())
        self.assertEqual(self.writes, [])
        self.assertTrue(self.path("queued").exists())

    def test_the_protocol_returns_a_queued_write_as_a_result(self):
        (config.STATE / "roles.json").write_text(json.dumps({"developer": ["github_create_pr"]}))
        args = {"repo": "acme/widgets", "head": "factory/x", "title": "T", "body": "b"}
        with mock.patch("passdesk.core.protocol.policy.load_policy", return_value=POLICY), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("written before approval")):
            text, is_error = protocol.call_tool("github_create_pr", args, "developer")
        self.assertFalse(is_error, text)
        self.assertTrue(text.startswith("Queued for approval (id "), text)
        last = self.audit_records()[-1]
        cid = text.split("(id ", 1)[1].split(")", 1)[0]
        self.assertEqual((last["tool"], last["decision"], last["approval"]), ("github_create_pr", "queued", cid))


class QueuedPullRequestTests(QueueBase):
    """github_create_pr and gitlab_create_mr, queued and opened by the
    gateway from the arguments they checked."""

    PR = {"repo": "acme/widgets", "head": "factory/x", "title": "Add it", "body": "x" * 3000}

    def setUp(self):
        super().setUp()
        self.policy.update({k: v for k, v in POLICY.items()})
        self.posts = []
        self.answer = b'{"number": 7, "html_url": "https://github.com/acme/widgets/pull/7"}'
        self.status = None

        def fake_urlopen(req, timeout):
            self.posts.append(req)
            if self.status:
                raise urllib.error.HTTPError(req.full_url, self.status, "x", None, io.BytesIO(b'{"message":"no"}'))
            return _FakeHTTPResponse(self.answer)
        for target in ("urllib.request.urlopen",):
            p = mock.patch(target, side_effect=fake_urlopen)
            p.start()
            self.addCleanup(p.stop)

    def open_pr(self):
        return git.tool_github_create_pr(dict(self.PR), POLICY)

    def queued_pr(self):
        with self.assertRaises(Queued) as cm:
            self.open_pr()
        self.assertEqual(self.posts, [])
        return cm.exception.cid

    def test_an_approved_queued_pr_is_opened_once_with_the_whole_body(self):
        cid = self.queued_pr()
        self.assertIn("pull request factory/x -> main on acme/widgets", self.queue_text(cid))
        self.decide(cid)
        holds.sweep_idle()
        [req] = self.posts
        self.assertEqual(json.loads(req.data), {"title": "Add it", "body": "x" * 3000, "head": "factory/x",
                                                "base": "main", "draft": True})
        self.assertEqual(self.read("done", cid)["result"], "Opened PR #7: https://github.com/acme/widgets/pull/7")
        with self.assertRaises(Queued) as cm:
            self.open_pr()  # a retry reports it, never opens a second
        self.assertTrue(cm.exception.text.startswith("Opened PR #7: https://github.com/acme/widgets/pull/7\n"))
        self.assertEqual(len(self.posts), 1)

    def queue_text(self, cid):
        with self.assertRaises(Queued) as cm:
            self.open_pr()
        self.assertEqual(cm.exception.cid, cid)
        return cm.exception.text

    def test_a_declined_or_expired_queued_pr_is_never_opened(self):
        cid = self.queued_pr()
        self.decide(cid, verdict="decline")
        holds.sweep_idle()
        self.assertRaises(Denied, self.open_pr)
        self.path("done", cid).unlink()
        cid = self.queued_pr()
        holds.sweep_idle(time.time() + 61)
        self.assertEqual(self.read("done", cid)["decision"], "expired")
        self.assertEqual(self.posts, [])

    def test_a_pr_job_that_does_not_match_its_card_is_never_opened(self):
        cid = self.queued_pr()
        rec = self.read("queued", cid)
        self.path("queued", cid).write_text(json.dumps({**rec, "job": {**rec["job"], "body": "y" * 3000}}))
        self.decide(cid)
        holds.sweep_idle()
        self.assertEqual(self.posts, [])
        self.assertIn("body does not match its approval card", self.read("done", cid)["result"])

    def test_github_refusing_the_queued_pr_is_a_recorded_failure(self):
        cid = self.queued_pr()
        self.status = 422
        self.decide(cid)
        holds.sweep_idle()
        done = self.read("done", cid)
        self.assertEqual(done["write"], "failed")
        self.assertIn("GitHub API returned HTTP 422", done["result"])
        self.assertEqual(len(self.posts), 1)

    def test_a_queued_merge_request_is_opened_once_with_the_gitlab_token(self):
        from . import test_gitlab
        self.policy.update(test_gitlab.GL_POLICY)
        self.addCleanup(setattr, config, "MIRRORS", config.MIRRORS)
        self.write_secret(config.SECRETS_DIR / "gitlab", test_gitlab.GL_TOKEN + "\n")
        sent = []

        def fake_open(req, timeout):
            sent.append(req)
            return _FakeHTTPResponse(b'{"iid": 12, "web_url": "https://gitlab.acme.net/platform/api/-/merge_requests/12"}')
        mr = dict(test_gitlab.MR_ARGS)
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake_open):
            with self.assertRaises(Queued) as cm:
                git.tool_gitlab_create_mr(mr, test_gitlab.GL_POLICY)
            cid = cm.exception.cid
            self.assertIn("merge request factory/x -> main on gitlab.acme.net/platform/api", cm.exception.text)
            self.decide(cid)
            holds.sweep_idle()
            with self.assertRaises(Queued) as cm:
                git.tool_gitlab_create_mr(mr, test_gitlab.GL_POLICY)
        self.assertEqual(cm.exception.state, "completed")
        [req] = sent
        self.assertEqual(req.get_header("Authorization"), f"Bearer {test_gitlab.GL_TOKEN}")
        self.assertEqual(json.loads(req.data)["title"], "Draft: Add the thing")
        self.assertNotIn(test_gitlab.GL_TOKEN, json.dumps(self.read("done", cid)))

    def test_a_queued_merge_request_whose_token_is_gone_is_not_opened(self):
        from . import test_gitlab
        self.policy.update(test_gitlab.GL_POLICY)
        secret = config.SECRETS_DIR / "gitlab"
        self.write_secret(secret, test_gitlab.GL_TOKEN + "\n")
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=AssertionError("sent")):
            with self.assertRaises(Queued) as cm:
                git.tool_gitlab_create_mr(dict(test_gitlab.MR_ARGS), test_gitlab.GL_POLICY)
            secret.unlink()
            self.decide(cm.exception.cid)
            holds.sweep_idle()
        done = self.read("done", cm.exception.cid)
        self.assertEqual(done["write"], "failed")
        self.assertIn("gitlab credential", done["result"])


class QueuedPushTests(HoldBase, test_git.GitFlowTests):
    """git_push queued for real: a local bare repo stands in for GitHub, and
    the gateway pushes the verified ref from its mirror on approval."""

    def setUp(self):
        super().setUp()
        self.policy = {**POLICY, "approvals": {"tools": list(HELD), "hold_seconds": 0, "expire_seconds": 60}}
        core_approvals.set_hook(holds.hook)
        results = mock.patch.object(notify, "send_result_async", lambda *a: None)
        results.start()
        self.addCleanup(results.stop)

    def make(self, branch="factory/feature", files=None, fname="f.bundle"):
        """A commit on branch (cumulative across calls) and its bundle."""
        if not (self.ws / ".git" / "refs" / "heads" / branch).exists():
            out = git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
            self.sh(*out.splitlines()[-1].split(), cwd=self.ws)
            self.sh("git", "checkout", "-q", "-b", branch, "refs/remotes/origin/main", cwd=self.ws)
        for name, text in (files or {"feature.txt": "new feature\n"}).items():
            (self.ws / name).write_text(text)
            self.sh("git", "add", name, cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "work", cwd=self.ws)
        (self.ws / ".factory/outgoing").mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", f".factory/outgoing/{fname}",
                f"refs/remotes/origin/main..{branch}", cwd=self.ws)
        self.canary.unlink(missing_ok=True)
        return self.sh("git", "rev-parse", branch, cwd=self.ws)

    def push(self, branch="factory/feature", fname="f.bundle", policy=POLICY):
        return git.tool_git_push({"repo": "acme/widgets", "branch": branch, "bundle": fname}, policy)

    def queued_push(self, **kw):
        with self.assertRaises(Queued) as cm:
            self.push(**kw)
        return cm.exception

    def on_origin(self, branch="factory/feature"):
        return self.sh("git", "ls-remote", "--heads", str(self.origin), branch, cwd=self.ws).split("\t")[0]

    def done(self, cid):
        return json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())

    def test_an_approved_queued_push_goes_out_from_the_mirror_alone(self):
        head = self.make()
        q = self.queued_push()
        self.assertIn("push of factory/feature to acme/widgets", q.text)
        self.assertEqual(self.on_origin(), "")
        saved = (self.ws / ".factory/outgoing/f.bundle").read_bytes()
        (self.ws / ".factory/outgoing/f.bundle").unlink()  # the workspace is never read again
        self.decide(q.cid)
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), head)
        self.assert_no_canary()
        done = self.done(q.cid)
        self.assertEqual(done["write"], "done")
        self.assertTrue(done["result"].startswith(f"Pushed 1 commit(s) to acme/widgets:factory/feature ({head})."))
        (self.ws / ".factory/outgoing/f.bundle").write_bytes(saved)
        with self.assertRaises(Queued) as cm:
            self.push()  # the retry: the result, not a second push
        self.assertEqual(cm.exception.state, "completed")
        self.assertTrue(cm.exception.text.startswith("Pushed 1 commit(s)"))

    def test_a_queued_push_whose_ref_moved_pushes_nothing(self):
        self.make()
        q = self.queued_push()
        mirror = git.ensure_mirror("acme/widgets")
        git.git("update-ref", f"{config.REF_NAMESPACE}/factory/feature", "refs/remotes/origin/main", cwd=mirror)
        self.decide(q.cid)
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), "")
        self.assertIn("changed in the broker's mirror after the push was queued", self.done(q.cid)["result"])

    def test_a_second_push_to_the_branch_while_queued_supersedes_the_first(self):
        self.make()
        first = self.queued_push()
        head2 = self.make(files={"more.txt": "more\n"}, fname="g.bundle")
        second = self.queued_push(fname="g.bundle")
        self.assertNotEqual(first.cid, second.cid)
        self.decide(first.cid)
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), "")  # the first's ref moved: nothing pushed
        self.assertEqual(self.done(first.cid)["write"], "failed")
        self.decide(second.cid)
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), head2)

    def test_a_queued_push_whose_mirror_is_gone_pushes_nothing(self):
        import shutil
        self.make()
        q = self.queued_push()
        shutil.rmtree(config.MIRRORS)
        self.decide(q.cid)
        holds.sweep_idle()
        self.assertIn("mirror of acme/widgets is gone", self.done(q.cid)["result"])
        self.assertFalse(config.MIRRORS.exists())  # not re-created to push from

    def test_a_restart_while_queued_still_pushes_after_approval(self):
        head = self.make()
        q = self.queued_push()
        self.decide(q.cid)
        with mock.patch.object(holds, "_running", set()):
            holds.start_sweeper(stop=FakeStop([True])).join(5)
        self.assertEqual(self.on_origin(), head)

    def test_a_decision_as_the_hold_ends_pushes_exactly_once(self):
        head = self.make()
        pushes = []
        real_git, real_queue = git.git, holds._queue

        def counting(*args, **kw):
            if args and args[0] == "push":
                pushes.append(args)
            return real_git(*args, **kw)

        def decided_meanwhile(pending, cid, job):
            self.decide(cid)
            return real_queue(pending, cid, job)
        with mock.patch.object(git, "git", counting), mock.patch.object(holds, "_queue", decided_meanwhile):
            out = self.push()
            holds.sweep_idle()
        self.assertTrue(out.startswith("Pushed 1 commit(s)"), out)
        self.assertIn("Held for approval", out)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.on_origin(), head)

    def test_a_declined_queued_push_never_leaves(self):
        self.make()
        q = self.queued_push()
        self.decide(q.cid, verdict="decline", reason="no")
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), "")
        with self.assertRaisesRegex(Denied, "declined by daron: no"):
            self.push()

    def test_a_hand_made_job_can_push_only_what_passes_the_secret_scan(self):
        # A push the scan refused still leaves its commits on the mirror's
        # incoming ref. A card and job forged for that head (by whoever can
        # write the state directory) must still not get them out.
        from .base import FAKE_GH
        head = self.make(files={"config.py": f"TOKEN = '{FAKE_GH}'\n"})
        with self.assertRaisesRegex(Denied, "github-token"):
            self.push()
        card = {"tool": "git_push", "principal": "", "repo": "acme/widgets", "branch": "factory/feature",
                "base": "main", "head": head, "bundle_sha256": "0" * 64}
        cid = holds.card_id(card)
        job = {k: card[k] for k in ("repo", "branch", "base", "head", "bundle_sha256")}
        self.write_pending(card, cid, "n1", time.time() + 600)
        q = config.STATE / "approvals" / "queued" / f"{cid}.json"
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(json.dumps({"id": cid, "nonce": "n1", "tool": "git_push", "job": job}))
        self.decide(cid)
        holds.sweep_idle()
        self.assertEqual(self.on_origin(), "")
        self.assertIn("appears to contain secrets", self.done(cid)["result"])
        self.assertNotIn(FAKE_GH, json.dumps(self.done(cid)))


# QueuedPushTests borrows GitFlowTests' fixture, not its tests (test_git runs those).
for _name in dir(test_git.GitFlowTests):
    if _name.startswith("test_"):
        setattr(QueuedPushTests, _name, None)
