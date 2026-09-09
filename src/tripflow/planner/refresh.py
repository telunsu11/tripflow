"""refresh：出发前刷新既有行程单的时效数据（车票余票/票价 + 天气），保留原编排。"""

from __future__ import annotations

from ..models import Itinerary, TransitChoice
from ..planner.transit import direct_choice
from ..providers.amap import AmapClient
from ..providers.rail import RailSession, TrainTicket


def match_leg(tickets: list[TrainTicket], code: str) -> TrainTicket | None:
    return next((t for t in tickets if t.train_code == code), None)


def refresh_leg_choice(
    old: TransitChoice, fresh_ticket: TrainTicket | None, travelers: int
) -> tuple[TransitChoice, str]:
    """用最新车次数据更新一段交通；返回 (新 choice, 变更说明)。"""
    if fresh_ticket is None:
        old.seats_ok = False
        return old, f"{old.code} 已不在最新查询结果中（可能停运或售罄下架），请人工确认"
    new = direct_choice(fresh_ticket, travelers, old.date)
    new.from_city, new.to_city = old.from_city, old.to_city
    changes = []
    if new.price_per_person != old.price_per_person:
        changes.append(
            f"{old.code} 票价 ¥{old.price_per_person or 0:g} → ¥{new.price_per_person or 0:g}"
        )
    if new.seats_ok != old.seats_ok:
        changes.append(f"{old.code} 余票 {'恢复充足' if new.seats_ok else '变为不足'}")
    return new, "；".join(changes) if changes else f"{old.code} 无变化"


async def run_refresh(
    rail: RailSession, amap: AmapClient, it: Itinerary
) -> tuple[Itinerary, list[str]]:
    """原地刷新 itinerary 的时效字段，返回 (刷新后行程单, 变更清单)。"""
    changes: list[str] = []
    travelers = it.request.travelers

    # 1) 逐段刷新车票
    for i, leg in enumerate(it.legs):
        if leg is None:
            continue
        tickets = await rail.tickets(
            leg.date, leg.from_city, leg.to_city, filter_flags="GD", sort="startTime"
        )
        fresh = match_leg(tickets, leg.code)
        new_leg, msg = refresh_leg_choice(leg, fresh, travelers)
        it.legs[i] = new_leg
        if msg:
            changes.append(msg)

    # 2) 按城市刷新天气
    cities = {d.city for d in it.days if d.city}
    for city in cities:
        try:
            forecasts = amap.weather(city)
        except Exception:  # noqa: BLE001, S112 - 单城天气失败不影响整体
            continue
        wmap = {f.date: f"{f.dayweather} {f.daytemp}/{f.nighttemp}°C" for f in forecasts}
        for day in it.days:
            if day.city == city and day.date in wmap and day.weather != wmap[day.date]:
                changes.append(f"{day.date} {city}天气：{day.weather or '—'} → {wmap[day.date]}")
                day.weather = wmap[day.date]

    it.generated_at = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    it.feasibility.notes.append(f"已于 {it.generated_at} 刷新车票与天气（POI 编排保持不变）")
    if any(not getattr(l, "seats_ok", True) for l in it.legs if l):
        it.feasibility.issues.append("刷新发现余票不足的班次，请候补或改选（见交通段）")
        if it.feasibility.status == "FEASIBLE":
            it.feasibility.status = "FEASIBLE_WITH_RISK"
    return it, changes
