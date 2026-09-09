"""需求解析：自然语言 → TripRequest。LLM 只做理解，日期/预算假设全部显式化。"""

from __future__ import annotations

import re

from pydantic import ValidationError

from ..llm import LLMClient, LLMError
from ..models import TripRequest
from ..util import parse_date

INTAKE_SYSTEM = """你是旅行规划助手的「需求解析」模块，把用户输入解析为 JSON（只输出 JSON，不要任何其他文字）。

规则：
- 今天日期：{today}，所有相对日期以它为基准换算
- depart_date / return_date 格式 yyyy-MM-dd；"玩 N 天"表示 return_date = 出发日 + N - 1 天
- 节假日（国庆、五一、中秋等）换算为当年具体日期
- 多目的地行程：途经城市按顺序放入 waypoints（不含出发地 origin 与最终目的地 destination）；
  城市总数（含目的地）不能超过行程天数，否则报错规则不适用、正常解析
- 预算没有说明人均还是总计时：默认按"人均"填 budget_per_person，并在 assumptions 里写明"预算默认按人均处理"
- 没提到的信息用合理默认值，并把每个假设写进 assumptions（如：默认 1 人出行）
- must_visit 只放用户明确要求必去的地点名
- 用户指定了出发时段（如"下午出发"、"晚上到"）时：depart_after 填对应 HH:MM 下限
  （下午=13:00，傍晚=17:00，晚上=19:00）；未指定留空

JSON 字段：
origin, destination, waypoints(字符串数组，可为空), depart_date, return_date,
depart_after(HH:MM 或空), travelers,
budget_per_person, budget_total, pace("省钱"|"均衡"|"松弛"),
must_visit(字符串数组), preferences(其他偏好一句话), assumptions(字符串数组)"""


def extract_json_object(text: str) -> dict:
    """从模型输出中提取第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    cleaned = text.replace("```json", "").replace("```", "")
    start = cleaned.find("{")
    if start < 0:
        raise LLMError(f"输出中找不到 JSON 对象: {text[:120]}")
    import json

    obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(obj, dict):
        raise LLMError("JSON 顶层不是对象")
    return obj


def parse_request(llm: LLMClient, user_text: str, today: str) -> TripRequest:
    system = INTAKE_SYSTEM.format(today=today)
    feedback = ""
    last_err: Exception | None = None
    for _ in range(3):
        user = user_text if not feedback else f"{user_text}\n\n上次输出的问题，请修正：{feedback}"
        try:
            raw = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
            )
            data = extract_json_object(raw)
            req = TripRequest(**data)
            _validate_semantics(req, today)
            return req
        except (ValidationError, LLMError, ValueError, KeyError, TypeError) as exc:
            last_err = exc
            feedback = str(exc)[:400]
    raise LLMError(f"需求解析失败（已重试 3 次）: {last_err}")


def _validate_semantics(req: TripRequest, today: str) -> None:
    d0 = parse_date(req.depart_date)
    d1 = parse_date(req.return_date)
    if d1 < d0:
        raise ValueError("return_date 不能早于 depart_date")
    if d0 < parse_date(today):
        raise ValueError("depart_date 不能早于今天")
    if req.days > 15:
        raise ValueError("行程天数超过 15 天，请拆分规划")
    if len(req.cities) > req.days:
        raise ValueError(f"{len(req.cities)} 个城市至少需要同样多的天数（当前 {req.days} 天）")
    if req.depart_after and not re.fullmatch(r"\d{1,2}:\d{2}", req.depart_after):
        raise ValueError("depart_after 需为 HH:MM 格式")
