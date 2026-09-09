"""可选 Web UI：本地起一个页面表单提交 plan（默认只绑 127.0.0.1，消耗本机 LLM 额度）。"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import get_settings

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tripflow · 行程规划</title>
<style>
 body{margin:0;font-family:-apple-system,"PingFang SC",sans-serif;background:#f4f6f5;color:#1f2933}
 .wrap{max-width:760px;margin:0 auto;padding:28px 16px 60px}
 header{background:#0b6e4f;color:#fff;border-radius:14px;padding:22px 24px;margin-bottom:16px}
 header h1{margin:0 0 4px;font-size:20px} header p{margin:0;opacity:.9;font-size:13px}
 form{background:#fff;border:1px solid #e4e7eb;border-radius:14px;padding:18px}
 textarea{width:100%;box-sizing:border-box;border:1px solid #cfd6db;border-radius:10px;
   padding:12px;font-size:14px;min-height:84px;resize:vertical}
 button{margin-top:12px;background:#0b6e4f;color:#fff;border:none;border-radius:10px;
   padding:10px 22px;font-size:15px;cursor:pointer}
 button:disabled{opacity:.5;cursor:wait}
 #log{margin-top:14px;background:#0f1c17;color:#9fd8bf;border-radius:10px;padding:12px 14px;
   font:12px/1.7 ui-monospace,monospace;white-space:pre-wrap;display:none}
 #result{margin-top:14px;display:none}
 iframe{width:100%;height:640px;border:1px solid #e4e7eb;border-radius:12px;background:#fff}
 .hint{color:#5b6b76;font-size:12px;margin-top:8px}
</style>
</head>
<body><div class="wrap">
<header><h1>tripflow</h1><p>输入预算和日期，产出数字真实、可执行、可分享的行程单（本页消耗本机 LLM 额度）</p></header>
<form id="f">
 <textarea id="q" placeholder='例：9月12日到15日从上海去苏州和杭州，2人，人均预算1500，必去拙政园'>9月12日到14日从上海去成都，2人，人均预算3000，必去宽窄巷子</textarea>
 <button id="go" type="submit">生成行程单</button>
 <div class="hint">全流程约 1–2 分钟：12306 实时车票 → 高德 POI/天气/通勤 → 可行性检查 → 行程地图</div>
</form>
<div id="log"></div>
<div id="result"><iframe id="frame"></iframe></div>
<script>
const $ = id => document.getElementById(id);
$("f").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("go"); btn.disabled = true; btn.textContent = "规划中…";
  $("log").style.display = "block"; $("log").textContent = "提交中…"; $("result").style.display = "none";
  try {
    const res = await fetch("/api/plan", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({request: $("q").value})});
    const {job_id} = await res.json();
    const timer = setInterval(async () => {
      const st = await (await fetch("/api/plan/" + job_id)).json();
      $("log").textContent = (st.log || []).join("\\n") || "排队中…";
      if (st.status === "done") {
        clearInterval(timer); btn.disabled = false; btn.textContent = "生成行程单";
        $("frame").src = "/api/plan/" + job_id + "/html"; $("result").style.display = "block";
        $("log").textContent += "\\n✅ 完成：" + (st.summary || []).join("\\n");
      } else if (st.status === "error") {
        clearInterval(timer); btn.disabled = false; btn.textContent = "生成行程单";
        $("log").textContent += "\\n❌ " + st.error;
      }
    }, 2000);
  } catch (err) { btn.disabled = false; btn.textContent = "生成行程单"; $("log").textContent += "\\n❌ " + err; }
});
</script>
</div></body></html>"""


class PlanRequest(BaseModel):
    request: str


def create_app() -> FastAPI:
    app = FastAPI(title="tripflow", docs_url=None, redoc_url=None)
    jobs: dict[str, dict] = {}

    def _run_job(job_id: str, text: str) -> None:
        from .planner.pipeline import run_plan

        def step_cb(i: int, total: int, msg: str) -> None:
            jobs[job_id]["log"].append(f"[{i}/{total}] {msg}")

        try:
            result = run_plan(text, settings=get_settings(), step_cb=step_cb)
            jobs[job_id].update(
                status="done",
                finished=time.time(),
                summary=result.summary_lines(),
                html=str(result.html_path),
                md=str(result.md_path),
                json=str(result.json_path),
                ical=str(result.ical_path),
            )
        except Exception as exc:  # noqa: BLE001 - 任务级失败记录到 job
            jobs[job_id].update(status="error", finished=time.time(), error=str(exc)[:300])

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.post("/api/plan")
    def submit(body: PlanRequest) -> JSONResponse:
        if not body.request.strip():
            raise HTTPException(status_code=400, detail="request 不能为空")
        job_id = uuid.uuid4().hex[:12]
        jobs[job_id] = {"status": "running", "log": [], "created": time.time()}
        threading.Thread(target=_run_job, args=(job_id, body.request), daemon=True).start()
        return JSONResponse({"job_id": job_id})

    @app.get("/api/plan/{job_id}")
    def status(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job 不存在")
        return JSONResponse(job)

    @app.get("/api/plan/{job_id}/html", response_class=HTMLResponse)
    def result_html(job_id: str) -> str:
        job = jobs.get(job_id)
        if job is None or job.get("status") != "done":
            raise HTTPException(status_code=404, detail="结果尚未就绪")
        from pathlib import Path

        return Path(job["html"]).read_text("utf-8")

    return app
