"""
CHAY 1 LAN TREN LAPTOP:  python vidiq_login.py
1. Mo trinh duyet -> ban dang nhap vidIQ va bam cho phep.
2. Script tao file VIDIQ_AUTH.txt  -> copy noi dung, dan vao GitHub Secret ten VIDIQ_AUTH.
3. Script tao file vidiq_tools.json -> danh sach cong cu vidIQ (gui cho Claude xem neu can chinh).
KHONG dua 2 file nay len GitHub (da co trong .gitignore).
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

import requests

from vidiq import MCP_URL, VidIQ

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
HERE = os.path.dirname(os.path.abspath(__file__))


def get_json(url):
    try:
        r = requests.get(url, timeout=20, headers={"Accept": "application/json"})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def discover():
    u = urllib.parse.urlparse(MCP_URL)
    origin = f"{u.scheme}://{u.netloc}"
    prm = None
    # 1) hoi MCP khong co token -> 401 + WWW-Authenticate resource_metadata="..."
    r = requests.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                      headers={"Accept": "application/json, text/event-stream"}, timeout=20)
    m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("WWW-Authenticate", ""))
    for url in ([m.group(1)] if m else []) + [f"{origin}/.well-known/oauth-protected-resource{u.path}",
                                               f"{origin}/.well-known/oauth-protected-resource"]:
        prm = get_json(url)
        if prm:
            break
    auth_server = (prm or {}).get("authorization_servers", [origin])[0].rstrip("/")
    a = urllib.parse.urlparse(auth_server)
    a_origin, a_path = f"{a.scheme}://{a.netloc}", a.path.rstrip("/")
    meta = None
    for url in [f"{a_origin}/.well-known/oauth-authorization-server{a_path}",
                f"{a_origin}/.well-known/openid-configuration{a_path}",
                f"{auth_server}/.well-known/openid-configuration",
                f"{a_origin}/.well-known/oauth-authorization-server"]:
        meta = get_json(url)
        if meta and meta.get("authorization_endpoint"):
            break
    if not meta:
        sys.exit("Khong tim thay thong tin dang nhap cua vidIQ. Gui man hinh nay cho Claude.")
    resource = (prm or {}).get("resource") or MCP_URL
    scopes = (prm or {}).get("scopes_supported") or []
    return meta, resource, scopes


class Handler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        Handler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Xong! Quay lai cua so den (terminal).</h2>".encode())

    def log_message(self, *a):
        pass


def main():
    print("Dang tim thong tin dang nhap vidIQ...")
    meta, resource, scopes = discover()

    reg = meta.get("registration_endpoint")
    if not reg:
        sys.exit("vidIQ khong cho dang ky ung dung tu dong. Gui man hinh nay cho Claude.")
    r = requests.post(reg, json={
        "client_name": "Topic Radar", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"}, timeout=20)
    if r.status_code not in (200, 201):
        sys.exit(f"Dang ky that bai: HTTP {r.status_code} {r.text[:300]}")
    client = r.json()

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": resource}
    if scopes:
        params["scope"] = " ".join(scopes)
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    srv = http.server.HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("\nMo trinh duyet de dang nhap vidIQ. Neu trinh duyet khong tu mo, copy link nay:\n" + url + "\n")
    webbrowser.open(url)
    for _ in range(600):
        if Handler.result:
            break
        time.sleep(0.5)
    res = Handler.result
    if res.get("state") != state or "code" not in res:
        sys.exit(f"Dang nhap khong thanh cong: {res}")

    data = {"grant_type": "authorization_code", "code": res["code"], "redirect_uri": REDIRECT,
            "client_id": client["client_id"], "code_verifier": verifier, "resource": resource}
    auth = (client["client_id"], client["client_secret"]) if client.get("client_secret") else None
    r = requests.post(meta["token_endpoint"], data=data, auth=auth, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Lay token that bai: HTTP {r.status_code} {r.text[:300]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        print("CANH BAO: vidIQ khong cap refresh_token -> bot chi chay duoc toi khi token het han.")

    blob = {"token_endpoint": meta["token_endpoint"], "client_id": client["client_id"],
            "client_secret": client.get("client_secret"), "resource": resource,
            "refresh_token": tok.get("refresh_token"), "access_token": tok["access_token"],
            "updated": int(time.time())}
    with open(os.path.join(HERE, "VIDIQ_AUTH.txt"), "w", encoding="utf-8") as f:
        f.write(base64.b64encode(json.dumps(blob).encode()).decode())

    print("Dang nhap OK. Dang lay danh sach cong cu vidIQ...")
    v = VidIQ(tok["access_token"])
    v.start()
    tools = v.list_tools()
    with open(os.path.join(HERE, "vidiq_tools.json"), "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False, indent=2)
    print(f"\nXONG. vidIQ co {len(tools)} cong cu (luu trong vidiq_tools.json).")
    print("Buoc tiep: mo VIDIQ_AUTH.txt, copy toan bo, dan vao GitHub Secret ten VIDIQ_AUTH.")


if __name__ == "__main__":
    main()
