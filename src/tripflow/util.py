"""通用小工具：时间与坐标。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta


def hm2min(hhmm: str) -> int:
    """'19:35' → 1175（分钟）。非法输入返回 0。"""
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def min2hm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def lishi_minutes(lishi: str) -> int:
    """历时 '11:10' → 670 分钟。"""
    return hm2min(lishi)


def fmt_lishi(lishi: str) -> str:
    """历时展示归一化：12306 原始 '10:0' → '10:00'。"""
    m = hm2min(str(lishi))
    return f"{m // 60}:{m % 60:02d}"


def wait_minutes(text: str) -> int:
    """换乘等待 '39分钟' → 39。"""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else 0


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()  # noqa: DTZ007 - 纯日期解析，无时区语义


def add_days(date_str: str, n: int) -> str:
    return (parse_date(date_str) + timedelta(days=n)).isoformat()


def fmt_date(s: str) -> str:
    """'2026-09-12' → '9月12日 周六'。"""
    d = parse_date(s)
    return f"{d.month}月{d.day}日 周{'一二三四五六日'[d.weekday()]}"


def date_range(start: str, end: str) -> list[str]:
    """闭区间日期串列表。"""
    d0, d1 = parse_date(start), parse_date(end)
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def parse_loc(location: str) -> tuple[float, float]:
    """'104.047992,30.646168' → (lng, lat)。"""
    lng, lat = str(location).split(",")[:2]
    return float(lng), float(lat)


def loc_dist(a: str, b: str) -> float:
    """城市尺度下的粗略距离（经纬度平面距离，用于排序而非展示）。"""
    ax, ay = parse_loc(a)
    bx, by = parse_loc(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
