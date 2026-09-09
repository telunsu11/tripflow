"""确定性可行性检查（纯 Python，无 LLM）：交通、容量、预算、天气提示。"""

from __future__ import annotations

from ..models import DayPlan, Feasibility, TransitChoice, TripRequest


def validate_all(
    req: TripRequest,
    legs: list[TransitChoice | None],
    days: list[DayPlan],
    dropped: list[str],
    total_cost: float,
    transit_notes: list[str],
    schedule_notes: list[str],
) -> Feasibility:
    issues: list[str] = []  # 触发降级的问题
    notes: list[str] = []  # 提示性说明

    if legs:
        if legs[0] is None:
            issues.append("去程无可用直达/中转班次（当日可能无票或无符合筛选条件的车次）")
        elif not legs[0].seats_ok:
            issues.append(f"去程 {legs[0].code} 余票不足，需候补或改选")
        if legs[-1] is None:
            issues.append("返程无可用直达/中转班次")
        elif not legs[-1].seats_ok:
            issues.append(f"返程 {legs[-1].code} 余票不足，需候补或改选")
        for i, leg in enumerate(legs[1:-1], start=1):
            if leg is None:
                issues.append(f"第 {i + 1} 段城际（途经段）无可用班次")
            elif not leg.seats_ok:
                issues.append(f"第 {i + 1} 段城际 {leg.code} 余票不足")
    else:
        issues.append("未查询到任何跨城交通班次")

    if dropped:
        issues.append(f"时间容量不足，未排入：{'、'.join(dropped)}")

    budget = req.budget_effective_total
    if budget is not None:
        if total_cost > budget * 1.3:
            issues.append(
                f"预算硬超支：估算总花费 ¥{total_cost:.0f} > 预算 ¥{budget:.0f} 的 130%，"
                "建议减少天数/人数或降低住宿标准"
            )
        elif total_cost > budget:
            issues.append(
                f"预算紧张：估算总花费 ¥{total_cost:.0f} 略超预算 ¥{budget:.0f}，"
                "可舍弃非必去点的门票项或换更早/更晚班次"
            )
        else:
            notes.append(f"预算内：估算 ¥{total_cost:.0f} / 预算 ¥{budget:.0f}")

    for day in days:
        if "雨" in day.weather:
            outdoor = [
                v.poi.name
                for v in day.items
                if v.poi.type and ("风景名胜" in v.poi.type or "公园" in v.poi.type)
            ]
            if outdoor:
                notes.append(
                    f"{day.date} {day.city}有雨，户外点（{'、'.join(outdoor[:3])}）"
                    "建议备伞或与室内点对调"
                )

    no_opentime = [v.poi.name for day in days for v in day.items if not v.poi.opentime]
    if no_opentime:
        notes.append(f"以下景点未获取到营业时间，按全天开放假设排程：{'、'.join(no_opentime[:5])}")

    notes.extend(transit_notes)
    notes.extend(schedule_notes)

    if any("无可用" in i or "硬超支" in i for i in issues):
        status = "INFEASIBLE"
    elif issues:
        status = "FEASIBLE_WITH_RISK"
    else:
        status = "FEASIBLE"
    return Feasibility(status=status, issues=issues, notes=notes)
