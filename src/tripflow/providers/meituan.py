"""美团酒旅 provider：经官方 ht-ai CLI 做优惠/价格查询（自然语言进、Markdown 出）。

定位是「优惠参考与预订信息」而不是预算数据源：返回内容为自然语言文本，
原文引用进行程单并带查询时间戳；预算数字仍以 12306 实价与标注估算为准。
需要用户自己的 MEITUAN_HT_TOKEN（developer.meituan.com 申请）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

QUERY_TIMEOUT = 150  # 官方说明单次查询可能 1-2 分钟


class MeituanError(RuntimeError):
    pass


def _npx_command(extra_args: list[str]) -> list[str]:
    """解析 npx；Windows 下 npm shim(.cmd) 需经 cmd /c（同 rail provider 的处理）。"""
    resolved = shutil.which("npx")
    if resolved is None:
        raise MeituanError("找不到 npx：美团优惠查询依赖 Node.js（https://nodejs.org/）")
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        cmd = shutil.which("cmd") or "cmd"
        return [cmd, "/c", resolved, *extra_args]
    return [resolved, *extra_args]


def _clean(text: str) -> str:
    """去掉后端泄漏的 prompt 标签（如实测出现的 </answer>）与首尾空白。"""
    cleaned = text.replace("</answer>", "").strip()
    return cleaned


class MeituanClient:
    def __init__(self, token: str, *, timeout: int = QUERY_TIMEOUT) -> None:
        if not token:
            raise MeituanError("未配置 MEITUAN_HT_TOKEN（https://developer.meituan.com 申请）")
        self._token = token
        self._timeout = timeout

    def query(self, query: str, city: str, origin_query: str = "") -> str:
        """执行一次自然语言查询，返回 Markdown 文本。"""
        argv = _npx_command(
            [
                "-y",
                "@meituan-travel/ht-ai@latest",
                "query",
                "--query",
                query,
                "--origin-query",
                origin_query or query,
                "--channel",
                "meituan-developer",
                "--city",
                city,
            ]
        )
        env = {**os.environ, "MEITUAN_HT_TOKEN": self._token, "MEITUAN_RAW_JSON": "1"}
        try:
            # 退出码自定义（3=鉴权失败），不能用 check=True
            proc = subprocess.run(  # noqa: PLW1510
                argv, capture_output=True, text=True, timeout=self._timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise MeituanError(f"美团查询超时（>{self._timeout}s），请稍后重试") from exc
        if proc.returncode == 3:
            raise MeituanError(
                "MEITUAN_HT_TOKEN 无效或过期（exit 3），请到 developer.meituan.com 重新获取"
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise MeituanError(
                f"美团查询失败（exit {proc.returncode}）：{detail[-1][:120] if detail else '未知错误'}"
            )
        try:
            payload = json.loads(proc.stdout)
            data = payload.get("data", "")
            status = payload.get("status", "")
        except json.JSONDecodeError:
            data, status = proc.stdout, ""
        if status and status != "success":
            raise MeituanError(f"美团返回异常状态: {status}")
        cleaned = _clean(str(data))
        if not cleaned:
            raise MeituanError("美团返回内容为空，可换个问法重试")
        return cleaned
