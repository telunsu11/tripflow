"""TTL 缓存测试。"""

from tripflow.providers.cache import TTLCache


def test_roundtrip(tmp_path):
    cache = TTLCache(tmp_path, default_ttl=60)
    checked_at = cache.set("k", [{"train": "G1974"}])
    hit = cache.get("k")
    assert hit is not None
    value, ts = hit
    assert value == [{"train": "G1974"}]
    assert ts == checked_at


def test_expired(monkeypatch, tmp_path):
    import time

    cache = TTLCache(tmp_path, default_ttl=60)
    checked_at = cache.set("k", "v")
    monkeypatch.setattr(time, "time", lambda: checked_at + 61)
    assert cache.get("k") is None


def test_corrupt_file_ignored(tmp_path):
    cache = TTLCache(tmp_path, default_ttl=60)
    cache.set("k", "v")
    files = list(tmp_path.glob("*.json"))
    assert files, "set() 应已产生缓存文件"
    files[0].write_text("{not json", encoding="utf-8")
    assert cache.get("k") is None


def test_missing_key(tmp_path):
    cache = TTLCache(tmp_path, default_ttl=60)
    assert cache.get("nope") is None
