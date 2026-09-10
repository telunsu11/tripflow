"""预算核算：多段交通实价 + 分城市住宿/餐饮/门票估算（估算项明确标注）。"""

from __future__ import annotations

import math

from ..models import BudgetItem, Poi, TransitChoice
from ..util import parse_date


def hotel_per_night(destination: str) -> int:
    from .reference import hotel_prices

    prices, default = hotel_prices()
    for city, price in prices.items():
        if city in destination:
            return price
    return default


def build_budget(
    req,
    legs: list[TransitChoice | None],
    pois: list[Poi],
    nights_by_city: dict[str, int],
    stays: list | None = None,
) -> tuple[list[BudgetItem], float]:
    items: list[BudgetItem] = []
    n = req.travelers
    kids = list(getattr(req, "child_ages", []) or [])
    under6 = sum(1 for a in kids if a < 6)  # 火车免票不占座（每名成人限带1名）
    kid6plus = len(kids) - under6           # 6+ 岁按儿童优惠票（约半价）估算
    kid_note = ""
    if kids:
        kid_note = (f"；儿童{len(kids)}名：{under6}名不满6岁免票不占座、"
                    f"{kid6plus}名按半价估算（以 12306/景区规则为准）")

    transport = 0.0
    parts = []
    for leg in legs:
        if leg and leg.price_per_person:
            transport += leg.price_per_person
            parts.append(f"{leg.from_city}→{leg.to_city} {leg.seat_name} ¥{leg.price_per_person:g}")
    transport_total = transport * n + transport * 0.5 * kid6plus
    items.append(
        BudgetItem(
            category="跨城交通",
            amount=transport_total,
            kind="real",
            note=f"{n} 成人{'+' + str(kid6plus) + '名儿童半价' if kid6plus else ''}"
                 f"（{'；'.join(parts) or '未含票价'}，12306 实价{kid_note}）",
        )
    )

    rooms = math.ceil((n + len(kids)) / 2)
    hotel = 0
    hotel_notes = []
    stay_by_city = {s.city: s for s in (stays or [])}
    for city, nights in nights_by_city.items():
        if nights <= 0:
            continue
        stay = stay_by_city.get(city)
        if stay is not None and stay.nights == nights:
            per = stay.price_per_night
            basis = "高德参考价" if stay.price_basis == "amap" else "城市档次估算"
            note = f"{city} {nights}晚×¥{per}({basis})"
            if stay.price_range:
                note += f"，美团区间{stay.price_range}未计入"
            hotel_notes.append(note)
        else:
            per = hotel_per_night(city)
            hotel_notes.append(f"{city} {nights}晚×¥{per}(城市档次估算)")
        hotel += per * rooms * nights
    items.append(
        BudgetItem(
            category="住宿",
            amount=hotel,
            kind="estimate",
            note=f"{rooms} 间（{'；'.join(hotel_notes) or '无需住宿'}；参考/估算价，以实际预订为准）",
        )
    )

    from .reference import daily_costs

    food_per_day = daily_costs()["food_per_person_day"]
    food = food_per_day * (n + 0.5 * len(kids)) * req.days
    items.append(
        BudgetItem(
            category="餐饮",
            amount=food,
            kind="estimate",
            note=f"{n} 成人{'+' + str(len(kids)) + '儿童半量' if kids else ''} × {req.days} 天 × 约 ¥{food_per_day}/人/天（估算）",
        )
    )

    entry_units = n + 0.5 * sum(1 for a in kids if a >= 6)  # 6 岁以下多数景区免票
    fees = sum(p.entry_fee_estimate for p in pois) * entry_units
    known = [p.name for p in pois if p.entry_fee_estimate]
    items.append(
        BudgetItem(
            category="景点门票",
            amount=fees,
            kind="estimate",
            note=f"按类型估算{('（' + '、'.join(known[:4]) + '…）') if known else ''}"
                 f"{'，儿童6岁以下免票/6岁以上半价估算' if kids else ''}，以现场为准",
        )
    )

    return items, sum(i.amount for i in items)


def nights_by_city(blocks) -> dict[str, int]:
    """同日换乘语义：在城 k 过夜的夜数 = end_k - start_k。"""
    out: dict[str, int] = {}
    for b in blocks:
        out[b.city] = (parse_date(b.end_date) - parse_date(b.start_date)).days
    return out
