"""12306 provider：默认经 npx 拉起社区 MCP（stdio），或连接远程 SSE 端点。

要求服务端暴露与 12306-mcp 同名的工具：
get-current-date / get-station-code-of-citys / get-tickets /
get-interline-tickets / get-train-route-stations
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Self

from ..config import Settings


class RailError(RuntimeError):
    pass


def normalize_num(num: Any) -> tuple[bool, int | None]:
    """余票词表归一化：「有」→(True, None 不限)、数字→(n>0, n)、「无」等→(False, 0)。"""
    s = str(num).strip()
    if s == "有":
        return True, None
    try:
        n = int(s)
        return n > 0, n
    except ValueError:
        return False, 0


@dataclass
class SeatPrice:
    name: str
    short: str
    num_raw: str
    available: bool
    count: int | None
    price: float | None
    discount: int | None

    @property
    def display(self) -> str:
        if not self.available:
            return "无"
        qty = "有" if self.count is None else f"{self.count}张"
        price = f" ¥{self.price:g}" if self.price else ""
        return f"{qty}{price}"


@dataclass
class TrainTicket:
    train_code: str
    from_station: str
    to_station: str
    from_telecode: str = ""
    to_telecode: str = ""
    start_date: str = ""
    arrive_date: str = ""
    start_time: str = ""
    arrive_time: str = ""
    lishi: str = ""
    seats: list[SeatPrice] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def seat(self, name: str) -> SeatPrice | None:
        return next((s for s in self.seats if s.name == name), None)

    @property
    def cheapest(self) -> SeatPrice | None:
        candidates = [s for s in self.seats if s.available and s.price]
        return min(candidates, key=lambda s: s.price or 0) if candidates else None


@dataclass
class TransferPlan:
    from_station: str
    middle_station: str
    to_station: str
    start_date: str = ""
    start_time: str = ""
    arrive_time: str = ""
    lishi: str = ""
    wait_time: str = ""
    same_station: bool = False
    same_train: bool = False
    legs: list[TrainTicket] = field(default_factory=list)


def parse_ticket(raw: dict) -> TrainTicket:
    seats = []
    for p in raw.get("prices", []):
        available, count = normalize_num(p.get("num"))
        price = p.get("price")
        seats.append(
            SeatPrice(
                name=str(p.get("seat_name", "")),
                short=str(p.get("short", "")),
                num_raw=str(p.get("num", "")),
                available=available,
                count=count,
                price=float(price) if price not in (None, "", "--") else None,
                discount=p.get("discount"),
            )
        )
    return TrainTicket(
        train_code=str(raw.get("start_train_code", "")),
        from_station=str(raw.get("from_station", "")),
        to_station=str(raw.get("to_station", "")),
        from_telecode=str(raw.get("from_station_telecode", "")),
        to_telecode=str(raw.get("to_station_telecode", "")),
        start_date=str(raw.get("start_date", "")),
        arrive_date=str(raw.get("arrive_date", "")),
        start_time=str(raw.get("start_time", "")),
        arrive_time=str(raw.get("arrive_time", "")),
        lishi=str(raw.get("lishi", "")),
        seats=seats,
        flags=[str(f) for f in (raw.get("dw_flag") or [])],
    )


def parse_transfer(raw: dict) -> TransferPlan:
    return TransferPlan(
        from_station=str(raw.get("from_station_name", "")),
        middle_station=str(raw.get("middle_station_name", "")),
        to_station=str(raw.get("end_station_name", "")),
        start_date=str(raw.get("start_date", "")),
        start_time=str(raw.get("start_time", "")),
        arrive_time=str(raw.get("arrive_time", "")),
        lishi=str(raw.get("lishi", "")),
        wait_time=str(raw.get("wait_time", "")),
        same_station=bool(raw.get("same_station")),
        same_train=bool(raw.get("same_train")),
        legs=[parse_ticket(t) for t in raw.get("ticketList", [])],
    )


def resolve_stdio_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """解析命令为可执行路径；Windows 下 npm shim（.cmd/.bat）需经 cmd /c 启动
    （MCP Python SDK 在 Windows 直接 spawn 批处理脚本的已知问题）。"""
    resolved = shutil.which(command)
    if resolved is None:
        raise RailError(
            f"找不到命令 {command!r}：默认 12306 MCP 依赖 Node.js 的 npx"
            "（https://nodejs.org/），或通过 RAIL_MCP_COMMAND / RAIL_MCP_URL 换用其它服务端"
        )
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return shutil.which("cmd") or "cmd", ["/c", resolved, *args]
    return resolved, args


class RailSession:
    """一次 CLI 调用内复用的 12306 MCP 会话（async context manager）。"""

    TOOL_DATE = "get-current-date"
    TOOL_CITY_CODES = "get-station-code-of-citys"
    TOOL_TICKETS = "get-tickets"
    TOOL_INTERLINE = "get-interline-tickets"
    TOOL_ROUTE = "get-train-route-stations"

    def __init__(self, settings: Settings, call_timeout: float = 90.0) -> None:
        self._s = settings
        self._timeout = call_timeout
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    async def __aenter__(self) -> Self:
        from mcp import ClientSession

        self._stack = AsyncExitStack()
        try:
            if self._s.rail_mcp_mode == "sse":
                if not self._s.rail_mcp_url:
                    raise RailError("RAIL_MCP_MODE=sse 但未配置 RAIL_MCP_URL")
                from mcp.client.sse import sse_client

                read, write = await self._stack.enter_async_context(
                    sse_client(self._s.rail_mcp_url)
                )
            else:
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                command, extra_args = resolve_stdio_command(
                    self._s.rail_mcp_command, shlex.split(self._s.rail_mcp_args)
                )
                params = StdioServerParameters(command=command, args=extra_args)
                # 默认把服务端 stderr（启动横幅等噪声）导入 devnull；TRIPFLOW_DEBUG=1 时直通终端。
                # errlog 必须是带 fileno 的真实文件（SDK 借它重定向子进程 stderr），生命周期与会话一致。
                if os.environ.get("TRIPFLOW_DEBUG"):
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                else:
                    devnull = open(os.devnull, "w")  # noqa: ASYNC230, SIM115 - 由下方 AsyncExitStack 关闭
                    self._stack.callback(devnull.close)
                    read, write = await self._stack.enter_async_context(
                        stdio_client(params, errlog=devnull)
                    )
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    # ---- 底层 ----
    @staticmethod
    def _text(result: Any) -> str:
        parts = [getattr(c, "text", "") for c in getattr(result, "content", [])]
        return "\n".join(p for p in parts if p)

    async def _call(self, tool: str, args: dict | None = None) -> Any:
        if self._session is None:
            raise RailError("会话未打开（需 async with RailSession(...)）")
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, args or {}), timeout=self._timeout
            )
        except TimeoutError as exc:
            raise RailError(f"调用 {tool} 超时（{self._timeout:.0f}s）") from exc
        text = self._text(result)
        if getattr(result, "isError", False):
            raise RailError(f"{tool} 返回错误: {text[:300]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def list_tool_names(self) -> list[str]:
        if self._session is None:
            raise RailError("会话未打开")
        tools = await self._session.list_tools()
        return [t.name for t in tools.tools]

    # ---- 业务 ----
    async def current_date(self) -> str:
        return str(await self._call(self.TOOL_DATE)).strip().strip('"')

    async def city_codes(self, *cities: str) -> dict:
        data = await self._call(self.TOOL_CITY_CODES, {"citys": "|".join(cities)})
        return data if isinstance(data, dict) else {}

    async def tickets_raw(
        self,
        date: str,
        from_station: str,
        to_station: str,
        *,
        filter_flags: str = "",
        sort: str = "",
        limit: int = 0,
    ) -> list[dict]:
        args: dict[str, Any] = {
            "date": date,
            "fromStation": from_station,
            "toStation": to_station,
            "format": "json",
        }
        if filter_flags:
            args["trainFilterFlags"] = filter_flags
        if sort:
            args["sortFlag"] = sort
        if limit:
            args["limitedNum"] = limit
        data = await self._call(self.TOOL_TICKETS, args)
        return data if isinstance(data, list) else []

    async def tickets(
        self, date: str, from_station: str, to_station: str, **kw: Any
    ) -> list[TrainTicket]:
        return [
            parse_ticket(r) for r in await self.tickets_raw(date, from_station, to_station, **kw)
        ]

    async def interline_raw(
        self,
        date: str,
        from_station: str,
        to_station: str,
        *,
        middle: str = "",
        sort: str = "duration",
        limit: int = 10,
    ) -> list[dict]:
        args: dict[str, Any] = {
            "date": date,
            "fromStation": from_station,
            "toStation": to_station,
            "sortFlag": sort,
            "limitedNum": limit,
            "format": "json",
        }
        if middle:
            args["middleStation"] = middle
        data = await self._call(self.TOOL_INTERLINE, args)
        return data if isinstance(data, list) else []

    async def interline(
        self, date: str, from_station: str, to_station: str, **kw: Any
    ) -> list[TransferPlan]:
        return [
            parse_transfer(r)
            for r in await self.interline_raw(date, from_station, to_station, **kw)
        ]
