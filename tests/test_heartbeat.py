import asyncio
from types import SimpleNamespace

import httpx

from tradio import bot


def test_heartbeat_pings_the_monitor_and_a_failure_only_logs(monkeypatch):
    seen, real = [], httpx.AsyncClient  # bot.httpx is the module itself: keep the real class for the fake

    def client(fail: bool):
        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if fail:
                raise httpx.ConnectError("no route")
            return httpx.Response(200)

        return lambda **kw: real(transport=httpx.MockTransport(handle), **kw)

    context = SimpleNamespace(job=SimpleNamespace(data="https://hc-ping.com/abc"))
    monkeypatch.setattr(bot.httpx, "AsyncClient", client(fail=False))
    asyncio.run(bot._heartbeat(context))
    monkeypatch.setattr(bot.httpx, "AsyncClient", client(fail=True))
    asyncio.run(bot._heartbeat(context))  # doesn't raise: the job queue keeps running
    assert seen == ["https://hc-ping.com/abc"] * 2
