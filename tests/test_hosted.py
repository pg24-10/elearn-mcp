"""End-to-end test of hosted mode against the fake eLearn.

Starts tests/fake_moodle.py and the connector in serve mode, walks the same
OAuth sign-in Claude does (register, authorize, password page, token, refresh),
then calls every tool over MCP. Also restarts the server to prove sign-ins
survive restarts.

Run from the repo folder:  python tests/test_hosted.py
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://localhost:8765"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "correct horse battery"
ENV = dict(os.environ, ELEARN_URL="http://127.0.0.1:8099",
           ELEARN_TOKEN="testtoken0123456789abcdef01234567",
           PUBLIC_URL=BASE, ACCESS_PASSWORD=PASSWORD, PORT="8765",
           NO_PROXY="*", no_proxy="*")
for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    ENV.pop(k, None)

failures = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def start_server():
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "elearn_mcp.py"), "serve"], env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                return p
        except Exception:
            time.sleep(0.25)
    p.kill()
    print(p.stdout.read())
    sys.exit("server didn't start")


async def call_tools(token: str) -> None:
    async with streamablehttp_client(f"{BASE}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", names)
            for n in ("announcements", "forum_discussions", "read_discussion", "grades", "my_groups",
                      "quizzes", "calendar", "find_files", "recent_files", "list_courses", "assignments"):
                check(n in names, f"tool listed: {n}")

            async def run(name, args=None):
                res = await s.call_tool(name, args or {})
                text = "".join(getattr(c, "text", "") for c in res.content)
                check(not res.isError, f"{name}{args or ''} ran without error")
                if res.isError:
                    print("   ", text[:400])
                return text

            t = await run("announcements")
            check("Submit choice of project" in t and "Old notice" not in t, "announcements: recent only")
            check(t.find("Submit choice") < t.find("Group project"), "announcements: newest first")
            check("<strong>" not in t and "JSW" in t, "announcements: html cleaned")
            t = await run("forum_discussions", {"forum_id": 58963})
            check("75413" in t, "forum_discussions: ids")
            t = await run("read_discussion", {"discussion_id": 75273})
            check("Is the deadline firm?" in t and "q.png" in t, "read_discussion: posts and files")
            t = await run("grades")
            check("B+" in t and "MKTS" in t, "grades overview")
            t = await run("grades", {"course_id": 11056})
            check("Quiz 1" in t and "Course total" in t and "Good work" in t, "grades per course")
            t = await run("my_groups")
            check("MKTS-Section B-Group 7" in t and "Ritika Asnani" in t, "my_groups with members")
            t = await run("quizzes")
            check("15 min" in t and "7.5 / 10" in t, "quizzes: limit and best grade")
            t = await run("calendar", {"days": 7, "course_id": 11056})
            check("AC4 LT" in t and "Personal" in t, "calendar events")
            t = await run("find_files", {"query": "mkts group project"})
            check("MKTS Group Project.pdf" in t, "find_files: by words")
            t = await run("find_files", {"query": "jsw"})
            check("filewithwatermark" in t, "find_files: watermarked items")
            t = await run("recent_files", {"days": 7})
            check("MKTS Group Project.pdf" in t and "s1.pptx" not in t, "recent_files window")
            t = await run("course_contents", {"course_id": 11056})
            check('"forum_id": 58963' in t or '"forum_id":58963' in t, "course_contents: forum_id")
            check("hbr.org" in t, "course_contents: url link")
            t = await run("assignments", {"course_id": 11056})
            check(t.find("Sooner one") < t.find("Later one"), "assignments sorted by due date")
            check("+05:30" not in t, "dates are plain")


def main():
    fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_moodle.py"), "8099"])
    srv = start_server()
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    try:
        prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
        check(prm["authorization_servers"][0].rstrip("/") == BASE, "protected resource metadata")
        meta = c.get("/.well-known/oauth-authorization-server").json()
        check(meta["registration_endpoint"].endswith("/register"), "auth server metadata")

        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"accept": "application/json, text/event-stream"})
        check(r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""),
              "no token -> 401 with resource metadata")

        bad = c.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "x",
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"]})
        check(bad.status_code == 400, "register: foreign redirect refused")

        reg = c.post("/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude",
                                        "token_endpoint_auth_method": "client_secret_post",
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"]}).json()
        cid, csecret = reg["client_id"], reg.get("client_secret")
        check(bool(cid) and bool(csecret), "register: got client id and secret")

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CALLBACK,
                                        "code_challenge": challenge, "code_challenge_method": "S256",
                                        "state": "st8", "resource": f"{BASE}/mcp"})
        check(r.status_code == 302 and "/login?req=" in r.headers["location"], "authorize -> password page")
        login_url = r.headers["location"]
        page = c.get(login_url)
        check(page.status_code == 200 and "Claude" in page.text and 'type="password"' in page.text,
              "password page renders")
        req = parse_qs(urlparse(login_url).query)["req"][0]
        wrong = c.post("/login", data={"req": req, "password": "nope"})
        check(wrong.status_code == 401 and "Wrong password" in wrong.text, "wrong password rejected")
        ok = c.post("/login", data={"req": req, "password": PASSWORD})
        loc = ok.headers.get("location", "")
        check(ok.status_code == 302 and loc.startswith(CALLBACK), "right password -> back to Claude")
        q = parse_qs(urlparse(loc).query)
        check(q.get("state") == ["st8"], "state passed through")

        tok = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                     "redirect_uri": CALLBACK, "client_id": cid, "client_secret": csecret,
                                     "code_verifier": verifier})
        check(tok.status_code == 200, f"code -> tokens ({tok.status_code} {tok.text[:200]})")
        tok = tok.json()
        badpkce = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                         "redirect_uri": CALLBACK, "client_id": cid, "client_secret": csecret,
                                         "code_verifier": "wrong" * 10})
        check(badpkce.status_code == 400, "wrong PKCE verifier refused")
        forged = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"authorization": "Bearer " + tok["access_token"][:-3] + "AAA",
                                 "accept": "application/json, text/event-stream"})
        check(forged.status_code == 401, "tampered token refused")

        asyncio.run(call_tools(tok["access_token"]))

        ref = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                     "client_id": cid, "client_secret": csecret})
        check(ref.status_code == 200 and ref.json().get("access_token"), "refresh token works")

        # Restart: tokens and client registration must survive (no database)
        srv.terminate()
        srv.wait(10)
        srv = start_server()
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"authorization": f"Bearer {tok['access_token']}",
                            "accept": "application/json, text/event-stream",
                            "content-type": "application/json"})
        check(r.status_code != 401, f"access token valid after restart ({r.status_code})")
        ref2 = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": ref.json()["refresh_token"],
                                      "client_id": cid, "client_secret": csecret})
        check(ref2.status_code == 200, "refresh after restart works")
    finally:
        srv.terminate()
        fake.terminate()
    print()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
