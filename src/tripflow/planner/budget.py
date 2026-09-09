"""预算核算：多段交通实价 + 分城市住宿/餐饮/门票估算（估算项明确标注）。"""

from __future__ import annotations

import math

from ..models import BudgetItem, Poi, TransitChoice
from ..util import parse_date

# 城市档次 → 每间夜参考价（估算值；暂不查 OTA，后续接高德酒店 POI）
HOTEL_PER_NIGHT = {
    "北京": 450,
    "上海": 450,
    "深圳": 420,
    "广州": 380,
    "杭州": 350,
    "南京": 330,
    "苏州": 330,
    "厦门": 340,
    "武汉": 300,
    "长沙": 300,
    "西安": 290,
    "重庆": 300,
    "成都": 300,
    "昆明": 300,
    "贵阳": 280,
    "哈尔滨": 280,
    "青岛": 320,
    "大连": 310,
}
HOTEL_DEFAULT = 300
FOOD_PER_PERSON_DAY = 150


def hotel_per_night(destination: str) -> int:
    for city, price in HOTEL_PER_NIGHT.items():
        if city in destination:
            return price
    return HOTEL_DEFAULT


def build_budget(
    req,
    legs: list[TransitChoice | None],
    pois: list[Poi],
    nights_by_city: dict[str, int],
) -> tuple[list[BudgetItem], float]:
    items: list[BudgetItem] = []
    n = req.travelers

    transport = 0.0
    parts = []
    for leg in legs:
        if leg and leg.price_per_person:
            transport += leg.price_per_person
            parts.append(f"{leg.from_city}→{leg.to_city} {leg.seat_name} ¥{leg.price_per_person:g}")
    items.append(
        BudgetItem(
            category="跨城交通",
            amount=transport * n,
            kind="real",
            note=f"{n} 人（{'；'.join(parts) or '未含票价'}，12306 实价，含查询时间戳）",
        )
    )

    rooms = math.ceil(n / 2)
    hotel = 0
    hotel_notes = []
    for city, nights in nights_by_city.items():
        if nights <= 0:
            continue
        per = hotel_per_night(city)
        hotel += per * rooms * nights
        hotel_notes.append(f"{city} {nights}晚×¥{per}")
    items.append(
        BudgetItem(
            category="住宿",
            amount=hotel,
            kind="estimate",
            note=f"{rooms} 间（{'；'.join(hotel_notes) or '无需住宿'}，按城市档次的估算，未查询 OTA）",
        )
    )

    food = FOOD_PER_PERSON_DAY * n * req.days
    items.append(
        BudgetItem(
            category="餐饮",
            amount=food,
            kind="estimate",
            note=f"{n} 人 × {req.days} 天 × 约 ¥{FOOD_PER_PERSON_DAY}/人/天（估算）",
        )
    )

    fees = sum(p.entry_fee_estimate for p in pois) * n
    known = [p.name for p in pois if p.entry_fee_estimate]
    items.append(
        BudgetItem(
            category="景点门票",
            amount=fees,
            kind="estimate",
            note=f"按类型估算{('（' + '、'.join(known[:4]) + '…）') if known else ''}，以现场为准",
        )
    )

    return items, sum(i.amount for i in items)


def nights_by_city(blocks) -> dict[str, int]:
    """同日换乘语义：在城 k 过夜的夜数 = end_k - start_k。"""
    out: dict[str, int] = {}
    for b in blocks:
        out[b.city] = (parse_date(b.end_date) - parse_date(b.start_date)).days
    return out
