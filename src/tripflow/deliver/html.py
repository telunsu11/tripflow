"""行程单 HTML 渲染：单文件可分享（内联样式 + 内嵌地图二维码）。"""

from __future__ import annotations

import base64
from pathlib import Path

from ..models import Itinerary
from ..util import fmt_date, fmt_ts

STATUS_TEXT = {
    "FEASIBLE": "✅ 可以执行",
    "FEASIBLE_WITH_RISK": "⚠️ 可执行但有风险",
    "INFEASIBLE": "❌ 按当前条件排不通",
}
STATUS_CLASS = {"FEASIBLE": "ok", "FEASIBLE_WITH_RISK": "risk", "INFEASIBLE": "bad"}

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "itinerary.html.j2"


def render_html(it: Itinerary, qr_png: Path | None = None) -> str:
    from jinja2 import Template

    tpl = Template(TEMPLATE_PATH.read_text("utf-8"))
    qr_uri = ""
    if qr_png is not None and qr_png.exists():
        qr_uri = "data:image/png;base64," + base64.b64encode(qr_png.read_bytes()).decode()

    req = it.request
    if req.budget_per_person is not None:
        budget_text = f"人均预算 ¥{req.budget_per_person:g}"
    elif req.budget_total is not None:
        budget_text = f"总预算 ¥{req.budget_total:g}"
    else:
        budget_text = ""
    route = req.route
    route_title = (
        f"{req.origin} → {req.destination}"
        if len(route) <= 2
        else req.origin + " → " + " → ".join(req.cities)
    )
    legs_view = []
    for i, leg in enumerate(it.legs, 1):
        label = f"第 {i} 段" if len(it.legs) > 2 else ("去程" if i == 1 else "返程")
        legs_view.append(
            {
                "label": label,
                "choice": leg,
                "date_cn": fmt_date(leg.date) if leg else "",
                "checked_cn": fmt_ts(leg.checked_at) if leg else "",
            }
        )
    comparison_keys = list(it.comparison[0].keys()) if it.comparison else []

    html = tpl.render(
        it=it,
        req=req,
        route_title=route_title,
        budget_text=budget_text,
        legs_view=legs_view,
        comparison_keys=comparison_keys,
        status_text=STATUS_TEXT.get(it.feasibility.status, it.feasibility.status),
        status_class=STATUS_CLASS.get(it.feasibility.status, "risk"),
        qr_uri=qr_uri,
        date_cn=fmt_date,
    )
    return html
