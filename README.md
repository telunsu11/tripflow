# tripflow

[![CI](https://github.com/telunsu11/tripflow/actions/workflows/ci.yml/badge.svg)](https://github.com/telunsu11/tripflow/actions/workflows/ci.yml)

> 输入预算和日期，产出一份**数字真实、排得通、能直接执行和分享**的旅行行程单。

不是又一份"AI 攻略"。tripflow 的行程单里，每个数字——余票、票价、通勤耗时、天气——都来自 12306 与高德的实时查询，带查询时间戳；LLM 只负责取舍与解释，**不生成任何数字**；方案先过确定性可行性检查，排不通的到不了你面前。

- 🚄 **交通方案对比**：12306 直达余票/票价，直达不合适自动展开**中转方案**（含换乘余量校验）
- 🗺️ **可分享行程地图**：经高德生成个人专属地图，手机扫码即可在高德 App 打开、逐点导航
- ☁️ 天气逐日穿插、预算分项核算（实价与估算分开标注）
- 🔑 **密钥全部走环境变量**：LLM 任意 OpenAI 兼容端点可插拔（GLM / DeepSeek / Qwen / Ollama…）
- 🎫 `tripflow deals`（可选）：美团酒旅优惠核查——门票价格/免票政策原文附进行程单，预算口径不变
- 🔒 只读查询：不购票、不支付、不碰你的任何账号

> 状态：**M3 已完成**——多目的地、HTML/日历导出、`refresh`、余票监控 `watch`、住宿候选、跨站换乘校验、本地 Web UI `serve`（实测样例：[examples/chengdu-3d-itinerary.md](examples/chengdu-3d-itinerary.md)）

## 快速开始（≤3 条命令，≤10 分钟）

平台：macOS / Linux（已实测完整功能）· Windows（安装/测试/CLI 经 CI 三平台持续验证，完整功能欢迎反馈）。

前置：[uv](https://docs.astral.sh/uv/getting-started/install/)、[Node.js](https://nodejs.org/)（12306 MCP 经 `npx` 拉起，`npx -v` 可检查）。

```bash
git clone https://github.com/telunsu11/tripflow.git && cd tripflow
cp .env.example .env     # 填入下面两个 Key
uv run tripflow doctor   # 环境自检：缺什么、去哪补，它都会告诉你
```

只需配置两个 Key：

| 变量 | 申请地址 | 注意 |
|---|---|---|
| `LLM_API_KEY` | [智谱](https://open.bigmodel.cn/) / [DeepSeek](https://platform.deepseek.com/) 等 | 任意 OpenAI 兼容端点，配 `LLM_BASE_URL` / `LLM_MODEL` |
| `AMAP_API_KEY` | [高德开放平台](https://console.amap.com/) | ⚠️ Key 类型必须选 **「Web服务」** |

不想手改 `.env`？直接跑交互式向导：`uv run tripflow setup`

## 命令

```bash
# 端到端规划：一句话出完整行程单（Markdown + 单文件 HTML + JSON + 高德地图二维码）
# 支持多目的地："9月12日到15日从上海去苏州和杭州，2人，人均1500"
uv run tripflow plan "9月12日到14日从上海去成都，2人，人均预算3000，必去宽窄巷子"

# 出发前刷新既有行程单的车票余票/天气（编排保持不变）
uv run tripflow refresh output/成都-2026-09-12.json

# 余票监控：变化即提醒（终端 + 可选 webhook），--once 适合 cron
uv run tripflow watch output/成都-2026-09-12.json --interval 1800

# 美团优惠核查：按城市查行程单内景点的门票价格/优惠政策，原文附进行程单（可选，需 MEITUAN_HT_TOKEN）
uv run tripflow deals output/成都-2026-09-12.json --hotels

# 住宿候选快查（高德 POI 级）/ 日历导出 / 本地 Web UI
uv run tripflow hotels 成都 --near 宽窄巷子
uv run tripflow ical output/成都-2026-09-12.json
uv run tripflow serve

# 环境自检（LLM / 高德 REST / 高德云端 MCP / 12306 MCP）
uv run tripflow doctor

# 交互式配置向导：收 Key → 即时验证 → 写入 .env
uv run tripflow setup

# 查直达余票（date 留空自动取 12306 今日；--refresh 跳过缓存）
uv run tripflow tickets 上海 成都 2026-09-12
```

`plan` 会输出（真实运行样例：[examples/chengdu-3d-itinerary.md](examples/chengdu-3d-itinerary.md)）：

```
[1/8] 解析需求（LLM）…              → 出发/日期/人数/预算 + 假设清单
[2/8] 查交通（12306）…              → 直达选班 + 中转兜底 + 方案对比表
[3/8] 查询目的地天气…
[4/8] 提名并校验景点 …              → LLM 提名 → 高德逐个核实坐标/营业时间
[5/8] 编排逐日行程 …                → 真实通勤耗时 + 时间窗可行性
[6/8] 核算预算 …                    → 交通实价 + 住宿/餐饮/门票估算（分开标注）
[7/8] 确定性可行性检查 …            → FEASIBLE / RISK / INFEASIBLE + 问题清单
[8/8] 生成高德行程地图 …            → amapuri:// 链接 + 扫码二维码
```

可行性结论示例（真实输出）：

```
可行性: FEASIBLE_WITH_RISK
去程: G1974 上海虹桥 07:16 → 成都东 18:26（历时 11:10，二等座 ¥1040）
返程: G239 成都东 08:16 → 上海虹桥 18:22（历时 10:06，二等座 ¥1074）
预算: 合计 ¥6408（人均 ¥3204 / 预算 ¥6000）
  ⚠️ 时间容量不足，未排入：成都大熊猫繁育研究基地
  ⚠️ 预算紧张：估算总花费 ¥6408 略超预算 ¥6000
```

## 工作方式

```
接入层   12306 MCP（npx 本地拉起，stdio/SSE 可切）· 高德双通道（REST 直连 + 官方云端 MCP）· 任意 OpenAI 兼容 LLM
规划核心  确定性流水线：需求解析 → 交通(直达/中转) → POI 校验 → 逐日编排 → 预算 → 可行性检查（纯 Python，无 LLM）
交付层   Itinerary JSON（含证据链）→ Markdown/HTML + 高德个人地图分享链接（扫码即用）
```

设计红线：模型不生成任何数字；每条数据带来源与 `checked_at`；事实与建议分开呈现；余票过期标红不静默呈现。

## 路线图

- [x] **M0** 骨架：config/env、LLM 客户端、12306/高德 provider（含 TTL 缓存）、`doctor` / `setup` / `tickets`
- [x] **M1 MVP**：`plan` 一条命令产出完整行程单（直达/中转对比、逐日时间线、天气、预算、可行性、高德地图链接）
- [x] **M2**：多目的地串联（同日换乘、分城编排）、HTML 行程单、`refresh` 出发前刷新、交互式预算追问、英文 README
- [x] **M3**：日历导出（.ics）、余票监控（`watch` + webhook）、住宿候选（高德 POI）、跨站换乘校验（高德算站间通勤）、本地 Web UI（`serve`）


## 免责声明

数据来自 12306 公开查询接口与高德开放平台；本项目为非官方开源工具，仅供个人行程参考，**不构成购票建议**，余票与票价以 12306 实际为准。请勿用于商业化批量查询。

## License

[MIT](LICENSE)
