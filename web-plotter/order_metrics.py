from datetime import datetime, timezone
from math import isfinite


SUM_METRICS = ("lengthMm", "areaMm2", "partsAreaMm2", "cutDistanceMm", "travelDistanceMm", "estimatedWorkSeconds")
SPEED_METRICS = ("cutSpeedMmS", "travelSpeedMmS")
PLAN_METRICS = (*SUM_METRICS, "utilizationPercent", *SPEED_METRICS)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def metric_value(value):
    if type(value) in (int, float) and isfinite(value) and value >= 0:
        return value
    return None


def plan_snapshot(metrics, source):
    if metrics is None:
        return None
    snapshot = {key: metric_value(metrics.get(key)) for key in PLAN_METRICS}
    if all(value is None for value in snapshot.values()):
        return None
    return {**snapshot, "source": source, "approximate": metrics.get("approximate") is True}


def elapsed_seconds(start, finish):
    if not (start and finish):
        return None
    seconds = (datetime.fromisoformat(finish.replace("Z", "+00:00"))
               - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return seconds if seconds >= 0 else None


def cut_metrics(metrics, status, start, finish):
    start = start if status in ("active", "done") else None
    finish = finish if status == "done" else None
    actual = elapsed_seconds(start, finish)
    plan = {**dict.fromkeys(PLAN_METRICS), "source": None, "approximate": None, **(metrics or {})}
    estimate = plan["estimatedWorkSeconds"]
    return {**plan, "startedAt": start, "finishedAt": finish, "actualSeconds": actual,
            "deltaSeconds": actual - estimate if actual is not None and estimate is not None else None}


def summarize_cuts(cuts):
    cuts = list(cuts)
    records = [cut.get("stats") or {} for cut in cuts]
    plans = [record for record in records if record.get("source")]
    timed = [record["actualSeconds"] for record in records if record.get("actualSeconds") is not None]
    starts = [record["startedAt"] for record in records if record.get("startedAt")]
    finishes = [record["finishedAt"] for record in records if record.get("finishedAt")]
    totals = dict.fromkeys(PLAN_METRICS)
    for key in SUM_METRICS:
        values = [record[key] for record in plans if record.get(key) is not None]
        if values:
            totals[key] = sum(values)
    for key in SPEED_METRICS:
        speeds = {record.get(key) for record in plans}
        if len(speeds) == 1:
            totals[key] = speeds.pop()
    area = totals["areaMm2"]
    if area and all(record.get("areaMm2") is not None and record.get("partsAreaMm2") is not None
                    and record.get("utilizationPercent") is not None for record in plans):
        totals["utilizationPercent"] = totals["partsAreaMm2"] / area * 100

    count = len(cuts)
    missing_start = any(cut["status"] in ("active", "done") and not record.get("startedAt")
                        for cut, record in zip(cuts, records))
    started = min(starts, default=None) if not missing_start else None
    all_done = bool(count) and all(cut["status"] == "done" for cut in cuts)
    finished = max(finishes) if all_done and len(finishes) == count else None
    actual = elapsed_seconds(started, finished) if count and len(timed) == count else None
    delta = (actual - totals["estimatedWorkSeconds"] if actual is not None
             and all(record.get("estimatedWorkSeconds") is not None for record in records) else None)
    sources = {record["source"] for record in plans}
    return {**totals, "source": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else None,
            "approximate": any(record.get("approximate") for record in plans) if plans else None,
            "plannedCount": len(plans), "totalCount": count, "timedCount": len(timed),
            "fabricActualSeconds": sum(timed) if timed else None, "actualSeconds": actual, "deltaSeconds": delta,
            "startedAt": started, "finishedAt": finished}
