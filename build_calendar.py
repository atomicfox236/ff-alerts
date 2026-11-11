"""Build an iPhone-friendly calendar feed of Forex Factory USD alerts.

Pulls Forex Factory's weekly export, keeps USD bank holidays plus
NFP / CPI / PPI / FOMC Meeting Minutes, remembers them in data/events.json
(the FF feed only covers the current week), adds computed Federal Reserve
bank holidays a year ahead as a backstop, and writes docs/ff-usd.ics.

Alerts:
  * Holidays: 8:00 PM Pacific the night before.
  * News: 8:00 PM Pacific the night before, and 30 minutes before release.

Standard library only (plus the `tzdata` package on Windows).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "events.json"
OUTPUT = ROOT / "docs" / "ff-usd.ics"

PACIFIC = ZoneInfo("America/Los_Angeles")
ALERT_HOUR_PT = 20          # 8 PM the night before
PRE_RELEASE_MINUTES = 30    # second alert for news events
KEEP_PAST_DAYS = 30         # drop stored events older than this
BACKSTOP_DAYS = 400         # how far ahead to seed computed holidays

# (category, label, emoji, matcher). Order matters: first match wins.
NEWS_RULES = [
    ("nfp", "NFP", "\U0001F4BC", lambda t: t == "Non-Farm Employment Change"),
    ("cpi", "CPI", "\U0001F4CA", lambda t: re.search(r"\bCPI\b", t) is not None),
    ("ppi", "PPI", "\U0001F3ED", lambda t: re.search(r"\bPPI\b", t) is not None),
    ("fomc-minutes", "FOMC Minutes", "\U0001F3DB", lambda t: t == "FOMC Meeting Minutes"),
]


# ---------------------------------------------------------------- fetching
def fetch_feed(url: str = FEED_URL) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "ff-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- selecting
def classify(item: dict) -> tuple[str, str, str] | None:
    """Return (category, label, emoji) for wanted USD rows, else None."""
    if item.get("country") != "USD":
        return None
    title = (item.get("title") or "").strip()
    if item.get("impact") == "Holiday":
        return ("holiday", "Bank Holiday", "\U0001F6AB")
    for category, label, emoji, match in NEWS_RULES:
        if match(title):
            return (category, label, emoji)
    return None


def events_from_feed(items: list[dict]) -> dict[str, dict]:
    """Group wanted rows into events keyed by stable UID."""
    events: dict[str, dict] = {}
    for item in items:
        kind = classify(item)
        if not kind:
            continue
        category, label, emoji = kind
        start = datetime.fromisoformat(item["date"])
        title = item["title"].strip()

        if category == "holiday":
            day = start.date().isoformat()  # FF date is in ET; date part is the holiday
            uid = f"holiday-{day.replace('-', '')}@ff-alerts"
            ev = events.setdefault(uid, {
                "uid": uid, "category": "holiday", "all_day": True,
                "date": day, "titles": [], "lines": [], "source": "Forex Factory",
            })
        else:
            start_utc = start.astimezone(timezone.utc)
            day = start.date().isoformat()
            uid = f"{category}-{day.replace('-', '')}@ff-alerts"
            ev = events.setdefault(uid, {
                "uid": uid, "category": category, "all_day": False,
                "start_utc": start_utc.isoformat(), "label": label, "emoji": emoji,
                "titles": [], "lines": [], "source": "Forex Factory",
            })
            # if FF lists sub-items at different times, alert on the earliest
            if start_utc.isoformat() < ev["start_utc"]:
                ev["start_utc"] = start_utc.isoformat()

        if title not in ev["titles"]:
            ev["titles"].append(title)
            detail = []
            if item.get("forecast"):
                detail.append(f"forecast {item['forecast']}")
            if item.get("previous"):
                detail.append(f"previous {item['previous']}")
            ev["lines"].append(title + (f" ({', '.join(detail)})" if detail else ""))
    return events


# ---------------------------------------------------------------- Fed holidays
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def fed_holidays(year: int) -> dict[date, str]:
    """Federal Reserve bank holidays. Fed rule: Sunday -> Monday, Saturday not moved."""
    fixed = {
        date(year, 1, 1): "New Year's Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        date(year, 11, 11): "Veterans Day",
        date(year, 12, 25): "Christmas Day",
    }
    out: dict[date, str] = {}
    for d, name in fixed.items():
        if d.weekday() == 6:
            out[d + timedelta(days=1)] = name + " (observed)"
        elif d.weekday() != 5:
            out[d] = name
    MON, THU = 0, 3
    out[_nth_weekday(year, 1, MON, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, MON, 3)] = "Presidents' Day"
    out[_last_weekday(year, 5, MON)] = "Memorial Day"
    out[_nth_weekday(year, 9, MON, 1)] = "Labor Day"
    out[_nth_weekday(year, 10, MON, 2)] = "Columbus Day"
    out[_nth_weekday(year, 11, THU, 4)] = "Thanksgiving Day"
    return out


def backstop_holidays(today: date) -> dict[str, dict]:
    events = {}
    end = today + timedelta(days=BACKSTOP_DAYS)
    for year in range(today.year, end.year + 1):
        for d, name in fed_holidays(year).items():
            if today - timedelta(days=KEEP_PAST_DAYS) <= d <= end:
                uid = f"holiday-{d.strftime('%Y%m%d')}@ff-alerts"
                events[uid] = {
                    "uid": uid, "category": "holiday", "all_day": True,
                    "date": d.isoformat(), "titles": [f"Bank Holiday ({name})"],
                    "lines": [name], "source": "Federal Reserve holiday schedule",
                }
    return events


# ---------------------------------------------------------------- merging
def merge(stored: dict, backstop: dict, fresh: dict, today: date) -> dict:
    merged = dict(backstop)
    merged.update(stored)   # previously seen FF events beat the computed backstop
    merged.update(fresh)    # this week's FF data beats everything
    cutoff = today - timedelta(days=KEEP_PAST_DAYS)

    def event_day(ev: dict) -> date:
        if ev["all_day"]:
            return date.fromisoformat(ev["date"])
        return datetime.fromisoformat(ev["start_utc"]).date()

    return {uid: ev for uid, ev in sorted(merged.items(), key=lambda kv: event_day(kv[1]).isoformat())
            if event_day(ev) >= cutoff}


# ---------------------------------------------------------------- ICS
def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """Fold to 75 octets per RFC 5545."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(cur) + len(b) > limit:
            out.append(cur.decode("utf-8"))
            cur = b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return [out[0]] + [" " + p for p in out[1:]]


def _duration(td: timedelta) -> str:
    """Negative ICS duration like -PT9H30M (td is the positive lead time)."""
    total = int(td.total_seconds()) // 60
    days, rem = divmod(total, 24 * 60)
    hours, mins = divmod(rem, 60)
    s = "-P" + (f"{days}D" if days else "")
    if hours or mins or not days:
        s += "T" + (f"{hours}H" if hours else "") + (f"{mins}M" if mins else "")
    return s


def _alarm(trigger: str, text: str) -> list[str]:
    return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(text)}",
            f"TRIGGER:{trigger}", "END:VALARM"]


def night_before_lead(start_utc: datetime) -> timedelta:
    """Time between 8 PM Pacific the evening before and the release."""
    local = start_utc.astimezone(PACIFIC)
    alert = datetime.combine(local.date() - timedelta(days=1),
                             datetime.min.time().replace(hour=ALERT_HOUR_PT), PACIFIC)
    return start_utc - alert.astimezone(timezone.utc)


def build_ics(events: dict, now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ff-alerts//Forex Factory USD alerts//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:FF USD Alerts",
        "X-WR-CALDESC:Forex Factory USD bank holidays\\, NFP\\, CPI\\, PPI and FOMC Minutes",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events.values():
        desc = "\n".join(ev["lines"] + ["", f"Source: {ev['source']}",
                                        "https://www.forexfactory.com/calendar"])
        lines += ["BEGIN:VEVENT", f"UID:{ev['uid']}", f"DTSTAMP:{stamp}"]
        if ev["all_day"]:
            d = date.fromisoformat(ev["date"])
            name = ev["titles"][0]
            summary = f"\U0001F6AB USD {name} - no trading" if name.startswith("Bank Holiday") \
                else f"\U0001F6AB USD Bank Holiday: {', '.join(ev['titles'])} - no trading"
            lines += [f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                      "TRANSP:TRANSPARENT",
                      f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(desc)}"]
            # all-day events start at local midnight, so -4h = 8 PM the night before
            lines += _alarm(f"-PT{24 - ALERT_HOUR_PT}H",
                            f"Tomorrow is a USD bank holiday - don't plan to trade. {summary}")
        else:
            start = datetime.fromisoformat(ev["start_utc"])
            end = start + timedelta(minutes=15)
            summary = f"{ev['emoji']} USD {', '.join(ev['titles'])}"
            local = start.astimezone(PACIFIC).strftime("%-I:%M %p PT" if sys.platform != "win32" else "%#I:%M %p PT")
            lines += [f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                      f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
                      f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(desc)}"]
            lines += _alarm(_duration(night_before_lead(start)),
                            f"Tomorrow {local}: {ev['label']} release ({', '.join(ev['titles'])})")
            lines += _alarm(f"-PT{PRE_RELEASE_MINUTES}M",
                            f"{ev['label']} in {PRE_RELEASE_MINUTES} min ({local})")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = [part for line in lines for part in _fold(line)]
    return "\r\n".join(folded) + "\r\n"


# ---------------------------------------------------------------- main
def main(feed_items: list[dict] | None = None) -> None:
    now = datetime.now(timezone.utc)
    today = now.astimezone(PACIFIC).date()
    stored = json.loads(STORE.read_text("utf-8")) if STORE.exists() else {}

    if feed_items is None:
        try:
            feed_items = fetch_feed()
        except Exception as exc:  # keep publishing stored events if FF is down
            print(f"WARNING: could not fetch Forex Factory feed: {exc}", file=sys.stderr)
            feed_items = []

    fresh = events_from_feed(feed_items)
    merged = merge(stored, backstop_holidays(today), fresh, today)

    # persist only Forex Factory-sourced events; the backstop is recomputed each run
    to_store = {uid: ev for uid, ev in merged.items() if ev["source"] == "Forex Factory"}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(to_store, indent=2, ensure_ascii=False) + "\n", "utf-8")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(build_ics(merged, now).encode("utf-8"))
    print(f"FF rows: {len(feed_items)}, matched this week: {len(fresh)}, "
          f"events in calendar: {len(merged)} -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
