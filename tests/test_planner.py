"""planner 纯逻辑测试：intake、选班/换乘规则、日期分派、编排、预算、可行性、刷新。"""

from test_rail_parse import INTERLINE, TICKET

from tripflow.llm import LLMClient, LLMError
from tripflow.models import (
    CityBlock,
    Poi,
    TransitChoice,
    TripRequest,
)
from tripflow.planner.budget import build_budget, hotel_per_night, nights_by_city
from tripflow.planner.intake import extract_json_object, parse_request
from tripflow.planner.pois import entry_fee_for, is_attraction, stay_minutes_for
from tripflow.planner.refresh import match_leg, refresh_leg_choice
from tripflow.planner.schedule import block_capacity, build_days, windows_from_transit
from tripflow.planner.transit import (
    allocate_days,
    best_seat,
    filter_city_stations,
    interline_feasible,
    pick_back,
    pick_go,
    pick_intercity,
    prefer_hub_stations,
)
from tripflow.planner.validate import validate_all
from tripflow.providers.amap import Place
from tripflow.providers.rail import parse_ticket, parse_transfer


# ---------- intake ----------
def test_extract_json_object_with_fences():
    text = '好的，以下是解析结果：\n```json\n{"origin": "上海", "travelers": 2}\n```\n祝旅途愉快'
    assert extract_json_object(text) == {"origin": "上海", "travelers": 2}


class FakeLLM:
    """按脚本回放的假 LLM。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.replies.pop(0)

    @property
    def configured(self):
        return True


GOOD_INTAKE = """{"origin":"上海","destination":"成都","waypoints":[],
"depart_date":"2026-09-12","return_date":"2026-09-14","travelers":2,
"budget_per_person":3000,"pace":"均衡","must_visit":["宽窄巷子"],
"preferences":"","assumptions":["预算默认按人均处理"]}"""


def test_parse_request_ok():
    req = parse_request(FakeLLM([GOOD_INTAKE]), "国庆从上海去成都3天2人人均3000", "2026-09-09")
    assert isinstance(req, TripRequest)
    assert req.destination == "成都"
    assert req.waypoints == []
    assert req.cities == ["成都"]
    assert req.route == ["上海", "成都"]
    assert req.days == 3
    assert req.nights == 2
    assert req.budget_effective_total == 6000


def test_parse_request_multi_city():
    payload = GOOD_INTAKE.replace(
        '"destination":"成都","waypoints":[]', '"destination":"杭州","waypoints":["苏州"]'
    )
    payload = payload.replace('"return_date":"2026-09-14"', '"return_date":"2026-09-15"')
    req = parse_request(FakeLLM([payload]), "从上海去苏州杭州4天", "2026-09-09")
    assert req.cities == ["苏州", "杭州"]
    assert req.route == ["上海", "苏州", "杭州"]
    assert req.days == 4


def test_parse_request_retries_on_bad_output():
    bad = '{"origin":"上海"}'
    llm = FakeLLM([bad, bad, GOOD_INTAKE])
    req = parse_request(llm, "anything", "2026-09-09")
    assert req.destination == "成都"
    assert llm.calls == 3


def test_parse_request_gives_up():
    llm = FakeLLM(["not json at all"] * 3)
    try:
        parse_request(llm, "x", "2026-09-09")
        raise AssertionError("应抛 LLMError")
    except LLMError:
        pass


# ---------- transit ----------
def test_best_seat_priority():
    t = parse_ticket(TICKET)
    seat = best_seat(t, 2)
    assert seat.name == "二等座"
    # 「有」= 余量充足（不限张数），大队伍也能选
    assert best_seat(t, 999).name == "二等座"
    # 全部坐席余量不足时不得落到「无座」
    sold_out = parse_ticket(
        {
            **TICKET,
            "prices": [
                {"seat_name": "商务座", "num": "无", "price": 3584},
                {"seat_name": "一等座", "num": "无", "price": 1687},
                {"seat_name": "二等座", "num": "无", "price": 1040},
                {"seat_name": "无座", "num": "有", "price": 1040},
            ],
        }
    )
    assert best_seat(sold_out, 1) is None  # 无座不作为推荐席别


def test_interline_rules():
    same_train = parse_transfer({**INTERLINE, "same_train": True, "same_station": False})
    tight = parse_transfer(
        {**INTERLINE, "same_train": False, "same_station": True, "wait_time": "10分钟"}
    )
    ok_wait = parse_transfer(
        {**INTERLINE, "same_train": False, "same_station": True, "wait_time": "39分钟"}
    )
    cross = parse_transfer({**INTERLINE, "same_train": False, "same_station": False})
    assert interline_feasible(same_train)
    assert not interline_feasible(tight)
    assert interline_feasible(ok_wait)
    assert not interline_feasible(cross)


def test_pick_go_prefers_early_arrival():
    a = parse_ticket(
        {
            **TICKET,
            "start_train_code": "G-A",
            "start_time": "07:16",
            "arrive_time": "18:26",
            "lishi": "11:10",
        }
    )
    b = parse_ticket(
        {
            **TICKET,
            "start_train_code": "G-B",
            "start_time": "09:04",
            "arrive_time": "17:00",
            "lishi": "07:56",
        }
    )
    assert pick_go([a, b], 1).train_code == "G-B"


def test_pick_back_prefers_afternoon():
    morning = parse_ticket(
        {**TICKET, "start_time": "07:00", "arrive_time": "18:00", "lishi": "11:00"}
    )
    afternoon = parse_ticket(
        {**TICKET, "start_time": "15:00", "arrive_time": "23:00", "lishi": "08:00"}
    )
    assert pick_back([morning, afternoon], 1).start_time == "15:00"


def test_pick_go_depart_after():
    morning = parse_ticket({**TICKET, "start_time": "08:00", "arrive_time": "09:00",
                            "lishi": "01:00"})
    afternoon = parse_ticket({**TICKET, "start_time": "14:30", "arrive_time": "15:40",
                              "lishi": "01:10"})
    # 用户要求下午出发 → 不得选早班车
    assert pick_go([morning, afternoon], 1, not_before="13:00").start_time == "14:30"
    # 无偏好时仍偏好上午
    assert pick_go([morning, afternoon], 1).start_time == "08:00"


def test_pick_intercity_prefers_morning():
    evening = parse_ticket(
        {**TICKET, "start_time": "16:00", "arrive_time": "18:00", "lishi": "02:00"}
    )
    morning = parse_ticket(
        {**TICKET, "start_time": "09:00", "arrive_time": "11:30", "lishi": "02:30"}
    )
    assert pick_intercity([evening, morning], 1).start_time == "09:00"


def test_filter_city_stations():
    main = parse_ticket({**TICKET, "from_station": "苏州北", "to_station": "杭州东"})
    remote_from = parse_ticket({**TICKET, "from_station": "盛泽", "to_station": "杭州东"})
    remote_to = parse_ticket({**TICKET, "from_station": "苏州北", "to_station": "金山北"})
    keep = filter_city_stations([main, remote_from, remote_to], "苏州", "杭州")
    assert [t.train_code for t in keep] == [main.train_code]
    # 全部被过滤时回退原列表，不返回空
    assert filter_city_stations([remote_from], "苏州", "杭州") == [remote_from]


def test_prefer_hub_stations():
    hub = parse_ticket({**TICKET, "from_station": "杭州东", "to_station": "上海虹桥"})
    suburb = parse_ticket({**TICKET, "from_station": "杭州东", "to_station": "上海松江"})
    keep = prefer_hub_stations([suburb, hub], "杭州", "上海")
    assert [t.train_code for t in keep] == [hub.train_code]
    # 未覆盖城市回退
    assert prefer_hub_stations([hub], "丽水", "上海") == [hub]


def test_allocate_days_even_and_remainder():
    # 同日换乘：边界日期共享，跨度总和 = 天数 + 城市数 - 1；首城多吃余数
    alloc = allocate_days(["苏州", "杭州", "南京"], "2026-09-12", "2026-09-17")
    assert alloc == [
        ("苏州", "2026-09-12", "2026-09-14"),
        ("杭州", "2026-09-14", "2026-09-16"),
        ("南京", "2026-09-16", "2026-09-17"),
    ]
    # 单城：start=出发日、end=返回日
    assert allocate_days(["成都"], "2026-09-12", "2026-09-14") == [
        ("成都", "2026-09-12", "2026-09-14")
    ]


def test_allocate_days_too_few_days():
    try:
        allocate_days(["苏州", "杭州", "南京"], "2026-09-12", "2026-09-13")
        raise AssertionError("应抛 ValueError")
    except ValueError as e:
        assert "至少" in str(e)


# ---------- pois ----------
def _place(name, typecode, ptype):
    return Place(id="B123", name=name, location="104.0,30.6", typecode=typecode, type=ptype)


def test_is_attraction():
    assert is_attraction(_place("武侯祠", "140100", "风景名胜"))
    assert is_attraction(_place("人民公园", "110101", "公园"))
    assert not is_attraction(_place("如家酒店", "100100", "住宿服务"))


def test_stay_and_fee_rules():
    museum = _place("武侯祠博物馆", "140100", "科教文化服务;博物馆")
    park = _place("人民公园", "110101", "公园")
    assert stay_minutes_for(museum) == 150
    assert stay_minutes_for(park) == 120
    assert entry_fee_for(museum) == 50


# ---------- schedule ----------
def _poi(name, loc, stay=120, core=False):
    return Poi(name=name, poi_id=f"id-{name}", location=loc, stay_minutes=stay, core=core)


def fake_commute(a, b, date=None):
    from tripflow.models import CommuteLeg

    return CommuteLeg(from_name="", to_name="", mode="公交", minutes=30)


def _block(city, start, end, arrive=None, depart=None):
    return CityBlock(
        city=city, start_date=start, end_date=end, arrive_leg=arrive, depart_leg=depart
    )


def _choice(date, dep, arr):
    return TransitChoice(
        kind="direct",
        date=date,
        code="G1",
        depart_time=dep,
        arrive_time=arr,
        summary="",
        seats_ok=True,
        checked_at=0,
    )


def test_build_days_core_first():
    block = _block("成都", "2026-09-12", "2026-09-12")
    pois = [
        _poi("远点A", "104.30,30.70"),
        _poi("核心B", "104.05,30.65", core=True),
        _poi("普通C", "104.06,30.66"),
    ]
    days, dropped = build_days(
        [block], {"成都": pois}, {"成都": "104.05,30.65"}, {"成都": {}}, {"成都": fake_commute}, []
    )
    assert not dropped
    assert days[0].city == "成都"
    assert days[0].items[0].poi.name == "核心B"
    assert days[0].items[0].start == "09:30"


def test_build_days_transit_window():
    # 到达 19:35 → 20:35 开始；22:00 结束 → 只能排 60 分钟的点
    arrive = _choice("2026-09-12", "09:00", "19:35")
    block = _block("成都", "2026-09-12", "2026-09-12", arrive=arrive)
    pois = [_poi(f"P{i}", f"104.0{i},30.6", stay=60) for i in range(3)]
    days, dropped = build_days(
        [block], {"成都": pois}, {"成都": "104.0,30.6"}, {"成都": {}}, {"成都": fake_commute}, []
    )
    assert len(days[0].items) == 1  # 20:35-21:35 放得下一站，其余放不下
    assert [d.split("（")[0] for d in dropped] == ["P1", "P2"]


def test_build_days_multi_city_sorted():
    blocks = [
        _block("苏州", "2026-09-12", "2026-09-13"),
        _block("杭州", "2026-09-13", "2026-09-14"),
    ]
    pois = {"苏州": [_poi("拙政园", "120.62,31.32")], "杭州": [_poi("西湖", "120.15,30.25")]}
    days, dropped = build_days(
        blocks,
        pois,
        {"苏州": "120.6,31.3", "杭州": "120.1,30.2"},
        {"苏州": {}, "杭州": {}},
        {"苏州": fake_commute, "杭州": fake_commute},
        [],
    )
    # 9/13 为同日换乘日：出现在两个城市的 DayPlan 中
    assert [d.city for d in days] == ["苏州", "苏州", "杭州", "杭州"]
    assert [d.date for d in days] == ["2026-09-12", "2026-09-13", "2026-09-13", "2026-09-14"]
    assert not dropped


def test_block_capacity():
    arrive = _choice("2026-09-12", "09:00", "19:35")
    depart = _choice("2026-09-14", "08:16", "18:00")
    block = CityBlock(
        city="成都",
        start_date="2026-09-12",
        end_date="2026-09-14",
        arrive_leg=arrive,
        depart_leg=depart,
    )
    text, count = block_capacity(block)
    assert "无有效时间" in text  # 9/14 06:46 前结束 → 无窗口
    # Day1 晚 ~1.6h→1，Day2 全天→4，Day3 无→0
    assert count == 5


def test_windows_from_transit():
    go = _choice("2026-09-12", "09:00", "19:35")
    back = _choice("2026-09-14", "10:00", "20:00")
    d1, last = windows_from_transit(go, back)
    assert d1 == 20 * 60 + 35
    assert last == 9 * 60  # 10:00 - 60min（缓冲收紧，保住离开日半天）


# ---------- budget ----------
def test_budget_multi_leg_and_cities():
    req = TripRequest(
        origin="上海",
        destination="杭州",
        waypoints=["苏州"],
        depart_date="2026-09-12",
        return_date="2026-09-15",
        travelers=3,
        budget_per_person=3000,
    )
    legs = [
        TransitChoice(
            kind="direct",
            date="2026-09-12",
            code="G1",
            from_city="上海",
            to_city="苏州",
            depart_time="09:00",
            arrive_time="10:00",
            summary="",
            seat_name="二等座",
            price_per_person=40,
            seats_ok=True,
            checked_at=0,
        ),
        TransitChoice(
            kind="direct",
            date="2026-09-13",
            code="G2",
            from_city="苏州",
            to_city="杭州",
            depart_time="09:00",
            arrive_time="11:00",
            summary="",
            seat_name="二等座",
            price_per_person=110,
            seats_ok=True,
            checked_at=0,
        ),
        TransitChoice(
            kind="direct",
            date="2026-09-15",
            code="G3",
            from_city="杭州",
            to_city="上海",
            depart_time="15:00",
            arrive_time="17:00",
            summary="",
            seat_name="二等座",
            price_per_person=110,
            seats_ok=True,
            checked_at=0,
        ),
    ]
    blocks = [
        CityBlock(city="苏州", start_date="2026-09-12", end_date="2026-09-13"),
        CityBlock(city="杭州", start_date="2026-09-13", end_date="2026-09-15"),
    ]
    nights = nights_by_city(blocks)
    assert nights == {"苏州": 1, "杭州": 2}  # 合计 3 = 行程 4 天 3 晚

    pois = [_poi("武侯祠博物馆", "104.0,30.6")]
    pois[0].entry_fee_estimate = 50
    items, total = build_budget(req, legs, pois, nights)
    by_cat = {i.category: i for i in items}
    assert by_cat["跨城交通"].amount == (40 + 110 + 110) * 3
    assert (
        by_cat["住宿"].amount == hotel_per_night("苏州") * 2 * 1 + hotel_per_night("杭州") * 2 * 2
    )
    assert by_cat["餐饮"].amount == 150 * 3 * 4
    assert total == sum(i.amount for i in items)


# ---------- validate ----------
def _leg(seats_ok=True, missing=False):
    if missing:
        return None
    return TransitChoice(
        kind="direct",
        date="2026-09-12",
        code="G1",
        depart_time="09:00",
        arrive_time="19:00",
        summary="",
        seats_ok=seats_ok,
        checked_at=0,
    )


def test_validate_missing_leg_infeasible():
    req = TripRequest(
        origin="上海",
        destination="成都",
        depart_date="2026-09-12",
        return_date="2026-09-13",
        travelers=1,
    )
    from tripflow.models import DayPlan

    days = [DayPlan(date="2026-09-12"), DayPlan(date="2026-09-13")]
    f = validate_all(req, [None, _leg()], days, [], 10, [], [])
    assert f.status == "INFEASIBLE"
    assert any("去程" in i for i in f.issues)


def test_validate_budget_over_risk():
    req = TripRequest(
        origin="上海",
        destination="成都",
        depart_date="2026-09-12",
        return_date="2026-09-13",
        travelers=1,
        budget_total=100,
    )
    from tripflow.models import DayPlan

    days = [DayPlan(date="2026-09-12"), DayPlan(date="2026-09-13")]
    f = validate_all(req, [_leg(), _leg()], days, [], 120, [], [])
    assert f.status == "FEASIBLE_WITH_RISK"


# ---------- refresh ----------
def test_match_leg_and_refresh():
    old = TransitChoice(
        kind="direct",
        date="2026-09-12",
        code="G1974",
        from_city="上海",
        to_city="成都",
        depart_time="07:16",
        arrive_time="18:26",
        summary="旧",
        seat_name="二等座",
        price_per_person=1040,
        seats_ok=True,
        checked_at=1000,
    )
    t = parse_ticket(TICKET)  # G1974 二等座 ¥1040 有票
    assert match_leg([t], "G1974").train_code == "G1974"
    assert match_leg([t], "G999") is None

    new, msg = refresh_leg_choice(old, t, 2)
    assert new.code == "G1974"
    assert new.from_city == "上海" and new.to_city == "成都"  # 城市信息保留
    assert "无变化" in msg or "票价" in msg or "余票" in msg

    gone, msg2 = refresh_leg_choice(old, None, 2)
    assert gone.seats_ok is False
    assert "人工确认" in msg2


def test_llm_client_unconfigured():
    class S:
        llm_api_key = ""
        llm_base_url = ""
        llm_model = ""
        llm_fast_model = ""

    client = LLMClient(S())  # type: ignore[arg-type]
    assert not client.configured
    try:
        client.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("应抛 LLMError")
    except LLMError:
        pass
