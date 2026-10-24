"""
Ket noi vidIQ MCP (https://mcp.vidiq.com/mcp) bang requests thuan.
- vidiq_login.py (chay 1 lan tren laptop) tao "auth blob": token_endpoint, client_id, refresh_token...
- Bot dung refresh_token de lay access_token moi moi lan chay, roi goi MCP (JSON-RPC qua HTTP).
"""
import json
import time

import requests

MCP_URL = "https://mcp.vidiq.com/mcp"
PROTOCOL = "2025-06-18"


class VidIQError(Exception):
    pass


def refresh_access(blob):
    """Doi refresh_token -> access_token. Tra ve blob moi (refresh_token co the bi xoay vong)."""
    data = {"grant_type": "refresh_token", "refresh_token": blob["refresh_token"], "client_id": blob["client_id"]}
    if blob.get("resource"):
        data["resource"] = blob["resource"]
    auth = (blob["client_id"], blob["client_secret"]) if blob.get("client_secret") else None
    r = requests.post(blob["token_endpoint"], data=data, auth=auth, timeout=30)
    if r.status_code != 200:
        raise VidIQError(f"refresh HTTP {r.status_code}")
    tok = r.json()
    new = dict(blob)
    new["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        new["refresh_token"] = tok["refresh_token"]
    new["updated"] = int(time.time())
    return new


def _parse(resp):
    """MCP tra ve JSON hoac SSE (data: {...}). Lay message JSON-RPC co 'id'."""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        msgs = []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    msgs.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
        msgs = [m for m in msgs if "id" in m] or msgs
        if not msgs:
            raise VidIQError("SSE rong")
        return msgs[-1]
    return resp.json()


class VidIQ:
    def __init__(self, access_token, url=MCP_URL):
        self.url = url
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        self._id = 0
        self.calls = 0  # so lan tools/call (tinh credit)

    def _post(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        r = self.s.post(self.url, data=json.dumps(body), timeout=180)
        if r.status_code == 401:
            raise VidIQError("401 (token het han / bi thu hoi)")
        if notify:
            return None
        if r.status_code >= 400:
            raise VidIQError(f"{method} HTTP {r.status_code}")
        msg = _parse(r)
        if "error" in msg:
            raise VidIQError(f"{method}: {msg['error'].get('message', '')[:200]}")
        return msg.get("result", {})

    def start(self):
        body = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "topic-radar", "version": "2.0"}}}
        r = self.s.post(self.url, data=json.dumps(body), timeout=60)
        if r.status_code == 401:
            raise VidIQError("401 (token het han / bi thu hoi)")
        if r.status_code >= 400:
            raise VidIQError(f"initialize HTTP {r.status_code}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.s.headers["Mcp-Session-Id"] = sid
        info = _parse(r).get("result", {})
        if info.get("protocolVersion"):
            self.s.headers["MCP-Protocol-Version"] = info["protocolVersion"]
        self._post("notifications/initialized", notify=True)
        return info

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(10):
            res = self._post("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, name, arguments, count=True):
        if count:
            self.calls += 1
        res = self._post("tools/call", {"name": name, "arguments": arguments or {}})
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c["text"])
        if not parts and res.get("structuredContent"):
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts)
        if res.get("isError"):
            raise VidIQError(f"tool loi: {text[:200]}")
        return text
