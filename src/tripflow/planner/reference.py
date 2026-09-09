"""参考数据加载：包内 data/*.yaml 为基线，~/.tripflow/data/ 同名文件按键覆盖/合并。

小城市用户可以自己补表（如家乡的枢纽站、本地住宿价位），无需改代码。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PACKED_DATA_DIR = Path(__file__).parent.parent / "data"


def user_data_dir() -> Path:
    return Path.home() / ".tripflow" / "data"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_reference(name: str) -> dict:
    """加载一份参考数据（yaml），叠加用户覆盖。文件缺失/损坏时回退为空。"""
    data: dict = {}
    packed = PACKED_DATA_DIR / name
    if packed.exists():
        try:
            loaded = yaml.safe_load(packed.read_text("utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError:
            data = {}
    user = user_data_dir() / name
    if user.exists():
        try:
            loaded = yaml.safe_load(user.read_text("utf-8"))
            if isinstance(loaded, dict):
                data = _deep_merge(data, loaded)
        except yaml.YAMLError:
            pass  # 用户文件损坏时静默回退基线
    return data


@lru_cache(maxsize=1)
def hub_stations() -> dict[str, frozenset[str]]:
    raw = load_reference("hub_stations.yaml")
    return {str(city): frozenset(str(s) for s in stations) for city, stations in raw.items()}


@lru_cache(maxsize=1)
def hotel_prices() -> tuple[dict[str, int], int]:
    raw = load_reference("hotel_prices.yaml")
    default = int(raw.get("_default", 300))
    prices = {
        str(city): int(price)
        for city, price in (raw.get("per_night_by_city") or {}).items()
    }
    return prices, default


@lru_cache(maxsize=1)
def daily_costs() -> dict:
    raw = load_reference("daily_costs.yaml")
    return {"food_per_person_day": int(raw.get("food_per_person_day", 150))}
