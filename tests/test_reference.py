"""参考数据层测试：打包基线加载、用户覆盖合并、损坏回退。"""



from tripflow.planner import reference


def test_packed_baseline_loads():
    hubs = reference.hub_stations()
    assert "上海" in hubs and "上海虹桥" in hubs["上海"]
    prices, default = reference.hotel_prices()
    assert prices.get("成都") == 300 and default == 300
    assert reference.daily_costs()["food_per_person_day"] == 150


def test_hotel_price_lookup_via_budget():
    from tripflow.planner.budget import hotel_per_night

    assert hotel_per_night("成都") == 300
    assert hotel_per_night("丽水") == 300  # 未列出城市 → _default


def test_user_override_merges(tmp_path, monkeypatch):
    (tmp_path / "hub_stations.yaml").write_text(
        "丽水:\n  - 丽水\n  - 丽水南\n上海:\n  - 上海\n  - 上海虹桥\n  - 上海松江\n", "utf-8"
    )
    (tmp_path / "hotel_prices.yaml").write_text(
        "per_night_by_city:\n  丽水: 220\n", "utf-8"
    )
    monkeypatch.setattr(reference, "user_data_dir", lambda: tmp_path)
    reference.hub_stations.cache_clear()
    reference.hotel_prices.cache_clear()
    try:
        hubs = reference.hub_stations()
        assert "丽水南" in hubs["丽水"]  # 新增城市
        assert "上海松江" in hubs["上海"]  # 覆盖已有城市（按用户为准）
        assert "上海西" not in hubs["上海"]  # 用户键整体替换该城市
        prices, _ = reference.hotel_prices()
        assert prices["丽水"] == 220 and prices["成都"] == 300  # 其余保留
    finally:
        reference.hub_stations.cache_clear()
        reference.hotel_prices.cache_clear()


def test_broken_user_file_falls_back(tmp_path, monkeypatch):
    (tmp_path / "hub_stations.yaml").write_text("!!!: [broken", "utf-8")
    monkeypatch.setattr(reference, "user_data_dir", lambda: tmp_path)
    reference.hub_stations.cache_clear()
    try:
        assert "上海" in reference.hub_stations()  # 基线不受损坏文件影响
    finally:
        reference.hub_stations.cache_clear()
