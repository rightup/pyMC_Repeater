import asyncio

import cherrypy
import pytest

from repeater.web.companion_endpoints import CompanionAPIEndpoints
from repeater.web.mobile_endpoints import CompanionsV1


@pytest.mark.parametrize("endpoint_cls", [CompanionAPIEndpoints, CompanionsV1])
def test_run_async_closes_coroutine_when_loop_is_missing(endpoint_cls):
    endpoint = endpoint_cls(event_loop=None)

    async def operation():
        return True

    coro = operation()
    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint._run_async(coro)

    assert exc.value.status == 503
    assert coro.cr_frame is None


@pytest.mark.parametrize("endpoint_cls", [CompanionAPIEndpoints, CompanionsV1])
def test_run_async_closes_coroutine_when_loop_is_closed(endpoint_cls):
    loop = asyncio.new_event_loop()
    loop.close()
    endpoint = endpoint_cls(event_loop=loop)

    async def operation():
        return True

    coro = operation()
    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint._run_async(coro)

    assert exc.value.status == 503
    assert coro.cr_frame is None
