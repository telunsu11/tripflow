"""配置加载测试：环境变量覆盖、默认值、MCP 端点拼接。"""

from tripflow.config import Settings


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("AMAP_API_KEY", "amap-test")
    monkeypatch.setenv("TRIP_TICKET_TTL", "60")
    s = Settings(_env_file=None)
    assert s.llm_api_key == "sk-test"
    assert s.amap_api_key == "amap-test"
    assert s.trip_ticket_ttl == 60


def test_defaults():
    s = Settings(_env_file=None)
    assert s.rail_mcp_command == "npx"
    assert s.rail_mcp_args == "-y 12306-mcp"
    assert s.rail_mcp_mode == "stdio"
    assert s.trip_ticket_ttl == 300


def test_amap_mcp_endpoint_from_key(monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "abc123")
    s = Settings(_env_file=None)
    assert s.amap_mcp_endpoint == "https://mcp.amap.com/mcp?key=abc123"


def test_amap_mcp_url_override(monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "abc123")
    monkeypatch.setenv("AMAP_MCP_URL", "https://example.com/mcp")
    s = Settings(_env_file=None)
    assert s.amap_mcp_endpoint == "https://example.com/mcp"
