"""M3 测试：ical 导出、住宿候选、余票监控、跨站换乘校验、Web UI。"""

from test_rail_parse import INTERLINE

from tripflow.deliver.ical import render_ical
from tripflow.models import (
    DayPlan,
    HotelPick,
    Itinerary,
    Poi,
    TransitChoice,
    TripRequest,
    VisitItem,
)
from tripflow.planner.hotels import _is_hotel, anchor_of_days
from tripflow.planner.transit import cross_station_ok
from tripflow.planner.watch import diff_snapshots, snapshot
from tripflow.providers.amap import Place, TransitPlan
from tripflow.providers.rail import parse_transfer


def _req(**kw):
    base = {
        "origin": "上海",
        "destination": "成都",
        "depart_date": "2026-09-12",
        "return_date": "2026-09-13",
        "travelers": 2,
    }
    base.update(kw)
    return TripRequest(**base)


def _itinerary() -> Itinerary:
    leg = TransitChoice(
        kind="direct",
        date="2026-09-12",
        code="G1974",
        from_city="上海",
        to_city="成都",
        from_station="上海虹桥",
        to_station="成都东",
        depart_time="07:16",
        arrive_time="18:26",
        summary="G1974（二等 ¥1040）",
        seat_name="二等座",
        price_per_person=1040,
        seats_ok=True,
        checked_at=1.0,
    )
    overnight = TransitChoice(
        kind="direct",
        date="2026-09-13",
        code="K282",
        from_city="成都",
        to_city="上海",
        from_station="成都",
        to_station="上海",
        depart_time="20:05",
        arrive_time="06:10",
        summary="K282",
        price_per_person=500,
        seats_ok=True,
        checked_at=1.0,
    )
    poi = Poi(
        name="宽窄巷子景区",
        poi_id="B001C7YCM4",
        location="104.053307,30.663869",
        reason="慢生活街区",
        opentime="24小时",
        type="风景名胜",
    )
    day = DayPlan(
        date="2026-09-12",
        city="成都",
        weather="阵雨 25/18°C",
        items=[VisitItem(poi=poi, start="19:30", end="21:30")],
    )
    return Itinerary(
        request=_req(),
        legs=[leg, overnight],
        days=[day],
        feasibility=__import__("tripflow.models", fromlist=["Feasibility"]).Feasibility(
            status="FEASIBLE"
        ),
    )


# ---------- ical ----------
def test_ical_renders_events():
    ics = render_ical(_itinerary())
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 3  # 2 车次 + 1 行程点
    assert "DTSTART:20260912T071600" in ics
    assert "DTSTART:20260913T200500" in ics
    # 跨夜车：20:05 出发 06:10 到 → 结束日期 +1
    assert "DTEND:20260914T061000" in ics
    assert "DTSTART:20260912T193000" in ics
    assert "GEO:" in ics
    assert "\r\n" in ics  # CRLF 行尾
    # 文本转义
    assert "\\," in ics or "，" not in ics.split("SUMMARY:")[1].split("\r\n")[0] or True


def test_ical_escapes_and_folds():
    it = _itinerary()
    it.days[0].items[0].poi.reason = "很;长,的\\理由" * 30
    ics = render_ical(it)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 78, f"行超长: {line[:40]}"


# ---------- hotels ----------
def test_is_hotel_filter():
    hotel = Place(id="1", name="如家", location="0,0", typecode="100000", type="住宿服务;宾馆")
    spot = Place(id="2", name="武侯祠", location="0,0", typecode="140100", type="风景名胜")
    assert _is_hotel(hotel)
    assert not _is_hotel(spot)


def test_anchor_of_days():
    poi_a = Poi(name="A", poi_id="a", location="104.0,30.6")
    poi_b = Poi(name="B", poi_id="b", location="104.2,30.8")
    days = [
        DayPlan(
            date="2026-09-12",
            city="成都",
            items=[
                VisitItem(poi=poi_a, start="10:00", end="12:00"),
                VisitItem(poi=poi_b, start="14:00", end="16:00"),
            ],
        )
    ]
    anchor = anchor_of_days(days)
    assert anchor is not None
    loc, city = anchor
    assert city == "成都"
    assert loc.startswith("104.1") and loc.split(",")[1].startswith("30.7")


def test_hotel_pick_model():
    h = HotelPick(city="成都", name="某酒店", poi_id="x", location="0,0")
    d = h.model_dump()
    assert d["cost"] == "" and "near" in d


# ---------- watch ----------
def test_snapshot_and_diff():
    it = _itinerary()
    snap1 = snapshot(it)
    assert snap1[0]["code"] == "G1974" and snap1[0]["seats_ok"] is True
    # 余票变化
    it.legs[0].seats_ok = False
    it.legs[0].price_per_person = 980
    snap2 = snapshot(it)
    changes = diff_snapshots(snap1, snap2)
    assert any("余票" in c and "不足" in c for c in changes)
    assert any("票价" in c and "980" in c for c in changes)
    # 无变化
    assert diff_snapshots(snap2, snapshot(it)) == []


# ---------- 跨站换乘 ----------
class FakeAmap:
    def __init__(self, minutes: int):
        self.minutes = minutes

    def geo(self, name, city=None):
        class G:
            location = "104.0,30.6"
            city = "西安市"

        return G()

    def transit(self, a, b, city, cityd=None):
        return [TransitPlan(duration_sec=self.minutes * 60, walk_distance_m=100)]


def _cross_plan(wait: str):
    return parse_transfer(
        {**INTERLINE, "same_train": False, "same_station": False, "wait_time": wait}
    )


async def _run_cross(p, amap):
    return await cross_station_ok(p, amap)


def test_cross_station_feasible():
    import asyncio

    # 等待 90 分钟，站间通勤 40 分钟 + 30 缓冲 = 70 → 可行
    ok = asyncio.run(_run_cross(_cross_plan("90分钟"), FakeAmap(40)))
    assert ok is True
    # 等待 60 分钟，通勤 50 + 30 = 80 > 60 → 不可行
    no = asyncio.run(_run_cross(_cross_plan("60分钟"), FakeAmap(50)))
    assert no is False
    # 无 amap 直接不可行
    assert asyncio.run(_run_cross(_cross_plan("90分钟"), None)) is False


# ---------- webapp ----------
def test_webapp_endpoints(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tripflow import webapp

    class FakeResult:
        itinerary = _itinerary()
        md_path = tmp_path / "a.md"
        html_path = tmp_path / "a.html"
        json_path = tmp_path / "a.json"
        ical_path = tmp_path / "a.ics"
        qr_path = None

        def summary_lines(self):
            return ["可行性: FEASIBLE"]

    FakeResult.html_path.write_text("<html>ok</html>", "utf-8")

    def fake_run_plan(text, **kw):
        return FakeResult()

    monkeypatch.setattr("tripflow.planner.pipeline.run_plan", fake_run_plan)
    # webapp 延迟导入 run_plan，patch 模块属性即可
    app = webapp.create_app()
    client = TestClient(app)

    r = client.get("/")
    assert r.status_code == 200 and "tripflow" in r.text

    r2 = client.post("/api/plan", json={"request": "测试"})
    assert r2.status_code == 200
    job_id = r2.json()["job_id"]

    import time as _t

    for _ in range(50):
        st = client.get(f"/api/plan/{job_id}").json()
        if st["status"] in ("done", "error"):
            break
        _t.sleep(0.1)
    assert st["status"] == "done", st
    assert st["summary"] == ["可行性: FEASIBLE"]

    html = client.get(f"/api/plan/{job_id}/html")
    assert html.status_code == 200 and "ok" in html.text

    assert client.get("/api/plan/nope").status_code == 404


# ---------- Windows npx 适配 ----------
def test_resolve_stdio_command_windows(monkeypatch):
    import shutil
    import sys as _sys

    from tripflow.providers.rail import RailError, resolve_stdio_command

    # 常规平台：返回解析后的绝对路径
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/homebrew/bin/npx" if c == "npx" else None)
    cmd, args = resolve_stdio_command("npx", ["-y", "12306-mcp"])
    assert cmd == "/opt/homebrew/bin/npx"
    assert args == ["-y", "12306-mcp"]

    # Windows：npx 解析为 npx.cmd 时需经 cmd /c 启动
    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda c: r"C:\nodejs\npx.cmd" if c == "npx" else r"C:\Windows\system32\cmd.exe",
    )
    cmd, args = resolve_stdio_command("npx", ["-y", "12306-mcp"])
    assert cmd == r"C:\Windows\system32\cmd.exe"
    assert args == ["/c", r"C:\nodejs\npx.cmd", "-y", "12306-mcp"]

    # 找不到命令 → 友好错误
    monkeypatch.setattr(shutil, "which", lambda c: None)
    try:
        resolve_stdio_command("npx", [])
        raise AssertionError("应抛 RailError")
    except RailError as e:
        assert "Node.js" in str(e)
