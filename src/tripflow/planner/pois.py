"""POI：LLM 提名（不出数字）→ 高德逐个校验坐标/类型/营业时间。"""

from __future__ import annotations

from ..llm import LLMClient, LLMError
from ..models import Poi
from ..providers.amap import AmapClient, Place
from .intake import extract_json_object

NOMINATE_SYSTEM = """你是旅行规划的「景点提名」模块。根据目的地城市、行程容量、节奏、必去点和天气，提名候选景点。

只输出 JSON：{{"pois": [{{"name": "正式景点名", "reason": "一句话推荐理由"}}]}}

要求：
- 必去点中只提名位于「本城市」的那些（若有），且放在最前面
- 提名数量以给定的「行程容量」为准，宁少勿多——超出容量的景点会在编排时被丢弃
- 被交通占用的日子不要提名当日景点；晚间时段只提名适合夜游的街区/夜景类地点
- 只提名广为人知的真实景点/街区/场馆，用官方正式名称，不要编造
- reason 可以给季节/天气适配建议，但不要出现价格、时长、评分等任何数字"""

# 高德 typecode：14 风景名胜 / 1101 公园 / 1102 风景名胜相关 / 1103 场馆 / 1105 度假村 / 0805 游乐园
TYPECODE_PREFIX_OK = ("14", "1101", "1102", "1103", "1105", "0805")
TYPE_KEYWORDS = (
    "风景名胜",
    "公园",
    "博物馆",
    "古镇",
    "步行街",
    "商业街",
    "广场",
    "游乐",
    "海洋馆",
    "水族",
    "寺",
    "塔",
    "纪念馆",
    "动物园",
    "植物园",
    "湿地",
    "湖泊",
    "江河",
)

STAY_RULES = [
    ("博物馆", 150),
    ("游乐", 240),
    ("海洋", 180),
    ("古镇", 180),
    ("风景名胜", 150),
    ("动物园", 150),
    ("植物园", 120),
    ("公园", 120),
    ("步行街", 120),
    ("商业街", 120),
    ("寺", 90),
    ("塔", 60),
    ("广场", 60),
]

ENTRY_FEE_RULES = [
    ("游乐", 200),
    ("海洋", 180),
    ("风景名胜", 80),
    ("古镇", 30),
    ("博物馆", 50),
    ("寺", 20),
]


def is_attraction(place: Place) -> bool:
    tc = place.typecode or ""
    if tc.startswith(TYPECODE_PREFIX_OK):
        return True
    text = f"{place.type}{place.name}"
    return any(k in text for k in TYPE_KEYWORDS)


def stay_minutes_for(place) -> int:
    text = f"{getattr(place, 'type', '')}{getattr(place, 'name', '')}"
    for key, minutes in STAY_RULES:
        if key in text:
            return minutes
    return 120


def entry_fee_for(place) -> int:
    text = f"{getattr(place, 'type', '')}{getattr(place, 'name', '')}"
    for key, fee in ENTRY_FEE_RULES:
        if key in text:
            return fee
    return 0


def nominate_pois(
    llm: LLMClient,
    city: str,
    capacity_text: str,
    suggested_count: int,
    *,
    pace: str = "均衡",
    must_visit: list[str] | None = None,
    weather_text: str = "",
    preferences: str = "",
) -> list[dict]:
    user = (
        f"本城市：{city}｜节奏：{pace}\n"
        f"行程容量：{capacity_text}\n"
        f"建议提名数量：约 {suggested_count} 个\n"
        f"用户必去点（只提名位于本城市的，若有）：{'、'.join(must_visit) if must_visit else '无'}\n"
        f"其他偏好：{preferences or '无'}\n"
        f"期间天气（仅覆盖近期，供参考）：{weather_text or '暂无'}"
    )
    raw = llm.chat(
        [
            {"role": "system", "content": NOMINATE_SYSTEM},
            {"role": "user", "content": user},
        ],
        model=llm.fast_model,  # 提名只需出名字和理由，用轻模型即可（未配置则同主模型）
        temperature=0.3,
    )
    data = extract_json_object(raw)
    pois = data.get("pois")
    if not isinstance(pois, list) or not pois:
        raise LLMError("景点提名为空")
    return pois


def _match_core(name: str, must_visit: list[str]) -> bool:
    return any(m in name or name in m for m in must_visit)


def validate_pois(
    amap: AmapClient, destination: str, nominated: list[dict], must_visit: list[str]
) -> tuple[list[Poi], list[str]]:
    """逐个提名 → 高德校验。返回 (校验通过的 POI 列表, 需人工确认的提示)。"""
    pois: list[Poi] = []
    seen_ids: set[str] = set()
    notes: list[str] = []

    for item in nominated:
        name = str(item.get("name", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not name:
            continue
        try:
            places = amap.place_text(name, city=destination, limit=3)
        except Exception:  # noqa: BLE001 - 单点校验失败不应中断整体
            notes.append(f"「{name}」在高德无校验结果，已跳过")
            continue
        # 选点：名称精确优先，其次第一个"景点类"结果
        picked = next((p for p in places if p.name == name and is_attraction(p)), None) or next(
            (p for p in places if is_attraction(p)), None
        )
        if picked is None:
            notes.append(f"「{name}」未匹配到景点类 POI（可能为商业场所或名称有误），已跳过")
            continue
        if picked.id in seen_ids:
            continue
        seen_ids.add(picked.id)
        try:
            detail = amap.place_detail(picked.id)
            location, opentime, rating = detail.location, detail.opentime, detail.rating
        except Exception:  # noqa: BLE001
            location, opentime, rating = picked.location, "", ""
        core = _match_core(name, must_visit) or _match_core(picked.name, must_visit)
        pois.append(
            Poi(
                name=picked.name,
                poi_id=picked.id,
                location=location,
                typecode=picked.typecode,
                type=picked.type,
                opentime=opentime,
                rating=rating,
                reason=reason,
                stay_minutes=stay_minutes_for(picked),
                core=core,
                entry_fee_estimate=entry_fee_for(picked),
            )
        )

    # 必去点若全部校验失败，明确提示（不静默丢弃）
    for m in must_visit:
        if not any(_match_core(p.name, [m]) for p in pois):
            notes.append(f"必去点「{m}」未能通过高德校验，请人工确认名称（方案中未包含）")
    return pois, notes
