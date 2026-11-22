"""Build an iPhone-friendly calendar feed of Forex Factory USD alerts.

Pulls Forex Factory's weekly export, keeps USD bank holidays plus
NFP / CPI / PPI / FOMC rate decisions, remembers them in data/events.json
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
WEEK_PAGE = ROOT / "docs" / "week.html"   # embeddable "this week" box (e.g. in Notion)
WEEK_IMPACTS = {"High", "Holiday"}        # which USD rows the week page shows

PACIFIC = ZoneInfo("America/Los_Angeles")
ALERT_HOUR_PT = 20          # 8 PM the night before
PRE_RELEASE_MINUTES = 30    # second alert for news events
MORNING_ALERT = (6, 30)     # 6:30 AM PT morning-of reminder ...
MORNING_CATEGORIES = {"holiday", "fomc-decision"}  # ... for these categories
KEEP_PAST_DAYS = 30         # drop stored events older than this
BACKSTOP_DAYS = 400         # how far ahead to seed computed holidays

# (category, label, emoji, matcher). Order matters: first match wins.
NEWS_RULES = [
    ("nfp", "NFP", "\U0001F4BC", lambda t: t == "Non-Farm Employment Change"),
    ("cpi", "CPI", "\U0001F4CA", lambda t: re.search(r"\bCPI\b", t) is not None),
    ("ppi", "PPI", "\U0001F3ED", lambda t: re.search(r"\bPPI\b", t) is not None),
    ("fomc-decision", "FOMC Rate Decision", "\U0001F3DB",
     lambda t: t in ("Federal Funds Rate", "FOMC Statement")),
]
ACTIVE_CATEGORIES = {"holiday"} | {rule[0] for rule in NEWS_RULES}


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

    # drop past events and any category no longer in NEWS_RULES
    return {uid: ev for uid, ev in sorted(merged.items(), key=lambda kv: event_day(kv[1]).isoformat())
            if event_day(ev) >= cutoff and ev["category"] in ACTIVE_CATEGORIES}


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


def morning_lead(start_utc: datetime) -> timedelta:
    """Time between the morning-of alert (Pacific) and the release; <= 0 if release is earlier."""
    local = start_utc.astimezone(PACIFIC)
    h, m = MORNING_ALERT
    alert = datetime.combine(local.date(), datetime.min.time().replace(hour=h, minute=m), PACIFIC)
    return start_utc - alert.astimezone(timezone.utc)


def build_ics(events: dict, now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ff-alerts//Forex Factory USD alerts//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:FF USD Alerts",
        "X-WR-CALDESC:Forex Factory USD bank holidays\\, NFP\\, CPI\\, PPI and FOMC rate decisions",
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
            if "holiday" in MORNING_CATEGORIES:
                h, m = MORNING_ALERT  # positive trigger = time after local midnight
                lines += _alarm(f"PT{h}H{m}M" if m else f"PT{h}H",
                                f"Today is a USD bank holiday - don't trade. {summary}")
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
            morning = morning_lead(start)
            if ev["category"] in MORNING_CATEGORIES and morning > timedelta(minutes=PRE_RELEASE_MINUTES):
                lines += _alarm(_duration(morning),
                                f"Today {local}: {ev['label']} - careful before you trade")
            lines += _alarm(f"-PT{PRE_RELEASE_MINUTES}M",
                            f"{ev['label']} in {PRE_RELEASE_MINUTES} min ({local})")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = [part for line in lines for part in _fold(line)]
    return "\r\n".join(folded) + "\r\n"


# ---------------------------------------------------------------- week page
def _html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def build_week_html(items: list[dict], now: datetime) -> str:
    """Compact Sun-Sat table of this week's USD high-impact news + holidays, times in PT."""
    rows = []
    for item in items:
        if item.get("country") != "USD" or item.get("impact") not in WEEK_IMPACTS:
            continue
        start = datetime.fromisoformat(item["date"])
        rows.append((start, item))
    rows.sort(key=lambda r: r[0])

    # the FF feed covers one Sun-Sat week; anchor on its first row (or today)
    anchor = (rows[0][0].astimezone(PACIFIC).date() if rows
              else now.astimezone(PACIFIC).date())
    sunday = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
    days = [sunday + timedelta(days=i) for i in range(7)]
    today = now.astimezone(PACIFIC).date()

    by_day: dict[date, list[str]] = {d: [] for d in days}
    for start, item in rows:
        title = (item.get("title") or "").strip()
        if item.get("impact") == "Holiday":
            day = start.date()  # FF holiday date is the holiday itself
            cells = ('<td class="time">All day</td>'
                     f'<td class="ev"><span class="hol">\U0001F6AB {_html(title)} - no trading</span></td>'
                     '<td class="num"></td><td class="num"></td>')
        else:
            local = start.astimezone(PACIFIC)
            day = local.date()
            kind = classify(item)
            tag = f"{kind[2]} " if kind else ""
            t = local.strftime("%I:%M%p").lstrip("0").lower()
            cells = (f'<td class="time">{t}</td>'
                     f'<td class="ev"><span class="dot"></span>{tag}{_html(title)}</td>'
                     f'<td class="num">{_html(item.get("forecast") or "")}</td>'
                     f'<td class="num">{_html(item.get("previous") or "")}</td>')
        if day in by_day:
            by_day[day].append(cells)

    body = []
    for i, d in enumerate(days):
        label = f'{d.strftime("%a")}<br>{d.strftime("%b")} {d.day}'
        cls = "day today" if d == today else "day"
        shade = " alt" if i % 2 else ""
        events = by_day[d] or ['<td class="time"></td><td class="ev empty"></td>'
                               '<td class="num"></td><td class="num"></td>']
        for j, cells in enumerate(events):
            first = (f'<td class="{cls}" rowspan="{len(events)}">{label}</td>' if j == 0 else "")
            body.append(f'<tr class="row{shade}">{first}{cells}</tr>')

    week_label = f'{days[0].strftime("%b")} {days[0].day} - {days[-1].strftime("%b")} {days[-1].day}'
    updated = now.astimezone(PACIFIC).strftime("%a %b %d, %I:%M %p PT").replace(" 0", " ")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USD news - {week_label}</title>
<style>
:root {{ --bg:#ffffff; --alt:#f5f5f4; --fg:#2f2f2f; --muted:#8a8a87; --line:#e6e6e4;
        --head:#37352f; --red:#d44c47; --today:#2383e2; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#191919; --alt:#202020; --fg:#e6e6e4; --muted:#8a8a87; --line:#2f2f2f;
          --head:#e6e6e4; --red:#ff6b6b; --today:#5aa9ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:13px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
.top {{ display:flex; justify-content:space-between; align-items:baseline; padding:10px 12px 6px; }}
.top b {{ color:var(--head); font-size:14px; }}
.top span {{ color:var(--muted); font-size:11px; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ text-align:left; font-weight:500; color:var(--muted); font-size:11px; padding:4px 8px;
      border-bottom:1px solid var(--line); }}
td {{ padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:middle; }}
tr.alt td {{ background:var(--alt); }}
td.day {{ width:64px; text-align:center; color:var(--muted); font-size:12px; }}
td.day.today {{ color:var(--today); font-weight:600; }}
td.time {{ width:72px; white-space:nowrap; color:var(--muted); }}
td.num {{ width:70px; text-align:right; white-space:nowrap; }}
th.num {{ text-align:right; }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:2px; background:var(--red);
        margin-right:8px; vertical-align:1px; }}
.hol {{ color:var(--red); font-weight:500; }}
.foot {{ padding:6px 12px 10px; color:var(--muted); font-size:11px; }}
.foot a {{ color:var(--muted); }}
</style></head><body>
<div class="top"><b>USD high-impact news &middot; {week_label}</b><span>times in PT</span></div>
<table>
<tr><th></th><th>Time</th><th>Event</th><th class="num">Forecast</th><th class="num">Previous</th></tr>
{chr(10).join(body)}
</table>
<div class="foot">Updated {updated} &middot; <a href="https://www.forexfactory.com/calendar?week=this" target="_blank" rel="noopener">Forex Factory</a></div>
</body></html>
"""


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

    # only rewrite the week page when FF answered, so an outage doesn't blank it
    if feed_items:
        WEEK_PAGE.write_text(build_week_html(feed_items, now), "utf-8")
    print(f"FF rows: {len(feed_items)}, matched this week: {len(fresh)}, "
          f"events in calendar: {len(merged)} -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
