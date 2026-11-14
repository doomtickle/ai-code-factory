"""Factory analytics: aggregates dispatch/broker audit logs into a cost and
performance report for the AI Code Factory.

Reads, read-only, never executing anything from them, under the factory state
dir (<state>, outside the repo; see FACTORY_STATE in scripts/lib/common.sh):
  <state>/dispatch/audit.jsonl        Coordinator tool calls and job-end records
  <state>/broker/audit.jsonl          Individual broker tool calls
  <state>/dispatch/sessions/*.json    Each role's latest session totals

`--html` on a terminal writes the rendered page to <state>/dispatch/analytics.html
(overwriting any previous report there) and opens it with the stdlib `webbrowser`
module; piped or redirected, it prints the HTML to stdout instead and writes nothing.

`--changes` reports per change instead: job-end records grouped by their `change`
(the factory/<topic> branch), with review rounds, first-pass approval, wall time,
cost and diff size, then median/p90 and last 7 days vs the 7 before. Records
without a change count as unknown. It works with `--json`, not `--html`.

`--bench [label ...]` reports the benchmark runs (`factory/bench-<task>-<label>-<n>`
changes) per label and per task: medians across the runs, and with two labels the
difference, flagging only metrics whose [min, max] ranges across runs don't overlap.
Task names come from bench/tasks/*.md. A missing grade field is no data, never 0.

Run directly, or via `factory analytics`.

Standard library only; compatible with the macOS system Python (3.9).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import statistics
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory.metrics import (  # noqa: E402,F401
    TS_FORMAT, _finite_sum, _num, _round, change_row, group_changes, parse_ts, percentile, ts)

_WORD_RE = re.compile(r"[A-Za-z]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_IDENTIFIER_FIELDS = ("job_id", "role", "principal", "task", "model")


def _str(value) -> str | None:
    """Coerce a scalar identifier to a string, or None if its shape can't be
    (e.g. a role logged as a list) -- used to stop malformed log data from
    crashing later when it's hashed as a dict/set key or formatted as text."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value) if math.isfinite(value) or isinstance(value, int) else None
    return None


def _sanitize_identifiers(record: dict) -> dict:
    """Coerce job_id/role/task/model to strings in place, dropping any that
    have the wrong shape, so a malformed field (e.g. a list-valued role)
    can't crash hashing or table formatting downstream."""
    for field in _IDENTIFIER_FIELDS:
        if field in record:
            coerced = _str(record[field])
            if coerced is None:
                del record[field]
            else:
                record[field] = coerced
    return record


def _clean(value) -> str:
    """Strip ASCII control characters (including ESC/BEL) from log-derived
    text before it reaches the terminal, so malicious or corrupted log data
    can't inject escape sequences or break table row alignment."""
    return _CONTROL_CHARS_RE.sub("", str(value))


# --------------------------------------------------------------------------
# Loading (tolerant of missing files and malformed lines: a broken audit log
# must not stop the rest of the report from rendering)

def read_jsonl(path: Path) -> tuple[list[dict], int]:
    """(records, malformed_line_count). Missing file -> no records."""
    if not path.exists():
        return [], 0
    records: list[dict] = []
    malformed = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(record, dict):
                records.append(_sanitize_identifiers(record))
            else:
                malformed += 1
    return records, malformed


def load_jobs(dispatch_audit: Path) -> tuple[list[dict], int]:
    records, malformed = read_jsonl(dispatch_audit)
    return [r for r in records if r.get("event") == "job-end"], malformed


def load_broker_calls(broker_audit: Path) -> tuple[list[dict], int]:
    records, malformed = read_jsonl(broker_audit)
    # git_push, github_create_pr, gitlab_create_mr and github_create_issue
    # write an "intent" record (role + tool, no "decision") before the call
    # runs, in addition to the normal allow/deny/error record once it
    # finishes; skip it so those tools aren't counted twice.
    calls = []
    for r in records:
        if "tool" not in r or "event" in r:
            continue
        # "principal" is the canonical field as of passdesk-broker's
        # decouple-identity change; "role" is kept as a duplicate alias there,
        # but fall back to it for audit lines written before that change.
        role = r.get("principal") or r.get("role")
        if role is None:
            continue
        r["role"] = role
        calls.append(r)
    return calls, malformed


def load_sessions(sessions_dir: Path) -> tuple[dict, int]:
    """({role: session metadata}, malformed_file_count), from
    dispatch/sessions/<lane>.json (one per lane of each role). Missing dir -> no sessions."""
    sessions: dict = {}
    malformed = 0
    if not sessions_dir.is_dir():
        return sessions, malformed
    for meta_file in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            malformed += 1
            print(_clean(f"warning: skipping malformed session file {meta_file}: {exc}"), file=sys.stderr)
            continue
        if isinstance(data, dict):
            sessions[meta_file.stem] = data
        else:
            malformed += 1
            print(_clean(f"warning: skipping malformed session file {meta_file}: not a JSON object"), file=sys.stderr)
    return sessions, malformed


# --------------------------------------------------------------------------
# Aggregation

def summarize_totals(jobs: list[dict], sessions: dict) -> dict:
    costs = [c for c in (_num(j.get("cost_usd")) for j in jobs) if c is not None]
    context_now = [c for c in (_num(s.get("context_tokens")) for s in sessions.values()) if c is not None]
    total_cost = _finite_sum(costs) if costs else None
    total_context = _finite_sum(context_now) if context_now else None
    return {
        "jobs": len(jobs),
        "jobs_with_known_cost": len(costs),
        "total_cost_usd": _round(total_cost) if total_cost is not None else None,
        "avg_cost_per_job_usd": _round(total_cost / len(costs)) if total_cost is not None else None,
        "total_context_tokens_current": int(total_context) if total_context is not None else None,
    }


_RESET_DROP_RATIO = 0.75  # a reading below 75% of the previous one (i.e. more than a
                          # 25% drop) counts as a session reset or compaction, not noise


def context_trend(series: list[float]) -> tuple[str | None, float | None]:
    """series: context_tokens in chronological order for one role.

    Job-end records don't carry a session id, so a session reset (Claude
    compacting or restarting with a smaller context) is inferred from a
    meaningful drop -- more than 25% below the previous reading, to avoid
    treating ordinary fluctuation as a reset. The trend is computed only
    since the last such drop -- otherwise a role's first job in one session
    would get compared against its latest job in a much later, unrelated
    session. If the drop is itself the newest reading, there's nothing yet
    to compare it to, so report "reset" instead of falling back to comparing
    across the reset (which would misreport a shrink as growth or vice
    versa)."""
    if len(series) < 2:
        return None, None
    reset_at = 0
    for i in range(1, len(series)):
        if series[i] < series[i - 1] * _RESET_DROP_RATIO:
            reset_at = i
    if reset_at == len(series) - 1:
        return "reset", None
    first, last = series[reset_at], series[-1]
    if first == 0:
        return ("growing" if last > 0 else "stable"), None
    change_pct = round((last - first) / first * 100, 1)
    if change_pct > 10:
        trend = "growing"
    elif change_pct < -10:
        trend = "shrinking"
    else:
        trend = "stable"
    return trend, change_pct


_LANE_SUFFIX = re.compile(r"-lane[0-9]+$")


def per_role_stats(jobs: list[dict], broker_calls: list[dict], sessions: dict) -> dict:
    # A role with several lanes keeps one session file per lane (<role>-lane<k>.json);
    # its current context is the sum over its lanes.
    role_sessions: dict = {}
    for name, meta in sessions.items():
        role_sessions.setdefault(_LANE_SUFFIX.sub("", name), []).append(meta)
    roles = sorted({j.get("role") for j in jobs if j.get("role")}
                   | {c.get("role") for c in broker_calls if c.get("role")}
                   | set(role_sessions))
    result = {}
    for role in roles:
        role_jobs = [j for j in jobs if j.get("role") == role]
        costs = [c for c in (_num(j.get("cost_usd")) for j in role_jobs) if c is not None]
        durations = [d for d in (_num(j.get("elapsed_seconds")) for j in role_jobs) if d is not None]
        statuses = [j.get("status") for j in role_jobs]
        done = statuses.count("done")
        terminal = sum(1 for s in statuses if s in ("done", "failed", "cancelled"))

        dated_context = sorted(
            (pair for pair in ((parse_ts(j.get("ts")), _num(j.get("context_tokens"))) for j in role_jobs)
             if pair[0] is not None and pair[1] is not None),
            key=lambda pair: pair[0])
        context_series = [c for _, c in dated_context]
        trend, change_pct = context_trend(context_series)

        lane_context = [c for c in (_num(m.get("context_tokens")) for m in role_sessions.get(role, []))
                        if c is not None]
        current_context = _finite_sum(lane_context) if lane_context else None
        if current_context is None and context_series:
            current_context = context_series[-1]

        role_calls = [c for c in broker_calls if c.get("role") == role]
        denied = sum(1 for c in role_calls if c.get("decision") == "deny")
        errors = sum(1 for c in role_calls if c.get("decision") == "error")
        call_ms = [m for m in (_num(c.get("ms")) for c in role_calls) if m is not None]

        costs_total = _finite_sum(costs) if costs else None
        durations_total = _finite_sum(durations) if durations else None
        call_ms_total = _finite_sum(call_ms) if call_ms else None

        result[role] = {
            "jobs": len(role_jobs),
            "done": done,
            "failed": statuses.count("failed"),
            "cancelled": statuses.count("cancelled"),
            "success_rate": round(done / terminal, 4) if terminal else None,
            "total_cost_usd": _round(costs_total) if costs_total is not None else None,
            "avg_cost_usd": _round(costs_total / len(costs)) if costs_total is not None else None,
            "avg_duration_seconds": round(durations_total / len(durations), 1) if durations_total is not None else None,
            "context_tokens_current": current_context,
            "context_trend": trend,
            "context_change_pct": change_pct,
            "broker_calls": len(role_calls),
            "broker_denied": denied,
            "broker_errors": errors,
            "broker_avg_ms": round(call_ms_total / len(call_ms), 1) if call_ms_total is not None else None,
        }
    return result


def task_type(task) -> str:
    """First word of the task text, as a coarse bucket ('add', 'fix', 'review', ...)."""
    m = _WORD_RE.match(task.strip()) if isinstance(task, str) else None
    return m.group(0).lower() if m else "other"


def cost_drivers(jobs: list[dict], top_n: int = 10) -> dict:
    costed = [j for j in jobs if _num(j.get("cost_usd")) is not None]

    top_jobs = sorted(costed, key=lambda j: j["cost_usd"], reverse=True)[:top_n]

    by_type: dict = defaultdict(list)
    for j in costed:
        by_type[task_type(j.get("task"))].append(j["cost_usd"])
    by_task_type = sorted(
        ({"type": t, "cost_usd": _round(s), "jobs": len(v)}
         for t, v in by_type.items() for s in [_finite_sum(v)] if s is not None),
        key=lambda r: r["cost_usd"], reverse=True)

    by_model_costs: dict = defaultdict(list)
    for j in costed:
        by_model_costs[j.get("model") or "unknown"].append(j["cost_usd"])
    by_model = sorted(
        ({"model": m, "cost_usd": _round(s), "jobs": len(v)}
         for m, v in by_model_costs.items() for s in [_finite_sum(v)] if s is not None),
        key=lambda r: r["cost_usd"], reverse=True)

    return {
        "top_jobs": [
            {"job_id": j.get("job_id"), "role": j.get("role"), "cost_usd": _round(j["cost_usd"]),
             "elapsed_seconds": _num(j.get("elapsed_seconds")), "model": j.get("model"),
             "task": j.get("task")}
            for j in top_jobs],
        "by_task_type": by_task_type,
        "by_model": by_model,
    }


_MAD_SCALE = 1.4826  # scales MAD to be comparable to a normal distribution's SD
_MAD_K = 3            # how many scaled-MADs above the peer median counts as an outlier
_MIN_MULTIPLE = 2      # and it must be at least this many times the peer median


def _fmt_value(field: str, v: float) -> str:
    return f"${v:.2f}" if field == "cost_usd" else f"{v:.1f}s"


def detect_anomalies(jobs: list[dict]) -> list[dict]:
    """Jobs whose cost or duration is unusually high next to their role's
    *other* jobs (leave-one-out): each job is compared against the median and
    MAD (median absolute deviation) of the same role's remaining jobs for
    that field, so a single outlier can be caught with as few as 3 jobs total
    (2 others to compare against) rather than needing many more to overcome
    its own pull on a mean/SD. A job is flagged only if it's both more than
    3 scaled-MADs above the peer median AND at least 2x that median -- the
    second condition keeps ordinary variance in small, noisy samples (e.g.
    log-normal job durations) from tripping the first. When the peers are
    all identical (MAD 0), the 2x-median condition alone decides, falling
    back to "any amount above zero" when that median is itself zero."""
    anomalies = []
    by_role: dict = defaultdict(list)
    for j in jobs:
        by_role[j.get("role")].append(j)
    for role, role_jobs in by_role.items():
        for field, label in (("cost_usd", "cost"), ("elapsed_seconds", "duration")):
            pairs = [(j, _num(j.get(field))) for j in role_jobs]
            pairs = [(j, v) for j, v in pairs if v is not None]
            if len(pairs) < 3:
                continue
            values = [v for _, v in pairs]
            for i, (j, v) in enumerate(pairs):
                others = values[:i] + values[i + 1:]
                med = statistics.median(others)
                mad = statistics.median(abs(x - med) for x in others) * _MAD_SCALE
                above_floor = v >= med * _MIN_MULTIPLE if med > 0 else v > 0
                if mad > 0:
                    is_outlier = above_floor and v > med + _MAD_K * mad
                else:
                    is_outlier = above_floor
                if is_outlier:
                    anomalies.append({
                        "job_id": j.get("job_id"), "role": role,
                        "reason": f"{label} {_fmt_value(field, v)} is unusually high "
                                  f"(peer median {_fmt_value(field, med)})",
                        field: _round(v),
                    })
    return anomalies


def trends(jobs: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    dated = [(parse_ts(j.get("ts")), j) for j in jobs]
    dated = [(dt, j) for dt, j in dated if dt is not None]

    by_day: dict = defaultdict(float)
    for dt, j in dated:
        cost = _num(j.get("cost_usd"))
        if cost is not None:
            by_day[dt.date().isoformat()] += cost
    cost_by_day = [{"date": d, "cost_usd": _round(c)} for d, c in sorted(by_day.items())]

    def window_cost(days: int) -> float:
        cutoff = now - timedelta(days=days)
        costs = [_num(j.get("cost_usd")) for dt, j in dated if dt >= cutoff]
        return _round(sum(c for c in costs if c is not None))

    return {
        "cost_by_day": cost_by_day,
        "last_7_days_cost_usd": window_cost(7),
        "last_30_days_cost_usd": window_cost(30),
        "anomalies": detect_anomalies(jobs),
    }


def build_report(state: Path, top_n: int = 10) -> dict:
    dispatch_audit = state / "dispatch" / "audit.jsonl"
    broker_audit = state / "broker" / "audit.jsonl"
    sessions_dir = state / "dispatch" / "sessions"

    jobs, dispatch_malformed = load_jobs(dispatch_audit)
    broker_calls, broker_malformed = load_broker_calls(broker_audit)
    sessions, session_malformed = load_sessions(sessions_dir)

    warnings = []
    if not dispatch_audit.exists():
        warnings.append(f"no dispatch audit log yet ({dispatch_audit})")
    if not broker_audit.exists():
        warnings.append(f"no broker audit log yet ({broker_audit})")
    if dispatch_malformed:
        warnings.append(f"skipped {dispatch_malformed} malformed line(s) in {dispatch_audit}")
    if broker_malformed:
        warnings.append(f"skipped {broker_malformed} malformed line(s) in {broker_audit}")
    if session_malformed:
        warnings.append(f"skipped {session_malformed} malformed session file(s) in {sessions_dir}")

    return {
        "generated_at": datetime.now(timezone.utc).strftime(TS_FORMAT),
        "warnings": warnings,
        "totals": summarize_totals(jobs, sessions),
        "roles": per_role_stats(jobs, broker_calls, sessions),
        "cost_drivers": cost_drivers(jobs, top_n),
        "trends": trends(jobs),
    }


# --------------------------------------------------------------------------
# Per-change metrics (--changes): job-end records grouped by the factory/<topic>
# branch they worked on. Records without a "change" (older ones, or a job that
# brought no branch) are counted as unknown, never dropped. The grouping and the
# per-change row live in factory/metrics.py, shared with the dispatch bridge.

CHANGE_METRICS = ("jobs", "review_rounds", "wall_seconds", "cost_usd", "diff_lines", "diff_files",
                  "test_lines_share")


def _stat(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "p90": None}
    return {"n": len(values), "median": _round(statistics.median(values)), "p90": _round(percentile(values, 0.9))}


def _first_pass_rate(rows: list[dict]):
    known = [r["first_pass_approval"] for r in rows if r["first_pass_approval"] is not None]
    return {"known": len(known), "approved": sum(known), "rate": round(sum(known) / len(known), 4)
            if known else None}


def _window_summary(rows: list[dict]) -> dict:
    return {"changes": len(rows), "first_pass": _first_pass_rate(rows),
            **{m: _stat([r[m] for r in rows])["median"] for m in CHANGE_METRICS}}


def changes_report(jobs: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    grouped, unknown = group_changes(jobs)
    rows = sorted((change_row(c, js) for c, js in grouped.items()),
                  key=lambda r: r["last_job_at"] or "", reverse=True)
    unknown_costs = [c for c in (_num(j.get("cost_usd")) for j in unknown) if c is not None]
    unknown_cost = _finite_sum(unknown_costs) if unknown_costs else None

    def in_window(row, newer_than, older_than):
        end = parse_ts(row["last_job_at"])
        return end is not None and newer_than < end <= older_than

    last = [r for r in rows if in_window(r, now - timedelta(days=7), now)]
    prev = [r for r in rows if in_window(r, now - timedelta(days=14), now - timedelta(days=7))]
    return {
        "changes": rows,
        "unknown": {"jobs": len(unknown), "cost_usd": _round(unknown_cost) if unknown_cost is not None else None},
        "summary": {
            **{m: _stat([r[m] for r in rows]) for m in CHANGE_METRICS},
            "first_pass": _first_pass_rate(rows),
            "last_7_days": _window_summary(last),
            "previous_7_days": _window_summary(prev),
        },
    }


def build_changes_report(state: Path, now: datetime | None = None) -> dict:
    dispatch_audit = state / "dispatch" / "audit.jsonl"
    jobs, malformed = load_jobs(dispatch_audit)
    warnings = []
    if not dispatch_audit.exists():
        warnings.append(f"no dispatch audit log yet ({dispatch_audit})")
    if malformed:
        warnings.append(f"skipped {malformed} malformed line(s) in {dispatch_audit}")
    return {"generated_at": datetime.now(timezone.utc).strftime(TS_FORMAT), "warnings": warnings,
            **changes_report(jobs, now)}


# --------------------------------------------------------------------------
# Benchmark (--bench): the factory/bench-<task>-<label>-<n> changes, grouped by task and
# label. Task names and labels both contain hyphens, so the branch is split with the known
# task names (the stems of bench/tasks/*.md), the longest one that fits winning.

_BENCH_RE = re.compile(r"factory/bench-([a-z0-9-]+)-([0-9]+)")
BENCH_MIN_RUNS = 3
# (key, column header, how the runs combine). "rate" is the share of passing runs; the rest
# are medians. A run with no data for a metric is left out of it, never counted as 0.
BENCH_METRICS = (("oracle_pass", "Oracle", "rate"), ("review_rounds", "Rounds", "median"),
                 ("wall_seconds", "Wall", "median"), ("cost_usd", "Cost", "median"),
                 ("turns", "Turns", "median"), ("grade_done_when", "Done when", "median"),
                 ("grade_scope", "Scope", "median"), ("grade_size_ratio", "Size ratio", "median"))


def default_bench_tasks_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "bench" / "tasks"


def bench_task_names(tasks_dir: Path) -> list[str]:
    return sorted(p.stem for p in tasks_dir.glob("*.md"))


def split_bench_change(change: str, task_names: list[str]):
    """(task, label, run number) of a bench branch, or None if no known task fits."""
    m = _BENCH_RE.fullmatch(change)
    if not m:
        return None
    rest = m.group(1)
    for task in sorted(task_names, key=len, reverse=True):
        if rest.startswith(task + "-") and len(rest) > len(task) + 1:
            return task, rest[len(task) + 1:], int(m.group(2))
    return None


def _latest_grade(jobs: list[dict], key: str, valid):
    """The newest valid value of a grade field across a change's jobs, or None."""
    ordered = sorted(jobs, key=lambda j: parse_ts(j.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    for j in reversed(ordered):
        v = valid(j.get(key))
        if v is not None:
            return v
    return None


def _grade_score(value):
    v = _num(value)
    return v if v is not None and not isinstance(v, float) and 1 <= v <= 5 else None


def _grade_ratio(value):
    v = _num(value)
    return v if v is not None and v >= 0 else None


def bench_run(change: str, jobs: list[dict]) -> dict:
    row = change_row(change, jobs)
    oracle = _latest_grade(jobs, "grade_oracle", lambda v: v if v in ("pass", "fail") else None)
    return {
        "change": change,
        "oracle_pass": None if oracle is None else 1.0 if oracle == "pass" else 0.0,
        # no reviewer verdict means no data, not a clean pass
        "review_rounds": row["review_rounds"] if row["first_pass_approval"] is not None else None,
        "wall_seconds": row["wall_seconds"], "cost_usd": row["cost_usd"], "turns": row["turns"],
        "grade_done_when": _latest_grade(jobs, "grade_done_when", _grade_score),
        "grade_scope": _latest_grade(jobs, "grade_scope", _grade_score),
        "grade_size_ratio": _latest_grade(jobs, "grade_size_ratio", _grade_ratio),
    }


def _bench_stats(runs: list[dict]) -> dict:
    out = {}
    for key, _, how in BENCH_METRICS:
        values = [r[key] for r in runs if r[key] is not None]
        if not values:
            out[key] = {"n": 0, "value": None, "min": None, "max": None}
            continue
        value = sum(values) / len(values) if how == "rate" else statistics.median(values)
        out[key] = {"n": len(values), "value": _round(value), "min": _round(min(values)),
                    "max": _round(max(values))}
    return out


def _bench_compare(first: dict, second: dict) -> list[dict]:
    """Per metric, second minus first, flagged `separated` only when both sides have at least
    BENCH_MIN_RUNS runs with data and their [min, max] ranges don't overlap."""
    rows = []
    for key, _, _ in BENCH_METRICS:
        a, b = first[key], second[key]
        diff = _round(b["value"] - a["value"]) if a["n"] and b["n"] else None
        enough = a["n"] >= BENCH_MIN_RUNS and b["n"] >= BENCH_MIN_RUNS
        rows.append({"metric": key, "first": a["value"], "second": b["value"], "diff": diff,
                     "enough_runs": enough,
                     "separated": enough and (a["max"] < b["min"] or b["max"] < a["min"])})
    return rows


def bench_report(jobs: list[dict], task_names: list[str], labels: list[str] | None = None) -> dict:
    grouped: dict = defaultdict(list)
    for j in jobs:
        change = j.get("change")
        if isinstance(change, str) and change.startswith("factory/bench-"):
            grouped[change].append(j)
    warnings = []
    by_label: dict = defaultdict(lambda: defaultdict(list))
    for change in sorted(grouped):
        parts = split_bench_change(change, task_names)
        if parts is None:
            warnings.append(f"{change} matches no task in bench/tasks; skipped")
            continue
        task, label, number = parts
        if labels and label not in labels:
            continue
        by_label[label][task].append({"run": number, **bench_run(change, grouped[change])})
    for label in labels or []:
        if label not in by_label:
            warnings.append(f"no bench runs found for label {label!r}")

    out_labels = []
    for label in (list(dict.fromkeys(labels)) if labels else sorted(by_label)):
        if label not in by_label:
            continue
        tasks = []
        for task in sorted(by_label[label]):
            runs = sorted(by_label[label][task], key=lambda r: r["run"])
            if len(runs) < BENCH_MIN_RUNS:
                warnings.append(f"{task} / {label}: {len(runs)} run(s), fewer than {BENCH_MIN_RUNS}; "
                                f"don't read anything into it")
            tasks.append({"task": task, "runs": runs, "metrics": _bench_stats(runs)})
        all_runs = [r for t in tasks for r in t["runs"]]
        out_labels.append({"label": label, "tasks": tasks, "run_count": len(all_runs),
                           "metrics": _bench_stats(all_runs)})

    comparison = None
    if len(out_labels) == 2:
        first, second = out_labels
        by_task = {lab["label"]: {t["task"]: t for t in lab["tasks"]} for lab in out_labels}
        common = sorted(set(by_task[first["label"]]) & set(by_task[second["label"]]))
        if {t["task"] for t in first["tasks"]} != {t["task"] for t in second["tasks"]}:
            warnings.append(f"{first['label']} and {second['label']} ran different tasks; "
                            f"the all-tasks comparison covers only the {len(common)} they share")
        pooled = [_bench_stats([r for t in common for r in by_task[lab["label"]][t]["runs"]])
                  for lab in out_labels]
        comparison = {
            "first": first["label"], "second": second["label"],
            "all_tasks": {"tasks": common, "metrics": _bench_compare(*pooled)},
            "tasks": [{"task": t, "metrics": _bench_compare(by_task[first["label"]][t]["metrics"],
                                                            by_task[second["label"]][t]["metrics"])}
                      for t in common],
        }
    return {"min_runs": BENCH_MIN_RUNS, "warnings": warnings, "labels": out_labels, "comparison": comparison}


def build_bench_report(state: Path, tasks_dir: Path, labels: list[str] | None = None) -> dict:
    dispatch_audit = state / "dispatch" / "audit.jsonl"
    jobs, malformed = load_jobs(dispatch_audit)
    warnings = []
    if not dispatch_audit.exists():
        warnings.append(f"no dispatch audit log yet ({dispatch_audit})")
    if malformed:
        warnings.append(f"skipped {malformed} malformed line(s) in {dispatch_audit}")
    report = bench_report(jobs, bench_task_names(tasks_dir), labels)
    return {"generated_at": datetime.now(timezone.utc).strftime(TS_FORMAT),
            **report, "warnings": warnings + report["warnings"]}


# --------------------------------------------------------------------------
# Rendering

def _fmt_cost(v) -> str:
    return f"${v:.2f}" if isinstance(v, (int, float)) else "n/a"


def _fmt_pct(v) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "n/a"


def _fmt_int(v) -> str:
    return f"{v:,}" if isinstance(v, (int, float)) else "n/a"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def render_table(report: dict) -> str:
    lines = ["AI Code Factory — Analytics", f"Generated {report['generated_at']}"]
    for w in report["warnings"]:
        lines.append(f"! {_clean(w)}")
    lines.append("")

    totals = report["totals"]
    lines.append("== Summary ==")
    lines.append(f"Total jobs:              {totals['jobs']}")
    cost_note = "" if totals["jobs_with_known_cost"] == totals["jobs"] or not totals["jobs"] \
        else f" ({totals['jobs_with_known_cost']}/{totals['jobs']} jobs had a known cost)"
    lines.append(f"Total cost:              {_fmt_cost(totals['total_cost_usd'])}{cost_note}")
    lines.append(f"Avg cost per job:        {_fmt_cost(totals['avg_cost_per_job_usd'])}")
    lines.append(f"Context now (all roles): {_fmt_int(totals['total_context_tokens_current'])} tokens")
    lines.append("")

    lines.append("== Per role ==")
    roles = report["roles"]
    if roles:
        rows = []
        for role, r in sorted(roles.items(), key=lambda kv: kv[1]["total_cost_usd"] or 0, reverse=True):
            ctx_trend = (r["context_trend"] or "n/a") + (
                f" ({r['context_change_pct']:+.1f}%)" if r["context_change_pct"] is not None else "")
            rows.append([
                _clean(role), str(r["jobs"]), _fmt_cost(r["total_cost_usd"]), _fmt_cost(r["avg_cost_usd"]),
                f"{r['avg_duration_seconds']:.0f}s" if r["avg_duration_seconds"] is not None else "n/a",
                _fmt_pct(r["success_rate"] * 100 if r["success_rate"] is not None else None),
                _fmt_int(r["context_tokens_current"]), ctx_trend,
                f"{r['broker_calls']} ({r['broker_denied']} denied, {r['broker_errors']} errors)",
            ])
        lines.append(_table(
            ["Role", "Jobs", "Total $", "Avg $/job", "Avg dur", "Success", "Context", "Ctx trend", "Broker calls"],
            rows))
    else:
        lines.append("No jobs recorded yet.")
    lines.append("")

    drivers = report["cost_drivers"]
    lines.append("== Most expensive jobs ==")
    if drivers["top_jobs"]:
        rows = [[_clean(j["job_id"] or ""), _clean(j["role"] or ""), _fmt_cost(j["cost_usd"]),
                  f"{j['elapsed_seconds']:.0f}s" if j["elapsed_seconds"] is not None else "n/a",
                  _clean(j.get("model") or "n/a"), _clean((j.get("task") or "")[:60])]
                 for j in drivers["top_jobs"]]
        lines.append(_table(["Job", "Role", "Cost", "Duration", "Model", "Task"], rows))
    else:
        lines.append("No costed jobs yet.")
    lines.append("")

    lines.append("== Cost by task type ==")
    if drivers["by_task_type"]:
        rows = [[t["type"], str(t["jobs"]), _fmt_cost(t["cost_usd"])] for t in drivers["by_task_type"]]
        lines.append(_table(["Task type", "Jobs", "Cost"], rows))
    else:
        lines.append("n/a")
    lines.append("")

    lines.append("== Cost by model ==")
    if drivers["by_model"]:
        rows = [[_clean(m["model"]), str(m["jobs"]), _fmt_cost(m["cost_usd"])] for m in drivers["by_model"]]
        lines.append(_table(["Model", "Jobs", "Cost"], rows))
    else:
        lines.append("n/a")
    lines.append("")

    trend = report["trends"]
    lines.append("== Trends ==")
    lines.append(f"Last 7 days:  {_fmt_cost(trend['last_7_days_cost_usd'])}")
    lines.append(f"Last 30 days: {_fmt_cost(trend['last_30_days_cost_usd'])}")
    if trend["cost_by_day"]:
        rows = [[d["date"], _fmt_cost(d["cost_usd"])] for d in trend["cost_by_day"][-14:]]
        lines.append(_table(["Date", "Cost"], rows))
    if trend["anomalies"]:
        lines.append("")
        lines.append("Anomalies:")
        for a in trend["anomalies"]:
            lines.append(f"  ! {_clean(a['role'])} job {_clean(a['job_id'])}: {_clean(a['reason'])}")
    return "\n".join(lines)


def _fmt_secs(v) -> str:
    if not isinstance(v, (int, float)):
        return "n/a"
    v = int(round(v))
    return f"{v // 3600}h{v % 3600 // 60:02d}m" if v >= 3600 else f"{v // 60}m{v % 60:02d}s"


def _fmt_num(v, digits: int = 0) -> str:
    return f"{v:,.{digits}f}" if isinstance(v, (int, float)) else "n/a"


def _fmt_share(v) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "n/a"


def _yes_no(v) -> str:
    return "unknown" if v is None else "yes" if v else "no"


# (key, column header, formatter) for a summary line
_CHANGE_FORMATS = (
    ("jobs", "Jobs", _fmt_num), ("review_rounds", "Rounds", _fmt_num), ("wall_seconds", "Wall", _fmt_secs),
    ("cost_usd", "Cost", _fmt_cost), ("diff_lines", "Diff lines", _fmt_num), ("diff_files", "Files", _fmt_num),
    ("test_lines_share", "Tests", _fmt_share))


def render_changes_table(report: dict) -> str:
    lines = ["AI Code Factory — Analytics: changes", f"Generated {report['generated_at']}"]
    for w in report["warnings"]:
        lines.append(f"! {_clean(w)}")
    lines.append("")
    lines.append("== Per change ==")
    if report["changes"]:
        rows = [[_clean(r["change"]), str(r["jobs"]), str(r["review_rounds"]), _yes_no(r["first_pass_approval"]),
                 _fmt_secs(r["wall_seconds"]), _fmt_cost(r["cost_usd"]), _fmt_num(r["diff_lines"]),
                 _fmt_num(r["diff_files"]), _fmt_share(r["test_lines_share"])] for r in report["changes"]]
        lines.append(_table(["Change", "Jobs", "Rounds", "First pass", "Wall", "Cost", "Diff lines", "Files",
                             "Tests"], rows))
    else:
        lines.append("No job has a change recorded yet.")
    unknown = report["unknown"]
    if unknown["jobs"]:
        lines.append(f"Unknown change (older records, or no branch): {unknown['jobs']} job(s), "
                     f"{_fmt_cost(unknown['cost_usd'])}")
    lines.append("")

    summary = report["summary"]
    fp = summary["first_pass"]
    lines.append("== Summary ==")
    lines.append(f"First-pass approval: {_fmt_pct(fp['rate'] * 100 if fp['rate'] is not None else None)} "
                 f"({fp['approved']}/{fp['known']} changes with a verdict)")
    rows = [[label, fmt(summary[key]["median"]),
             fmt(summary[key]["p90"]), str(summary[key]["n"])] for key, label, fmt in _CHANGE_FORMATS]
    lines.append(_table(["Metric", "Median", "p90", "Changes"], rows))
    lines.append("")
    lines.append("== Last 7 days vs previous 7 days (by the change's last job) ==")
    last, prev = summary["last_7_days"], summary["previous_7_days"]

    def rate(w):
        return _fmt_pct(w["first_pass"]["rate"] * 100 if w["first_pass"]["rate"] is not None else None)

    rows = [["Changes", str(last["changes"]), str(prev["changes"])],
            ["First-pass approval", rate(last), rate(prev)]]
    rows += [[f"Median {label.lower()}", fmt(last[key]), fmt(prev[key])] for key, label, fmt in _CHANGE_FORMATS]
    lines.append(_table(["", "Last 7 days", "Previous 7 days"], rows))
    return "\n".join(lines)


def _fmt_bench(key: str, v) -> str:
    if v is None:
        return "n/a"
    return {"oracle_pass": lambda: _fmt_pct(v * 100), "wall_seconds": lambda: _fmt_secs(v),
            "cost_usd": lambda: _fmt_cost(v), "review_rounds": lambda: _fmt_num(v, 1),
            "turns": lambda: _fmt_num(v, 1), "grade_size_ratio": lambda: _fmt_num(v, 2)
            }.get(key, lambda: _fmt_num(v, 1))()


def _fmt_bench_diff(key: str, v) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v > 0 else "-" if v < 0 else ""
    return sign + _fmt_bench(key, abs(v))


def _bench_cells(metrics: dict) -> list[str]:
    return [_fmt_bench(key, metrics[key]["value"]) for key, _, _ in BENCH_METRICS]


def _bench_comparison_table(rows: list[dict]) -> str:
    labels = {key: header for key, header, _ in BENCH_METRICS}
    body = []
    for r in rows:
        verdict = "REAL DIFFERENCE (ranges don't overlap)" if r["separated"] else \
            f"not enough runs (< {BENCH_MIN_RUNS} with data)" if not r["enough_runs"] else "within the spread"
        body.append([labels[r["metric"]], _fmt_bench(r["metric"], r["first"]),
                     _fmt_bench(r["metric"], r["second"]), _fmt_bench_diff(r["metric"], r["diff"]), verdict])
    return _table(["Metric", "First", "Second", "Diff", "Verdict"], body)


def render_bench_table(report: dict) -> str:
    lines = ["AI Code Factory — Analytics: benchmark", f"Generated {report['generated_at']}"]
    for w in report["warnings"]:
        lines.append(f"! {_clean(w)}")
    lines.append("")
    if not report["labels"]:
        lines.append("No factory/bench-* changes found.")
        return "\n".join(lines)
    headers = ["Task", "Runs"] + [h for _, h, _ in BENCH_METRICS]
    for lab in report["labels"]:
        lines.append(f"== Label {_clean(lab['label'])}: medians across runs (oracle: share of runs passing) ==")
        rows = [[_clean(t["task"]), str(len(t["runs"]))] + _bench_cells(t["metrics"]) for t in lab["tasks"]]
        rows.append(["all tasks", str(lab["run_count"])] + _bench_cells(lab["metrics"]))
        lines.append(_table(headers, rows))
        lines.append("")
    cmp = report["comparison"]
    if cmp:
        first, second = _clean(cmp["first"]), _clean(cmp["second"])
        lines.append(f"== {first} (First) vs {second} (Second): Diff is Second minus First ==")
        lines.append(f"Only metrics whose [min, max] ranges across runs don't overlap are real differences; "
                     f"the rest is run-to-run noise. Per task is the fair comparison; all tasks pools the "
                     f"runs of {len(cmp['all_tasks']['tasks'])} shared task(s).")
        lines.append("")
        for t in cmp["tasks"]:
            lines.append(f"-- {_clean(t['task'])} --")
            lines.append(_bench_comparison_table(t["metrics"]))
            lines.append("")
        lines.append("-- all tasks --")
        lines.append(_bench_comparison_table(cmp["all_tasks"]["metrics"]))
    return "\n".join(lines).rstrip()


def _html_table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict) -> str:
    totals, roles, drivers, trend = report["totals"], report["roles"], report["cost_drivers"], report["trends"]

    role_rows = [[role, r["jobs"], _fmt_cost(r["total_cost_usd"]), _fmt_cost(r["avg_cost_usd"]),
                  f"{r['avg_duration_seconds']:.0f}s" if r["avg_duration_seconds"] is not None else "n/a",
                  _fmt_pct(r["success_rate"] * 100) if r["success_rate"] is not None else "n/a",
                  _fmt_int(r["context_tokens_current"]), r["context_trend"] or "n/a",
                  r["broker_calls"], r["broker_denied"], r["broker_errors"],
                  f"{r['broker_avg_ms']:.1f}" if r["broker_avg_ms"] is not None else "n/a"]
                 for role, r in sorted(roles.items(), key=lambda kv: kv[1]["total_cost_usd"] or 0, reverse=True)]
    job_rows = [[j["job_id"], j["role"], _fmt_cost(j["cost_usd"]),
                 j["elapsed_seconds"] if j["elapsed_seconds"] is not None else "n/a",
                 j.get("model") or "n/a", j.get("task") or ""] for j in drivers["top_jobs"]]
    type_rows = [[t["type"], t["jobs"], _fmt_cost(t["cost_usd"])] for t in drivers["by_task_type"]]
    model_rows = [[m["model"], m["jobs"], _fmt_cost(m["cost_usd"])] for m in drivers["by_model"]]
    day_rows = [[d["date"], _fmt_cost(d["cost_usd"])] for d in trend["cost_by_day"]]
    anomaly_rows = [[a["role"], a["job_id"], a["reason"]] for a in trend["anomalies"]]
    warnings_html = "".join(f"<li>{html.escape(w)}</li>" for w in report["warnings"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Code Factory — Analytics</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ margin-bottom: 0; }}
  .meta {{ color: #666; margin-top: 0.25rem; }}
  h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 0.5rem; }}
  th, td {{ text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #eee; }}
  th {{ background: #fafafa; }}
  .stats {{ margin-top: 1rem; }}
  .stat {{ display: inline-block; margin-right: 2.5rem; }}
  .stat .value {{ font-size: 1.4rem; font-weight: 600; display: block; }}
  ul.warnings {{ color: #a15c00; }}
</style>
</head>
<body>
<h1>AI Code Factory — Analytics</h1>
<p class="meta">Generated {html.escape(report['generated_at'])}</p>
<ul class="warnings">{warnings_html}</ul>

<div class="stats">
  <div class="stat"><span class="value">{totals['jobs']}</span>jobs</div>
  <div class="stat"><span class="value">{html.escape(_fmt_cost(totals['total_cost_usd']))}</span>total cost</div>
  <div class="stat"><span class="value">{html.escape(_fmt_cost(totals['avg_cost_per_job_usd']))}</span>avg cost / job</div>
</div>

<h2>Per role</h2>
{_html_table(["Role", "Jobs", "Total $", "Avg $/job", "Avg duration", "Success", "Context now", "Context trend", "Broker calls", "Denied", "Errors", "Avg ms"], role_rows) if role_rows else "<p>No jobs recorded yet.</p>"}

<h2>Most expensive jobs</h2>
{_html_table(["Job", "Role", "Cost", "Duration (s)", "Model", "Task"], job_rows) if job_rows else "<p>No costed jobs yet.</p>"}

<h2>Cost by task type</h2>
{_html_table(["Task type", "Jobs", "Cost"], type_rows) if type_rows else "<p>n/a</p>"}

<h2>Cost by model</h2>
{_html_table(["Model", "Jobs", "Cost"], model_rows) if model_rows else "<p>n/a</p>"}

<h2>Cost over time</h2>
<p>Last 7 days: {html.escape(_fmt_cost(trend['last_7_days_cost_usd']))} &nbsp; Last 30 days: {html.escape(_fmt_cost(trend['last_30_days_cost_usd']))}</p>
{_html_table(["Date", "Cost"], day_rows) if day_rows else ""}

<h2>Anomalies</h2>
{_html_table(["Role", "Job", "Reason"], anomaly_rows) if anomaly_rows else "<p>No anomalies detected.</p>"}
</body>
</html>
"""


# --------------------------------------------------------------------------
# CLI

def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 (got {n})")
    return n


def _write_and_open_html(state: Path, page: str) -> Path:
    """Write the HTML report to dispatch/analytics.html under state and try
    to open it in a browser. Returns the path regardless of whether a
    browser could actually be opened (headless host, no DISPLAY, etc.) --
    the caller always has the path to fall back on."""
    out_dir = state / "dispatch"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "analytics.html"
    out_path.write_text(page, encoding="utf-8")
    try:
        webbrowser.open(out_path.as_uri())
    except Exception:
        pass  # no usable browser controller (headless host, etc.) -- the path is still printed
    return out_path


def default_state() -> Path:
    """The factory state dir; keep in sync with FACTORY_STATE in scripts/lib/common.sh."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "state"
    return base / "ai-code-factory"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory analytics",
        description="Aggregate dispatch/broker audit logs into a cost and performance report.")
    parser.add_argument("--state", type=Path, default=None,
                        help="factory state dir that holds dispatch/ and broker/ "
                             "(default: $XDG_STATE_HOME/ai-code-factory or ~/.local/state/ai-code-factory)")
    parser.add_argument("--top", type=_positive_int, default=10,
                        help="how many top-cost jobs to list (default 10)")
    parser.add_argument("--changes", action="store_true",
                        help="report per change (factory/<topic> branch) instead: jobs, review rounds, "
                             "first-pass approval, wall time, cost and diff size, then median/p90 and "
                             "last 7 days vs the 7 before; works with --json")
    parser.add_argument("--bench", nargs="*", default=None, metavar="LABEL",
                        help="report the factory/bench-<task>-<label>-<n> changes by task and label: medians "
                             "across runs (oracle pass rate, rounds, wall time, cost, turns, grades, size "
                             "ratio); with two labels, also the difference and which metrics' ranges don't "
                             "overlap. No label: every label found; works with --json")
    parser.add_argument("--bench-tasks", type=Path, default=None, metavar="DIR",
                        help="directory of benchmark task files, whose names split the branch names "
                             "(default: bench/tasks in this checkout)")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the report as JSON")
    fmt.add_argument("--html", action="store_true",
                        help="render the report as HTML: opens it in a browser when stdout is "
                             "a terminal (written to <state>/dispatch/analytics.html first), otherwise "
                             "prints the HTML to stdout (e.g. when piped or redirected to a file)")
    args = parser.parse_args(argv)
    if args.changes and args.html:
        parser.error("--changes works with the table and --json, not --html")
    if args.bench is not None and (args.html or args.changes):
        parser.error("--bench works with the table and --json, not --html or --changes")

    state = (args.state or default_state()).resolve()
    if args.bench is not None:
        tasks_dir = args.bench_tasks or default_bench_tasks_dir()
        if not tasks_dir.is_dir():
            print(f"factory analytics: no benchmark tasks at {tasks_dir}; the task names split the "
                  f"branch names, so --bench needs them (--bench-tasks DIR to point elsewhere)", file=sys.stderr)
            return 1
        report = build_bench_report(state, tasks_dir, args.bench)
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_bench_table(report))
        return 0
    if args.changes:
        report = build_changes_report(state)
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_changes_table(report))
        return 0
    report = build_report(state, top_n=args.top)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.html:
        page = render_html(report)
        if sys.stdout.isatty():
            out_path = _write_and_open_html(state, page)
            print(f"Analytics report written to {out_path}")
        else:
            print(page)
    else:
        print(render_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
