"""The hold (core/holds.py): cards, decisions, retries, expiry, notification
and installation at start-up. Ported from passdesk-pro's tests when the
hold moved into core (docs/decisions.md, 2026-10-21)."""

import io
import json
import os
import threading
import time
import unittest
from unittest import mock

from passdesk.core import approvals as core_approvals
from passdesk.core import config, holds, notify, plugins, protocol
from passdesk.core.errors import Denied

from .base import Base

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
        holds.hook(CARD)  # git_push passes straight through
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
            self.assertEqual((hold, expire), (45, 1800), bad)
        self.assertEqual(holds.settings({"approvals": {"hold_seconds": 7.9, "expire_seconds": 100}})[:2], (7, 100))

    def test_settings_default_to_off(self):
        self.assertEqual(holds.settings({}), (45, 1800, []))
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
