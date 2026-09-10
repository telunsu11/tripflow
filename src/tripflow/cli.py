"""tripflow CLI：doctor 环境自检 / setup 配置向导 / tickets 余票查询 / plan 端到端规划。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Settings, get_settings
from .llm import LLMClient, LLMError
from .providers.amap import AmapClient, AmapError
from .providers.cache import TTLCache
from .providers.rail import RailError, RailSession, TrainTicket, parse_ticket

app = typer.Typer(
    help="tripflow —— 输入预算和日期，产出数字真实、可执行、可分享的行程单",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

AMAP_KEY_URL = "https://console.amap.com/dev/key/app"
LLM_HINT = (
    "LLM 配置：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（智谱 https://open.bigmodel.cn 、"
    "DeepSeek https://platform.deepseek.com ，或本地 Ollama http://localhost:11434/v1 ）"
)


# ============================================================
# doctor：环境自检
# ============================================================
@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    critical: bool = True


def _check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    detail = f"{v.major}.{v.minor}.{v.micro}" + (
        "" if ok else "（需要 ≥ 3.11；建议用 uv run 自动管理版本）"
    )
    return Check("Python", "ok" if ok else "fail", detail)


def _check_llm(s: Settings) -> Check:
    ok, msg = LLMClient(s).ping()
    return Check("LLM 端点", "ok" if ok else "fail", msg)


def _check_amap(s: Settings) -> Check:
    if not s.amap_api_key:
        return Check("高德 REST", "fail", f"未配置 AMAP_API_KEY（{AMAP_KEY_URL} 创建 Key）")
    try:
        with AmapClient(s.amap_api_key) as client:
            casts = client.weather("北京")
        today = casts[0] if casts else None
        detail = "Key 有效" + (f"，北京今日: {today.dayweather} {today.daytemp}°C" if today else "")
        return Check("高德 REST", "ok", detail)
    except AmapError as exc:
        return Check("高德 REST", "fail", f"{exc}（确认 Key 类型为「Web服务」: {AMAP_KEY_URL}）")
    except Exception as exc:  # noqa: BLE001 - doctor 要给出可读原因而非裸异常
        return Check("高德 REST", "fail", f"{type(exc).__name__}: {exc}")


def _check_amap_mcp(s: Settings) -> Check:
    """非关键检查：高德云端 MCP 是行程地图（schema_personal_map）的生成通道。"""

    async def run() -> list[str]:
        from mcp import ClientSession

        try:  # mcp >= 2 命名为 streamable_http_client；1.x 为 streamablehttp_client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:  # pragma: no cover - 仅旧版 SDK 走到这里
            from mcp.client.streamable_http import (  # type: ignore[attr-defined]
                streamablehttp_client as streamable_http_client,
            )

        async with streamable_http_client(s.amap_mcp_endpoint) as streams:
            read, write = streams[0], streams[1]  # 2.x 双元组 / 1.x 三元组
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    try:
        tools = asyncio.run(asyncio.wait_for(run(), timeout=30))
        has_map = "maps_schema_personal_map" in tools
        detail = f"可用，{len(tools)} 个工具" + (
            "，含行程地图生成" if has_map else "，但未见 maps_schema_personal_map"
        )
        return Check("高德云端 MCP", "ok" if has_map else "warn", detail, critical=False)
    except ImportError:
        return Check(
            "高德云端 MCP", "warn", "当前 mcp SDK 不含 streamable http 客户端，跳过", critical=False
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            "高德云端 MCP",
            "warn",
            f"连接失败: {type(exc).__name__}（行程地图生成依赖它，可稍后重试）",
            critical=False,
        )


def _check_meituan(s: Settings) -> Check:
    """非关键：美团优惠查询（Token 即配置即用，真实查询较慢故 doctor 只做静态检查）。"""
    if not s.meituan_ht_token:
        return Check(
            "美团优惠(可选)",
            "warn",
            "未配置 MEITUAN_HT_TOKEN——tripflow deals 不可用（可选功能，不影响规划）",
            critical=False,
        )
    if shutil.which("npx") is None:
        return Check(
            "美团优惠(可选)", "warn", "已配置 Token 但找不到 npx（需 Node.js）", critical=False
        )
    return Check(
        "美团优惠(可选)",
        "ok",
        "Token 已配置（静态检查；用 tripflow deals 实测查询）",
        critical=False,
    )


def _check_rail(s: Settings) -> Check:
    async def run() -> tuple[int, str]:
        async with RailSession(s) as rail:
            names = await rail.list_tool_names()
            if "get-tickets" not in names:
                raise RailError(
                    f"服务端缺少 get-tickets 工具（当前暴露: {', '.join(names[:5])}…）；"
                    "自定义服务端需提供与 12306-mcp 同名的工具"
                )
            today = await rail.current_date()
            return len(names), today

    try:
        count, today = asyncio.run(asyncio.wait_for(run(), timeout=150))
        return Check("12306 MCP", "ok", f"连接正常（{count} 个工具），12306 今日: {today}")
    except RailError as exc:
        return Check("12306 MCP", "fail", str(exc))
    except Exception as exc:  # noqa: BLE001
        return Check("12306 MCP", "fail", f"{type(exc).__name__}: {exc}")


@app.command()
def doctor(
    mcp: bool = typer.Option(
        True, "--mcp/--no-mcp", help="是否实测 MCP 连接（12306 本地拉起 + 高德云端，较慢）"
    ),
    allow_missing: Annotated[
        bool, typer.Option("--allow-missing", help="关键项缺失时不返回非零退出码（CI 冒烟用）")
    ] = False,
) -> None:
    """环境自检：每项给出状态与「去哪补」。"""
    s = get_settings()
    console.rule(f"[bold]tripflow doctor[/] v{__version__}")
    checks = [_check_python(), _check_llm(s), _check_amap(s)]
    if mcp:
        checks.append(_check_amap_mcp(s))
        checks.append(_check_rail(s))
        checks.append(_check_meituan(s))

    icons = {"ok": "[green]✅[/]", "warn": "[yellow]⚠️ [/]", "fail": "[red]❌[/]"}
    table = Table(show_header=True, header_style="bold")
    table.add_column("检查项", style="bold")
    table.add_column("状态", justify="center")
    table.add_column("说明", overflow="fold")
    for c in checks:
        table.add_row(c.name, icons[c.status], c.detail)
    console.print(table)

    failed = [c for c in checks if c.critical and c.status != "ok"]
    if not failed:
        console.print(
            "[green]全绿，可以出发。[/]试试: [bold]uv run tripflow tickets 上海 成都 2026-09-12[/]"
        )
        return
    console.print(f"[red]{len(failed)} 项关键检查未就绪：[/]")
    for c in failed:
        if c.name == "LLM 端点":
            console.print(f"  · {LLM_HINT}")
        elif c.name.startswith("高德"):
            console.print(f"  · 到 {AMAP_KEY_URL} 创建「Web服务」类型 Key，填入 AMAP_API_KEY")
        elif c.name.startswith("12306"):
            console.print(
                "  · 默认经 npx 拉起 12306-mcp：先安装 Node.js（https://nodejs.org/），"
                "或用 RAIL_MCP_* 环境变量换用其它服务端（见 .env.example）"
            )
    if not allow_missing:
        raise typer.Exit(1)


# ============================================================
# setup：交互式配置向导
# ============================================================
@app.command()
def setup() -> None:
    """交互式配置：收 Key → 即时验证 → 写入 .env（权限 600）。"""
    s = get_settings()
    console.rule("[bold]tripflow setup[/]")
    console.print("只需两个 Key，两分钟搞定。直接回车保留[]中的默认值。\n")

    base_url = typer.prompt("LLM_BASE_URL（OpenAI 兼容端点）", default=s.llm_base_url)
    api_key = typer.prompt("LLM_API_KEY（输入隐藏）", default="", hide_input=True)
    model = typer.prompt(
        "LLM_MODEL（示例 glm-4.7 / deepseek-chat，以你的账号为准）", default=s.llm_model
    )
    amap_key = typer.prompt(
        f"AMAP_API_KEY（高德「Web服务」Key，{AMAP_KEY_URL} 申请；输入隐藏）",
        default="",
        hide_input=True,
    )

    llm_ok, llm_msg = True, "（未填写，跳过验证）"
    if api_key:
        probe = Settings(
            _env_file=None, llm_base_url=base_url, llm_api_key=api_key, llm_model=model
        )
        llm_ok, llm_msg = LLMClient(probe).ping()

    amap_ok, amap_msg = True, "（未填写，跳过验证）"
    if amap_key:
        try:
            with AmapClient(amap_key) as client:
                client.weather("北京")
            amap_msg = "Key 有效"
        except Exception as exc:  # noqa: BLE001
            amap_ok = False
            amap_msg = f"{exc}"

    lines = [f"LLM_BASE_URL={base_url}"]
    if api_key:
        lines.append(f"LLM_API_KEY={api_key}")
    if model:
        lines.append(f"LLM_MODEL={model}")
    if amap_key:
        lines.append(f"AMAP_API_KEY={amap_key}")
    env_path = Path(".env")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)

    console.print(f"\n已写入 [bold]{env_path.resolve()}[/]（权限 600，已被 gitignore）")
    console.print(f"  LLM : {'✅' if llm_ok else '❌'} {llm_msg}")
    console.print(f"  高德: {'✅' if amap_ok else '❌'} {amap_msg}")
    console.print("\n最后跑一次完整自检: [bold]uv run tripflow doctor[/]")


# ============================================================
# tickets：直达余票查询
# ============================================================
def _seat_cell(t: TrainTicket, name: str) -> str:
    seat = t.seat(name)
    return seat.display if seat else "—"


@app.command()
def tickets(
    from_city: str = typer.Argument(..., help="出发城市/站名，如 上海"),
    to_city: str = typer.Argument(..., help="到达城市/站名，如 成都"),
    date: str = typer.Argument(None, help="日期 yyyy-MM-dd；留空自动取 12306 今日"),
    type_filter: str = typer.Option(
        "G", "--type", "-t", help="车次筛选 G/D/Z/T/K/O 组合；传空字符串查全部"
    ),
    refresh: bool = typer.Option(False, "--refresh", help="忽略缓存强制重查"),
) -> None:
    """查直达余票（带 TTL 缓存；M1 的 plan 将并入中转比对）。"""
    s = get_settings()
    s.ensure_dirs()
    cache = TTLCache(s.cache_dir, default_ttl=s.trip_ticket_ttl)

    def cache_key(d: str) -> str:
        return f"tickets:{d}:{from_city}->{to_city}:{type_filter}"

    data: list[dict] | None = None
    checked_at: float | None = None
    if date and not refresh:
        hit = cache.get(cache_key(date))
        if hit is not None:
            data, checked_at = hit

    if data is None:

        async def run() -> tuple[str, list[dict]]:
            async with RailSession(s) as rail:
                d = date or await rail.current_date()
                raw = await rail.tickets_raw(
                    d, from_city, to_city, filter_flags=type_filter, sort="startTime"
                )
                return d, raw

        try:
            date, data = asyncio.run(asyncio.wait_for(run(), timeout=150))
        except RailError as exc:
            console.print(f"[red]12306 查询失败:[/] {exc}")
            raise typer.Exit(1) from exc
        checked_at = cache.set(cache_key(date), data)

    parsed = [parse_ticket(r) for r in data]
    table = Table(
        title=f"{date}  {from_city} → {to_city}  直达余票（{len(parsed)} 班）",
        show_lines=False,
        header_style="bold",
    )
    table.add_column("车次", style="bold cyan")
    table.add_column("出发站 → 到达站")
    table.add_column("时刻")
    table.add_column("历时", justify="right")
    table.add_column("二等座")
    table.add_column("一等座")
    table.add_column("商务座")
    table.add_column("标签", style="dim")
    for t in parsed:
        table.add_row(
            t.train_code,
            f"{t.from_station} → {t.to_station}",
            f"{t.start_time} → {t.arrive_time}",
            t.lishi,
            _seat_cell(t, "二等座"),
            _seat_cell(t, "一等座"),
            _seat_cell(t, "商务座"),
            "、".join(t.flags),
        )
    console.print(table)

    if not parsed:
        console.print("[yellow]无直达班次[/]——M1 的 plan 会自动展开中转方案比对。")
    when = time.strftime("%H:%M:%S", time.localtime(checked_at))
    console.print(f"[dim]查询时间 {when}｜缓存 {s.trip_ticket_ttl}s（--refresh 强制重查）[/]")


# ============================================================
# plan：端到端行程规划
# ============================================================
def _step(idx: int, total: int, text: str) -> None:
    console.print(f"[bold cyan]\\[{idx}/{total}][/bold cyan] {text}")


@app.command()
def plan(
    request: str = typer.Argument(
        ...,
        help='自然语言需求，例："9月12日到14日从上海去成都和重庆，2人，人均3000，必去宽窄巷子"',
    ),
    out_dir: Annotated[Path | None, typer.Option(help="输出目录（默认取 TRIP_OUTPUT_DIR）")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过交互追问（CI/脚本场景）")] = False,
    no_hotels: Annotated[bool, typer.Option("--no-hotels", help="跳过住宿搜索与锚点编排")] = False,
    with_hotel_prices: Annotated[
        bool, typer.Option(
            "--with-hotel-prices",
            help="经美团查每城住宿报价（需 MEITUAN_HT_TOKEN，每城约多等 1–2 分钟）",
        )
    ] = False,
    compare_styles: Annotated[
        bool, typer.Option("--compare-styles", help="附加紧凑/休闲两种编排方案的对比")
    ] = False,
) -> None:
    """端到端规划：需求 → 交通 → POI → 逐日编排 → 住宿选店 → 预算 → 可行性 → 行程单+地图+日历。"""
    from .planner.pipeline import run_plan

    s = get_settings()
    if not s.llm_api_key or not s.amap_api_key:
        console.print(
            "[red]缺少 LLM_API_KEY 或 AMAP_API_KEY[/]，先运行: [bold]uv run tripflow setup[/]"
        )
        raise typer.Exit(1)

    def interactive(req) -> None:
        """唯一没有安全默认值的输入：预算按人均还是总价。"""
        if (
            not yes
            and sys.stdin.isatty()
            and req.budget_per_person is not None
            and any("预算默认按人均" in a for a in req.assumptions)
        ):
            console.print(f"[yellow]预算按 [bold]人均 ¥{req.budget_per_person:g}[/bold] 处理？[/]")
            if not typer.confirm("  Enter=确认人均 / 输入 n 改为总价", default=True):
                req.budget_total = req.budget_per_person * req.travelers
                req.budget_per_person = None
                req.assumptions = [a for a in req.assumptions if "预算默认按人均" not in a]
                req.assumptions.append(f"预算已确认为总价 ¥{req.budget_total:g}")

    def step(idx: int, total: int, text: str) -> None:
        _step(idx, total, text)

    try:
        result = run_plan(
            request,
            settings=s,
            out_dir=out_dir,
            step_cb=step,
            interactive_cb=interactive,
            include_hotels=not no_hotels,
            with_hotel_prices=with_hotel_prices,
            compare_styles=compare_styles,
        )
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    status_color = {"FEASIBLE": "green", "FEASIBLE_WITH_RISK": "yellow", "INFEASIBLE": "red"}
    console.rule("[bold]行程单生成完毕[/]")
    for line in result.summary_lines():
        if line.startswith("可行性"):
            console.print(
                f"[{status_color.get(result.itinerary.feasibility.status, 'white')}]{line}[/]"
            )
        elif line.startswith("⚠️"):
            console.print(f"  [yellow]{line}[/]")
        else:
            console.print(line)
    console.print(f"文件: [link={result.md_path}]{result.md_path}[/link]")
    console.print(f"      {result.html_path}（单文件可分享）")
    console.print(f"      {result.json_path}")
    console.print(f"      {result.ical_path}（日历导入）")
    if result.qr_path:
        console.print(f"      {result.qr_path}（高德 App 扫码打开行程地图）")
    if result.itinerary.map_uri:
        console.print(f"地图: [link={result.itinerary.map_uri}]{result.itinerary.map_uri}[/link]")


# ============================================================
# ical：导出日历
# ============================================================
@app.command()
def ical(
    target: Annotated[Path, typer.Argument(help="行程单 JSON 路径")],
    out: Annotated[Path | None, typer.Option("--out", help="输出 .ics 路径（默认同名）")] = None,
) -> None:
    """把既有行程单导出为 .ics 日历（交通段 + 行程点）。"""
    import json as _json

    from .deliver.ical import write_ical
    from .models import Itinerary

    itinerary = Itinerary.model_validate(_json.loads(target.read_text("utf-8")))
    path = out or target.with_suffix(".ics")
    write_ical(itinerary, path)
    n_legs = sum(1 for leg in itinerary.legs if leg)
    n_visits = sum(len(d.items) for d in itinerary.days)
    console.print(f"已导出 [bold]{path}[/]（{n_legs} 个交通段 + {n_visits} 个行程点）")


# ============================================================
# watch：余票监控
# ============================================================
def _guard_check(itinerary, *, final_24h: bool) -> tuple[list, list]:
    """天气预警 + （最终24h）营业时间复查；天气更新直接改写 day.weather 由调用方处理。"""
    from .planner.guard import opentime_changes, weather_alerts
    from .providers.amap import AmapClient

    alerts: list[str] = []
    updates: list = []
    with AmapClient(get_settings().amap_api_key) as amap:
        forecasts_by_city = {}
        for city in {d.city for d in itinerary.days if d.city}:
            try:
                forecasts_by_city[city] = amap.weather(city)
            except Exception:  # noqa: BLE001, S112 - 单城失败跳过
                continue
        alerts, updates = weather_alerts(itinerary.days, forecasts_by_city)
        if final_24h:
            alerts = alerts + opentime_changes(itinerary, amap)
    return alerts, updates


@app.command()
def watch(
    target: Annotated[Path, typer.Argument(help="行程单 JSON 路径")],
    interval: Annotated[
        int, typer.Option("--interval", "-i", min=60, help="检查间隔秒数（默认 1800）")
    ] = 1800,
    once: Annotated[bool, typer.Option("--once", help="只检查一次（适合 cron）")] = False,
    webhook: Annotated[
        str, typer.Option("--webhook", help="变化时 POST 通知的 URL（默认取 WATCH_WEBHOOK_URL）")
    ] = "",
    add_train: Annotated[str, typer.Option("--add-train", help="加入候补车次号（配合下方三项）")] = "",
    train_date: Annotated[str, typer.Option("--train-date", help="候补车次日期 yyyy-MM-dd")] = "",
    train_from: Annotated[str, typer.Option("--train-from", help="候补出发城市")] = "",
    train_to: Annotated[str, typer.Option("--train-to", help="候补到达城市")] = "",
    remove_train: Annotated[str, typer.Option("--remove-train", help="移除候补：车次@日期")] = "",
) -> None:
    """监控行程车次余票/票价 + 候补车次放票；可选守护（天气/营业时间）。"""
    import json as _json

    from .models import Itinerary
    from .planner.guard import is_final_24h
    from .planner.watch import diff_snapshots, notify_webhook, snapshot, watch_once

    s = get_settings()
    if not s.amap_api_key and not s.rail_mcp_url:
        pass  # watch 只依赖 12306 MCP
    itinerary = Itinerary.model_validate(_json.loads(target.read_text("utf-8")))
    from .models import TrainWatch

    if add_train:
        if not (train_date and train_from and train_to):
            console.print("[red]--add-train 需同时提供 --train-date/--train-from/--train-to[/]")
            raise typer.Exit(1)
        if any(w.code == add_train and w.date == train_date for w in itinerary.watch_extra):
            console.print(f"[yellow]{add_train}@{train_date} 已在候补列表[/]")
        else:
            itinerary.watch_extra.append(
                TrainWatch(code=add_train, date=train_date, from_city=train_from, to_city=train_to)
            )
        target.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"✅ 候补已加入：{add_train} {train_date} {train_from}→{train_to}"
                      f"（共 {len(itinerary.watch_extra)} 个候补）")
        raise typer.Exit(0)
    if remove_train:
        code, _, d = remove_train.partition("@")
        before = len(itinerary.watch_extra)
        itinerary.watch_extra = [
            w for w in itinerary.watch_extra if not (w.code == code and (not d or w.date == d))
        ]
        target.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"已移除 {before - len(itinerary.watch_extra)} 个候补")
        raise typer.Exit(0)
    hook = webhook or os.environ.get("WATCH_WEBHOOK_URL", "")
    console.print(
        f"开始监控 [bold]{target.name}[/]"
        f"（{sum(1 for leg in itinerary.legs if leg)} 个车次段，"
        f"每 {interval}s 检查，Ctrl-C 退出）"
    )

    async def check() -> tuple[Itinerary, list[str]]:
        from .planner.watch import check_extra_trains

        async with RailSession(s) as rail:
            it2 = await watch_once(rail, itinerary)
            extra_changes = await check_extra_trains(rail, it2) if it2.watch_extra else []
            return it2, extra_changes

    prev = snapshot(itinerary)
    while True:
        try:
            itinerary, extra_changes = asyncio.run(asyncio.wait_for(check(), timeout=240))
        except RailError as exc:
            console.print(f"[red]检查失败:[/] {exc}")
        else:
            now = time.strftime("%H:%M:%S")
            changes = diff_snapshots(prev, snapshot(itinerary))
            changes.extend(extra_changes)
            # —— 出行守护：天气预警（覆盖期内总是查）+ 最终 24h 营业时间复查 ——
            if s.amap_api_key:
                alerts, updates = _guard_check(itinerary, final_24h=is_final_24h(
                    itinerary.request.depart_date))
                for day, new_text in updates:
                    changes.append(f"{day.date} {day.city}天气：{day.weather} → {new_text}")
                    day.weather = new_text  # 行程单天气同步为最新预报
                changes.extend(alerts)
            if changes:
                console.print(f"[{now}] [bold yellow]发现变化[/]")
                for c in changes:
                    console.print(f"  · {c}")
                if hook:
                    ok = notify_webhook(hook, changes, target.name)
                    console.print(f"  ↗ webhook {'已通知' if ok else '通知失败'}")
                prev = snapshot(itinerary)
                target.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")
            else:
                console.print(f"[{now}] 无变化")
        if once:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n监控结束")
            break


# ============================================================
# hotels：住宿候选快查
# ============================================================
@app.command()
def hotels(
    city: Annotated[str, typer.Argument(help="城市名，如 成都")],
    near: Annotated[str, typer.Option("--near", help="锚点 POI 名（默认市中心）")] = "",
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=10)] = 5,
) -> None:
    """按活动区域搜住宿候选（高德 POI 级，仅信息展示）。"""
    from .planner.hotels import search_hotels
    from .providers.amap import AmapError

    s = get_settings()
    if not s.amap_api_key:
        console.print("[red]缺少 AMAP_API_KEY[/]")
        raise typer.Exit(1)
    try:
        with AmapClient(s.amap_api_key) as amap:
            if near:
                places = amap.place_text(near, city=city, limit=1)
                if not places:
                    console.print(f"[red]找不到锚点「{near}」[/]")
                    raise typer.Exit(1)
                anchor, anchor_name = places[0].location, places[0].name
            else:
                anchor, anchor_name = amap.geo(city).location, f"{city}市中心"
            picks = search_hotels(amap, city, anchor, limit=limit)
    except AmapError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    table = Table(title=f"{city} 住宿候选（锚点：{anchor_name}，高德 POI）", header_style="bold")
    table.add_column("酒店", style="bold cyan")
    table.add_column("评分")
    table.add_column("参考价")
    table.add_column("地址", overflow="fold")
    for h in picks:
        table.add_row(h.name, h.rating or "—", h.cost or "—", h.address or "—")
    console.print(table)
    console.print("[dim]POI 级信息展示，不含预订；价格以实际渠道为准[/]")


# ============================================================
# replan：局部改签（锁定车次与住宿，只换景点重排）
# ============================================================
@app.command()
def replan(
    target: Annotated[Path, typer.Argument(help="行程单 JSON 路径")],
    remove_poi: Annotated[
        list[str] | None, typer.Option("--remove-poi", help="移除景点名（可多次）")
    ] = None,
    add_poi: Annotated[str, typer.Option("--add-poi", help="新增景点：城市@景点名")] = "",
    no_map: Annotated[bool, typer.Option("--no-map", help="跳过地图重生成")] = False,
    style: Annotated[str, typer.Option("--style", help="编排风格：均衡|紧凑|休闲")] = "均衡",
) -> None:
    """局部调整：不查 12306、不问 LLM、不动酒店——车次与住宿天然锁定，只重排景点。

    显式 --add-poi 的景点会强制排入（允许当日提早出发，硬约束不变）。"""
    import json as _json

    from .llm import LLMError
    from .models import Itinerary
    from .planner.pipeline import write_outputs
    from .planner.replan import run_replan

    s = get_settings()
    if not s.amap_api_key:
        console.print("[red]缺少 AMAP_API_KEY[/]")
        raise typer.Exit(1)
    itinerary = Itinerary.model_validate(_json.loads(target.read_text("utf-8")))
    adds = []
    if add_poi:
        city, _, name = add_poi.partition("@")
        if not (city and name):
            console.print("[red]--add-poi 格式：城市@景点名，如 杭州@西溪国家湿地公园[/]")
            raise typer.Exit(1)
        adds.append((city, name))

    console.print(
        f"局部调整 [bold]{target.name}[/]"
        f"（车次 {sum(1 for leg in itinerary.legs if leg)} 段与"
        f" {len(itinerary.stays)} 处住宿锁定不变）…"
    )
    try:
        new_it, changes = run_replan(
            itinerary, s, remove_pois=remove_poi, add_pois=adds,
            regenerate_map=not no_map, style=style,
        )
    except (ValueError, LLMError) as exc:
        console.print(f"[red]{exc}[/]（原行程单未改动）")
        raise typer.Exit(1) from exc

    paths = write_outputs(new_it, s, target.parent)
    target.write_text(new_it.model_dump_json(indent=2), encoding="utf-8")

    console.rule("[bold]局部调整完成[/]")
    for c in changes:
        console.print(f"  · {c}")
    console.print(f"可行性: {new_it.feasibility.status}｜预算: ¥{new_it.total_cost:.0f}")
    console.print(f"已更新: {paths['md']} / {paths['html']} / {target.name}")


# ============================================================
# deals：美团优惠核查
# ============================================================
@app.command()
def deals(
    target: Annotated[Path, typer.Argument(help="行程单 JSON 路径")],
    hotels_flag: Annotated[
        bool, typer.Option("--hotels", help="同时查询住宿优惠（默认仅门票）")
    ] = False,
) -> None:
    """按城市对既有行程单做美团优惠核查（原文附进行程单，预算口径不变）。"""
    import json as _json

    from .deliver.html import render_html
    from .deliver.markdown import render
    from .models import Itinerary
    from .planner.deals import run_deals
    from .providers.meituan import MeituanClient, MeituanError

    s = get_settings()
    if not s.meituan_ht_token:
        console.print(
            "[red]未配置 MEITUAN_HT_TOKEN[/]（https://developer.meituan.com/zh/v2/dev/token 申请）"
        )
        raise typer.Exit(1)

    itinerary = Itinerary.model_validate(_json.loads(target.read_text("utf-8")))
    n_queries = len({d.city for d in itinerary.days}) * (2 if hotels_flag else 1)
    console.print(
        f"对 [bold]{target.name}[/] 做美团优惠核查（{n_queries} 次查询，"
        "单次可能需要 1–2 分钟，请稍候…）"
    )

    try:
        client = MeituanClient(s.meituan_ht_token)
        itinerary, failures = run_deals(client, itinerary, with_hotels=hotels_flag)
    except MeituanError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    itinerary.feasibility.notes.append(
        f"已于 {time.strftime('%Y-%m-%d %H:%M:%S')} 附加美团优惠参考（原文引用，非预算依据）"
    )
    md_path = target.with_suffix(".md")
    html_path = target.with_suffix(".html")
    qr_path = target.with_name(target.stem + "-map-qr.png")
    md_path.write_text(render(itinerary), encoding="utf-8")
    html_path.write_text(
        render_html(itinerary, qr_path if qr_path.exists() else None), encoding="utf-8"
    )
    target.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")

    console.rule("[bold]美团优惠核查完成[/]")
    for deal in itinerary.deals:
        hint = f"（{deal.price_hint}）" if deal.price_hint else ""
        console.print(f"  ✅ {deal.city}·{deal.topic}{hint}")
    for f in failures:
        console.print(f"  ❌ {f}")
    console.print(f"已更新: {md_path} / {html_path} / {target.name}")


# ============================================================
# serve：本地 Web UI
# ============================================================
@app.command()
def serve(
    host: Annotated[str, typer.Option(help="监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="端口")] = 8300,
) -> None:
    """启动本地 Web UI（表单提交规划，默认仅本机可访问）。"""
    import uvicorn

    from .webapp import create_app

    console.print(
        f"[bold]tripflow Web UI[/] → [link=http://{host}:{port}]http://{host}:{port}[/link]"
        "（Ctrl-C 退出；本页会消耗本机 LLM 额度）"
    )
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# ============================================================
# refresh：出发前刷新既有行程单
# ============================================================
@app.command()
def refresh(
    target: Annotated[
        Path, typer.Argument(help="行程单 JSON 路径，或包含它的目录（仅一个 json 时自动选中）")
    ],
) -> None:
    """刷新既有行程单的时效数据：车票余票/票价 + 天气（POI 编排保持不变）。"""
    import json as _json

    from .deliver.html import render_html
    from .deliver.markdown import render
    from .models import Itinerary
    from .planner.refresh import run_refresh

    s = get_settings()
    if not s.amap_api_key:
        console.print("[red]缺少 AMAP_API_KEY[/]")
        raise typer.Exit(1)

    json_path = target
    if target.is_dir():
        jsons = sorted(target.glob("*.json"))
        if len(jsons) != 1:
            console.print(f"[red]目录中有 {len(jsons)} 个 json，请直接指定行程单 JSON 路径[/]")
            raise typer.Exit(1)
        json_path = jsons[0]
    if not json_path.exists():
        console.print(f"[red]文件不存在: {json_path}[/]")
        raise typer.Exit(1)

    itinerary = Itinerary.model_validate(_json.loads(json_path.read_text("utf-8")))
    console.print(
        f"刷新 [bold]{json_path.name}[/]（{itinerary.request.origin} → "
        f"{'→'.join(itinerary.request.cities)}）…"
    )

    async def rail_phase():
        async with RailSession(s) as rail:
            with AmapClient(s.amap_api_key) as amap:
                return await run_refresh(rail, amap, itinerary)

    try:
        itinerary, changes = asyncio.run(asyncio.wait_for(rail_phase(), timeout=300))
    except RailError as exc:
        console.print(f"[red]刷新失败:[/] {exc}")
        raise typer.Exit(1) from exc

    md_path = json_path.with_suffix(".md")
    html_path = json_path.with_suffix(".html")
    qr_path = json_path.with_name(json_path.stem + "-map-qr.png")
    md_path.write_text(render(itinerary), encoding="utf-8")
    html_path.write_text(
        render_html(itinerary, qr_path if qr_path.exists() else None), encoding="utf-8"
    )
    json_path.write_text(itinerary.model_dump_json(indent=2), encoding="utf-8")

    console.rule("[bold]刷新完成[/]")
    if changes:
        for c in changes:
            console.print(f"  · {c}")
    else:
        console.print("  · 车票与天气无变化")
    console.print(f"已更新: {md_path} / {html_path} / {json_path.name}")


def main() -> None:
    # Windows 低编码控制台（如 cp1252）下，emoji/中文会导致 UnicodeEncodeError；
    # 降级为替换字符显示，而不是让命令崩溃
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    app()


if __name__ == "__main__":
    main()
