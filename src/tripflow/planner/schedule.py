"""逐日编排：按城市分块，块内 POI 排序 + 真实通勤（公交优先、步行兜底）。"""

from __future__ import annotations

from ..models import CityBlock, CommuteLeg, DayPlan, Poi, TransitChoice, VisitItem
from ..providers.amap import AmapClient, AmapError
from ..util import hm2min, loc_dist, min2hm

DAY_START = 9 * 60 + 30  # 09:30
DAY_END = 22 * 60  # 22:00（晚间逛街/夜景是旅行常态）
BUFFER_MIN = 15  # 两个行程点之间的缓冲

# 风格参数（仅改编排节奏，不改数据来源；均衡为主方案）
STYLE_PARAMS: dict[str, dict] = {
    "均衡": {},
    "紧凑": {"day_start": 9 * 60, "day_end": 22 * 60 + 30, "buffer": 10},
    "休闲": {"day_start": 10 * 60, "day_end": 21 * 60, "buffer": 25},
}
ARRIVAL_BUFFER = 60  # 到站后 60 分钟开始游玩
DEPART_BUFFER = 90  # 提前 90 分钟到站
MIN_WINDOW = 60  # 窗口不足 1 小时视为无效


def make_commute_fn(amap: AmapClient, city: str):
    """相邻两点通勤：公交方案优先，无公交或查询失败退化为步行。带请求级缓存。"""
    cache: dict[tuple[str, str], CommuteLeg] = {}

    def commute(a: str, b: str) -> CommuteLeg:
        if a == b:
            return CommuteLeg(from_name="", to_name="", mode="同点", minutes=0)
        key = (a, b)
        if key in cache:
            return cache[key]
        leg = None
        try:
            plans = amap.transit(a, b, city=city)
            if plans:
                p = plans[0]
                leg = CommuteLeg(
                    from_name="",
                    to_name="",
                    mode="公交",
                    minutes=max(1, round(p.duration_sec / 60)),
                    distance_m=p.walk_distance_m,
                    lines=p.lines[:2],
                )
        except AmapError:
            leg = None
        if leg is None:
            try:
                walks = amap.walking(a, b)
                leg = CommuteLeg(
                    from_name="",
                    to_name="",
                    mode="步行",
                    minutes=walks[0],
                    distance_m=walks[1],
                )
            except AmapError:
                # 兜底：按直线距离粗估（4 km/h），标注假设
                km = loc_dist(a, b) * 111 * 0.75  # 经验折算
                leg = CommuteLeg(
                    from_name="", to_name="", mode="步行(估)", minutes=max(10, round(km / 4 * 60))
                )
        cache[key] = leg
        return leg

    return commute


def order_pois(pois: list[Poi], center: str) -> list[Poi]:
    """CORE 优先，其余按从市中心出发的最近邻链排序。"""
    ordered: list[Poi] = []
    for pool in ([p for p in pois if p.core], [p for p in pois if not p.core]):
        remaining = list(pool)
        cur = center
        while remaining:
            nxt = min(remaining, key=lambda p: loc_dist(cur, p.location))
            ordered.append(nxt)
            remaining.remove(nxt)
            cur = nxt.location
    return ordered


def block_windows(block: CityBlock) -> list[tuple[str, int | None, int | None]]:
    """每天窗口（分钟）；None 表示用默认。到达日覆盖起点、离开日覆盖终点。"""
    wins = []
    for d in block.dates:
        start = end = None
        arrive = block.arrive_leg
        depart = block.depart_leg
        if isinstance(arrive, TransitChoice) and arrive.date == d:
            start = hm2min(arrive.arrive_time) + ARRIVAL_BUFFER
        if isinstance(depart, TransitChoice) and depart.date == d:
            end = hm2min(depart.depart_time) - DEPART_BUFFER
        wins.append((d, start, end))
    return wins


def _effective(start: int | None, end: int | None) -> tuple[int, int]:
    return (start if start is not None else DAY_START, end if end is not None else DAY_END)


def block_capacity(block: CityBlock) -> tuple[str, int]:
    """估算该城市块每天可游玩时长与建议 POI 提名数（供提名阶段约束数量）。"""
    parts: list[str] = []
    count = 0
    for d, s, e in block_windows(block):
        start, end = _effective(s, e)
        hours = max(0.0, (end - start) / 60)
        label = "全天" if hours >= 10 else ("无有效时间" if hours < 1 else f"约 {hours:.1f} 小时")
        parts.append(f"{d} {label}")
        if hours >= 9:
            count += 4
        elif hours >= 5:
            count += 3
        elif hours >= 2.5:
            count += 2
        elif hours >= 1:
            count += 1
    return "；".join(parts), max(1, count)


def windows_from_transit(go, back) -> tuple[int | None, int | None]:
    """（单目的地兼容）由所选班次推 Day1 开始 / 末日结束（分钟）。"""
    day1 = hm2min(go.arrive_time) + ARRIVAL_BUFFER if go else None
    last = hm2min(back.depart_time) - DEPART_BUFFER if back else None
    return day1, last


def _fill_block(
    block: CityBlock,
    pois: list[Poi],
    center: str,
    weather: dict[str, str],
    commute_fn,
    days_map: dict[str, DayPlan],
    notes_out: list[str],
    anchor: tuple[str, str] | None = None,
    style: str = "均衡",
) -> list[str]:
    """把 POI 贪心填进一个城市块，返回未排入的 POI 名。

    anchor=(location, name) 为建议住宿锚点：每日首段通勤从酒店出发；
    无锚点时保持旧行为（首点不计通勤，按住处就近假设）。"""
    ov = STYLE_PARAMS.get(style) or {}
    d_start = ov.get("day_start", DAY_START)
    d_end = ov.get("day_end", DAY_END)
    buf = ov.get("buffer", BUFFER_MIN)

    def eff(s: int | None, e: int | None) -> tuple[int, int]:
        return (s if s is not None else d_start, e if e is not None else d_end)

    wins = block_windows(block)
    valid = [(d, s, e) for (d, s, e) in wins if (eff(s, e)[1] - eff(s, e)[0]) >= MIN_WINDOW]
    for d, s, e in wins:
        if (d, s, e) not in valid:
            days_map[d].notes.append("本日不安排景点（被交通占用）")
    if anchor is not None and isinstance(block.arrive_leg, TransitChoice):
        first_day = block.dates[0] if block.dates else ""
        if first_day in days_map:
            days_map[first_day].notes.append(
                f"到达日假设先到「{anchor[1]}」寄存行李后出发（已计入到站缓冲）"
            )
    dropped: list[str] = []
    if not valid or not pois:
        return [p.name for p in pois]

    cur_idx = 0
    cur_time = eff(valid[0][1], valid[0][2])[0]
    prev: Poi | None = None

    for poi in order_pois(pois, center):
        placed = False
        while cur_idx < len(valid):
            d, s, e = valid[cur_idx]
            _, w_end = eff(s, e)
            leg = None
            if prev is not None and prev.location != poi.location:
                leg = commute_fn(prev.location, poi.location)
            elif prev is None and anchor is not None and anchor[0] != poi.location:
                leg = commute_fn(anchor[0], poi.location)
            start = cur_time + (leg.minutes if leg else 0)
            end = start + poi.stay_minutes
            if end <= w_end:
                if leg is not None:
                    leg.from_name = prev.name if prev else (anchor[1] if anchor else "")
                    leg.to_name = poi.name
                    days_map[d].legs.append(leg)
                days_map[d].items.append(VisitItem(poi=poi, start=min2hm(start), end=min2hm(end)))
                cur_time = end + buf
                prev = poi
                placed = True
                break
            cur_idx += 1
            if cur_idx < len(valid):
                cur_time = eff(valid[cur_idx][1], valid[cur_idx][2])[0]
                prev = None  # 新的一天默认从住处/市中心出发
        if not placed:
            dropped.append(poi.name)
    return dropped


def build_days(
    blocks: list[CityBlock],
    pois_by_city: dict[str, list[Poi]],
    centers: dict[str, str],
    weather_by_city: dict[str, dict[str, str]],
    commute_fns: dict[str, object],
    schedule_notes: list[str],
    anchors: dict[str, tuple[str, str]] | None = None,
    style: str = "均衡",
) -> tuple[list[DayPlan], list[str]]:
    """多城市逐块编排；anchors 为各城住宿锚点；style 见 STYLE_PARAMS。

    返回 (按日期排序的全部 DayPlan, 未排入 POI 列表)。"""
    all_days: list[DayPlan] = []
    dropped: list[str] = []
    for block in blocks:
        city = block.city
        days_map = {
            d: DayPlan(date=d, city=city, weather=weather_by_city.get(city, {}).get(d, ""))
            for d in block.dates
        }
        pois = pois_by_city.get(city, [])
        if not pois:
            schedule_notes.append(f"{city} 无校验通过的景点")
        block_dropped = _fill_block(
            block,
            pois,
            centers.get(city, "0,0"),
            weather_by_city.get(city, {}),
            commute_fns.get(city, None),
            days_map,
            schedule_notes,
            anchor=(anchors or {}).get(city),
            style=style,
        )
        dropped.extend(f"{name}（{city}）" for name in block_dropped)
        for day in days_map.values():
            if not day.items and not day.notes:
                day.notes.append("本日无安排（自由活动）")
        all_days.extend(days_map[d] for d in block.dates)

    all_days.sort(key=lambda x: x.date)
    return all_days, dropped
