"""高德 provider·通道一：Web 服务 REST 直连（geo / 天气 / POI / 公交规划）。

高频查询走这条通道（快、无子进程开销）；行程地图 schema_personal_map
走通道二（高德官方云端 MCP，见 cli.doctor 的 amap-mcp 检查与 M1 的 deliver 层）。
两通道共用同一个「Web服务」Key。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

import httpx


class AmapError(RuntimeError):
    pass


@dataclass
class GeoResult:
    location: str
    adcode: str
    city: str
    level: str


@dataclass
class Forecast:
    date: str
    week: str
    dayweather: str
    nightweather: str
    daytemp: str
    nighttemp: str


@dataclass
class Place:
    id: str
    name: str
    location: str
    address: str = ""
    typecode: str = ""
    type: str = ""


@dataclass
class PlaceDetail:
    id: str
    name: str
    location: str
    address: str = ""
    alias: str = ""
    opentime: str = ""
    rating: str = ""
    type: str = ""
    city: str = ""


@dataclass
class TransitPlan:
    duration_sec: int
    walk_distance_m: int
    lines: list[str] = field(default_factory=list)


class AmapClient:
    V3 = "https://restapi.amap.com/v3"
    V5 = "https://restapi.amap.com/v5"
    # 高德个人开发者 QPS 限流码（超限即稍候重试，而非把整个校验链吞掉）
    QPS_RETRY_CODES = frozenset({"10019", "10021", "20003"})

    def __init__(
        self,
        key: str,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 0.35,
    ) -> None:
        if not key:
            raise AmapError("未配置 AMAP_API_KEY")
        self._key = key
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._adcode_cache: dict[str, str] = {}
        # 个人 Key QPS≈3 → 默认 0.35s 请求间隔；企业 Key 可传 0 关闭
        self._min_interval = min_interval
        self._last_request_ts = 0.0

    # ---- 生命周期 ----
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 底层 ----
    def _get(self, url: str, params: dict[str, str]) -> dict:
        import time as _time

        tries = 0
        while True:
            # 客户端限速：避免 POI 校验阶段连发请求触发 QPS 限流
            wait = self._min_interval - (_time.monotonic() - self._last_request_ts)
            if wait > 0:
                _time.sleep(wait)
            self._last_request_ts = _time.monotonic()
            try:
                resp = self._http.get(url, params={**params, "key": self._key})
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                raise AmapError(f"请求高德失败: {exc}") from exc
            if str(data.get("infocode", "")) in self.QPS_RETRY_CODES and tries < 3:
                tries += 1
                _time.sleep(0.4 * tries)  # 退避后重试
                continue
            return data

    @staticmethod
    def _ensure_v3(data: dict, what: str) -> None:
        if data.get("status") != "1" or data.get("infocode") not in (None, "10000"):
            raise AmapError(f"{what}失败: [{data.get('infocode', '?')}] {data.get('info', '?')}")

    @staticmethod
    def _ensure_v5(data: dict, what: str) -> None:
        if str(data.get("status")) != "1":
            raise AmapError(f"{what}失败: [{data.get('infocode', '?')}] {data.get('info', '?')}")

    # ---- 地理编码 / 天气 ----
    def geo(self, address: str, city: str | None = None) -> GeoResult:
        params = {"address": address}
        if city:
            params["city"] = city
        data = self._get(f"{self.V3}/geocode/geo", params)
        self._ensure_v3(data, "地理编码")
        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise AmapError(f"地理编码无结果: {address}")
        g = geocodes[0]
        return GeoResult(
            location=g.get("location", ""),
            adcode=str(g.get("adcode", "")),
            city=str(g.get("city", "") or ""),
            level=str(g.get("level", "")),
        )

    def adcode(self, city: str) -> str:
        if city.isdigit():
            return city
        if city not in self._adcode_cache:
            self._adcode_cache[city] = self.geo(city).adcode
        return self._adcode_cache[city]

    def weather(self, city: str) -> list[Forecast]:
        """逐日天气预报（注意：高德仅提供 ~4 天窗口）。"""
        data = self._get(
            f"{self.V3}/weather/weatherInfo",
            {"city": self.adcode(city), "extensions": "all"},
        )
        self._ensure_v3(data, "天气查询")
        forecasts = (data.get("forecasts") or [{}])[0].get("casts") or []
        return [
            Forecast(
                date=c.get("date", ""),
                week=str(c.get("week", "")),
                dayweather=c.get("dayweather", ""),
                nightweather=c.get("nightweather", ""),
                daytemp=str(c.get("daytemp", "")),
                nighttemp=str(c.get("nighttemp", "")),
            )
            for c in forecasts
        ]

    # ---- POI ----
    def place_text(self, keywords: str, city: str | None = None, *, limit: int = 10) -> list[Place]:
        params: dict[str, str] = {"keywords": keywords, "page_size": str(limit)}
        if city:
            params["region"] = city
            params["city_limit"] = "true"
        data = self._get(f"{self.V5}/place/text", params)
        self._ensure_v5(data, "POI 搜索")
        return [self._parse_place(p) for p in (data.get("pois") or [])]

    def place_around(
        self, location: str, keywords: str, *, radius: int = 3000, limit: int = 8
    ) -> list[Place]:
        """周边搜索（坐标 lng,lat）。"""
        data = self._get(
            f"{self.V5}/place/around",
            {
                "location": location,
                "keywords": keywords,
                "radius": str(radius),
                "page_size": str(limit),
            },
        )
        self._ensure_v5(data, "周边搜索")
        return [self._parse_place(p) for p in (data.get("pois") or [])]

    @staticmethod
    def _parse_place(p: dict) -> Place:
        return Place(
            id=p.get("id", ""),
            name=p.get("name", ""),
            location=p.get("location", ""),
            address=p.get("address", "") or "",
            typecode=str(p.get("typecode", "") or ""),
            type=str(p.get("type", "") or ""),
        )

    def place_detail(self, poi_id: str) -> PlaceDetail:
        data = self._get(f"{self.V5}/place/detail", {"id": poi_id, "show_fields": "business"})
        self._ensure_v5(data, "POI 详情")
        pois = data.get("pois") or []
        if not pois:
            raise AmapError(f"POI 详情无结果: {poi_id}")
        p = pois[0]
        biz = p.get("business") or {}
        # v5 实测字段：opentime_week / opentime_today / rating / alias / keytag / rectag
        return PlaceDetail(
            id=p.get("id", ""),
            name=p.get("name", ""),
            location=p.get("location", ""),
            address=p.get("address", "") or "",
            alias=str(biz.get("alias") or p.get("alias") or ""),
            opentime=str(
                biz.get("opentime_week")
                or biz.get("opentime_today")
                or biz.get("opentime2")
                or biz.get("opentime")
                or ""
            ),
            rating=str(biz.get("rating", "") or ""),
            type=str(p.get("type", "") or ""),
            city=str(p.get("city", "") or ""),
        )

    # ---- 市内交通 ----
    def walking(self, origin: str, destination: str) -> tuple[int, int]:
        """步行规划 → (分钟, 米)。"""
        data = self._get(
            f"{self.V3}/direction/walking", {"origin": origin, "destination": destination}
        )
        self._ensure_v3(data, "步行规划")
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            raise AmapError("步行规划无结果")
        p = paths[0]
        return round(float(p.get("duration", 0)) / 60), int(float(p.get("distance", 0)))

    def transit(
        self, origin: str, destination: str, city: str, cityd: str | None = None
    ) -> list[TransitPlan]:
        """跨城时必须传 cityd（起点城市）。坐标格式：lng,lat。"""
        params = {"origin": origin, "destination": destination, "city": city}
        if cityd:
            params["cityd"] = cityd
        data = self._get(f"{self.V3}/direction/transit/integrated", params)
        self._ensure_v3(data, "公交规划")
        plans: list[TransitPlan] = []
        for t in (data.get("route") or {}).get("transits") or []:
            lines = [
                line.get("name", "")
                for seg in (t.get("segments") or [])
                for line in ((seg.get("bus") or {}).get("buslines") or [])
            ]
            plans.append(
                TransitPlan(
                    duration_sec=int(float(t.get("duration", 0))),
                    walk_distance_m=int(float(t.get("walking_distance", 0))),
                    lines=lines,
                )
            )
        return plans
