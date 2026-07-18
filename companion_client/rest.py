"""Client for the Mobile Companion API v1 (``/api/v1/*``).

This is the surface a phone app lives on: pair once to get a device token,
fetch a snapshot to bootstrap, then follow the journal with ``sync``. The TCP
frame protocol (:mod:`companion_client.client`) is a different, lower-level
interface -- the two are not equivalent, and this one is the newer of the pair.

Stdlib ``urllib`` only, matching the repeater's own outbound HTTP (see
``push_notifier._default_poster``), so the client adds no dependency.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("companion_client.rest")


class _MethodPreservingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the HTTP method across a redirect.

    CherryPy mounts object endpoints (``/pair``, ``/devices``) such that the
    bare path 301-redirects to the trailing-slash form. urllib -- like most
    clients, following RFC 7231 for 301/302 -- reissues the redirect as GET,
    so ``POST /api/v1/pair`` arrives as ``GET /api/v1/pair/`` and the server
    answers ``405 Method not allowed. Use POST.`` The message is actively
    misleading: the caller *did* use POST.

    Preserving the method makes the bare and trailing-slash forms behave the
    same, which is what a caller expects. Real phone clients built on
    stock HTTP libraries will hit the same downgrade unless they either do
    this or always send the trailing slash.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and req.get_method() != new.get_method():
            data = req.data
            new = urllib.request.Request(
                new.full_url,
                data=data,
                headers=dict(req.header_items()),
                method=req.get_method(),
            )
        return new


_opener = urllib.request.build_opener(_MethodPreservingRedirectHandler)


class _Headers(dict):
    """Case-insensitive response headers.

    HTTP header names are case-insensitive (RFC 7230 §3.2), and servers differ
    on casing: the spec documents ``ETag`` while CherryPy puts ``Etag`` on the
    wire. A case-sensitive lookup silently returns None and the caller
    quietly loses conditional-request support.
    """

    def __init__(self, raw) -> None:
        super().__init__(raw)
        self._lower = {k.lower(): v for k, v in raw.items()}

    def get(self, key, default=None):
        return self._lower.get(key.lower(), default)

    def __getitem__(self, key):
        return self._lower[key.lower()]

    def __contains__(self, key) -> bool:
        return key.lower() in self._lower


class RestError(Exception):
    """Non-2xx response. Carries the v1 error envelope when there is one."""

    def __init__(self, status: int, body: Any, url: str) -> None:
        detail = body
        if isinstance(body, dict):
            detail = body.get("error") or body.get("message") or body
        super().__init__(f"{status} from {url}: {detail}")
        self.status = status
        self.body = body
        self.url = url


class NotModified(Exception):
    """304 -- the ETag matched, so there is no body to read."""


@dataclass
class SyncResult:
    events: list
    next_cursor: str
    has_more: bool
    snapshot_required: bool = False
    etag: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)


class CompanionRestClient:
    """Talks to one repeater's ``/api/v1`` tree.

    ``token`` is a device token from :meth:`pair` (or an admin API token for
    operator-level calls). It is sent as ``Authorization: Bearer``.
    """

    def __init__(self, base_url: str, token: Optional[str] = None, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        auth: bool = True,
    ) -> tuple[int, Any, dict]:
        url = f"{self.base_url}/api/v1{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Accept": "application/json"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if auth and self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})

        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else None
                return response.status, parsed, _Headers(dict(response.headers))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 304:
                raise NotModified() from exc
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", errors="replace")
            raise RestError(exc.code, parsed, url) from exc

    def _data(self, method: str, path: str, **kwargs) -> Any:
        """Unwrap the ``{success, data}`` envelope the v1 tree returns."""
        _status, payload, _headers = self._request(method, path, **kwargs)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- unauthenticated ---------------------------------------------------

    def server_info(self) -> dict:
        """Public bootstrap document. Deliberately excludes companion names."""
        return self._data("GET", "/server_info", auth=False)

    # -- pairing -----------------------------------------------------------

    def pair_start(self, companion_name: str, admin_token: str) -> dict:
        """Operator-side: mint a short-lived pairing code.

        Needs admin auth -- a device cannot bootstrap itself.
        """
        previous, self.token = self.token, admin_token
        try:
            return self._data("POST", "/pair/start", body={"companion_name": companion_name})
        finally:
            self.token = previous

    def pair(self, code: str, device_id: str, name: str, platform: Optional[str] = None) -> dict:
        """Device-side: exchange a pairing code for a device token.

        The code is single-use. On success the returned token is adopted as
        this client's credential.
        """
        body = {"code": code, "device_id": device_id, "name": name}
        if platform is not None:
            body["platform"] = platform
        data = self._data("POST", "/pair", body=body, auth=False)
        token = data.get("token") if isinstance(data, dict) else None
        if token:
            self.token = token
        return data

    # -- companions --------------------------------------------------------

    def companions(self) -> list:
        return self._data("GET", "/companions")

    def snapshot(
        self,
        companion_name: str,
        *,
        messages_limit: Optional[int] = None,
        etag: Optional[str] = None,
    ) -> tuple[dict, Optional[str]]:
        """Bootstrap document: self, contacts, channels, recent messages.

        This is the only place channels and contacts are handed out in full --
        there is no dedicated list endpoint for either. Returns
        ``(data, etag)``; pass the etag back to get :class:`NotModified`.
        """
        headers = {"If-None-Match": etag} if etag else None
        _status, payload, response_headers = self._request(
            "GET",
            f"/companions/{urllib.parse.quote(companion_name)}/snapshot",
            params={"messages_limit": messages_limit},
            headers=headers,
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        return data, response_headers.get("ETag")

    def sync(
        self,
        companion_name: str,
        cursor: str,
        *,
        limit: Optional[int] = None,
        include: Optional[str] = None,
        etag: Optional[str] = None,
    ) -> SyncResult:
        """Journal delta since ``cursor``.

        ``rf_reception`` events are omitted unless ``include='rf_receptions'``;
        every other event type -- including ``channel`` -- comes through by
        default.

        A cursor below the prune floor yields ``snapshot_required``: the delta
        would be silently incomplete, so the client must re-snapshot.
        """
        headers = {"If-None-Match": etag} if etag else None
        _status, payload, response_headers = self._request(
            "GET",
            f"/companions/{urllib.parse.quote(companion_name)}/sync",
            params={"cursor": cursor, "limit": limit, "include": include},
            headers=headers,
        )
        data = (payload.get("data") if isinstance(payload, dict) else payload) or {}
        return SyncResult(
            events=data.get("events", []),
            next_cursor=str(data.get("next_cursor", cursor)),
            has_more=bool(data.get("has_more")),
            snapshot_required=bool(data.get("snapshot_required")),
            etag=response_headers.get("ETag"),
            raw=data,
        )

    def messages(
        self,
        companion_name: str,
        *,
        limit: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> dict:
        return self._data(
            "GET",
            f"/companions/{urllib.parse.quote(companion_name)}/messages",
            params={"limit": limit, "before_id": before_id},
        )

    # -- devices / push ----------------------------------------------------

    def devices(self) -> list:
        return self._data("GET", "/devices")

    def register_push(
        self,
        device_id: str,
        push_token: str,
        push_relay_url: str,
        *,
        push_detail: str = "none",
        mention_push: Optional[bool] = None,
        mention_keywords: Optional[list] = None,
    ) -> dict:
        body: dict = {
            "push_token": push_token,
            "push_relay_url": push_relay_url,
            "push_detail": push_detail,
        }
        if mention_push is not None:
            body["mention_push"] = mention_push
        if mention_keywords is not None:
            body["mention_keywords"] = mention_keywords
        return self._data("POST", f"/devices/{urllib.parse.quote(device_id)}/push", body=body)

    def unregister_push(self, device_id: str) -> dict:
        return self._data("DELETE", f"/devices/{urllib.parse.quote(device_id)}/push")

    # -- convenience -------------------------------------------------------

    def follow(self, companion_name: str, cursor: str, *, limit: int = 200) -> tuple[list, str]:
        """Drain sync pages until caught up. Returns ``(events, cursor)``.

        Raises :class:`RestError` if the server asks for a re-snapshot, since
        callers must handle that by re-bootstrapping rather than looping.
        """
        collected: list = []
        for _ in range(50):  # bounded: a runaway journal must not spin forever
            result = self.sync(companion_name, cursor, limit=limit)
            if result.snapshot_required:
                raise RestError(409, {"error": "snapshot_required"}, "sync")
            collected.extend(result.events)
            cursor = result.next_cursor
            if not result.has_more:
                break
        return collected, cursor
