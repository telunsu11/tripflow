"""局部改签 + 候补车次监控测试。"""

from test_m3 import _itinerary
from test_rail_parse import TICKET

from tripflow.models import (
    DayPlan,
    Poi,
    TrainWatch,
    TransitChoice,
    VisitItem,
)
from tripflow.planner.replan import blocks_from_days, diff_days, validate_single_poi
from tripflow.planner.watch import check_extra_trains


# ---------- blocks 重建 ----------
def _leg(date, frm_city, to_city, code="G1", dep="09:00", arr="10:00"):
    return TransitChoice(kind="direct", date=date, code=code, from_city=frm_city,
                         to_city=to_city, depart_time=dep, arrive_time=arr,
                         summary="", seats_ok=True, checked_at=0)


def test_blocks_from_days_multi_city_same_day_transfer():
    days = [
        DayPlan(date="2026-09-12", city="苏州"),
        DayPlan(date="2026-09-13", city="苏州"),
        DayPlan(date="2026-09-13", city="杭州"),  # 同日换乘日归属两城
        DayPlan(date="2026-09-14", city="杭州"),
    ]
    legs = [
        _leg("2026-09-12", "上海", "苏州", "G1"),
        _leg("2026-09-13", "苏州", "杭州", "G2"),
        _leg("2026-09-14", "杭州", "上海", "G3"),
    ]
    blocks = blocks_from_days(days, legs)
    assert [(b.city, b.start_date, b.end_date) for b in blocks] == [
        ("苏州", "2026-09-12", "2026-09-13"),
        ("杭州", "2026-09-13", "2026-09-14"),
    ]
    # 城际段/返程段正确挂到块的 arrive/depart
    assert blocks[0].arrive_leg.code == "G1"
    assert blocks[0].depart_leg.code == "G2"
    assert blocks[1].depart_leg.code == "G3"


def test_diff_days_detects_changes():
    poi_a = Poi(name="A", poi_id="a", location="0,0")
    old = [DayPlan(date="2026-09-12", city="杭州",
                   items=[VisitItem(poi=poi_a, start="10:00", end="12:00")])]
    poi_b = Poi(name="B", poi_id="b", location="1,1")
    new = [DayPlan(date="2026-09-12", city="杭州",
                   items=[VisitItem(poi=poi_b, start="10:00", end="12:00")])]
    changes = diff_days(old, new)
    assert len(changes) == 1 and "旧" in changes[0] and "新" in changes[0]
    assert diff_days(old, old) == []


# ---------- 单点校验 ----------
class FakeAmap:
    def __init__(self):
        from tripflow.providers.amap import Place

        self.place = Place(id="B001", name="西溪国家湿地公园", location="120.06,30.26",
                           typecode="110200", type="风景名胜;湿地")

    def place_text(self, name, city=None, limit=10):
        return [self.place]

    def place_detail(self, poi_id):
        class D:
            location = self.place.location
            opentime = "08:00-17:30 最晚进入16:30"
            rating = "4.7"
            city = "杭州市"

        return D()

    def geo(self, name, city=None):
        class G:
            location = "120.1,30.2"
            city = "杭州市"

        return G()


def test_validate_single_poi():
    from tripflow.providers.amap import AmapClient  # noqa: F401

    poi = validate_single_poi(FakeAmap(), "杭州", "西溪国家湿地公园")
    assert poi.poi_id == "B001"
    assert poi.opentime == "08:00-17:30 最晚进入16:30"
    assert poi.stay_minutes == 150 and poi.core is False


# ---------- run_replan 端到端（注入假通勤） ----------
def test_run_replan_removes_and_reschedules(monkeypatch):
    from tripflow.config import Settings
    from tripflow.models import CommuteLeg
    from tripflow.planner import replan as replan_mod

    it = _itinerary()
    it.days = [
        DayPlan(date="2026-09-12", city="成都",
                items=[VisitItem(poi=Poi(name="宽窄巷子", poi_id="p1", location="104.05,30.66"),
                                 start="10:00", end="12:00"),
                       VisitItem(poi=Poi(name="锦里", poi_id="p2", location="104.04,30.64"),
                                 start="14:00", end="16:00")]),
    ]

    def fake_make_commute(amap, city):
        def commute(a, b, date=None):
            return CommuteLeg(from_name="", to_name="", mode="公交", minutes=20)
        return commute

    monkeypatch.setattr(replan_mod, "make_commute_fn", fake_make_commute)

    class S(Settings):
        pass

    s = Settings(_env_file=None, amap_api_key="test")

    # amap 全走 FakeAmap
    fake = FakeAmapPlusDays()
    monkeypatch.setattr(replan_mod, "AmapClient", lambda *a, **k: fake)

    new_it, changes = replan_mod.run_replan(
        it, s, remove_pois=["锦里"], regenerate_map=False
    )
    assert all(v.poi.name != "锦里" for d in new_it.days for v in d.items)
    assert any("移除" in c for c in changes)
    # 车次与住宿锁定：legs/stays 引用不变
    assert new_it.legs == it.legs and new_it.stays == it.stays


class FakeAmapPlusDays(FakeAmap):
    """补齐 run_replan 用到的 geo/road；days 由外部设置。"""

    def __init__(self):
        super().__init__()
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        return False


# ---------- 候补车次监控 ----------
class FakeRail:
    def __init__(self, tickets_by_query):
        self._data = tickets_by_query
        self.calls = []

    async def tickets(self, date, frm, to, filter_flags="", sort="", limit=0):
        self.calls.append((date, frm, to))
        from tripflow.providers.rail import parse_ticket

        raw = self._data.get((date, frm, to), [])
        return [parse_ticket(r) for r in raw]


def _sold_out(code="G9", start="08:00"):
    return {**TICKET, "start_train_code": code, "start_time": start,
            "arrive_time": "10:00", "prices": [
                {"seat_name": "二等座", "num": "无", "price": 100},
                {"seat_name": "一等座", "num": "无", "price": 200}]}


def _available(code="G9", start="08:00"):
    return {**TICKET, "start_train_code": code, "start_time": start,
            "arrive_time": "10:00", "prices": [
                {"seat_name": "二等座", "num": "有", "price": 100},
                {"seat_name": "一等座", "num": "有", "price": 200}]}


def test_watch_extra_state_machine():
    import asyncio

    it = _itinerary()
    it.watch_extra = [TrainWatch(code="G9", date="2026-09-19",
                                 from_city="上海", to_city="杭州")]

    # 首轮：只记基线
    rail = FakeRail({("2026-09-19", "上海", "杭州"): [_sold_out()]})
    msgs = asyncio.run(check_extra_trains(rail, it))
    assert len(msgs) == 1 and "基线" in msgs[0]
    assert it.watch_extra[0].last_seats_ok is False

    # 放票 → 告警
    rail2 = FakeRail({("2026-09-19", "上海", "杭州"): [_available()]})
    msgs2 = asyncio.run(check_extra_trains(rail2, it))
    assert any("余票恢复" in m for m in msgs2)
    assert it.watch_extra[0].last_seats_ok is True

    # 无变化 → 静默
    rail3 = FakeRail({("2026-09-19", "上海", "杭州"): [_available()]})
    assert asyncio.run(check_extra_trains(rail3, it)) == []
