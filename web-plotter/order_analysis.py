from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from order_metrics import summarize_cuts


def analysis_period(params):
    try:
        zone = ZoneInfo(params.get("tz", "UTC"))
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Неизвестный часовой пояс.") from error
    today = datetime.now(zone).date()
    try:
        end = date.fromisoformat(params["to"]) if "to" in params else today
        start = date.fromisoformat(params["from"]) if "from" in params else end - timedelta(days=29)
    except (ValueError, OverflowError) as error:
        raise ValueError("Укажите даты периода в формате ГГГГ-ММ-ДД.") from error
    if start > end:
        raise ValueError("Дата начала позже даты окончания периода.")
    return start, end, zone


def closure_day(stats, zone):
    stamp = stats.get("finishedAt")
    if stamp:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(zone).date()
    return None


def productivity(cuts):
    # Only compare distances and elapsed times recorded for the same fabrics.
    measured = [cut["stats"] for cut in cuts if cut["stats"].get("actualSeconds") is not None
                and cut["stats"].get("estimatedWorkSeconds") is not None]
    hours = sum(item["actualSeconds"] for item in measured) / 3600

    def speed(field):
        if hours and all(item.get(field) is not None for item in measured):
            return sum(item[field] for item in measured) / 1000 / hours
        return None

    return {
        "pairedCount": len(measured),
        "efficiencyPercent": (sum(item["estimatedWorkSeconds"] for item in measured) / (hours * 3600) * 100
                              if hours else None),
        "fabricMetersPerHour": speed("lengthMm"),
        "cutMetersPerHour": speed("cutDistanceMm"),
    }


def order_analysis(orders, period):
    start, end, zone = period
    daily = defaultdict(list)
    rows = []
    closed = []
    for order in orders:
        count = 0
        for cut in order["materials"]:
            day = closure_day(cut["stats"], zone) if cut["status"] == "done" else None
            if day and start <= day <= end:
                daily[day].append(cut)
                count += 1
        day = closure_day(order["stats"], zone)
        closed_here = bool(day and start <= day <= end)
        if closed_here:
            closed.append(order["stats"])
        if closed_here or count:
            stats = order["stats"] if closed_here else {**order["stats"], "deltaSeconds": None}
            rows.append({**order, "stats": stats, "closedInPeriod": closed_here, "periodCutCount": count})

    cuts = [cut for group in daily.values() for cut in group]
    spans = [order["actualSeconds"] for order in closed if order.get("actualSeconds") is not None]
    totals = summarize_cuts(cuts)
    summary = {field: totals[field] for field in (
        "lengthMm", "cutDistanceMm", "travelDistanceMm", "areaMm2", "partsAreaMm2", "utilizationPercent",
        "estimatedWorkSeconds", "fabricActualSeconds", "plannedCount", "timedCount", "approximate",
    )}
    summary.update(productivity(cuts))
    summary.update(completedOrders=len(closed), completedCuts=len(cuts),
                   orderActualSeconds=sum(spans) if spans else None, timedOrderCount=len(spans))
    summary["actualSeconds"] = summary["orderActualSeconds"]
    for field, result in (("cutSpeedMmS", "cutSpeedsMmS"), ("travelSpeedMmS", "travelSpeedsMmS")):
        summary[result] = sorted({c["stats"][field] for c in cuts if c["stats"].get(field) is not None})
    days = []
    for day, group in sorted(daily.items()):
        lengths = [cut["stats"]["lengthMm"] for cut in group if cut["stats"].get("lengthMm") is not None]
        days.append({"date": day.isoformat(), "completedCuts": len(group),
                     "lengthMm": sum(lengths) if lengths else None, **productivity(group)})
    return {"from": start.isoformat(), "to": end.isoformat(), "summary": summary, "days": days, "orders": rows}
