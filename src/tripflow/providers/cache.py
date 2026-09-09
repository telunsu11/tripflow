"""带 TTL 的 JSON 磁盘缓存：余票等时效数据的 checked_at 全链路透传。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class TTLCache:
    def __init__(self, directory: Path, default_ttl: int = 300) -> None:
        self._dir = Path(directory)
        self._ttl = default_ttl
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def get(self, key: str, *, ttl: int | None = None) -> tuple[Any, float] | None:
        """命中返回 (value, checked_at)；过期或损坏返回 None。"""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            checked_at = float(data["checked_at"])
            value = data["value"]
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            path.unlink(missing_ok=True)
            return None
        if time.time() - checked_at > (self._ttl if ttl is None else ttl):
            return None
        return value, checked_at

    def set(self, key: str, value: Any) -> float:
        checked_at = time.time()
        payload = json.dumps({"checked_at": checked_at, "value": value}, ensure_ascii=False)
        self._path(key).write_text(payload, "utf-8")
        return checked_at
