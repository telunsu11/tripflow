"""核心数据模型：TripRequest（输入）与 Itinerary（交付物，含证据链）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .util import date_range, parse_date


class TripRequest(BaseModel):
    origin: str
    destination: str
    depart_date: str
    return_date: str
    travelers: int = Field(default=1, ge=1)
    budget_per_person: float | None = None
    budget_total: float | None = None
    pace: str = "均衡"
    must_visit: list[str] = Field(default_factory=list)
    waypoints: list[str] = Field(default_factory=list)  # 途经城市（按顺序），单目的地为空
    preferences: str = ""
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("origin", "destination")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("城市不能为空")
        return v.strip()

    @property
    def days(self) -> int:
        return len(date_range(self.depart_date, self.return_date))

    @property
    def nights(self) -> int:
        return (parse_date(self.return_date) - parse_date(self.depart_date)).days

    @property
    def route(self) -> list[str]:
        """完整城市序列：出发地 → 途经 → 目的地。"""
        return [self.origin, *self.waypoints, self.destination]

    @property
    def cities(self) -> list[str]:
        """实际到访城市（途经 + 目的地）。"""
        return [*self.waypoints, self.destination]

    @property
    def budget_effective_total(self) -> float | None:
        if self.budget_total is not None:
            return self.budget_total
        if self.budget_per_person is not None:
            return self.budget_per_person * self.travelers
        return None


class Poi(BaseModel):
    name: str
    poi_id: str
    location: str  # lng,lat
    typecode: str = ""
    type: str = ""
    opentime: str = ""
    rating: str = ""
    reason: str = ""
    stay_minutes: int = 120
    core: bool = False
    entry_fee_estimate: int = 0  # 类型估算，仅进预算"估算"项


class CommuteLeg(BaseModel):
    from_name: str
    to_name: str
    mode: str  # 公交 / 步行
    minutes: int
    distance_m: int = 0
    lines: list[str] = Field(default_factory=list)


class VisitItem(BaseModel):
    poi: Poi
    start: str  # HH:MM
    end: str  # HH:MM


class DayPlan(BaseModel):
    date: str
    city: str = ""
    weather: str = ""
    items: list[VisitItem] = Field(default_factory=list)
    legs: list[CommuteLeg] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TransitChoice(BaseModel):
    kind: str  # direct | interline
    date: str
    code: str
    from_city: str = ""
    to_city: str = ""
    from_station: str = ""
    to_station: str = ""
    depart_time: str
    arrive_time: str
    summary: str
    seat_name: str = ""
    price_per_person: float | None = None
    seats_ok: bool
    transfer_note: str = ""
    checked_at: float


class CityBlock(BaseModel):
    """一个城市的停留块：start 当天到达、end 当天（可能）离开去下一城。"""

    city: str
    start_date: str
    end_date: str
    arrive_leg: TransitChoice | None = None  # 到达该城的跨城段
    depart_leg: TransitChoice | None = None  # 离开该城的跨城段（末城=返程）

    @property
    def dates(self) -> list[str]:
        return date_range(self.start_date, self.end_date)


class TransitOutcome(BaseModel):
    legs: list[TransitChoice | None] = Field(default_factory=list)  # 按行程顺序（失败段为 None）
    blocks: list[CityBlock] = Field(default_factory=list)
    comparison: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def go(self) -> TransitChoice | None:
        return self.legs[0] if self.legs else None

    @property
    def back(self) -> TransitChoice | None:
        return self.legs[-1] if self.legs else None


class HotelPick(BaseModel):
    """住宿候选（高德 POI 级，仅信息展示，不含预订）。"""

    city: str
    name: str
    poi_id: str
    location: str
    address: str = ""
    rating: str = ""
    cost: str = ""  # 高德返回的参考价（可能为空）
    near: str = ""  # 锚点说明（如“距 拙政园 3km 内”）


class BudgetItem(BaseModel):
    category: str
    amount: float
    kind: str  # real（实价） | estimate（估算）
    note: str = ""


class Feasibility(BaseModel):
    status: str  # FEASIBLE | FEASIBLE_WITH_RISK | INFEASIBLE
    issues: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Itinerary(BaseModel):
    request: TripRequest
    legs: list[TransitChoice | None] = Field(default_factory=list)
    comparison: list[dict] = Field(default_factory=list)
    days: list[DayPlan] = Field(default_factory=list)
    hotels: list[HotelPick] = Field(default_factory=list)
    budget: list[BudgetItem] = Field(default_factory=list)
    total_cost: float = 0
    feasibility: Feasibility
    map_uri: str = ""
    generated_at: str = ""

    @property
    def go(self) -> TransitChoice | None:
        return self.legs[0] if self.legs else None

    @property
    def back(self) -> TransitChoice | None:
        return self.legs[-1] if self.legs else None
