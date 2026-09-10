"""局部改签：锁定车次与住宿，只替换景点并重排当日及后续行程。

与全量 plan 的区别：不查 12306、不问 LLM、不动酒店——确定性单步重放，
已购/已选车次天然保留（"锁定车次"即默认行为）。
"""

from __future__ import annotations

import asyncio
import time

from ..config import Settings
from ..models import CityBlock, Itinerary, Poi, TransitChoice
from ..planner.budget import build_budget, nights_by_city
from ..planner.pois import entry_fee_for, is_attraction, stay_minutes_for
from ..planner.schedule import build_days, make_commute_fn
from ..planner.validate import validate_all
from ..providers.amap import AmapClient, clean_invisible


# ---------- 单点 POI 校验（与 plan 同规则，无 LLM） ----------
def validate_single_poi(amap: AmapClient, city: str, name: str) -> Poi:
    from ..llm import LLMError

    places = amap.place_text(name, city=city, limit=3)
    picked = next((p for p in places if p.name == name and is_attraction(p)), None) or next(
        (p for p in places if is_attraction(p)), None
    )
    if picked is None:
        raise LLMError(f"「{name}」在 {city} 未匹配到景点类 POI，未做任何修改")
    try:
        detail = amap.place_detail(picked.id)
        location = detail.location
        opentime = clean_invisible(detail.opentime)
        rating = detail.rating
    except Exception:  # noqa: BLE001 - 详情失败用搜索结果兜底
        location, opentime, rating = picked.location, "", ""
    return Poi(
        name=picked.name, poi_id=picked.id, location=location,
        typecode=picked.typecode, type=picked.type,
        opentime=opentime, rating=rating,
        reason="（行程单调整时新增）",
        stay_minutes=stay_minutes_for(picked), core=False,
        entry_fee_estimate=entry_fee_for(picked),
    )


# ---------- 行程单 → 城市块重建（同日换乘日归属两城） ----------
def blocks_from_days(days, legs) -> list[CityBlock]:
    """按连续同城日序重建 CityBlock；到/离段从既有 legs 按日期与城市匹配。"""
    runs: list[tuple[str, list[str]]] = []
    for day in days:
        if not day.city:
            continue
        if runs and runs[-1][0] == day.city:
            runs[-1][1].append(day.date)
        else:
            runs.append((day.city, [day.date]))

    def _leg_for(city: str, date: str, direction: str) -> TransitChoice | None:
        for leg in legs:
            if not isinstance(leg, TransitChoice):
                continue
            if direction == "arrive" and leg.date == date and leg.to_city == city:
                return leg
            if direction == "depart" and leg.date == date and leg.from_city == city:
                return leg
        return None

    blocks = []
    for i, (city, dates) in enumerate(runs):
        start, end = min(dates), max(dates)
        arrive = _leg_for(city, start, "arrive")
        # 到达段若不在 start 日（如首城到达日=出发日必在），也可为 None
        depart = _leg_for(city, end, "depart")
        if i == len(runs) - 1 and depart is None:
            depart = next(
                (leg for leg in legs if isinstance(leg, TransitChoice)
                 and leg.date == end), None
            )
        blocks.append(CityBlock(city=city, start_date=start, end_date=end,
                                arrive_leg=arrive, depart_leg=depart))
    return blocks


# ---------- 差异摘要 ----------
def diff_days(old_days, new_days) -> list[str]:
    def sketch(days):
        out = {}
        for d in days:
            if d.items:
                out[d.date] = f"{d.city}：" + " → ".join(v.poi.name for v in d.items)
        return out

    a, b = sketch(old_days), sketch(new_days)
    changes = []
    for date in sorted(set(a) | set(b)):
        if a.get(date) != b.get(date):
            changes.append(f"{date}\n    旧: {a.get(date, '（无）')}\n    新: {b.get(date, '（无）')}")
    return changes


# ---------- 主入口 ----------
def run_replan(
    itinerary: Itinerary,
    settings: Settings,
    *,
    remove_pois: list[str] | None = None,
    add_pois: list[tuple[str, str]] | None = None,  # (city, name)
    regenerate_map: bool = True,
) -> tuple[Itinerary, list[str]]:
    """返回 (新行程单, 变更说明)。失败抛异常且不改动原对象。"""
    from ..deliver.amap_map import generate_map_uri
    from ..planner.pipeline import write_outputs  # noqa: F401 - 由 CLI 落盘

    remove_pois = [r.strip() for r in (remove_pois or []) if r.strip()]
    add_pois = add_pois or []

    with AmapClient(settings.amap_api_key) as amap:
        # 1) 应用编辑：收集已排 POI（去重），移除/新增
        pois_by_city: dict[str, list[Poi]] = {}
        for day in itinerary.days:
            for v in day.items:
                pool = pois_by_city.setdefault(day.city or "", [])
                if all(p.poi_id != v.poi.poi_id for p in pool):
                    pool.append(v.poi.model_copy(deep=True))
        removed = []
        for name in remove_pois:
            hit = False
            for pool in pois_by_city.values():
                match = [p for p in pool if name in p.name or p.name in name]
                if match:
                    pool.remove(match[0])
                    removed.append(match[0].name)
                    hit = True
                    break
            if not hit:
                raise ValueError(f"未找到要移除的景点「{name}」（行程内已排景点中无此名称）")
        added = []
        for city, name in add_pois:
            poi = validate_single_poi(amap, city, name)
            pois_by_city.setdefault(city, []).append(poi)
            added.append(f"{city}·{poi.name}")

        # 2) 重建城市块与编排（锚点=既有住宿，车次不动）
        blocks = blocks_from_days(itinerary.days, itinerary.legs)
        weather_by_city: dict[str, dict[str, str]] = {}
        for day in itinerary.days:
            if day.city:
                weather_by_city.setdefault(day.city, {})[day.date] = day.weather
        centers = {city: amap.geo(city).location
                   for city in {b.city for b in blocks}}
        commute_fns = {city: make_commute_fn(amap, city) for city in centers}
        anchors = {s.city: (s.hotel.location, s.hotel.name)
                   for s in itinerary.stays} or None
        notes: list[str] = []
        new_days, dropped = build_days(
            blocks, pois_by_city, centers, weather_by_city, commute_fns, notes, anchors
        )

        # 3) 预算与可行性（车次/住宿沿用原值）
        scheduled = [v.poi for d in new_days for v in d.items]
        stays = itinerary.stays
        budget_items, total = build_budget(
            itinerary.request, itinerary.legs, scheduled,
            nights_by_city(blocks), stays=stays,
        )
        feasibility = validate_all(
            itinerary.request, itinerary.legs, new_days, dropped,
            total, [], notes,
        )

        # 4) 地图重生成（点位变了；失败沿用旧链接）
        map_uri = itinerary.map_uri
        if regenerate_map:
            try:
                map_uri = asyncio.run(
                    asyncio.wait_for(
                        generate_map_uri(
                            settings.amap_mcp_endpoint,
                            new_days,
                            hotel_by_city={s.hotel.city: s.hotel for s in stays} or None,
                        ),
                        timeout=45,
                    )
                )
            except Exception:  # noqa: BLE001 - 地图失败沿用旧链接
                feasibility.notes.append("地图重生成失败，沿用原链接")

    new_it = itinerary.model_copy(deep=True)
    new_it.days = new_days
    new_it.budget = budget_items
    new_it.total_cost = total
    new_it.feasibility = feasibility
    new_it.map_uri = map_uri
    new_it.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    new_it.feasibility.notes.append("经 replan 局部调整（车次与住宿保持不变）")

    changes = []
    if removed:
        changes.append(f"移除：{'、'.join(removed)}")
    if added:
        changes.append(f"新增：{'、'.join(added)}")
    if dropped:
        changes.append(f"容量不足未排入：{'、'.join(dropped)}")
    changes.extend(diff_days(itinerary.days, new_days))
    return new_it, changes
