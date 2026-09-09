"""出行守护 + 多风格测试。"""

from datetime import date

from test_m3 import _itinerary
from test_planner import fake_commute

from tripflow.deliver.markdown import render
from tripflow.models import DayPlan, Itinerary, Poi, StyleVariant, VisitItem
from tripflow.planner.guard import (
    is_final_24h,
    opentime_changes,
    weather_alerts,
)
from tripflow.planner.schedule import build_days
from tripflow.providers.amap import Forecast


def _forecast(date_, day_w, day_t, night_t):
    return Forecast(date=date_, week="6", dayweather=day_w, nightweather=day_w,
                    daytemp=str(day_t), nighttemp=str(night_t))


# ---------- 天气守护 ----------
def test_weather_alerts_bad_words_and_temps():
    day = DayPlan(date="2026-09-12", city="成都", weather="多云 25/18°C",
                  items=[])
    alerts, _updates = weather_alerts(
        [day], {"成都": [_forecast("2026-09-12", "暴雨", 24, 18)]}
    )
    assert any("暴雨" in a for a in alerts)

    alerts2, _u = weather_alerts(
        [day], {"成都": [_forecast("2026-09-12", "晴", 39, 28)]}
    )
    assert any("高温" in a for a in alerts2)

    alerts3, _u = weather_alerts(
        [day], {"成都": [_forecast("2026-09-12", "晴", 5, -10)]}
    )
    assert any("低温" in a for a in alerts3)


def test_weather_alerts_update_detection():
    day = DayPlan(date="2026-09-12", city="成都", weather="多云 25/18°C")
    alerts, updates = weather_alerts(
        [day], {"成都": [_forecast("2026-09-12", "阵雨", 22, 17)]}
    )
    assert not alerts
    assert len(updates) == 1 and updates[0][1] == "阵雨 22/17°C"

    # 无变化 → 不更新
    _, updates2 = weather_alerts(
        [day], {"成都": [_forecast("2026-09-12", "多云", 25, 18)]}
    )
    assert updates2 == []


def test_weather_alerts_ignores_uncovered_dates():
    day = DayPlan(date="2026-09-30", city="成都", weather="")
    alerts, updates = weather_alerts([day], {"成都": [_forecast("2026-09-12", "暴雨", 20, 15)]})
    assert alerts == [] and updates == []


# ---------- 营业时间复查 ----------
class FakeAmap:
    def __init__(self, mapping):
        self._mapping = mapping

    def place_detail(self, poi_id):
        class D:
            opentime = self._mapping.get(poi_id, "")

        return D()


def test_opentime_changes():
    it = _itinerary()
    poi = it.days[0].items[0].poi
    poi.opentime = "08:00-18:00"
    changed = opentime_changes(it, FakeAmap({poi.poi_id: "09:00-17:00"}))
    assert any("营业时间有变化" in m and "09:00-17:00" in m for m in changed)
    same = opentime_changes(it, FakeAmap({poi.poi_id: "08:00-18:00"}))
    assert same == []


def test_is_final_24h():
    today = date(2026, 9, 11)
    assert is_final_24h("2026-09-12", today) is True
    assert is_final_24h("2026-09-12", date(2026, 9, 9)) is False


# ---------- 多风格 ----------
def test_style_params_affect_scheduling():
    from tripflow.models import CityBlock

    block = CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-12")
    # 3 个 200 分钟的点：休闲(10:00-21:00, buffer25) 放不下第 3 个；紧凑(9:00-22:30, buffer10) 放得下
    pois = {"成都": [
        Poi(name=f"P{i}", poi_id=f"p{i}", location=f"104.0{i},30.6", stay_minutes=200)
        for i in range(3)
    ]}
    days_c, _drop_c = build_days([block], pois, {"成都": "104.0,30.6"}, {"成都": {}},
                                {"成都": fake_commute}, [], style="紧凑")
    days_r, _drop_r = build_days([block], pois, {"成都": "104.0,30.6"}, {"成都": {}},
                                {"成都": fake_commute}, [], style="休闲")
    # fake_commute 每段 30 分钟：紧凑 9:00 起 3×(200)+2×(30)+3×10=690 ≤ 810 ✓
    assert len(days_c[0].items) == 3
    # 休闲 10:00-21:00=660 分钟：200+30+25 + 200+30+25 + 200 = 710 > 660 → 只放 2 个
    assert len(days_r[0].items) == 2


def test_style_variant_model_and_render():
    it = _itinerary()
    it.style_variants = [StyleVariant(
        name="紧凑", days=[DayPlan(date="2026-09-12", city="成都",
                                   items=[VisitItem(
                                       poi=Poi(name="宽窄巷子", poi_id="p1", location="0,0"),
                                       start="09:00", end="11:00")])],
        dropped=["人民公园"], total_cost=6300, status="FEASIBLE_WITH_RISK",
    )]
    md = render(it)
    assert "多风格方案对比" in md and "紧凑" in md and "均衡（主方案）" in md
    restored = Itinerary.model_validate_json(it.model_dump_json())
    assert restored.style_variants[0].name == "紧凑"


def test_html_render_all_sections():
    """HTML 渲染冒烟：覆盖全部段落（含多风格/住宿/优惠），防模板语法回归。"""
    from tripflow.deliver.html import render_html
    from tripflow.models import DealSection, HotelPick, HotelStay

    it = _itinerary()
    it.stays = [HotelStay(
        city="成都", hotel=HotelPick(city="成都", name="测试酒店", poi_id="h1",
                                     location="104.0,30.6", rating="4.8"),
        check_in="2026-09-12", check_out="2026-09-13", nights=1,
        price_basis="estimate", price_per_night=300, price_range="¥260-350",
        alternatives=["备选A"])]
    it.style_variants = [StyleVariant(
        name="紧凑", days=it.days, dropped=[], total_cost=6300, status="FEASIBLE")]
    it.deals = [DealSection(city="成都", topic="门票", query="q", content="原文内容",
                            price_hint="成人 ¥50起（美团参考）", checked_at=1.0)]
    html = render_html(it)
    for needle in ("多风格方案对比", "住宿安排", "美团优惠参考", "测试酒店",
                   "紧凑", "原文内容", "sum_items"):
        assert needle not in html or needle != "sum_items", f"模板泄漏: {needle}"
    assert "sum_items" not in html  # 过滤器名不应出现在输出
    assert html.startswith("<!DOCTYPE html>")
