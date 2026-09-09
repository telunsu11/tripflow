"""跨城交通：多目的地逐段选班（直达优先、中转兜底），含日期分派与换乘规则。"""

from __future__ import annotations

from ..models import CityBlock, TransitChoice, TransitOutcome
from ..providers.rail import RailSession, SeatPrice, TrainTicket, TransferPlan
from ..util import add_days, fmt_lishi, hm2min, lishi_minutes, now_ts, wait_minutes

SEAT_PRIORITY = ["二等座", "一等座", "商务座"]  # 无座不作为推荐席别（长途不可执行）
SHOW_PRIORITY = SEAT_PRIORITY + ["无座"]  # 展示参考价时可落到无座
# 换乘缓冲规则（来自实测数据结构：same_train=同车换乘坐原位；same_station=同站换乘）
SAME_STATION_BUFFER_MIN = 30


# ---------- 城市日期分派 ----------
def allocate_days(
    cities: list[str], depart_date: str, return_date: str
) -> list[tuple[str, str, str]]:
    """把行程日期均分给各城市（前面城市优先多吃余数天）；同日换乘：上一城 end=下一城 start。
    返回 [(city, start, end)]。要求天数 ≥ 城市数。
    """
    from ..util import date_range

    total = len(date_range(depart_date, return_date))
    if total < len(cities):
        raise ValueError(f"{len(cities)} 个城市至少需要 {len(cities)} 天（当前 {total} 天）")
    # 同日换乘下边界日期被相邻城市共享：跨度总和 = 天数 + 城市数 - 1
    span_total = total + len(cities) - 1
    base, rem = divmod(span_total, len(cities))
    spans = [base + (1 if k < rem else 0) for k in range(len(cities))]
    out: list[tuple[str, str, str]] = []
    start = depart_date
    for city, span in zip(cities, spans, strict=True):
        end = add_days(start, span - 1)
        out.append((city, start, end))
        start = end  # 同日换乘：end 当天上午离开、午后到达下一城
    return out



def prefer_hub_stations(
    trains: list[TrainTicket], frm_city: str, to_city: str
) -> list[TrainTicket]:
    """出发/到达优先落在该城市的枢纽站；无匹配时回退。"""
    keep = trains
    for city, attr in ((frm_city, "from_station"), (to_city, "to_station")):
        from .reference import hub_stations

        hubs = hub_stations().get(city)
        if not hubs:
            continue
        hub_trains = [t for t in keep if getattr(t, attr) in hubs]
        keep = hub_trains or keep
    return keep


def filter_city_stations(
    trains: list[TrainTicket], frm_city: str, to_city: str
) -> list[TrainTicket]:
    """只保留出发站/到达站站名归属对应城市的车次（用城市码查询会带出下辖远郊站，
    如 苏州→盛泽、上海→金山北），无匹配时回退原列表。"""
    keep = [t for t in trains if frm_city in t.from_station and to_city in t.to_station]
    return keep or trains


# ---------- 选座 ----------
def best_seat(t: TrainTicket, travelers: int) -> SeatPrice | None:
    """按二等→一等→商务优先，返回余量足够的席别。"""
    for name in SEAT_PRIORITY:
        s = t.seat(name)
        if s and s.available and (s.count is None or s.count >= travelers):
            return s
    return None


def show_seat(t: TrainTicket, travelers: int) -> tuple[str, float | None, bool]:
    seat = best_seat(t, travelers)
    if seat:
        return seat.name, seat.price, True
    # 余量不足时仍展示参考价
    for name in SHOW_PRIORITY:
        s = t.seat(name)
        if s and s.price:
            return name, s.price, False
    return "", None, False


def direct_choice(t: TrainTicket, travelers: int, date: str) -> TransitChoice:
    seat_name, price, ok = show_seat(t, travelers)
    flag = "" if ok else "⚠️ 余票不足"
    if price:
        summary = (
            f"{t.train_code} {t.from_station} {t.start_time} → {t.to_station} {t.arrive_time}"
            f"（历时 {fmt_lishi(t.lishi)}，{seat_name} ¥{price:g}{flag}）"
        )
    else:
        summary = (
            f"{t.train_code} {t.from_station} {t.start_time} → {t.to_station} {t.arrive_time}"
            f"（无余票信息）"
        )
    return TransitChoice(
        kind="direct",
        date=date,
        code=t.train_code,
        from_station=t.from_station,
        to_station=t.to_station,
        depart_time=t.start_time,
        arrive_time=t.arrive_time,
        summary=summary,
        seat_name=seat_name,
        price_per_person=price,
        seats_ok=ok,
        checked_at=now_ts(),
    )


def interline_feasible(p: TransferPlan) -> bool:
    """同车/同站换乘的规则判定（跨站换乘需 amap 实测，见 cross_station_ok）。"""
    if p.same_train:
        return True
    if p.same_station:
        return wait_minutes(p.wait_time) >= SAME_STATION_BUFFER_MIN
    return False


async def cross_station_ok(p: TransferPlan, amap) -> bool:
    """跨站换乘：用高德算两站间真实通勤，等待时间 ≥ 通勤 + 30 分钟缓冲才可行。"""
    if not p.legs or len(p.legs) < 2 or amap is None:
        return False
    wait = wait_minutes(p.wait_time)
    if wait <= 0:
        return False
    try:
        station_a, station_b = p.legs[0].to_station, p.legs[1].from_station
        mid = amap.geo(p.middle_station)
        if not mid.city:
            return False
        ga = amap.geo(station_a, city=mid.city)
        gb = amap.geo(station_b, city=mid.city)
        plans = amap.transit(ga.location, gb.location, city=mid.city)
        if not plans:
            return False
        need = round(plans[0].duration_sec / 60) + SAME_STATION_BUFFER_MIN
        return wait >= need
    except Exception:  # noqa: BLE001 - 查询失败按不可行处理，不阻塞选班
        return False


def interline_choice(p: TransferPlan, travelers: int, date: str) -> TransitChoice | None:
    if not p.legs or len(p.legs) < 2:
        return None
    seats = [best_seat(leg, travelers) for leg in p.legs]
    ok = all(seats)
    prices = [s.price for s in seats if s]
    total = sum(prices) if len(prices) == len(p.legs) else None
    transfer = (
        "同车换乘"
        if p.same_train
        else (f"同站换乘 等待{p.wait_time}" if p.same_station else "跨站换乘")
    )
    codes = "+".join(leg.train_code for leg in p.legs)
    price_text = f"，二等座合计约 ¥{total:g}" if total else ""
    flag = "" if ok else "⚠️ 余票不足"
    return TransitChoice(
        kind="interline",
        date=date,
        code=codes,
        from_station=p.from_station,
        to_station=p.to_station,
        depart_time=p.start_time,
        arrive_time=p.arrive_time,
        summary=(
            f"{codes} {p.from_station} {p.start_time} → {p.to_station} {p.arrive_time}"
            f"（{p.middle_station}{transfer}，总历时 {fmt_lishi(p.lishi)}{price_text}{flag}）"
        ),
        price_per_person=total,
        seats_ok=ok,
        transfer_note=f"{p.middle_station} {transfer}",
        checked_at=now_ts(),
    )


# ---------- 选班 ----------
def pick_go(trains: list[TrainTicket], travelers: int, not_before: str = "") -> TrainTicket | None:
    """去程：优先 07:30–13:00 出发、22:00 前到达，到得越早越好。

    not_before（HH:MM）：用户指定出发时段下限（如"下午出发"=13:00），
    时段窗口随之平移，无合适班次时才放宽。
    """
    if not trains:
        return None
    pool = [t for t in trains if best_seat(t, travelers)] or trains
    floor = hm2min(not_before) if not_before else hm2min("07:30")
    upper = max(hm2min("13:00"), floor + 4 * 60)
    window = [t for t in pool if t.arrive_time <= "22:00" and floor <= hm2min(t.start_time) <= upper]
    if window:
        return min(window, key=lambda t: hm2min(t.arrive_time))
    after_floor = [t for t in pool if t.arrive_time <= "22:00" and hm2min(t.start_time) >= floor]
    if after_floor:
        return min(after_floor, key=lambda t: hm2min(t.arrive_time))
    before22 = [t for t in pool if t.arrive_time <= "22:00"]
    if before22:
        return min(before22, key=lambda t: hm2min(t.arrive_time))
    return min(pool, key=lambda t: lishi_minutes(t.lishi))


def pick_back(trains: list[TrainTicket], travelers: int) -> TrainTicket | None:
    """返程：优先 13:00–19:00 出发（保住最后一天白天），历时最短。"""
    if not trains:
        return None
    pool = [t for t in trains if best_seat(t, travelers)] or trains
    afternoon = [t for t in pool if "13:00" <= t.start_time <= "19:00"]
    if afternoon:
        return min(afternoon, key=lambda t: lishi_minutes(t.lishi))
    return min(pool, key=lambda t: lishi_minutes(t.lishi))


def pick_intercity(trains: list[TrainTicket], travelers: int) -> TrainTicket | None:
    """城际段：优先 07:30–13:00 出发（保住到达日游玩时间），历时最短。"""
    if not trains:
        return None
    pool = [t for t in trains if best_seat(t, travelers)] or trains
    morning = [t for t in pool if "07:30" <= t.start_time <= "13:00"]
    if morning:
        return min(morning, key=lambda t: lishi_minutes(t.lishi))
    return min(pool, key=lambda t: lishi_minutes(t.lishi))


PICKERS = {"go": pick_go, "intercity": pick_intercity, "back": pick_back}


def comparison_pool(
    trains: list[TrainTicket], depart_after: str = ""
) -> list[TrainTicket]:
    """对比表候选：应用用户出发时段下限（无约束则全量），按到达时间排序。"""
    pool = trains
    if depart_after:
        in_window = [t for t in trains if hm2min(t.start_time) >= hm2min(depart_after)]
        pool = in_window or pool
    return sorted(pool, key=lambda t: hm2min(t.arrive_time))


# ---------- 单段查询 ----------
async def query_leg(
    rail: RailSession,
    date: str,
    frm: str,
    to: str,
    kind: str,
    travelers: int,
    amap=None,
    not_before: str = "",
) -> TransitChoice | None:
    raw = await rail.tickets(date, frm, to, filter_flags="GD", sort="startTime")
    trains = prefer_hub_stations(filter_city_stations(raw, frm, to), frm, to)
    pick = PICKERS[kind]
    if kind == "go" and not_before:
        chosen = pick_go(trains, travelers, not_before=not_before) if trains else None
    else:
        chosen = pick(trains, travelers) if trains else None
    choice: TransitChoice | None = None
    if chosen is not None:
        choice = direct_choice(chosen, travelers, date)
    else:
        # 直达无合适班次 → 中转兜底（同车/同站按规则；跨站用高德实测站间通勤）
        plans = await rail.interline(date, frm, to)
        feasible = [p for p in plans if interline_feasible(p)]
        for p in [x for x in plans if not interline_feasible(x) and x.legs][:3]:
            if await cross_station_ok(p, amap):
                feasible.append(p)
        if feasible:
            choice = interline_choice(
                min(feasible, key=lambda p: lishi_minutes(p.lishi)), travelers, date
            )
    if choice is not None:
        choice.from_city, choice.to_city = frm, to
    return choice


# ---------- 主入口 ----------
async def plan_transit(rail: RailSession, req, amap=None) -> TransitOutcome:
    travelers = req.travelers
    alloc = allocate_days(req.cities, req.depart_date, req.return_date)
    notes: list[str] = []

    legs: list[TransitChoice | None] = []
    # 第 1 段：出发地 → 城市 1
    legs.append(
        await query_leg(
            rail, req.depart_date, req.origin, req.cities[0], "go", travelers, amap,
            not_before=getattr(req, "depart_after", "") or "",
        )
    )
    # 中间段：城 k → 城 k+1（在城 k 的 end 当天上午出发，同日换乘）
    for k in range(1, len(req.cities)):
        date = alloc[k - 1][2]
        legs.append(
            await query_leg(
                rail, date, req.cities[k - 1], req.cities[k], "intercity", travelers, amap
            )
        )
    # 末段：最后一城 → 出发地
    legs.append(
        await query_leg(rail, req.return_date, req.cities[-1], req.origin, "back", travelers, amap)
    )

    blocks = [
        CityBlock(
            city=city, start_date=start, end_date=end, arrive_leg=legs[i], depart_leg=legs[i + 1]
        )
        for i, (city, start, end) in enumerate(alloc)
    ]
    # 最后一块的 depart_leg 即返程段（legs[-1]），上式已覆盖（i+1 == len(cities) 时）

    # 去程对比表：直达 top3（按到达时间）+ 中转 top2（按总历时）
    go_direct = await rail.tickets(
        req.depart_date, req.origin, req.cities[0], filter_flags="GD", sort="arriveTime"
    )
    rows: list[dict] = []
    same_from = [
        t for t in go_direct if not go_direct or t.from_station == go_direct[0].from_station
    ]
    for t in comparison_pool(same_from, req.depart_after)[:3]:
        seat_name, price, ok = show_seat(t, travelers)
        rows.append(
            {
                "类型": "直达",
                "车次": t.train_code,
                "日期": req.depart_date,
                "出发": f"{t.from_station} {t.start_time}",
                "到达": f"{t.to_station} {t.arrive_time}",
                "历时": fmt_lishi(t.lishi),
                "席别与价格": f"{seat_name} ¥{price:g}" if price else "—",
                "余票": "充足" if ok else "不足",
            }
        )
    plans = await rail.interline(req.depart_date, req.origin, req.cities[0], limit=4)
    feasible_plans = [p for p in plans if interline_feasible(p)]
    if req.depart_after:
        in_window = [p for p in feasible_plans if hm2min(p.start_time) >= hm2min(req.depart_after)]
        feasible_plans = in_window or feasible_plans
    for p in feasible_plans[:2]:
        c = interline_choice(p, travelers, req.depart_date)
        if c:
            rows.append(
                {
                    "类型": "中转",
                    "车次": c.code,
                    "日期": req.depart_date,
                    "出发": f"{p.from_station} {p.start_time}",
                    "到达": f"{p.to_station} {p.arrive_time}",
                    "历时": fmt_lishi(p.lishi),
                    "席别与价格": f"二等座合计 ¥{c.price_per_person:g}"
                    if c.price_per_person
                    else "—",
                    "余票": "充足" if c.seats_ok else "不足",
                }
            )

    if req.depart_after:
        notes.append(f"首段对比已按出发时段 ≥ {req.depart_after} 过滤")
    return TransitOutcome(legs=legs, blocks=blocks, comparison=rows, notes=notes)
