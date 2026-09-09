"""12306 JSON 解析测试：fixture 取自 2026-09-09 真实接口返回。"""

from tripflow.providers.rail import normalize_num, parse_ticket, parse_transfer

TICKET = {
    "train_no": "5l000G1974B4",
    "start_date": "2026-09-12",
    "arrive_date": "2026-09-12",
    "start_train_code": "G1974",
    "start_time": "07:16",
    "arrive_time": "18:26",
    "lishi": "11:10",
    "from_station": "上海虹桥",
    "to_station": "成都东",
    "from_station_telecode": "AOH",
    "to_station_telecode": "ICW",
    "prices": [
        {
            "seat_name": "商务座",
            "short": "swz",
            "seat_type_code": "9",
            "num": "4",
            "price": 3584,
            "discount": 87,
        },
        {
            "seat_name": "一等座",
            "short": "zy",
            "seat_type_code": "M",
            "num": "有",
            "price": 1687,
            "discount": 89,
        },
        {
            "seat_name": "二等座",
            "short": "ze",
            "seat_type_code": "O",
            "num": "有",
            "price": 1040,
            "discount": 88,
        },
        {
            "seat_name": "无座",
            "short": "wz",
            "seat_type_code": "W",
            "num": "无",
            "price": 1040,
            "discount": 88,
        },
    ],
    "dw_flag": ["复兴号", "静音车厢"],
}

INTERLINE = {
    "lishi": "10:0",
    "start_time": "08:55",
    "start_date": "2026-09-12",
    "arrive_date": "2026-09-12",
    "arrive_time": "18:55",
    "from_station_code": "AOH",
    "from_station_name": "上海虹桥",
    "middle_station_code": "EAY",
    "middle_station_name": "西安北",
    "end_station_code": "ICW",
    "end_station_name": "成都东",
    "start_train_code": "G94",
    "same_station": True,
    "same_train": False,
    "wait_time": "39分钟",
    "ticketList": [
        {
            "train_no": "5l00000G9401",
            "start_train_code": "G94",
            "start_time": "08:55",
            "arrive_time": "14:34",
            "lishi": "05:39",
            "from_station": "上海虹桥",
            "to_station": "西安北",
            "prices": [
                {"seat_name": "二等座", "short": "ze", "num": "3", "price": 756, "discount": 87},
            ],
        },
        {
            "train_no": "240000G96903",
            "start_train_code": "G969",
            "start_time": "15:13",
            "arrive_time": "18:55",
            "lishi": "03:42",
            "from_station": "西安北",
            "to_station": "成都东",
            "prices": [
                {"seat_name": "二等座", "short": "ze", "num": "有", "price": 285, "discount": 91},
            ],
        },
    ],
}


def test_normalize_num_states():
    assert normalize_num("有") == (True, None)
    assert normalize_num("3") == (True, 3)
    assert normalize_num("0") == (False, 0)
    assert normalize_num("无") == (False, 0)


def test_parse_ticket():
    t = parse_ticket(TICKET)
    assert t.train_code == "G1974"
    assert t.from_station == "上海虹桥"
    assert t.to_station == "成都东"
    assert t.flags == ["复兴号", "静音车厢"]

    sw = t.seat("商务座")
    assert sw is not None and sw.available and sw.count == 4 and sw.price == 3584.0
    zy = t.seat("一等座")
    assert zy is not None and zy.available and zy.count is None  # 「有」= 不限张数
    wz = t.seat("无座")
    assert wz is not None and not wz.available

    cheapest = t.cheapest
    assert cheapest is not None and cheapest.name == "二等座"


def test_parse_transfer():
    p = parse_transfer(INTERLINE)
    assert p.from_station == "上海虹桥"
    assert p.middle_station == "西安北"
    assert p.to_station == "成都东"
    assert p.same_station is True
    assert p.same_train is False
    assert p.wait_time == "39分钟"
    assert [leg.train_code for leg in p.legs] == ["G94", "G969"]
    assert p.legs[0].seat("二等座").count == 3
