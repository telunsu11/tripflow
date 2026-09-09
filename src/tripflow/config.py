"""配置：全部来自环境变量 / .env，密钥永不落仓库。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM（必填，任意 OpenAI 兼容端点）----
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_fast_model: str = ""

    # ---- 高德（必填，「Web服务」Key；REST 与云端 MCP 共用）----
    amap_api_key: str = ""
    amap_mcp_url: str = ""

    # ---- 12306（默认 npx 拉起社区 MCP；高级用户可换 stdio 命令或 SSE 远程端点）----
    rail_mcp_mode: Literal["stdio", "sse"] = "stdio"
    rail_mcp_command: str = "npx"
    rail_mcp_args: str = "-y 12306-mcp"
    rail_mcp_url: str = ""

    # ---- 其它 ----
    trip_output_dir: Path = Path("output")
    trip_ticket_ttl: int = 300
    trip_lang: str = "zh"

    @property
    def data_dir(self) -> Path:
        return Path.home() / ".tripflow"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def amap_mcp_endpoint(self) -> str:
        return self.amap_mcp_url or f"https://mcp.amap.com/mcp?key={self.amap_api_key}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
