"""行程单 Markdown 渲染：事实与建议分开、实价与估算分开、数字带时间戳。"""

from __future__ import annotations

from ..models import Itinerary
from ..util import fmt_date, fmt_ts

STATUS_EMOJI = {
    "FEASIBLE": "✅ 可以执行",
    "FEASIBLE_WITH_RISK": "⚠️ 可执行但有风险",
    "INFEASIBLE": "❌ 按当前条件排不通",
}


def _route_title(req) -> str:
    route = req.route
    if len(route) <= 2:
        return f"{req.origin} → {req.destination}"
    return req.origin + " → " + " → ".join(req.cities)


def render(it: Itinerary) -> str:
    req = it.request
    lines: list[str] = []
    add = lines.append

    budget_text = ""
    if req.budget_per_person is not None:
        budget_text = f"，人均预算 ¥{req.budget_per_person:g}"
    elif req.budget_total is not None:
        budget_text = f"，总预算 ¥{req.budget_total:g}"
    add(
        f"# 行程单：{_route_title(req)}"
        f"（{fmt_date(req.depart_date)} – {fmt_date(req.return_date)}，"
        f"{req.travelers} 人{budget_text}）"
    )
    add("")
    add(
        f"> 由 tripflow 生成于 {it.generated_at}｜票价/余票为 12306 实时查询，"
        f"以实际购票页为准｜估算项均已标注"
    )
    add("")

    if req.assumptions:
        add("## 规划假设（由需求推导，请确认）")
        add("")
        for a in req.assumptions:
            add(f"- {a}")
        add("")

    add("## 跨城交通")
    add("")
    for i, leg in enumerate(it.legs, 1):
        label = f"第 {i} 段" if len(it.legs) > 2 else ("去程" if i == 1 else "返程")
        if leg is None:
            add(f"**{label}**：❌ 无可用班次（直达与中转均未找到）")
        else:
            add(
                f"**{label}**（{fmt_date(leg.date)}｜{leg.from_city} → {leg.to_city}）：{leg.summary}"
                f"  <sub>查询于 {fmt_ts(leg.checked_at)}</sub>"
            )
        add("")
    if it.comparison:
        add("### 首段方案对比")
        add("")
        keys = list(it.comparison[0].keys())
        add("| " + " | ".join(keys) + " |")
        add("|" + "|".join(["---"] * len(keys)) + "|")
        for row in it.comparison:
            add("| " + " | ".join(str(row.get(k, "")) for k in keys) + " |")
        add("")

    add("## 逐日行程")
    add("")
    for i, day in enumerate(it.days, 1):
        city = f" · {day.city}" if day.city else ""
        weather = f" · {day.weather}" if day.weather else ""
        add(f"### Day {i} · {fmt_date(day.date)}{city}{weather}")
        add("")
        if not day.items:
            add("- （自由活动）")
        for v in day.items:
            rating = f"｜评分 {v.poi.rating}" if v.poi.rating else ""
            add(f"- **{v.start}–{v.end} {v.poi.name}**{rating}")
            if v.poi.reason:
                add(f"  - {v.poi.reason}")
            if v.poi.opentime:
                from ..planner.schedule import opentime_for_date

                add(f"  - 营业时间（高德核实）：{opentime_for_date(v.poi.opentime, day.date)}")
        for leg in day.legs:
            via = f"（{'→'.join(leg.lines)}）" if leg.lines else ""
            taxi = f"｜{leg.taxi_alt}" if leg.taxi_alt else ""
            add(f"- ↳ {leg.from_name} → {leg.to_name}：{leg.mode} 约 {leg.minutes} 分钟{via}{taxi}")
        for note in day.notes:
            add(f"- ℹ️ {note}")
        add("")

    if it.style_variants:
        primary_count = sum(len(d.items) for d in it.days)
        primary_drop = len(next(
            (i for i in it.feasibility.issues if "未排入" in i), ""
        ).split("：")[-1].split("、")) if any("未排入" in i for i in it.feasibility.issues) else 0
        add("## 多风格方案对比（同一批车次/景点/住宿，仅编排节奏不同）")
        add("")
        add("| 风格 | 排入景点 | 未排入 | 预算 | 结论 |")
        add("|---|---:|---:|---:|---|")
        add(f"| **均衡（主方案）** | {primary_count} | {primary_drop} | ¥{it.total_cost:.0f} |"
            f" {it.feasibility.status} |")
        for v in it.style_variants:
            add(f"| {v.name} | {sum(len(d.items) for d in v.days)} | {len(v.dropped)} |"
                f" ¥{v.total_cost:.0f} | {v.status} |")
        add("")
        for v in it.style_variants:
            add(f"### {v.name} · 逐日概览")
            for i, d in enumerate(v.days, 1):
                names = " → ".join(x.poi.name for x in d.items)
                add(f"- Day {i}（{d.city}）：{names or '—'}")
            if v.dropped:
                add(f"- 未排入：{'、'.join(v.dropped)}")
            add("")
        add("> 上文逐日详情、地图与日历均基于主方案（均衡）；如需切换风格可加 `--compare-styles`"
            " 后参考对比结果重新确认。")
        add("")

    if it.stays:
        add("## 住宿安排（确定性选店：评分优先、距活动区就近）")
        add("")
        for s in it.stays:
            basis = "高德参考价" if s.price_basis == "amap" else "城市档次估算"
            rng = f"｜美团区间 {s.price_range}（未计入预算）" if s.price_range else ""
            add(f"### {s.city} · {s.hotel.name}"
                f"（{fmt_date(s.check_in)} 入住 – {fmt_date(s.check_out)} 离店，{s.nights} 晚）")
            add("")
            add(f"- 每间夜约 ¥{s.price_per_night}（{basis}）{rng}")
            if s.hotel.rating:
                add(f"- 评分 {s.hotel.rating}"
                    f"{'｜地址：' + s.hotel.address if s.hotel.address else ''}")
            if s.hotel.cost:
                add(f"- 高德参考消费：{s.hotel.cost}")
            if s.alternatives:
                add(f"- 备选：{'、'.join(s.alternatives)}")
            add("")
        add("> 酒店为信息展示与通勤锚点，不含预订；价格以实际预订页为准。")
        add("")

    if it.hotels:
        add("## 住宿候选（高德 POI，不含预订）")
        add("")
        add("| 城市 | 酒店 | 评分 | 参考价 | 地址 |")
        add("|---|---|---|---|---|")
        for h in it.hotels:
            add(
                f"| {h.city} | {h.name} | {h.rating or '—'} | {h.cost or '—'} | {h.address or '—'} |"
            )
        add("")

    add("## 预算（估算合计见末行）")
    add("")
    add("| 项目 | 金额 | 性质 | 说明 |")
    add("|---|---:|:---:|---|")
    for b in it.budget:
        kind = "实价" if b.kind == "real" else "**估算**"
        add(f"| {b.category} | ¥{b.amount:.0f} | {kind} | {b.note} |")
    per_person = it.total_cost / req.travelers
    add(f"| **合计** | **¥{it.total_cost:.0f}** | | 人均约 ¥{per_person:.0f} |")
    add("")

    if it.deals:
        add("## 美团优惠参考（原文引用，非预算依据）")
        add("")
        for deal in it.deals:
            add(
                f"### {deal.city} · {deal.topic}"
                f"{'｜' + deal.price_hint if deal.price_hint else ''}"
                f"  <sub>查询于 {fmt_ts(deal.checked_at)}</sub>"
            )
            add("")
            add(deal.content)
            add("")
        add(
            "> 以上为美团酒旅接口返回的原文（含优惠政策），价格以实际下单页为准；"
            "预算表中的门票/住宿仍为类型估算。"
        )
        add("")

    add("## 可行性结论")
    add("")
    add(f"**{STATUS_EMOJI.get(it.feasibility.status, it.feasibility.status)}**")
    add("")
    if it.feasibility.issues:
        add("需要处理的问题：")
        for i in it.feasibility.issues:
            add(f"- ⚠️ {i}")
        add("")
    if it.feasibility.notes:
        add("说明与提示：")
        for n in it.feasibility.notes:
            add(f"- {n}")
        add("")

    if it.map_uri:
        add("## 行程地图（高德）")
        add("")
        add(f"用高德 App 扫描同目录下的二维码，或点击链接：[{it.map_uri}]({it.map_uri})")
        add("")

    add("## 出发前必读")
    add("")
    add("- 余票随时变化，购票时以 12306 页面为准；可运行 `tripflow refresh` 刷新车次与天气")
    add("- 估算项（住宿/餐饮/门票）为经验值，用于预算量级判断，不构成消费建议")
    add("- 建议出发前一天重新生成或刷新行程单")
    return "\n".join(lines)
