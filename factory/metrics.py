"""Per-change metrics shared by scripts/analytics.py and the dispatch bridge:
job-end records grouped by the factory/<topic> branch they worked on, and one
row of numbers per change. Standard library only; must not import scripts/."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(ts) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _round(value, ndigits: int = 4):
    return round(value, ndigits) if isinstance(value, (int, float)) else value


_MAX_ABS_VALUE = 1e12  # generous bound for any cost/duration/token/ms field; anything
                       # beyond this is corrupt data, not a real measurement, and risks
                       # OverflowError in formatting or Infinity from summing many of them


def _num(value):
    """A finite, sanely-bounded int/float, or None. Rejects bools, non-numbers,
    the NaN/Infinity values json.loads happily accepts, and absurdly large
    magnitudes (they must never reach sum()/int()/format() or leak into
    --json output, which has to stay strict JSON)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if abs(value) > _MAX_ABS_VALUE:
        return None
    return value


def _finite_sum(values):
    """sum(values), or None if it overflowed to +-inf (guards aggregation
    even though _num already bounds each individual term)."""
    total = sum(values)
    return total if math.isfinite(total) else None


_CHANGE_RE = re.compile(r"factory/[a-z0-9-]+")
_VERDICTS = ("APPROVED", "REVISE")


def _int_field(value):
    v = _num(value)
    return int(v) if v is not None and v >= 0 else None


def _job_span(job: dict):
    """(start, end) datetimes of a job-end record: its ts is the end, minus its elapsed_seconds."""
    end = parse_ts(job.get("ts"))
    if end is None:
        return None, None
    elapsed = _num(job.get("elapsed_seconds"))
    start = end - timedelta(seconds=elapsed) if elapsed is not None and elapsed >= 0 else end
    return start, end


def _diff_of(job: dict):
    """(lines, files, test share of the added lines) from a record's diffstat, or Nones."""
    size = job.get("size")
    if isinstance(size, dict):
        added, removed, files, tests = (_int_field(size.get(k)) for k in
                                        ("lines_added", "lines_removed", "files", "test_lines_added"))
        if None not in (added, removed, files):
            share = round(tests / added, 4) if tests is not None and added > 0 and tests <= added else None
            return added + removed, files, share
    lines, files = _int_field(job.get("diff_lines")), _int_field(job.get("diff_files"))
    return lines, files, None


def change_row(change: str, jobs: list[dict]) -> dict:
    jobs = sorted(jobs, key=lambda j: parse_ts(j.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    verdicts = [j["verdict"] for j in jobs if j.get("verdict") in _VERDICTS]
    spans = [sp for sp in (_job_span(j) for j in jobs) if sp[0] is not None]
    costs = [c for c in (_num(j.get("cost_usd")) for j in jobs) if c is not None]
    cost = _finite_sum(costs) if costs else None
    # Each developer record's diffstat is the branch against HEAD, so the latest one is the
    # whole change so far (summing the rounds would count the same lines again).
    diff = next((d for d in (_diff_of(j) for j in reversed(jobs)) if d[0] is not None), (None, None, None))
    turns = [t for t in (_int_field(j.get("turns")) for j in jobs) if t is not None]
    return {
        "change": change,
        "jobs": len(jobs),
        "review_rounds": verdicts.count("REVISE"),
        "first_pass_approval": verdicts[0] == "APPROVED" if verdicts else None,
        "wall_seconds": round((max(e for _, e in spans) - min(s for s, _ in spans)).total_seconds())
                        if spans else None,
        "cost_usd": _round(cost) if cost is not None else None,
        "diff_lines": diff[0], "diff_files": diff[1], "test_lines_share": diff[2],
        "turns": sum(turns) if turns else None,
        "last_job_at": ts(max(e for _, e in spans)) if spans else None,
    }


def ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def percentile(values: list, q: float):
    """q-th percentile (0..1) by linear interpolation between ranks; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def group_changes(jobs: list[dict]) -> tuple[dict, list[dict]]:
    """(change -> its job-end records, records without a valid change)."""
    grouped: dict = defaultdict(list)
    unknown = []
    for j in jobs:
        change = j.get("change")
        if isinstance(change, str) and _CHANGE_RE.fullmatch(change):
            grouped[change].append(j)
        else:
            unknown.append(j)
    return grouped, unknown
