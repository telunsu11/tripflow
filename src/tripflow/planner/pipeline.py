"""plan 流水线：CLI 与 Web 共用的编排入口（run_plan）。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..deliver.amap_map import generate_map_uri
from ..deliver.html import render_html
from ..deliver.ical import write_ical
from ..deliver.markdown import render
from ..llm import LLMClient, LLMError
from ..models import Itinerary
from ..providers.amap import AmapClient
from ..providers.rail import RailError, RailSession
from .budget import build_budget, nights_by_city
from .hotels import collect_hotels, pick_stays
from .intake import parse_request
from .pois import nominate_pois, validate_pois
from .schedule import block_capacity, build_days, make_commute_fn
from .transit import plan_transit
from .validate import validate_all

StepCb = Callable[[int, int, str], None]
RequestCb = Callable[[object], None]  # intake 之后、LLM/交通之前? —— 见调用处（预算追问）


@dataclass
class PlanResult:
    itinerary: Itinerary
    md_path: Path
    html_path: Path
    json_path: Path
    qr_path: Path | None
    ical_path: Path

    def summary_lines(self) -> list[str]:
        it = self.itinerary
        lines = [f"可行性: {it.feasibility.status}"]
        for i, leg in enumerate(it.legs, 1):
            label = f"第{i}段" if len(it.legs) > 2 else ("去程" if i == 1 else "返程")
            lines.append(f"{label}: {leg.summary}" if leg else f"{label}: 无可用班次")
        req = it.request
        budget = req.budget_effective_total
        hint = f" / 预算 ¥{budget:.0f}" if budget else ""
        lines.append(
            f"预算: 合计 ¥{it.total_cost:.0f}（人均 ¥{it.total_cost / req.travelers:.0f}{hint}）"
        )
        lines.extend(f"⚠️ {i}" for i in it.feasibility.issues)
        return lines


def run_plan(
    request_text: str,
    *,
    settings: Settings | None = None,
    out_dir: Path | None = None,
    step_cb: StepCb | None = None,
    interactive_cb: RequestCb | None = None,
    include_hotels: bool = True,
    with_hotel_prices: bool = False,
) -> PlanResult:
    """端到端规划。step_cb(i, total, text) 用于进度展示；interactive_cb(req) 用于
    交互式确认（仅在 TTY 场景由 CLI 传入）。"""

    def step(i: int, total: int, text: str) -> None:
        if step_cb:
            step_cb(i, total, text)

    s = settings or get_settings()
    if not s.llm_api_key or not s.amap_api_key:
        raise LLMError("缺少 LLM_API_KEY 或 AMAP_API_KEY（先运行 tripflow setup）")

    from ..util import fmt_date

    total_steps = 9 + (1 if (with_hotel_prices and s.meituan_ht_token) else 0)
    llm = LLMClient(s)

    step(1, total_steps, "解析需求（LLM）…")
    with AmapClient(s.amap_api_key) as amap:

        async def rail_phase():
            async with RailSession(s) as rail:
                today = await rail.current_date()
                req = parse_request(llm, request_text, today)
                step(
                    2,
                    total_steps,
                    f"{' → '.join(req.route)}，{fmt_date(req.depart_date)} – "
                    f"{fmt_date(req.return_date)}，{req.travelers} 人，查交通（12306）…",
                )
                try:
                    transit = await plan_transit(rail, req, amap=amap)
                except ValueError as exc:
                    raise RailError(str(exc)) from exc
                return req, transit

        try:
            req, transit = asyncio.run(asyncio.wait_for(rail_phase(), timeout=300))
        except (RailError, LLMError) as exc:
            raise LLMError(f"需求解析或交通查询失败: {exc}") from exc

        if interactive_cb is not None:
            interactive_cb(req)

        step(3, total_steps, f"查询天气（{len(req.cities)} 个城市）…")
        weather_by_city: dict[str, dict[str, str]] = {}
        all_notes: list[str] = []
        for city in req.cities:
            forecasts = amap.weather(city)
            weather_by_city[city] = {
                f.date: f"{f.dayweather} {f.daytemp}/{f.nighttemp}°C" for f in forecasts
            }
        last_covered = max((max(m) for m in weather_by_city.values() if m), default="")
        if req.return_date > last_covered:
            all_notes.append("高德天气仅覆盖近 4 天，行程后段天气临近出发用 refresh 刷新")

        step(4, total_steps, "提名并校验景点（LLM 提名 → 高德逐个核实坐标/营业时间）…")
        pois_by_city: dict[str, list] = {}
        for block in transit.blocks:
            capacity_text, suggested = block_capacity(block)
            weather_text = "；".join(
                f"{k}:{v}" for k, v in list(weather_by_city.get(block.city, {}).items())[:4]
            )
            nominated = nominate_pois(
                llm,
                block.city,
                capacity_text,
                suggested,
                pace=req.pace,
                must_visit=req.must_visit,
                weather_text=weather_text,
                preferences=req.preferences,
            )
            pois, poi_notes = validate_pois(amap, block.city, nominated, req.must_visit)
            pois_by_city[block.city] = pois
            all_notes.extend(poi_notes)
        if not any(pois_by_city.values()):
            raise LLMError("没有校验通过的景点，请检查目的地名称")

        step(5, total_steps, "编排逐日行程（真实通勤 + 时间窗）…")
        centers = {city: amap.geo(city).location for city in req.cities}
        commute_fns = {city: make_commute_fn(amap, city) for city in req.cities}
        schedule_notes: list[str] = []
        # 第一遍：定 POI 归属（供选店计算活动质心）
        days, dropped = build_days(
            transit.blocks, pois_by_city, centers, weather_by_city, commute_fns, schedule_notes
        )
        all_notes.extend(schedule_notes)

        stays = []
        hotels = []
        if include_hotels:
            step(6, total_steps, "搜索并选定住宿（高德 POI，评分/距活动区）…")
            try:
                hotels = collect_hotels(amap, days)
                stays = pick_stays(hotels, days, transit.blocks)
            except Exception as exc:  # noqa: BLE001 - 酒店失败不影响行程单
                hotels, stays = [], []
                all_notes.append(f"住宿候选获取失败: {type(exc).__name__}")
            if stays:
                # 第二遍：以酒店为锚点重排（复用通勤缓存，仅新增 酒店→首景点 段）
                anchors = {s.city: (s.hotel.location, s.hotel.name) for s in stays}
                days, dropped = build_days(
                    transit.blocks, pois_by_city, centers, weather_by_city,
                    commute_fns, [], anchors,
                )
            elif hotels:
                all_notes.append("未选出住宿锚点（候选不足），通勤仍按市中心假设")

        deals: list = []
        if with_hotel_prices and s.meituan_ht_token and stays:
            from ..models import DealSection
            from ..providers.meituan import MeituanClient
            from ..util import now_ts
            from .deals import build_priced_hotel_query, extract_price_range

            step(7, total_steps, "查询美团住宿报价（每城约 1–2 分钟）…")
            client = MeituanClient(s.meituan_ht_token)
            for stay in stays:
                query = build_priced_hotel_query(stay)
                try:
                    content = client.query(query, city=stay.city, origin_query=query)
                except Exception as exc:  # noqa: BLE001 - 报价失败保持估算
                    all_notes.append(f"{stay.city}住宿报价获取失败: {exc}")
                    continue
                rng = extract_price_range(content)
                if rng:
                    stay.price_range = rng
                deals.append(DealSection(
                    city=stay.city, topic="住宿报价", query=query, content=content,
                    price_hint=rng or "", checked_at=now_ts(),
                ))

        budget_step = 7 if not (with_hotel_prices and s.meituan_ht_token and stays) else 8
        step(budget_step, total_steps, "核算预算（交通实价 + 分城市住宿）…")
        scheduled_pois = [v.poi for day in days for v in day.items]
        budget_items, total_cost = build_budget(
            req, transit.legs, scheduled_pois,
            nights_by_city(transit.blocks), stays=stays,
        )

        validate_step = budget_step + 1
        step(validate_step, total_steps, "确定性可行性检查…")
        feasibility = validate_all(
            req, transit.legs, days, dropped, total_cost, transit.notes, all_notes
        )

        map_step = validate_step + 1
        step(map_step, total_steps, "生成高德行程地图（云端 MCP）…")
        map_uri = ""
        head = tail = None
        if transit.go is not None:
            head = _station_point(amap, transit.go.to_station, req.cities[0])
        if transit.back is not None:
            tail = _station_point(amap, transit.back.from_station, req.cities[-1])
        try:
            map_uri = asyncio.run(
                asyncio.wait_for(
                    generate_map_uri(
                        s.amap_mcp_endpoint, days, station_poi=head, return_station_poi=tail,
                        hotel_by_city={
                            s_.city: s_.hotel for s_ in stays
                        } if stays else None,
                    ),
                    timeout=45,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 地图失败不影响行程单
            feasibility.notes.append(f"行程地图生成失败: {type(exc).__name__}（可重跑 plan 重试）")

    stem = "-".join(req.cities) + "-" + req.depart_date
    target = Path(out_dir) if out_dir else Path(s.trip_output_dir)
    target.mkdir(parents=True, exist_ok=True)
    itinerary = Itinerary(
        request=req,
        legs=transit.legs,
        comparison=transit.comparison,
        days=days,
        hotels=hotels,
        stays=stays,
        deals=deals,
        budget=budget_items,
        total_cost=total_cost,
        feasibility=feasibility,
        map_uri=map_uri,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    md_path = target / f"{stem}.md"
    html_path = target / f"{stem}.html"
    json_path = target / f"{stem}.json"
    qr_path = target / f"{stem}-map-qr.png"
    ical_path = target / f"{stem}.ics"
    md_path.write_text(render(itinerary), encoding="utf-8")
    json_path.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")
    if map_uri:
        import qrcode

        qrcode.make(map_uri).save(qr_path)
    html_path.write_text(render_html(itinerary, qr_path if map_uri else None), encoding="utf-8")
    write_ical(itinerary, ical_path)
    return PlanResult(
        itinerary=itinerary,
        md_path=md_path,
        html_path=html_path,
        json_path=json_path,
        qr_path=qr_path if map_uri else None,
        ical_path=ical_path,
    )


def _station_point(amap: AmapClient, station_name: str, city: str) -> dict | None:
    """把火车站名解析为地图打点（不要求是景点类 POI）。"""
    from ..util import parse_loc

    try:
        places = amap.place_text(station_name, city=city, limit=1)
        if places:
            lng, lat = parse_loc(places[0].location)
            return {"name": places[0].name, "lon": lng, "lat": lat, "poiId": places[0].id}
    except Exception:  # noqa: BLE001, S110 - 打点失败不应影响行程单
        pass
    return None
