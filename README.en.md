# tripflow

[![CI](https://github.com/telunsu11/tripflow/actions/workflows/ci.yml/badge.svg)](https://github.com/telunsu11/tripflow/actions/workflows/ci.yml)

> Input a budget and dates, get a travel itinerary with **real numbers, validated schedule, and a shareable map** — not another "AI travel guide."

Every number in a tripflow itinerary — ticket availability, prices, commute times, weather — comes from live queries against China Railway (12306) and Amap, each stamped with its query time. The LLM only makes trade-offs and explains them; **it never invents numbers.** Every plan passes a deterministic feasibility check before it reaches you.

- 🚄 **Transport comparison**: direct 12306 trains first, automatic transfer plans (with transfer-buffer validation) when direct fails
- 🏨 **Hotel planning**: deterministic pick (rating first, nearest to activity cluster) as the daily commute anchor; price basis is tiered (Amap reference / city estimate / Meituan range) — never fake precision
- 🧭 **Multi-city itineraries**: e.g. Shanghai → Suzhou → Hangzhou, with same-day transfers and per-city day allocation
- 🗺️ **Shareable Amap itinerary map**: scan the QR with the Amap app to open your personalized map
- 📄 Deliverables: Markdown + standalone HTML (QR embedded) + machine-readable JSON with full evidence chain
- 🔄 `tripflow refresh`: re-check tickets and weather before departure, keep the plan intact
- 🔑 **All credentials via environment variables** — any OpenAI-compatible LLM endpoint works (GLM / DeepSeek / Qwen / Ollama…)
- 🎫 `tripflow deals` (optional): Meituan hotel-and-travel deal check — ticket prices & policies quoted verbatim into the itinerary
- 🔁 **Partial replan**: `replan` locks trains & hotel, swaps a single POI with deterministic re-scheduling and an old/new diff
- 🎫 **Waitlist monitor**: `watch --add-train` watches sold-out trains and alerts when tickets are released
- 🛡️ **Departure guardian**: `watch` monitors not just tickets — severe-weather alerts, forecast changes, and opening-hours recheck in the final 24h before departure
- 🎭 **Style comparison**: `--compare-styles` appends compact/relaxed variants (deterministic pipeline, same input → same output)
- 📊 Overridable reference data: hub stations / hotel prices / daily food costs live in `data/*.yaml`; drop same-named files into `~/.tripflow/data/` to merge
- 🔒 Read-only: no booking, no payments, never touches your accounts

> Status: **M3 complete** — .ics calendar export, ticket monitoring (`watch` + webhook), hotel picks (Amap POI), cross-station transfer validation, local Web UI (`serve`). Chinese docs (primary): [README.md](README.md).

## Quick start (≤3 commands)

Platforms: macOS / Linux (fully tested) · Windows (install/tests/CLI verified by CI on all three platforms — feedback welcome).

Prerequisites: [uv](https://docs.astral.sh/uv/getting-started/install/), [Node.js](https://nodejs.org/) (the 12306 MCP launches via `npx`).

```bash
git clone https://github.com/telunsu11/tripflow.git && cd tripflow
cp .env.example .env     # fill in the two keys below
uv run tripflow doctor   # environment self-check with fix-it hints
```

Two keys required:

| Variable | Where | Note |
|---|---|---|
| `LLM_API_KEY` | [Zhipu](https://open.bigmodel.cn/) / [DeepSeek](https://platform.deepseek.com/) etc. | Any OpenAI-compatible endpoint; set `LLM_BASE_URL` / `LLM_MODEL` |
| `AMAP_API_KEY` | [Amap Open Platform](https://console.amap.com/) | ⚠️ Key type must be **"Web Service"** |

Or run the interactive wizard: `uv run tripflow setup`

## Commands

```bash
# End-to-end planning (multi-city supported)
uv run tripflow plan "Sept 12–15, Shanghai to Suzhou and Hangzhou, 2 people, ¥1500/person each"

# Refresh tickets & weather before departure / monitor availability
uv run tripflow refresh output/苏州-杭州-2026-09-12.json
uv run tripflow watch output/苏州-杭州-2026-09-12.json --interval 1800

uv run tripflow deals output/成都-2026-09-12.json --hotels   # Meituan deals check (optional)
uv run tripflow hotels 成都 --near 宽窄巷子   # hotel picks (Amap POI)
uv run tripflow ical output/成都-2026-09-12.json   # .ics calendar export
uv run tripflow serve                          # local Web UI (127.0.0.1)
uv run tripflow doctor / setup / tickets …
```

## How it works

```
Providers  12306 MCP (npx, stdio/SSE) · Amap dual-channel (direct REST + official cloud MCP) · any OpenAI-compatible LLM
Planner    deterministic pipeline: intake → multi-leg transit → POI validation → per-city scheduling → budget → feasibility (pure Python, no LLM in checks)
Delivery   Markdown + standalone HTML + Amap personal-map QR + Itinerary JSON (evidence chain)
```

Design rules: the model never generates numbers; every fact carries a source and `checked_at`; facts and recommendations are rendered separately; stale ticket data is flagged, never silently shown.

## Disclaimer

Data comes from public 12306 queries and the Amap Open Platform. This is an unofficial open-source tool for personal trip planning only — **not booking advice**; ticket availability is subject to 12306. No commercial bulk querying.

## License

[MIT](LICENSE)
