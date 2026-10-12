"""Agent failures should be actionable without exposing provider internals."""

import pytest


class FailingAgent:
    async def ainvoke(self, req):
        raise RuntimeError("private-provider-token-and-host")

    async def astream(self, req):
        raise RuntimeError("private-provider-token-and-host")
        yield  # pragma: no cover - retain async iterator protocol


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_failure_hides_internal_details(http, as_user, stream):
    headers = await as_user("李娜")
    http.app.state.agent = FailingAgent()
    path = "/api/agent/chat" + ("/stream" if stream else "")
    response = await http.post(path, json={"message": "hello"}, headers=headers)
    assert response.status_code == (200 if stream else 502)
    assert "private-provider-token-and-host" not in response.text
    assert "直接预约表单" in response.text
    if stream:
        assert '"type": "error"' in response.text
