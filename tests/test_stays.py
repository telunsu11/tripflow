"""住宿规划测试：选店规则、锚点编排、预算分级、美团区间提取、ical/地图/渲染。"""

from test_m3 import _itinerary, _req
from test_planner import fake_commute

from tripflow.deliver.ical import render_ical
from tripflow.models import (
    CityBlock,
    CommuteLeg,
    DayPlan,
    HotelPick,
    HotelStay,
    Itinerary,
    Poi,
    VisitItem,
)
from tripflow.planner.budget import build_budget, nights_by_city
from tripflow.planner.deals import build_priced_hotel_query, extract_price_range
from tripflow.planner.hotels import parse_cost, pick_stays
from tripflow.planner.schedule import build_days


def _hotel(name, loc, rating="", cost="", city="成都"):
    return HotelPick(city=city, name=name, poi_id=f"h-{name}", location=loc,
                     rating=rating, cost=cost, address="某路1号")


def _days(city="成都", dates=("2026-09-12", "2026-09-13")):
    poi = Poi(name="景点A", poi_id="p1", location="104.05,30.65")
    return [
        DayPlan(date=d, city=city, items=[VisitItem(poi=poi, start="10:00", end="12:00")])
        for d in dates
    ]


# ---------- 选店 ----------
def test_parse_cost():
    assert parse_cost("¥288") == 288
    assert parse_cost("288") == 288
    assert parse_cost("200-400") == 200
    assert parse_cost("") is None
    assert parse_cost("—") is None


def test_pick_stays_rating_first_then_distance():
    days = _days()
    blocks = [CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-13")]
    near_low = _hotel("近但分低", "104.05,30.66", rating="4.2")
    far_high = _hotel("远但分高", "104.30,30.90", rating="4.8")
    mid = _hotel("中间", "104.06,30.66", rating="4.8")
    stays = pick_stays([near_low, far_high, mid], days, blocks)
    assert len(stays) == 1
    s = stays[0]
    assert s.hotel.name == "中间"  # 评分并列 → 距质心近者胜
    assert s.alternatives == ["远但分高", "近但分低"]
    assert s.check_in == "2026-09-12" and s.check_out == "2026-09-13"
    assert s.nights == 1
    assert s.price_basis == "estimate"  # 无 cost → 城市档次估算


def test_pick_stays_amap_cost_basis():
    days = _days()
    blocks = [CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-13")]
    with_cost = _hotel("有参考价", "104.05,30.66", rating="4.5", cost="¥288")
    stays = pick_stays([with_cost], days, blocks)
    assert stays[0].price_basis == "amap"
    assert stays[0].price_per_night == 288


def test_pick_stays_empty_city():
    assert pick_stays([], _days(), [CityBlock(city="成都", start_date="2026-09-12",
                                              end_date="2026-09-13")]) == []


# ---------- 锚点编排 ----------
def test_build_days_with_hotel_anchor():
    block = CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-13")
    pois = {"成都": [
        Poi(name="东点", poi_id="p1", location="104.20,30.65", stay_minutes=600),
        Poi(name="西点", poi_id="p2", location="104.00,30.65", stay_minutes=600),
    ]}  # 10h 停留 → 每天只容一个点，强制跨天
    calls = []

    def commute(a, b, date=None):
        calls.append((a, b))
        return CommuteLeg(from_name="", to_name="", mode="公交", minutes=20)

    days, dropped = build_days(
        [block], pois, {"成都": "104.10,30.65"}, {"成都": {}}, {"成都": commute}, [],
        anchors={"成都": ("104.10,30.66", "示例酒店")},
    )
    assert not dropped
    # 共 3 段通勤：Day1 酒店→首点、Day1内 点→点、Day2 酒店→首点（均从酒店出发起止）
    assert len(calls) == 3
    assert calls[0][0] == "104.10,30.66"
    assert calls[2][0] == "104.10,30.66"
    leg_names = [(leg.from_name, leg.to_name) for day in days for leg in day.legs]
    assert leg_names[0][0] == "示例酒店"
    assert leg_names[1][0] == "示例酒店"  # Day2 首段同样从酒店出发（calls 含一次跨天失败尝试）


def test_build_days_without_anchor_keeps_old_behavior():
    block = CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-12")
    pois = {"成都": [Poi(name="单点", poi_id="p1", location="104.1,30.65")]}
    days, _ = build_days([block], pois, {"成都": "104.1,30.65"}, {"成都": {}},
                         {"成都": fake_commute}, [])
    assert days[0].legs == []  # 无锚点：首点不计通勤（旧行为）


# ---------- 预算分级 ----------
def test_budget_with_stays_basis_labels():
    req = _req(return_date="2026-09-13")  # 2 天 1 晚，成都
    blocks = [CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-13")]
    nights = nights_by_city(blocks)
    stays = [HotelStay(city="成都", hotel=_hotel("X", "0,0", rating="4.5", cost="¥288"),
                       check_in="2026-09-12", check_out="2026-09-13", nights=1,
                       price_basis="amap", price_per_night=288,
                       price_range="¥260-350")]
    items, _ = build_budget(req, [], [], nights, stays=stays)
    hotel_item = next(i for i in items if i.category == "住宿")
    assert hotel_item.amount == 288  # 1 人 1 间 1 晚
    assert "高德参考价" in hotel_item.note
    assert "¥260-350" in hotel_item.note and "未计入" in hotel_item.note


def test_budget_estimate_fallback_without_stays():
    req = _req(return_date="2026-09-13")
    blocks = [CityBlock(city="成都", start_date="2026-09-12", end_date="2026-09-13")]
    items_a, _ = build_budget(req, [], [], nights_by_city(blocks), stays=[])
    items_b, _ = build_budget(req, [], [], nights_by_city(blocks))
    assert items_a[1].amount == items_b[1].amount  # 无 stays 时回退城市估算


# ---------- 美团区间 ----------
def test_extract_price_range():
    assert extract_price_range("大床房 ¥260-350 每晚") == "¥260-350"
    assert extract_price_range("经济型 ¥219起") == "¥219起"
    assert extract_price_range("暂无报价") == ""


def test_build_priced_hotel_query():
    stay = HotelStay(city="成都", hotel=_hotel("全季", "0,0"),
                     check_in="2026-09-12", check_out="2026-09-14", nights=2,
                     price_basis="estimate", price_per_night=300)
    q = build_priced_hotel_query(stay)
    assert "成都" in q and "2026-09-12" in q and "2晚" in q and "全季" in q


# ---------- 交付物 ----------
def test_ical_stay_events():
    it = _itinerary()
    it.stays = [HotelStay(city="成都", hotel=_hotel("全季成都宽窄巷子店", "104.05,30.66"),
                          check_in="2026-09-12", check_out="2026-09-13", nights=1,
                          price_basis="estimate", price_per_night=300)]
    ics = render_ical(it)
    assert ics.count("BEGIN:VEVENT") == 3 + 2  # 原 3 事件 + 入住/离店
    assert "DTSTART:20260912T150000" in ics  # 15:00 假设入住
    assert "DTSTART:20260913T113000" in ics  # 11:30 假设离店
    assert "入住" in ics and "离店" in ics


def test_markdown_stays_section():
    from tripflow.deliver.markdown import render

    it = _itinerary()
    it.stays = [HotelStay(city="成都", hotel=_hotel("全季", "104.0,30.6", rating="4.6"),
                          check_in="2026-09-12", check_out="2026-09-13", nights=1,
                          price_basis="estimate", price_per_night=300,
                          price_range="¥260-350", alternatives=["如家", "汉庭"])]
    md = render(it)
    assert "住宿安排" in md and "全季" in md and "城市档次估算" in md
    assert "¥260-350" in md and "未计入预算" in md
    assert "如家" in md


def test_map_line_list_hotel_first():
    from tripflow.deliver.amap_map import _line_list

    poi = Poi(name="景点", poi_id="p1", location="104.1,30.6")
    day = DayPlan(date="2026-09-12", city="成都",
                  items=[VisitItem(poi=poi, start="10:00", end="12:00")])
    lines = _line_list([day], None, None, {"成都": _hotel("全季", "104.0,30.6")})
    assert lines[0]["pointInfoList"][0]["name"] == "🏨全季"
    assert lines[0]["pointInfoList"][0]["poiId"] == "h-全季"


def test_itinerary_stays_roundtrip():
    it = _itinerary()
    it.stays = [HotelStay(city="成都", hotel=_hotel("X", "0,0"),
                          check_in="2026-09-12", check_out="2026-09-13", nights=1,
                          price_basis="estimate", price_per_night=300)]
    restored = Itinerary.model_validate_json(it.model_dump_json())
    assert restored.stays[0].hotel.name == "X"
