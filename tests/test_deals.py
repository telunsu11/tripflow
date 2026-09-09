"""美团优惠核查测试：provider 封装（mock subprocess）、查询组装、价格提取、渲染。"""

import json as _json

from test_m3 import _itinerary

from tripflow.models import DealSection, Itinerary
from tripflow.planner.deals import (
    build_hotel_query,
    build_ticket_query,
    extract_price_hint,
    run_deals,
)
from tripflow.providers.meituan import MeituanClient, MeituanError, _clean, _npx_command


# ---------- provider ----------
def test_meituan_requires_token():
    try:
        MeituanClient("")
        raise AssertionError("应抛 MeituanError")
    except MeituanError as e:
        assert "MEITUAN_HT_TOKEN" in str(e)


def test_clean_strips_answer_tag():
    assert _clean("内容\n</answer>\n") == "内容"


def test_npx_command_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda c: None)
    try:
        _npx_command(["-y", "x"])
        raise AssertionError("应抛 MeituanError")
    except MeituanError as e:
        assert "Node.js" in str(e)


def _fake_run_factory(returncode=0, stdout='{"status":"success","data":"成人票50元</answer>"}', stderr=""):
    import subprocess

    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["env"] = kw.get("env")
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return fake_run, calls


def test_query_success(monkeypatch):
    import subprocess

    import tripflow.providers.meituan as m

    fake, calls = _fake_run_factory()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(m.shutil, "which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    client = MeituanClient("tok")
    out = client.query("武侯祠门票", city="成都")
    assert out == "成人票50元"
    argv = calls["argv"]
    assert argv[-1] == "成都" and "--query" in argv and "ht-ai" in " ".join(argv)
    assert calls["env"]["MEITUAN_HT_TOKEN"] == "tok"
    assert calls["env"]["MEITUAN_RAW_JSON"] == "1"


def test_query_auth_failure(monkeypatch):
    import subprocess

    import tripflow.providers.meituan as m

    fake, _ = _fake_run_factory(returncode=3)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(m.shutil, "which", lambda c: "/usr/bin/npx" if c == "npx" else None)
    try:
        MeituanClient("tok").query("x", city="成都")
        raise AssertionError("应抛 MeituanError")
    except MeituanError as e:
        assert "Token" in str(e) or "重新获取" in str(e)


# ---------- 查询组装与提取 ----------
def test_build_ticket_query_groups_by_city():
    it = _itinerary()  # 单日成都宽窄巷子
    queries = build_ticket_query(it)
    assert len(queries) == 1
    city, q = queries[0]
    assert city == "成都"
    assert "宽窄巷子" in q and "门票" in q


def test_build_hotel_query():
    assert build_hotel_query(_itinerary()) == [("成都", "成都住宿酒店优惠和推荐")]


def test_extract_price_hint():
    assert extract_price_hint("成人票价格50元起，儿童票7元") == "成人 ¥50起（美团参考）"
    assert extract_price_hint("成人票71元") == "成人 ¥71（美团参考）"
    assert extract_price_hint("无价格信息") == ""


# ---------- run_deals + 渲染 ----------
def test_run_deals_writes_sections(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.n = 0

        def query(self, q, city, origin_query=""):
            self.n += 1
            return f"{city}的门票：成人票60元起"

    it = _itinerary()
    it, failures = run_deals(FakeClient(), it)
    assert not failures
    assert len(it.deals) == 1
    assert it.deals[0].price_hint == "成人 ¥60起（美团参考）"
    assert isinstance(it.deals[0].checked_at, float)


def test_run_deals_failure_isolated():
    class BoomClient:
        def query(self, q, city, origin_query=""):
            raise MeituanError("超时")

    it = _itinerary()
    it, failures = run_deals(BoomClient(), it)
    assert it.deals == []
    assert failures and "成都" in failures[0]


def test_markdown_renders_deals():
    from tripflow.deliver.markdown import render

    it = _itinerary()
    it.deals = [DealSection(city="成都", topic="门票", query="q",
                            content="成人票50元起", price_hint="成人 ¥50起（美团参考）",
                            checked_at=1.0)]
    md = render(it)
    assert "美团优惠参考" in md and "成人票50元起" in md and "非预算依据" in md


def test_itinerary_deals_roundtrip():
    it = _itinerary()
    it.deals = [DealSection(city="成都", topic="门票", query="q", content="c", checked_at=1.0)]
    restored = Itinerary.model_validate(_json.loads(it.model_dump_json()))
    assert restored.deals[0].city == "成都"
