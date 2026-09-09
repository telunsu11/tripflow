"""余票监控：定期复查行程单所选车次，余票/票价变化即提醒（终端 + 可选 webhook）。"""

from __future__ import annotations

import time as _time

import httpx

from ..models import Itinerary
from ..providers.rail import RailSession
from .refresh import match_leg, refresh_leg_choice


def snapshot(it: Itinerary) -> dict[int, dict]:
    """当前各段车票状态快照。"""
    out = {}
    for i, leg in enumerate(it.legs):
        if leg is None:
            continue
        out[i] = {
            "code": leg.code,
            "seats_ok": leg.seats_ok,
            "price": leg.price_per_person,
            "summary": leg.summary,
        }
    return out


def diff_snapshots(old: dict[int, dict], new: dict[int, dict]) -> list[str]:
    changes = []
    for i, cur in new.items():
        prev = old.get(i)
        if prev is None:
            continue
        if cur["seats_ok"] != prev["seats_ok"]:
            changes.append(
                f"{cur['code']} 余票{'恢复充足 ✅' if cur['seats_ok'] else '变为不足 ⚠️'}"
                f"（此前{'充足' if prev['seats_ok'] else '不足'}）"
            )
        if cur["price"] != prev["price"]:
            changes.append(f"{cur['code']} 票价 ¥{prev['price'] or 0:g} → ¥{cur['price'] or 0:g}")
    return changes


async def watch_once(rail: RailSession, it: Itinerary) -> Itinerary:
    """复查所有车段（复用 refresh 的匹配与更新逻辑，不查天气）。"""
    travelers = it.request.travelers
    for i, leg in enumerate(it.legs):
        if leg is None:
            continue
        tickets = await rail.tickets(
            leg.date, leg.from_city, leg.to_city, filter_flags="GD", sort="startTime"
        )
        fresh = match_leg(tickets, leg.code)
        new_leg, _ = refresh_leg_choice(leg, fresh, travelers)
        it.legs[i] = new_leg
    return it


def notify_webhook(url: str, changes: list[str], itinerary_name: str) -> bool:
    """通用 webhook 通知（POST JSON）；失败不中断监控。"""
    try:
        resp = httpx.post(
            url,
            json={
                "event": "tripflow.watch",
                "itinerary": itinerary_name,
                "changes": changes,
                "at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=10,
        )
        return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        return False
