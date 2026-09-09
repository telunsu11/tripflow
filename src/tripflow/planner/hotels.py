"""住宿候选：高德 POI 级搜索（按每日活动区域锚点），只做信息展示，不含预订。"""

from __future__ import annotations

from ..models import DayPlan, HotelPick
from ..providers.amap import AmapClient
from ..util import parse_loc

HOTEL_TYPECODE_PREFIX = ("10",)  # 高德住宿服务大类
HOTEL_KEYWORDS_IN_TYPE = ("酒店", "宾馆", "民宿", "旅馆", "公寓")


def _is_hotel(place) -> bool:
    tc = place.typecode or ""
    if tc.startswith(HOTEL_TYPECODE_PREFIX):
        return True
    return any(k in (place.type or "") for k in HOTEL_KEYWORDS_IN_TYPE)


def anchor_of_days(days: list[DayPlan]) -> tuple[str, str] | None:
    """取该城市已排景点的质心作为住宿锚点（吃喝玩乐的中心住得最顺）。"""
    locs = [v.poi.location for d in days for v in d.items if v.poi.location]
    if not locs:
        return None
    xs, ys = zip(*(parse_loc(loc) for loc in locs), strict=True)
    return f"{sum(xs) / len(xs):.6f},{sum(ys) / len(ys):.6f}", days[0].city


def search_hotels(
    amap: AmapClient, city: str, anchor_location: str, *, limit: int = 3, radius: int = 3000
) -> list[HotelPick]:
    places = amap.place_around(anchor_location, "酒店", radius=radius, limit=limit * 3)
    picks: list[HotelPick] = []
    for p in places:
        if not _is_hotel(p) or not p.location:
            continue
        rating = cost = ""
        try:
            detail = amap.place_detail(p.id)
            rating, cost = detail.rating, getattr(detail, "cost", "")
        except Exception:  # noqa: BLE001, S110 - 详情失败不阻塞候选列表
            pass
        picks.append(
            HotelPick(
                city=city,
                name=p.name,
                poi_id=p.id,
                location=p.location,
                address=p.address,
                rating=rating,
                cost=cost,
                near=f"距活动中心 {radius // 1000}km 内",
            )
        )
        if len(picks) >= limit:
            break
    return picks


def collect_hotels(amap: AmapClient, days: list[DayPlan], *, limit: int = 3) -> list[HotelPick]:
    """按城市聚合行程日 → 质心锚点 → 搜候选。"""
    by_city: dict[str, list[DayPlan]] = {}
    for d in days:
        if d.city:
            by_city.setdefault(d.city, []).append(d)
    out: list[HotelPick] = []
    for city, city_days in by_city.items():
        anchor = anchor_of_days(city_days)
        if anchor is None:
            continue
        location, _ = anchor
        out.extend(search_hotels(amap, city, location, limit=limit))
    return out
