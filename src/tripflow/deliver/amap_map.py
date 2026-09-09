"""行程地图：经高德云端 MCP 的 maps_schema_personal_map 生成可分享链接。"""

from __future__ import annotations

from ..models import DayPlan
from ..util import fmt_date, parse_loc


def _line_list(
    days: list[DayPlan],
    extra_head: dict | None,
    extra_tail: dict | None,
    hotel_by_city: dict | None = None,
) -> list[dict]:
    lines = []
    for i, day in enumerate(days, 1):
        points = []
        hotel = (hotel_by_city or {}).get(day.city)
        if hotel is not None:
            try:
                lng, lat = parse_loc(hotel.location)
                points.append({"name": f"🏨{hotel.name}", "lon": lng, "lat": lat,
                               "poiId": hotel.poi_id})
            except ValueError:
                pass
        if i == 1 and extra_head:
            points.append(extra_head)
        for v in day.items:
            try:
                lng, lat = parse_loc(v.poi.location)
            except ValueError:
                continue
            points.append({"name": v.poi.name, "lon": lng, "lat": lat, "poiId": v.poi.poi_id})
        if i == len(days) and extra_tail:
            points.append(extra_tail)
        if points:
            lines.append({"title": f"Day{i} {fmt_date(day.date)}", "pointInfoList": points})
    return lines


async def generate_map_uri(
    amap_mcp_endpoint: str,
    days: list[DayPlan],
    *,
    station_poi: dict | None = None,
    return_station_poi: dict | None = None,
    hotel_by_city: dict | None = None,
    org_name: str = "tripflow 行程单",
) -> str:
    """连接高德云端 MCP 生成个人地图。station_poi 形如 {name, lon, lat, poiId}。"""
    from mcp import ClientSession

    try:
        from mcp.client.streamable_http import streamable_http_client  # mcp >= 2
    except ImportError:  # pragma: no cover
        from mcp.client.streamable_http import (  # type: ignore[attr-defined]
            streamablehttp_client as streamable_http_client,
        )

    line_list = _line_list(days, station_poi, return_station_poi, hotel_by_city)
    if not line_list:
        raise RuntimeError("行程中没有任何可定位的点位，无法生成地图")

    async with streamable_http_client(amap_mcp_endpoint) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "maps_schema_personal_map",
                {"orgName": org_name, "lineList": line_list},
            )
            text = "\n".join(getattr(c, "text", "") for c in getattr(result, "content", [])).strip()
            if getattr(result, "isError", False) or not text:
                raise RuntimeError(f"maps_schema_personal_map 失败: {text[:200]}")
            return text.splitlines()[0].strip()
