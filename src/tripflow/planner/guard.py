"""出行守护：对既有行程单做临近出发的风险复核（天气预警 / 营业时间变化）。

与 watch 的车票监控拼成完整闭环：余票、灾害天气、临时闭馆线索，
发现即推送（终端 + webhook），管到"出发那一刻"。
"""

from __future__ import annotations

from datetime import date

from ..models import DayPlan, Itinerary
from ..providers.amap import AmapClient, Forecast
from ..util import parse_date

WEATHER_BAD_WORDS = ("暴雨", "大雨", "雷暴", "台风", "暴雪", "沙尘暴", "冰雹")
HIGH_TEMP_C = 38
LOW_TEMP_C = -8
OPENTIME_RECHECK_LIMIT = 12  # 最终 24h 内复查营业时间的 POI 上限（控 API 用量）


def _to_int(text: str) -> int | None:
    try:
        return int(str(text).strip())
    except (ValueError, TypeError):
        return None


def weather_alerts(
    days: list[DayPlan], forecasts_by_city: dict[str, list[Forecast]]
) -> tuple[list[str], list[tuple[DayPlan, str]]]:
    """对比行程记录与最新预报。

    返回 (预警列表, 更新列表[(day, 最新文本)])：
    - 预警：灾害天气词 / 白天 ≥38°C / 夜间 ≤-8°C
    - 更新：预报与行程单记录不一致（信息级，含 checked_at 语义）
    """
    latest: dict[tuple[str, str], Forecast] = {}
    for city, fcasts in forecasts_by_city.items():
        for f in fcasts:
            latest[(city, f.date)] = f
    alerts: list[str] = []
    updates: list[tuple[DayPlan, str]] = []
    for day in days:
        if not day.city:
            continue
        f = latest.get((day.city, day.date))
        if f is None:
            continue
        new_text = f"{f.dayweather} {f.daytemp}/{f.nighttemp}°C"
        if day.weather and day.weather != new_text:
            updates.append((day, new_text))
        for word in WEATHER_BAD_WORDS:
            if word in f.dayweather:
                alerts.append(
                    f"⚠️ {day.date} {day.city}预报「{f.dayweather}」，户外行程（"
                    f"{('、'.join(v.poi.name for v in day.items[:2])) or '自由活动'}）建议调整或备选室内"
                )
                break
        hi = _to_int(f.daytemp)
        lo = _to_int(f.nighttemp)
        if hi is not None and hi >= HIGH_TEMP_C:
            alerts.append(f"⚠️ {day.date} {day.city}高温 {hi}°C，注意防暑并避开正午户外")
        if lo is not None and lo <= LOW_TEMP_C:
            alerts.append(f"⚠️ {day.date} {day.city}夜间低温 {lo}°C，注意保暖与路面结冰")
    return alerts, updates


def opentime_changes(itinerary: Itinerary, amap: AmapClient) -> list[str]:
    """复查已排 POI 的营业时间（最终 24h 内执行），与行程记录不一致即提示人工确认。"""
    seen: set[str] = set()
    msgs: list[str] = []
    for day in itinerary.days:
        for v in day.items:
            if v.poi.poi_id in seen or len(seen) >= OPENTIME_RECHECK_LIMIT:
                continue
            seen.add(v.poi.poi_id)
            try:
                detail = amap.place_detail(v.poi.poi_id)
            except Exception:  # noqa: BLE001, S112 - 单点失败不阻塞
                continue
            if detail.opentime != v.poi.opentime:
                msgs.append(
                    f"⚠️ {v.poi.name} 营业时间有变化："
                    f"{v.poi.opentime or '原无记录'} → {detail.opentime or '暂无'}，请人工确认"
                )
    return msgs


def is_final_24h(depart_date: str, today: date | None = None) -> bool:
    """是否进入出发前最后 24 小时（营业时间复查档位）。"""
    import time as _time

    today = today or date(*_time.localtime()[:3])  # 本地日期，无时区换算语义
    return (parse_date(depart_date) - today).days <= 1
