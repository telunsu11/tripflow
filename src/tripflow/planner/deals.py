"""美团优惠核查：按城市组装查询、提取参考价提示。原文引用，不改预算数字。"""

from __future__ import annotations

import re

from ..models import DealSection, Itinerary
from ..providers.meituan import MeituanClient
from ..util import now_ts

# 从自然语言里提取「成人票N元(起)」式参考价；提取失败返回空（预算仍按估算）
_PRICE_PAT = re.compile(r"成人票(?:价格)?[为约]?\s*(\d+)(?:\.\d+)?\s*元(起)?")


def build_ticket_query(itinerary: Itinerary) -> list[tuple[str, str]]:
    """按城市返回 (city, 查询语句)：每个城市的已排景点门票一次查完。"""
    by_city: dict[str, list[str]] = {}
    for day in itinerary.days:
        for v in day.items:
            if day.city and v.poi.name not in by_city.setdefault(day.city, []):
                by_city[day.city].append(v.poi.name)
    out = []
    for city, names in by_city.items():
        out.append((city, f"{city}{'、'.join(names)}的门票价格和优惠政策"))
    return out


def build_hotel_query(itinerary: Itinerary) -> list[tuple[str, str]]:
    cities = []
    for day in itinerary.days:
        if day.city and day.city not in cities:
            cities.append(day.city)
    return [(c, f"{c}住宿酒店优惠和推荐") for c in cities]


def extract_price_hint(content: str) -> str:
    m = _PRICE_PAT.search(content)
    if not m:
        return ""
    qi = "起" if m.group(2) else ""
    return f"成人 ¥{m.group(1)}{qi}（美团参考）"


def run_deals(
    client: MeituanClient, itinerary: Itinerary, *, with_hotels: bool = False
) -> tuple[Itinerary, list[str]]:
    """逐城市查询并写入 itinerary.deals；返回 (行程单, 失败说明)。"""
    failures: list[str] = []
    queries = build_ticket_query(itinerary)
    if with_hotels:
        queries += build_hotel_query(itinerary)
    itinerary.deals = []  # 重复执行时覆盖旧结果
    for city, query in queries:
        topic = "住宿" if "住宿" in query else "门票"
        try:
            content = client.query(query, city=city, origin_query=query)
        except Exception as exc:  # noqa: BLE001 - 单城失败不阻塞整体
            failures.append(f"{city}{topic}: {exc}")
            continue
        itinerary.deals.append(
            DealSection(
                city=city,
                topic=topic,
                query=query,
                content=content,
                price_hint=extract_price_hint(content),
                checked_at=now_ts(),
            )
        )
    return itinerary, failures
