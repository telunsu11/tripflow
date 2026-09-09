"""住宿候选：高德 POI 级搜索（按每日活动区域锚点），只做信息展示，不含预订。"""

from __future__ import annotations

import re

from ..models import CityBlock, DayPlan, HotelPick, HotelStay
from ..providers.amap import AmapClient
from ..util import loc_dist, parse_loc

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
            detail_city = (getattr(detail, "city", "") or "").replace("市", "")
            if detail_city and detail_city not in (city + p.name):
                continue  # 口岸/边界区域的邻城 POI（如澳门酒店混入珠海）剔除
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


# 高德 cost 字段可能是 "¥288" / "288" / "200-400" 等形态，取首个整数作每间夜参考价
_COST_PAT = re.compile(r"(\d{2,5})")


def parse_cost(cost: str) -> int | None:
    m = _COST_PAT.search(str(cost or ""))
    return int(m.group(1)) if m else None


def pick_stays(
    hotels: list[HotelPick],
    days: list[DayPlan],
    blocks: list[CityBlock],
) -> list[HotelStay]:
    """确定性选店（无 LLM）：评分降序 → 距本城活动质心升序；价格依据分级。

    - 高德 cost 有值 → price_basis=amap（参考价）
    - 否则 → 城市档次估算（budget.HOTEL_PER_NIGHT）
    - 备选取同城排名 2-3 名
    """
    from .budget import hotel_per_night

    by_city_days: dict[str, list[DayPlan]] = {}
    for d in days:
        if d.city:
            by_city_days.setdefault(d.city, []).append(d)
    stays: list[HotelStay] = []
    for block in blocks:
        city_days = by_city_days.get(block.city, [])
        anchor = anchor_of_days(city_days)
        center = anchor[0] if anchor else "0,0"
        candidates = [h for h in hotels if h.city == block.city and h.location]

        def rank(h: HotelPick, _center: str = center) -> tuple:
            rating = (
                float(h.rating)
                if h.rating and re.fullmatch(r"\d+(\.\d+)?", h.rating)
                else 0.0
            )
            return (-rating, loc_dist(_center, h.location))

        ordered = sorted(candidates, key=rank)
        if not ordered:
            continue
        chosen = ordered[0]
        cost = parse_cost(chosen.cost)
        stays.append(HotelStay(
            city=block.city,
            hotel=chosen,
            check_in=block.start_date,
            check_out=block.end_date,  # 同日换乘：end 当天晨离开（末城即返程日）
            nights=parse_date_nights(block),
            price_basis="amap" if cost else "estimate",
            price_per_night=cost or hotel_per_night(block.city),
            alternatives=[h.name for h in ordered[1:3]],
        ))
    return stays


def parse_date_nights(block: CityBlock) -> int:
    """同日换乘语义：在城过夜数 = end - start。"""
    from ..util import parse_date

    return (parse_date(block.end_date) - parse_date(block.start_date)).days
