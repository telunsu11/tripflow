"""高德 REST provider 测试：httpx.MockTransport 全 mock，无网络无 Key。"""

import httpx
import pytest

from tripflow.providers.amap import AmapClient, AmapError


def make_client(handler) -> AmapClient:
    return AmapClient("test-key", transport=httpx.MockTransport(handler))


def handler_ok(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v3/geocode/geo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "geocodes": [
                    {
                        "location": "104.055,30.663",
                        "adcode": "510100",
                        "city": "成都市",
                        "level": "市",
                    }
                ],
            },
        )
    if path == "/v3/weather/weatherInfo":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "forecasts": [
                    {
                        "casts": [
                            {
                                "date": "2026-09-09",
                                "week": "3",
                                "dayweather": "阵雨",
                                "nightweather": "阵雨",
                                "daytemp": "25",
                                "nighttemp": "18",
                            }
                        ],
                    }
                ],
            },
        )
    if path == "/v5/place/text":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "pois": [
                    {
                        "id": "B001C07VJ2",
                        "name": "成都武侯祠博物馆",
                        "location": "104.047992,30.646168",
                        "address": "武侯祠大街231号",
                        "typecode": "140100",
                        "type": "科教文化服务;博物馆;博物馆",
                    }
                ],
            },
        )
    if path == "/v5/place/detail":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "pois": [
                    {
                        "id": "B001C07VJ2",
                        "name": "成都武侯祠博物馆",
                        "location": "104.047992,30.646168",
                        "address": "武侯祠大街231号",
                        # 字段名取自 2026-09-09 真实返回
                        "business": {
                            "opentime_week": "周一至周日 08:30-18:30 最晚进入17:30",
                            "opentime_today": "08:30-18:30",
                            "rating": "4.8",
                            "alias": "武侯祠|武侯祠景区",
                        },
                    }
                ],
            },
        )
    if path == "/v3/direction/transit/integrated":
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "route": {
                    "transits": [
                        {
                            "duration": "1664",
                            "walking_distance": "395",
                            "segments": [{"bus": {"buslines": [{"name": "82路"}]}}],
                        }
                    ]
                },
            },
        )
    return httpx.Response(404, json={"status": "0", "info": f"unexpected path {path}"})


def test_geo_and_weather():
    with make_client(handler_ok) as client:
        g = client.geo("成都")
        assert g.adcode == "510100"
        assert g.location == "104.055,30.663"

        casts = client.weather("成都")  # 城市名 → 内部先查 adcode
        assert casts[0].dayweather == "阵雨"
        assert casts[0].daytemp == "25"


def test_place_text_and_detail():
    with make_client(handler_ok) as client:
        places = client.place_text("武侯祠博物馆", city="成都")
        assert places[0].id == "B001C07VJ2"
        assert places[0].location == "104.047992,30.646168"

        detail = client.place_detail("B001C07VJ2")
        assert detail.opentime.startswith("周一至周日 08:30")
        assert detail.rating == "4.8"
        assert detail.alias == "武侯祠|武侯祠景区"


def test_transit():
    with make_client(handler_ok) as client:
        plans = client.transit("104.055,30.663", "104.047,30.646", city="成都")
        assert plans[0].duration_sec == 1664
        assert plans[0].lines == ["82路"]


def test_invalid_key_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "0",
                "info": "INVALID_USER_KEY",
                "infocode": "10001",
            },
        )

    with make_client(handler) as client, pytest.raises(AmapError, match="INVALID_USER_KEY"):
        client.geo("成都")


def test_empty_key_raises():
    with pytest.raises(AmapError, match="AMAP_API_KEY"):
        AmapClient("")
