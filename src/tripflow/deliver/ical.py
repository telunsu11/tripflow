"""ical 日历导出：交通段 + 每日行程点变成日历事件（浮动本地时间，随设备时区生效）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import Itinerary
from ..util import add_days, parse_loc


def _fmt_dt(date: str, hhmm: str) -> str:
    return f"{date.replace('-', '')}T{hhmm.replace(':', '')}00"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC5545 行长限制（75 八位组）——中文按字符折叠，主流日历软件兼容。"""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    out, cur = [], line
    while len(cur.encode("utf-8")) > 73:
        cut = len(cur)
        while len(cur[:cut].encode("utf-8")) > 73:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return out


def _event(
    uid: str,
    dt_start: str,
    dt_end: str,
    summary: str,
    location: str = "",
    description: str = "",
    geo: str = "",
    stamp: str = "",
) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}"]
    if stamp:
        lines.append(f"DTSTAMP:{stamp}")
    lines += [
        f"DTSTART:{dt_start}",
        f"DTEND:{dt_end}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if geo:
        lng, lat = parse_loc(geo)
        lines.append(f"GEO:{lat:.6f};{lng:.6f}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines.append("END:VEVENT")
    return lines


def render_ical(it: Itinerary) -> str:
    route = "→".join(it.request.route)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//tripflow//Itinerary//CN",
        f"X-WR-CALNAME:{_escape(f'tripflow {route}')}",
        "CALSCALE:GREGORIAN",
    ]
    stamp = __import__("time").strftime("%Y%m%dT%H%M%S")

    for i, leg in enumerate(it.legs, 1):
        if leg is None:
            continue
        # 跨夜到达：到达时刻小于出发时刻则结束日期 +1
        end_date = leg.date if leg.arrive_time >= leg.depart_time else add_days(leg.date, 1)
        uid = hashlib.md5(f"{leg.code}-{leg.date}-{i}".encode()).hexdigest() + "@tripflow"
        lines += _event(
            uid,
            _fmt_dt(leg.date, leg.depart_time),
            _fmt_dt(end_date, leg.arrive_time),
            f"🚄 {leg.code} {leg.from_station}→{leg.to_station}",
            location=leg.from_station,
            description=f"{leg.summary}\n出发城市: {leg.from_city} → {leg.to_city}",
            stamp=stamp,
        )

    for day in it.days:
        for j, v in enumerate(day.items):
            uid = hashlib.md5(f"{v.poi.poi_id}-{day.date}-{j}".encode()).hexdigest() + "@tripflow"
            desc = "\n".join(x for x in [v.poi.reason, v.poi.opentime] if x)
            lines += _event(
                uid,
                _fmt_dt(day.date, v.start),
                _fmt_dt(day.date, v.end),
                f"📍 {v.poi.name}",
                location=f"{v.poi.name}（{day.city}）" if day.city else v.poi.name,
                description=desc + (f"\n天气: {day.weather}" if day.weather else ""),
                geo=v.poi.location,
                stamp=stamp,
            )

    lines.append(f"X-TRIPFLOW-STAMP:{stamp}")
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"


def write_ical(it: Itinerary, path: Path) -> Path:
    path.write_text(render_ical(it), encoding="utf-8")
    return path
