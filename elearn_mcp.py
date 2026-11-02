"""
ISB eLearn connector for the Claude desktop app (read-only).

Easiest setup, Mac or Windows: open Terminal (Mac) or Command Prompt (Windows)
in the folder with this file and run
    Mac:      python3 elearn_mcp.py setup
    Windows:  py elearn_mcp.py setup
It installs what it needs, asks for your eLearn key, and connects Claude.
Run it again any time to update the tool or change your key.

Manual setup
  python3 -m pip install "mcp<2" httpx pypdf pypdfium2 pillow openpyxl python-docx python-pptx

Settings (in claude_desktop_config.json under "env")
  ELEARN_TOKEN          required. Your Moodle mobile token.
  ELEARN_PRIVATE_TOKEN  needed for watermarked files. Lets the server open a
                        normal web session for you, the same way the Moodle app does.
  ELEARN_COOKIE         optional fallback for watermarked files: the value of the
                        MoodleSession cookie from your browser (expires when you log out).

Getting the tokens (one time)
  1. Log in to https://elearn.isb.edu in Chrome.
  2. Open DevTools > Console, then visit in the same tab:
     https://elearn.isb.edu/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile
  3. Copy the text after token= from the red console error, then in the Console run:
       atob("PASTE_HERE").split(":::")
     The second item is ELEARN_TOKEN, the third is ELEARN_PRIVATE_TOKEN.
  Or run:  python3 elearn_mcp.py decode "moodlemobile://token=...."

Hosted mode (use it from Claude on your phone, web and desktop)
  python3 elearn_mcp.py serve
  Extra settings: PUBLIC_URL (this server's https address), ACCESS_PASSWORD
  (the password Claude's sign-in page asks for, 10+ characters), PORT.
  See README.md for the full deploy and connect steps.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit("This tool needs Python 3.10 or newer, but this is Python " + sys.version.split()[0] + ".\n"
             "Install the latest Python from https://www.python.org/downloads/ , then open a NEW "
             "Terminal / Command Prompt window and try again.")

import base64
import io
import os
import re
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

BASE = os.environ.get("ELEARN_URL", "https://elearn.isb.edu").rstrip("/")
HOST = urlparse(BASE).netloc
API = f"{BASE}/webservice/rest/server.php"
MAX_TEXT = 40_000       # characters per read_file reply
CACHE_FILES = 6         # files kept in memory
SERVER_NAME = "isb-elearn"


def parse_key(text: str) -> tuple:
    """Pull (token, private_token) out of whatever the person pasted: the red
    console line, a moodlemobile:// link, the part after token=, or a bare token."""
    text = unquote(text.strip().strip("'\""))
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return text, None
    m = re.search(r"token=([A-Za-z0-9+/=_-]+)", text)
    raw = m.group(1) if m else re.sub(r"\s", "", text)
    raw = raw.replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        parts = base64.b64decode(raw).decode().split(":::")
    except Exception:
        return None, None
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9]{16,128}", parts[1]):
        return None, None
    return parts[1], (parts[2] if len(parts) > 2 and parts[2] else None)


def decode_token(link: str) -> list:
    tok, priv = parse_key(link)
    return ["", tok or "(missing)", priv or "(not included)"]


if len(sys.argv) >= 3 and sys.argv[1] == "decode":
    parts = decode_token(sys.argv[2])
    print("ELEARN_TOKEN:        ", parts[1])
    print("ELEARN_PRIVATE_TOKEN:", parts[2])
    sys.exit(0)


# =====================================================================
# One-command setup:  python3 elearn_mcp.py setup   (Windows: py elearn_mcp.py setup)
# =====================================================================

REQUIREMENTS = ["mcp<2", "httpx", "pypdf", "pypdfium2", "pillow",
                "openpyxl", "python-docx", "python-pptx"]
KEY_URL = (f"{BASE}/admin/tool/mobile/launch.php"
           "?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile")


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _stop(msg: str) -> None:
    _say("")
    _say("STOPPED: " + msg)
    sys.exit(1)


def _claude_config_paths() -> tuple:
    """(existing config files, folder to create one in) for the Claude desktop app."""
    import glob
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        cands = [os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json")]
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        cands = [os.path.join(appdata, "Claude", "claude_desktop_config.json")]
        cands += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache",
                                        "Roaming", "Claude", "claude_desktop_config.json"))
    else:
        cands = [os.path.join(home, ".config", "Claude", "claude_desktop_config.json")]
    existing = [p for p in cands if os.path.isfile(p)]
    folders = [os.path.dirname(p) for p in cands if os.path.isdir(os.path.dirname(p))]
    return existing, (folders[0] if folders else None)


def _read_config(path: str) -> dict:
    import json
    import shutil
    with open(path, encoding="utf-8-sig") as fh:
        raw = fh.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        backup = path + ".broken-backup"
        shutil.copy2(path, backup)
        _say(f"\nClaude's settings file has a typing mistake in it (line {e.lineno}), "
             "probably from an earlier manual edit.")
        _say(f"A copy is saved at: {backup}")
        ans = input("Type YES to replace it with a clean settings file (Claude recreates its "
                    "other preferences on its own), or press Enter to stop: ").strip()
        if ans.upper() != "YES":
            _stop("Nothing was changed.")
        return {}


def _write_config(path: str, entry: dict) -> None:
    import json
    import shutil
    data = _read_config(path) if os.path.isfile(path) else {}
    if os.path.isfile(path):
        shutil.copy2(path, path + ".backup")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = data["mcpServers"] = {}
    servers[SERVER_NAME] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _missing_packages() -> list:
    """Requirements that aren't installed yet (or mcp 2.x, which this tool can't use)."""
    from importlib import metadata
    names = {"mcp<2": "mcp", "httpx": "httpx", "pypdf": "pypdf", "pypdfium2": "pypdfium2",
             "pillow": "pillow", "openpyxl": "openpyxl", "python-docx": "python-docx",
             "python-pptx": "python-pptx"}
    missing = []
    for req, dist in names.items():
        try:
            v = metadata.version(dist)
            if dist == "mcp" and int(v.split(".")[0]) >= 2:
                missing.append(req)
        except metadata.PackageNotFoundError:
            missing.append(req)
    return missing


def _pip(args: list) -> tuple:
    """Run pip, showing its progress live, and return (exit code, output)."""
    import subprocess
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "--progress-bar", "on", "--timeout", "20", "--retries", "2"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if line.startswith(("Collecting", "Downloading", "Installing", "Successfully", "ERROR")):
            _say("  " + line.rstrip()[:110])
    return proc.wait(), "".join(lines)


def _install_packages() -> None:
    import subprocess
    missing = _missing_packages()
    if not missing:
        _say("Helper libraries: already installed.")
    else:
        _say(f"Installing {len(missing)} helper libraries: {', '.join(missing)}")
        _say("This takes 1 to 3 minutes. Progress shows below.")
        code, out = _pip(missing)
        if code != 0 and "No module named pip" in out:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True)
            code, out = _pip(missing)
        if code != 0 and "externally-managed" in out:
            code, out = _pip(missing + ["--break-system-packages"])
        if code != 0:
            _say(out[-2500:])
            if "timed out" in out.lower() or "connection" in out.lower() or "resolve" in out.lower():
                _stop("Couldn't download the libraries: the internet connection failed. Campus Wi-Fi "
                      "sometimes blocks this. Try a phone hotspot and run the same command again.")
            _stop("Installing the libraries failed (details above). Run the same command again.")
    import importlib
    importlib.invalidate_caches()
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
        import httpx  # noqa: F401
    except Exception as e:
        _stop(f"The libraries installed but can't be loaded ({e}). Run the same command again.")
    _say("  Done.")


def _check_key(token: str) -> str:
    """Returns the eLearn account name, or '' if eLearn couldn't be reached."""
    import httpx
    try:
        r = httpx.post(API, timeout=30, data={"wstoken": token, "moodlewsrestformat": "json",
                                              "wsfunction": "core_webservice_get_site_info"})
        out = r.json()
    except Exception as e:
        _say(f"  Couldn't reach eLearn to test the key ({type(e).__name__}). "
             "Continuing anyway; it will be checked when Claude first uses it.")
        return ""
    if isinstance(out, dict) and out.get("exception"):
        _stop("eLearn didn't accept that key (" + str(out.get("message")) + ").\n"
              "Get a fresh one (the 'Get your eLearn key' step) and run setup again.")
    return out.get("fullname") or out.get("username") or "your account"


def run_setup() -> None:
    import shutil

    _say("")
    _say("=== ISB eLearn for Claude: setup ===")
    _say(f"Python {sys.version.split()[0]} at {sys.executable}")
    _say("")

    configs, folder = _claude_config_paths()
    if not configs and not folder:
        _stop("Couldn't find the Claude desktop app's settings.\n"
              "Install the Claude desktop app from https://claude.ai/download , open it, sign in,\n"
              "go to Settings > Developer and click 'Edit Config' once. Then run this again.")
    if not configs:
        configs = [os.path.join(folder, "claude_desktop_config.json")]

    # Reuse the key saved last time, if there is one
    old_env = {}
    for p in configs:
        try:
            import json
            with open(p, encoding="utf-8-sig") as fh:
                saved = json.loads(fh.read() or "{}")
            old_env = (saved.get("mcpServers") or {}).get(SERVER_NAME, {}).get("env", {}) or old_env
        except Exception:
            pass          # unreadable file: dealt with when saving

    _install_packages()
    _say("")

    token = priv = None
    if old_env.get("ELEARN_TOKEN"):
        _say("You already set up an eLearn key before.")
        text = input("Press Enter to keep it, or paste a new key and press Enter: ").strip()
        if not text:
            token, priv = old_env["ELEARN_TOKEN"], old_env.get("ELEARN_PRIVATE_TOKEN")
    else:
        text = ""
    while not token:
        if not text:
            _say("Paste your eLearn key: the whole red line from the browser Console that starts")
            _say("with  Failed to launch 'moodlemobile://token=...  (see the guide).")
            text = input("Key: ").strip()
        token, priv = parse_key(text)
        if not token:
            _say("  That doesn't look like the key. Copy the full red line and try again.\n")
            text = ""

    name = _check_key(token)
    if name:
        _say(f"  Key works. Connected to eLearn as: {name}")
    if not priv:
        _say("  Note: this key has no second part, so watermarked files may not open. "
             "Everything else works.")

    # Keep a copy of the tool in a fixed folder, so the downloaded file can be deleted
    install_dir = os.path.join(os.path.expanduser("~"), "elearn-mcp")
    os.makedirs(install_dir, exist_ok=True)
    tool_path = os.path.join(install_dir, "elearn_mcp.py")
    here = os.path.abspath(__file__)
    if os.path.normcase(here) != os.path.normcase(os.path.abspath(tool_path)):
        shutil.copy2(here, tool_path)

    env = {"ELEARN_TOKEN": token}
    if priv:
        env["ELEARN_PRIVATE_TOKEN"] = priv
    for k in ("ELEARN_COOKIE", "ELEARN_URL"):
        if old_env.get(k):
            env[k] = old_env[k]
    entry = {"command": sys.executable, "args": [tool_path], "env": env}
    for p in configs:
        _write_config(p, entry)

    _say("")
    _say("=== All done ===")
    _say(f"Tool saved in: {install_dir}")
    _say(f"Claude settings updated: {', '.join(configs)}")
    _say("")
    _say("Last step: fully quit the Claude app and open it again.")
    _say("  Mac:     click Claude, then press Cmd + Q.")
    _say("  Windows: right-click the Claude icon near the clock (bottom right) and choose Quit.")
    _say("Then start a new chat and ask: What's due on eLearn this week?")


def show_key() -> None:
    """Print the eLearn key saved by setup, for pasting into a hosted server."""
    import json
    for p in _claude_config_paths()[0]:
        try:
            with open(p, encoding="utf-8-sig") as fh:
                env = (json.loads(fh.read() or "{}").get("mcpServers") or {}).get(SERVER_NAME, {}).get("env", {})
        except Exception:
            continue
        if env.get("ELEARN_TOKEN"):
            _say("ELEARN_TOKEN         = " + env["ELEARN_TOKEN"])
            _say("ELEARN_PRIVATE_TOKEN = " + (env.get("ELEARN_PRIVATE_TOKEN") or "(not saved)"))
            _say("\nThese give read access to your eLearn account. Paste them only into your host's "
                 "settings, never into chats or files.")
            return
    _stop("No saved eLearn key found. Run setup first, or get a key as described at the top of this file.")


if len(sys.argv) >= 2 and sys.argv[1] == "show-key":
    show_key()
    sys.exit(0)

if len(sys.argv) >= 2 and sys.argv[1] == "setup":
    try:
        run_setup()
    except KeyboardInterrupt:
        _stop("Cancelled. Nothing more was changed.")
    sys.exit(0)

import functools  # noqa: E402
import threading  # noqa: E402

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

TOKEN = os.environ.get("ELEARN_TOKEN")
PRIVATE_TOKEN = os.environ.get("ELEARN_PRIVATE_TOKEN")
SESSION_COOKIE = os.environ.get("ELEARN_COOKIE")
if not TOKEN:
    sys.exit("Set ELEARN_TOKEN first (see the notes at the top of this file).")

# Show every date in ISB time, even on a cloud server that runs on UTC.
os.environ["TZ"] = os.environ.get("ELEARN_TIMEZONE", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

SERVE = len(sys.argv) >= 2 and sys.argv[1] == "serve"


def _setup_needed_page(public_url: str, password: str) -> None:
    """Hosted mode without PUBLIC_URL or a password: serve a page that says what
    to set (including this server's real address) instead of crashing."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def page(request):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        todo = []
        if not public_url:
            todo.append(f"Set PUBLIC_URL to:  https://{host}")
        if len(password) < 10:
            todo.append("Set ACCESS_PASSWORD to a password of at least 10 characters.")
        return PlainTextResponse("ISB eLearn connector: almost ready.\n\n" + "\n".join(todo) +
                                 "\n\nThen deploy again (in Cloud Run: Edit and deploy new revision).",
                                 status_code=503)

    print("PUBLIC_URL or ACCESS_PASSWORD missing: serving setup instructions only.", file=sys.stderr)
    uvicorn.run(Starlette(routes=[Route("/{path:path}", page)]), host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))
    sys.exit(0)

INSTRUCTIONS = (
    "Read-only access to the user's ISB eLearn (Moodle) courses. "
    "For 'what's due', use upcoming_deadlines, then assignments and forum_status to confirm "
    "submission and posting status. For course news or instructor posts, use announcements. "
    "To find a document without knowing where it is, use find_files. For group projects, "
    "my_groups gives the user's exact group name and members. Files: file_outline or search_file "
    "first for long files, read_file for text, view_pages for charts and exhibits in PDFs."
)

_kwargs: dict = {"instructions": INSTRUCTIONS}
_provider = None
if SERVE:
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
    PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
    _kwargs.update(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                   stateless_http=True, json_response=True)
    if os.environ.get("ALLOW_NO_AUTH") == "1":
        print("WARNING: running WITHOUT a password. Anyone with the URL can read your eLearn.",
              file=sys.stderr)
    else:
        if not PUBLIC_URL or len(PASSWORD) < 10:
            _setup_needed_page(PUBLIC_URL, PASSWORD)
        import remote_auth
        _provider = remote_auth.setup(_kwargs, PUBLIC_URL, PASSWORD, TOKEN)

mcp = FastMCP("isb-elearn", **_kwargs)
if _provider is not None:
    remote_auth.add_routes(mcp, _provider)

client = httpx.Client(timeout=60, follow_redirects=True)
_site: dict | None = None


def tool(fn):
    """Register a tool. On the server, run it in a worker thread so one slow
    eLearn request doesn't freeze sign-ins and other requests."""
    if not SERVE:
        return mcp.tool()(fn)
    import anyio

    @functools.wraps(fn)
    async def runner(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    mcp.tool()(runner)
    return fn


# =====================================================================
# Moodle web service helpers
# =====================================================================

def _flatten(params: dict, prefix: str = "") -> dict:
    """Moodle expects arrays as key[0]=a, key[0][field]=b."""
    flat = {}
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    flat.update(_flatten(item, f"{key}[{i}]"))
                else:
                    flat[f"{key}[{i}]"] = int(item) if isinstance(item, bool) else item
        elif isinstance(v, dict):
            flat.update(_flatten(v, key))
        elif isinstance(v, bool):
            flat[key] = int(v)
        else:
            flat[key] = v
    return flat


def call(function: str, _headers: dict | None = None, **params):
    data = {"wstoken": TOKEN, "wsfunction": function, "moodlewsrestformat": "json"}
    data.update(_flatten(params))
    r = client.post(API, data=data, headers=_headers)
    r.raise_for_status()
    out = r.json()
    if isinstance(out, dict) and out.get("exception"):
        raise RuntimeError(f"{out.get('errorcode')}: {out.get('message')}")
    return out


def site() -> dict:
    global _site
    if _site is None:
        _site = call("core_webservice_get_site_info")
    return _site


def user_id() -> int:
    return site()["userid"]


def my_course_ids() -> list[int]:
    return [c["id"] for c in call("core_enrol_get_users_courses", userid=user_id())]


def clean(html_text: str | None, limit: int = 600) -> str:
    """HTML to readable plain text, keeping paragraphs, bullets and table cells."""
    if not html_text:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "\n- ", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|tr|ul|ol|table)>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit] + ("..." if len(t) > limit else "")


def when(ts: int | None) -> str:
    if not ts:
        return "no date"
    return time.strftime("%a %d %b %Y, %I:%M %p", time.localtime(ts))


def _files(items) -> list[dict]:
    return [{"name": f.get("filename"), "file_url": f.get("fileurl")} for f in (items or [])]


# =====================================================================
# Assignments: group flag, submission status, full brief
# =====================================================================

STATUS_LABELS = {
    "new": "Not started",
    "draft": "Draft saved, not submitted",
    "submitted": "Submitted",
    "reopened": "Reopened for resubmission",
}


def _assign_index(course_ids: list[int]) -> dict[int, tuple[str, dict]]:
    """assignment id -> (course name, assignment data)"""
    if not course_ids:
        return {}
    data = call("mod_assign_get_assignments", courseids=course_ids)
    idx = {}
    for c in data.get("courses", []):
        for a in c.get("assignments", []):
            idx[a["id"]] = (c.get("fullname", ""), a)
    return idx


def _submission(assign_id: int) -> dict:
    try:
        s = call("mod_assign_get_submission_status", assignid=assign_id)
    except Exception as e:
        return {"status": "Unknown", "status_error": str(e)[:150]}
    la = s.get("lastattempt") or {}
    sub = la.get("teamsubmission") or la.get("submission") or {}
    raw = sub.get("status") or "new"
    out = {"status": STATUS_LABELS.get(raw, raw)}
    if la.get("submissionsenabled") is False:
        out["status"] = "No online submission on eLearn"
    if sub.get("timemodified") and raw != "new":
        out["last_changed"] = when(sub["timemodified"])
    fb = s.get("feedback") or {}
    grade = clean(fb.get("gradefordisplay"), 120)
    if grade:
        out["grade"] = grade
    if la.get("gradingstatus"):
        out["grading"] = la["gradingstatus"].replace("notgraded", "not graded yet")
    comments = [clean(p.get("editorfields", [{}])[0].get("text"), 800)
                for p in fb.get("plugins", []) if p.get("type") == "comments" and p.get("editorfields")]
    if comments and comments[0]:
        out["feedback_comments"] = comments[0]
    ad = s.get("assignmentdata") or {}
    if ad.get("activity"):
        out["_activity"] = ad["activity"]
    att = ad.get("attachments") or {}
    out["_extra_files"] = _files(att.get("intro")) + _files(att.get("activity"))
    return out


def _assignment_row(course: str, a: dict, with_status: bool, full: bool) -> dict:
    row = {
        "course": course,
        "name": a.get("name"),
        "assignment_id": a.get("id"),
        "due": when(a.get("duedate")),
        "type": "Group" if a.get("teamsubmission") else "Individual",
    }
    if a.get("teamsubmission") and a.get("requireallteammemberssubmit"):
        row["type"] += " (every member must click submit)"
    if a.get("cutoffdate"):
        row["hard_cutoff"] = when(a["cutoffdate"])
    status = _submission(a["id"]) if with_status else {}
    activity = status.pop("_activity", None) or a.get("activity")
    extra = status.pop("_extra_files", [])
    row.update(status)
    if full:
        limit = 6000
        row["instructions"] = clean(a.get("intro"), limit) or "(none in the description)"
        if activity:
            row["activity_instructions"] = clean(activity, limit)
        files = _files(a.get("introattachments")) + _files(a.get("activityattachments"))
        seen = {f["file_url"] for f in files}
        files += [f for f in extra if f["file_url"] not in seen]
        row["brief_files"] = files
        if files:
            row["tip"] = "Assignment questions are often inside these files. Open them with read_file."
    return row


@tool
def assignments(course_id: int | None = None, include_status: bool = True) -> list[dict]:
    """Assignments with due date, Group or Individual, my submission status
    (Not started / Draft / Submitted), grade if any, and the full brief:
    instructions text plus any attached brief files.
    Leave course_id empty to check every course.
    """
    ids = [course_id] if course_id else my_course_ids()
    idx = _assign_index(ids)
    ordered = sorted(idx.values(), key=lambda ca: ca[1].get("duedate") or 10**12)
    return [_assignment_row(c, a, include_status, full=True) for c, a in ordered]


# =====================================================================
# Forums
# =====================================================================

def _my_forum_posts(forum_id: int, cmid: int | None) -> int | None:
    """How many posts I've made in a forum. None if it can't be worked out."""
    uid = user_id()
    if cmid:
        try:
            d = call("mod_forum_get_discussion_posts_by_userid", userid=uid, cmid=cmid)
            return sum(len((x.get("posts") or {}).get("userposts") or [])
                       for x in d.get("discussions", []))
        except Exception:
            pass
    try:
        d = call("mod_forum_get_forum_discussions", forumid=forum_id, perpage=40)
        count = 0
        for disc in d.get("discussions", [])[:40]:
            posts = call("mod_forum_get_discussion_posts",
                         discussionid=disc.get("discussion") or disc.get("id"))
            count += sum(1 for p in posts.get("posts", [])
                         if (p.get("author") or {}).get("id") == uid)
        return count
    except Exception:
        return None


@tool
def forum_status(course_id: int | None = None, include_ungraded: bool = False) -> list[dict]:
    """Forums with a due date or a grade, and whether I have posted in them.
    Use this before treating a forum deadline as pending.
    Leave course_id empty to check every course.
    """
    ids = [course_id] if course_id else my_course_ids()
    names = {c["id"]: c["fullname"] for c in call("core_enrol_get_users_courses", userid=user_id())}
    forums = call("mod_forum_get_forums_by_courses", courseids=ids)
    out = []
    for f in forums:
        graded = bool(f.get("grade_forum")) or bool(f.get("assessed"))
        has_due = bool(f.get("duedate"))
        if f.get("type") == "news" or not (graded or has_due or include_ungraded):
            continue
        n = _my_forum_posts(f["id"], f.get("cmid"))
        out.append({
            "course": names.get(f.get("course"), f.get("course")),
            "forum": f.get("name"),
            "forum_id": f.get("id"),
            "due": when(f.get("duedate")),
            "graded": graded,
            "posted": "Unknown" if n is None else ("Yes" if n > 0 else "No"),
            "my_posts": n,
            "discussions": f.get("numdiscussions"),
            "instructions": clean(f.get("intro"), 1500),
            "url": f"{BASE}/mod/forum/view.php?id={f.get('cmid')}",
        })
    return out


# =====================================================================
# Courses and deadlines
# =====================================================================

@tool
def list_courses() -> list[dict]:
    """List the courses I'm enrolled in on ISB eLearn."""
    courses = call("core_enrol_get_users_courses", userid=user_id())
    return [{"id": c["id"], "name": c["fullname"], "short_name": c.get("shortname", "")}
            for c in courses]


@tool
def course_contents(course_id: int) -> list[dict]:
    """Sections, activities and files for one course. Use list_courses for the id.
    Every readable item has a file_url to pass to read_file, including
    watermarked files (which have no direct file link)."""
    sections = call("core_course_get_contents", courseid=course_id)
    result = []
    for s in sections:
        items = []
        for m in s.get("modules", []):
            files = [f for f in _files([c for c in m.get("contents", []) if c.get("type") == "file"])]
            item = {"name": m.get("name"), "type": m.get("modname"), "url": m.get("url"),
                    "description": clean(m.get("description"), 400), "files": files}
            if m.get("modname") == "forum":
                item["forum_id"] = m.get("instance")     # for forum_discussions
            links = [c.get("fileurl") for c in m.get("contents", []) if c.get("type") == "url"]
            if links:
                item["link"] = links[0]                  # where a URL activity points
            if not files and m.get("modname") in ("filewithwatermark", "resource") and m.get("url"):
                item["files"] = [{"name": m.get("name"), "file_url": m["url"],
                                  "note": "Watermarked or protected file. read_file opens it through your web session."}]
            items.append(item)
        result.append({"section": s.get("name"), "summary": clean(s.get("summary"), 400),
                       "items": items})
    return result


@tool
def upcoming_deadlines(days: int = 14) -> list[dict]:
    """Things due in the next N days across all courses, with Group or Individual,
    my submission status for assignments, and whether I've posted for forums.
    Note: eLearn usually hides assignments I've already submitted from this list;
    use assignments() to see everything."""
    now = int(time.time())
    data = call("core_calendar_get_action_events_by_timesort",
                timesortfrom=now, timesortto=now + days * 86400, limitnum=50)
    events = data.get("events", [])

    assign_courses = sorted({(e.get("course") or {}).get("id") for e in events
                             if e.get("modulename") == "assign"} - {None})
    idx = _assign_index(assign_courses) if assign_courses else {}

    out = []
    for e in events:
        row = {"name": e.get("name"),
               "course": (e.get("course") or {}).get("fullname"),
               "due": when(e.get("timesort")),
               "activity": e.get("modulename"),
               "url": e.get("url")}
        if e.get("modulename") == "assign" and e.get("instance") in idx:
            a = idx[e["instance"]][1]
            row["type"] = "Group" if a.get("teamsubmission") else "Individual"
            st = _submission(a["id"])
            row["status"] = st.get("status")
        elif e.get("modulename") == "forum":
            cmid = parse_qs(urlparse(e.get("url") or "").query).get("id", [None])[0]
            n = _my_forum_posts(e.get("instance"), int(cmid) if cmid else None)
            row["posted"] = "Unknown" if n is None else ("Yes" if n > 0 else "No")
        out.append(row)
    return out


# =====================================================================
# Web session (for watermarked files and activity pages)
# =====================================================================

web = httpx.Client(timeout=90, follow_redirects=True, headers={
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"})
_web_ready = False
_last_autologin = 0.0
_web_lock = threading.Lock()


def _is_login_page(r: httpx.Response) -> bool:
    u = urlparse(str(r.url))
    return u.netloc != HOST or "/login/" in u.path or "/auth/" in u.path


def _start_web_session():
    """Create a logged-in web session without a password.
    Uses Moodle's autologin key (the same mechanism the Moodle app uses to
    open pages in a browser), or a MoodleSession cookie if one is set."""
    global _web_ready, _last_autologin
    if SESSION_COOKIE:
        web.cookies.set("MoodleSession", SESSION_COOKIE, domain=HOST)
        _web_ready = True
        return
    if not PRIVATE_TOKEN:
        raise RuntimeError(
            "Watermarked files need ELEARN_PRIVATE_TOKEN (or ELEARN_COOKIE) in the config. "
            "See the notes at the top of elearn_mcp.py.")
    wait = 360 - (time.time() - _last_autologin)
    if wait > 0:
        raise RuntimeError(f"eLearn allows a new web login every 6 minutes. Try again in {int(wait) + 1} seconds.")
    _last_autologin = time.time()
    # Moodle only issues autologin keys to requests identifying as the Moodle app.
    k = call("tool_mobile_get_autologin_key", _headers={"User-Agent": "MoodleMobile"},
             privatetoken=PRIVATE_TOKEN)
    r = web.get(k["autologinurl"], params={"userid": user_id(), "key": k["key"],
                                           "urltogo": f"{BASE}/my/"})
    if _is_login_page(r):
        raise RuntimeError("eLearn refused the web login. Set ELEARN_COOKIE as a fallback.")
    _web_ready = True


def web_get(url: str) -> httpx.Response:
    global _web_ready
    with _web_lock:          # two tools at once must not both log in (6-minute limit)
        if not _web_ready:
            _start_web_session()
    r = web.get(url)
    if _is_login_page(r):
        with _web_lock:
            _web_ready = False
            _start_web_session()
        r = web.get(url)
        if _is_login_page(r):
            raise RuntimeError("Still sent to the login page. The session cookie or private token may have expired.")
    r.raise_for_status()
    return r


_SKIP = re.compile(r"(/theme/|/lib/|/javascript\.php|/yui|/styles\.php|/font|\.css|\.js(\?|$)|"
                   r"\.(png|jpe?g|gif|svg|ico|woff2?)(\?|$)|/user/|/login/|/course/view\.php|/my/)", re.I)


def _candidate_links(html: str, page_url: str) -> list[str]:
    """Links in an activity page that might be the real file, best guesses first."""
    found: dict[str, int] = {}

    def add(raw: str, score: int):
        u = urljoin(page_url, unescape(raw.strip()))
        q = parse_qs(urlparse(u).query)
        if "file" in q:                       # pdf.js style viewer?file=<url>
            add(unquote(q["file"][0]), score - 1)
        if urlparse(u).netloc != HOST or _SKIP.search(u) or u.split("#")[0] == page_url:
            return
        found[u] = min(score, found.get(u, 99))

    for tag, src in re.findall(r'(?is)<(iframe|embed|object)[^>]+?(?:src|data)\s*=\s*["\']([^"\']+)', html):
        add(src, 2)
    for raw in re.findall(r'(?:href|src|data|data-src|data-url|value)\s*=\s*["\']([^"\']+)["\']', html):
        low = raw.lower()
        if "filewithwatermark" in low and "pluginfile" in low:
            add(raw, 0)
        elif "pluginfile" in low or low.split("?")[0].endswith((".pdf", ".xlsx", ".docx", ".pptx")):
            add(raw, 1)
        elif "filewithwatermark" in low and "view.php" not in low:
            add(raw, 2)
        elif "forcedownload" in low or "download" in low:
            add(raw, 3)
    for raw in re.findall(r'["\'](https?:\\?/\\?/[^"\']*(?:pluginfile|filewithwatermark)[^"\']*)["\']', html):
        add(raw.replace("\\/", "/"), 1)
    return [u for u, _ in sorted(found.items(), key=lambda kv: kv[1])][:10]


def _fetch_via_web(view_url: str) -> tuple[bytes, str, str]:
    """Open an activity page with the web session and follow it to the real file."""
    r = web_get(view_url)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        return r.content, ctype, str(r.url)
    tried = []
    for u in _candidate_links(r.text, str(r.url)):
        tried.append(u)
        rr = web_get(u)
        ct = rr.headers.get("content-type", "")
        if "html" not in ct:
            return rr.content, ct, str(rr.url)
        for u2 in _candidate_links(rr.text, str(rr.url))[:5]:   # one level deeper
            if u2 in tried:
                continue
            tried.append(u2)
            r2 = web_get(u2)
            if "html" not in r2.headers.get("content-type", ""):
                return r2.content, r2.headers.get("content-type", ""), str(r2.url)
    fns = [f["name"] for f in site().get("functions", []) if "watermark" in f.get("name", "")]
    raise LookupError(
        "Opened the page but couldn't find the file in it. "
        f"Links tried: {tried or 'none found'}. "
        f"Watermark web service functions on this site: {fns or 'none'}. "
        "Open the page in Chrome with DevTools > Network, find the request that loads the PDF, "
        "and share its URL pattern so the reader can be taught it.")


# =====================================================================
# File parsing: PDF, Excel, Word, PowerPoint, text
# =====================================================================

_file_cache: dict[str, dict] = {}


def _has_image(page, depth: int = 0) -> bool:
    try:
        xobjects = page["/Resources"]["/XObject"]
        for ref in xobjects.values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form" and depth < 3 and _has_image(obj, depth + 1):
                return True
    except Exception:
        return False
    return False


def _chunk_text(text: str, size: int = 4000, label: str = "Part") -> tuple[list, list]:
    pages, labels, buf = [], [], ""
    for para in text.split("\n"):
        if buf and len(buf) + len(para) > size:
            pages.append(buf.strip())
            buf = ""
        buf += para + "\n"
    if buf.strip() or not pages:
        pages.append(buf.strip())
    labels = [f"{label} {i}" for i in range(1, len(pages) + 1)]
    return pages, labels


def _parse_pdf(data: bytes) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "").strip() for p in reader.pages]
    outline = []
    try:
        def walk(items, depth=0):
            for it in items:
                if isinstance(it, list):
                    walk(it, depth + 1)
                else:
                    outline.append({"title": ("  " * depth) + str(it.title),
                                    "page": reader.get_destination_page_number(it) + 1})
        walk(reader.outline)
    except Exception:
        outline = []
    return {"kind": "pdf", "pages": pages,
            "labels": [f"Page {i}" for i in range(1, len(pages) + 1)],
            "image_pages": {n for n, p in enumerate(reader.pages, 1) if _has_image(p)},
            "outline": outline, "bytes": data}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v).replace("\n", " ").strip()


def _parse_xlsx(data: bytes) -> dict:
    import openpyxl
    wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=True)
    pages, labels, summary = [], [], []
    for ws in wb_v.worksheets:
        wf = wb_f[ws.title]
        rows = []
        for rn, (rv, rf) in enumerate(zip(ws.iter_rows(values_only=True),
                                          wf.iter_rows(values_only=True)), start=1):
            cells = []
            for v, f in zip(rv, rf):
                if v is None and isinstance(f, str) and f.startswith("="):
                    cells.append(f)            # formula with no saved result
                else:
                    cells.append(_cell(v))
            while cells and cells[-1] == "":
                cells.pop()
            if any(cells):
                rows.append(f"R{rn}: " + " | ".join(cells))
        summary.append(f"{ws.title} ({len(rows)} rows with data)")
        if not rows:
            pages.append("(empty sheet)")
            labels.append(f"Sheet '{ws.title}'")
            continue
        for i in range(0, len(rows), 120):
            part = rows[i:i + 120]
            pages.append("\n".join(part))
            labels.append(f"Sheet '{ws.title}' rows {part[0].split(':')[0][1:]}-{part[-1].split(':')[0][1:]}")
    wb_v.close()
    wb_f.close()
    return {"kind": "xlsx", "pages": pages, "labels": labels,
            "note": "Workbook sheets: " + "; ".join(summary) + ". Cells shown as R<row>: A | B | C ..."}


def _parse_docx(data: bytes) -> dict:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(io.BytesIO(data))
    lines = []
    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            txt = p.text.strip()
            if not txt:
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading") or style == "title":
                txt = "## " + txt
            elif "list" in style:
                txt = "- " + txt
            lines.append(txt)
        elif tag == "tbl":
            for row in Table(el, d).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip().replace("\n", " ")
                    if not cells or cells[-1] != t:     # merged cells repeat
                        cells.append(t)
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    pages, labels = _chunk_text("\n".join(lines))
    return {"kind": "docx", "pages": pages, "labels": labels}


def _parse_pptx(data: bytes) -> dict:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = Presentation(io.BytesIO(data))
    pages, labels, pic_slides = [], [], set()

    def shape_text(shape, n, out):
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for s in shape.shapes:
                shape_text(s, n, out)
            return
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            pic_slides.add(n)
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                out.append("| " + " | ".join(c.text.strip().replace("\n", " ") for c in row.cells) + " |")
        elif getattr(shape, "has_chart", False) and shape.has_chart:
            try:
                ch = shape.chart
                cats = list(ch.plots[0].categories)
                for s in ch.series:
                    out.append(f"[Chart] {s.name}: " + ", ".join(
                        f"{c}={_cell(v)}" for c, v in zip(cats, s.values)))
            except Exception:
                out.append("[Chart]")
        elif shape.has_text_frame and shape.text_frame.text.strip():
            out.append(shape.text_frame.text.strip())

    for n, slide in enumerate(prs.slides, start=1):
        out = []
        for shape in slide.shapes:
            shape_text(shape, n, out)
        if n in pic_slides:
            out.append("[This slide has a picture. Its contents are not in the text.]")
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append("Speaker notes: " + slide.notes_slide.notes_text_frame.text.strip())
        pages.append("\n".join(out))
        labels.append(f"Slide {n}")
    return {"kind": "pptx", "pages": pages, "labels": labels}


def _parse(data: bytes, ctype: str, name: str) -> dict:
    name = name.lower().split("?")[0]
    if data[:4] == b"%PDF":
        return _parse_pdf(data)
    if data[:2] == b"PK":
        import zipfile
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            names = []
        if any(n.startswith("xl/") for n in names):
            return _parse_xlsx(data)
        if any(n.startswith("word/") for n in names):
            return _parse_docx(data)
        if any(n.startswith("ppt/") for n in names):
            return _parse_pptx(data)
    if name.endswith((".xls", ".doc", ".ppt")):
        return {"kind": "other", "ctype": f"old Office format ({name.rsplit('.', 1)[-1]}). "
                                          "Save it as .xlsx/.docx/.pptx to read it"}
    if ctype.startswith("text/") or "json" in ctype or "csv" in ctype or name.endswith((".csv", ".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
        if "html" in ctype:
            text = clean(text, 10**7)
        pages, labels = _chunk_text(text, 5000)
        return {"kind": "text", "pages": pages, "labels": labels}
    return {"kind": "other", "ctype": ctype or "unknown"}


def _load(file_url: str) -> dict:
    if urlparse(file_url).netloc != HOST:
        raise ValueError("Only ISB eLearn links are allowed.")
    if file_url in _file_cache:
        return _file_cache[file_url]

    if "pluginfile.php" in file_url:
        url = file_url if "/webservice/pluginfile.php" in file_url \
            else file_url.replace("/pluginfile.php", "/webservice/pluginfile.php", 1)
        r = client.get(url, params={"token": TOKEN})
        if r.status_code in (401, 403, 404) and "filewithwatermark" in file_url:
            data, ctype, final = _fetch_via_web(file_url)
        else:
            r.raise_for_status()
            data, ctype, final = r.content, r.headers.get("content-type", ""), str(r.url)
    else:
        # Activity pages (watermarked files, resources) need a web session, not the token
        data, ctype, final = _fetch_via_web(file_url)

    entry = _parse(data, ctype, urlparse(final).path)
    entry["source"] = final
    if len(_file_cache) >= CACHE_FILES:
        _file_cache.pop(next(iter(_file_cache)))
    _file_cache[file_url] = entry
    return entry


def _open(file_url: str):
    try:
        return _load(file_url), None
    except (ValueError, RuntimeError, LookupError) as e:
        return None, str(e)
    except httpx.HTTPStatusError as e:
        return None, f"eLearn returned {e.response.status_code} for {e.request.url}"


# =====================================================================
# File tools
# =====================================================================

@tool
def read_file(file_url: str, start_page: int = 1, end_page: int | None = None) -> str:
    """Read a course file: PDF, Excel (.xlsx), Word (.docx), PowerPoint (.pptx),
    text or CSV. Also accepts watermarked-file activity links from course_contents.

    Long files come back in chunks. "Pages" are PDF pages, PowerPoint slides,
    blocks of about 120 spreadsheet rows, or parts of a Word document.
    Use start_page and end_page to read any part. If a reply stops early,
    call again with the start_page it suggests. Use file_outline or
    search_file first to find the right place in long files.
    """
    f, err = _open(file_url)
    if err:
        return f"Couldn't open the file: {err}"
    if f["kind"] == "other":
        return f"File type {f['ctype']} can't be read as text here. Link: {file_url}"

    pages, labels = f["pages"], f["labels"]
    total = len(pages)
    start = min(max(1, start_page), total)
    end = max(start, min(end_page or total, total))
    image_pages = f.get("image_pages", set())

    out, size, last = [], 0, start - 1
    for n in range(start, end + 1):
        body = pages[n - 1] or "[No text here.]"
        if f["kind"] == "pdf" and (n in image_pages or len(pages[n - 1]) < 200):
            body += ("\n[This page contains a picture, such as a chart or table image. Its numbers are "
                     f"NOT in the text above. Call view_pages with start_page={n} to see it.]")
        block = f"=== {labels[n - 1]} ({n} of {total}) ===\n{body}\n"
        if out and size + len(block) > MAX_TEXT:
            break
        out.append(block)
        size += len(block)
        last = n

    header = f"[{f['kind'].upper()}: parts {start}-{last} of {total}.]"
    if f.get("note") and start == 1:
        header += " " + f["note"]
    if last < end:
        header += (f" Stopped at {last} to keep the reply readable. Call read_file again with "
                   f"start_page={last + 1}" + (f" and end_page={end}" if end_page else "") + " to continue.")
    return header + "\n\n" + "\n".join(out)


@tool
def search_file(file_url: str, query: str, max_results: int = 25) -> dict:
    """Find which pages, slides or sheet rows of a file mention a word or phrase
    (not case sensitive). Then read_file from that page number."""
    f, err = _open(file_url)
    if err:
        return {"error": err}
    if f["kind"] == "other":
        return {"error": f"Can't search a {f['ctype']} file."}
    q = query.lower()
    hits = []
    for n, text in enumerate(f["pages"], start=1):
        i = text.lower().find(q)
        if i == -1:
            continue
        snippet = re.sub(r"\s+", " ", text[max(0, i - 120): i + len(query) + 120]).strip()
        hits.append({"page": n, "where": f["labels"][n - 1], "snippet": snippet})
        if len(hits) >= max_results:
            break
    return {"total_pages": len(f["pages"]), "matches": len(hits), "results": hits}


@tool
def file_outline(file_url: str) -> dict:
    """Map of a file: number of pages/slides/sheets, PDF bookmarks, which PDF
    pages contain pictures, and the first line of each page or slide."""
    f, err = _open(file_url)
    if err:
        return {"error": err}
    if f["kind"] == "other":
        return {"error": f"Can't outline a {f['ctype']} file."}
    first = []
    for n, (label, text) in enumerate(zip(f["labels"], f["pages"]), start=1):
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "[no text]")
        first.append(f"{n}. {label}: {line[:70]}")
    out = {"type": f["kind"], "total_pages": len(f["pages"]), "first_line_of_each": first}
    if f.get("note"):
        out["note"] = f["note"]
    if f["kind"] == "pdf":
        out["bookmarks"] = f["outline"]
        out["pages_with_pictures"] = sorted(f["image_pages"])
    return out


@tool
def view_pages(file_url: str, start_page: int, end_page: int | None = None,
               sharp: bool = False) -> list:
    """See PDF pages as images, exactly as they look on screen.
    Use for exhibits, charts, graphs and tables that are pictures, since their
    numbers don't come through read_file. Up to 3 pages per call. Set sharp=True
    for one page at higher resolution when labels are small."""
    f, err = _open(file_url)
    if err:
        return [f"Couldn't open the file: {err}"]
    if f["kind"] != "pdf":
        return ["view_pages works on PDFs. Use read_file for other files."]

    import pypdfium2 as pdfium
    total = len(f["pages"])
    start = min(max(1, start_page), total)
    end = min(end_page or start, total, start + (1 if sharp else 3) - 1)
    scale = 2.6 if sharp else 1.6

    pdf = pdfium.PdfDocument(f["bytes"])
    out: list = []
    try:
        for n in range(start, end + 1):
            pil = pdf[n - 1].render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=80 if sharp else 72, optimize=True)
            out.append(f"Page {n} of {total}:")
            out.append(Image(data=buf.getvalue(), format="jpeg"))
    finally:
        pdf.close()
    if (end_page or start) > end:
        out.append(f"Showed pages {start}-{end}. Call view_pages with start_page={end + 1} for more.")
    return out


# =====================================================================
# Shared lookups (cached for 10 minutes)
# =====================================================================

_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 600


def cached(key: str, make):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = make()
    _cache[key] = (time.time(), value)
    return value


def course_names() -> dict[int, str]:
    return cached("courses", lambda: {c["id"]: c["fullname"] for c in
                                      call("core_enrol_get_users_courses", userid=user_id())})


def contents_of(course_id: int) -> list:
    return cached(f"contents:{course_id}", lambda: call("core_course_get_contents", courseid=course_id))


def _course_ids(course_id: int | None) -> list[int]:
    return [course_id] if course_id else list(course_names())


def _post_files(items) -> list[dict]:
    return [{"name": a.get("filename"), "file_url": a.get("fileurl") or a.get("url")}
            for a in (items or [])]


# =====================================================================
# Announcements and forum threads
# =====================================================================

@tool
def announcements(course_id: int | None = None, days: int = 21, limit: int = 25) -> list[dict]:
    """Recent posts in course Announcements (news) forums: instructor and TA
    notices such as project instructions, deadline changes and form links.
    Newest first, with the full message and any attached files.
    Leave course_id empty to check every course."""
    names = course_names()
    since = time.time() - days * 86400
    forums = [f for f in call("mod_forum_get_forums_by_courses", courseids=_course_ids(course_id))
              if f.get("type") == "news"]
    posts = []
    for f in forums:
        try:
            d = call("mod_forum_get_forum_discussions", forumid=f["id"], perpage=15)
        except Exception as e:
            posts.append({"course": names.get(f.get("course")), "error": str(e)[:150]})
            continue
        for disc in d.get("discussions", []):
            ts = disc.get("created") or disc.get("timemodified") or 0
            if ts < since:
                continue
            posts.append({
                "_ts": ts,
                "course": names.get(f.get("course"), f.get("course")),
                "title": disc.get("subject") or disc.get("name"),
                "posted": when(ts),
                "by": disc.get("userfullname"),
                "message": clean(disc.get("message"), 3000),
                "files": _post_files(disc.get("attachments")),
                "discussion_id": disc.get("discussion"),
            })
    posts.sort(key=lambda p: p.get("_ts", 0), reverse=True)
    for p in posts:
        p.pop("_ts", None)
    return posts[:limit]


@tool
def forum_discussions(forum_id: int, limit: int = 30) -> list[dict]:
    """List the threads in one forum (newest activity first): title, who started
    it, replies, and a preview. Get forum_id from forum_status or course_contents.
    Then use read_discussion to read a thread."""
    d = call("mod_forum_get_forum_discussions", forumid=forum_id, perpage=min(limit, 100))
    return [{
        "title": x.get("subject") or x.get("name"),
        "started_by": x.get("userfullname"),
        "started": when(x.get("created")),
        "last_activity": when(x.get("timemodified")),
        "replies": x.get("numreplies"),
        "preview": clean(x.get("message"), 300),
        "discussion_id": x.get("discussion"),
    } for x in d.get("discussions", [])[:limit]]


@tool
def read_discussion(discussion_id: int, max_chars: int = 30000) -> dict:
    """Read every post in a forum thread in order: author, time, full text and
    attachments. Use it for classmates' forum answers or a full announcement thread."""
    d = call("mod_forum_get_discussion_posts", discussionid=discussion_id,
             sortby="created", sortdirection="ASC")
    posts, used = [], 0
    raw = d.get("posts", [])
    for p in raw:
        text = clean(p.get("message"), 6000)
        if posts and used + len(text) > max_chars:
            break
        used += len(text)
        posts.append({
            "post_id": p.get("id"),
            "reply_to": p.get("parentid") or None,
            "by": (p.get("author") or {}).get("fullname"),
            "posted": when(p.get("timecreated")),
            "subject": p.get("subject"),
            "text": text,
            "files": _post_files(p.get("attachments")),
        })
    out = {"posts_shown": len(posts), "total_posts": len(raw), "posts": posts}
    if len(posts) < len(raw):
        out["note"] = "Stopped early to keep the reply readable. Ask with a larger max_chars for more."
    return out


# =====================================================================
# Grades, groups, quizzes, calendar
# =====================================================================

@tool
def grades(course_id: int | None = None) -> list[dict]:
    """My grades. With no course_id: the overall grade in each course.
    With a course_id: every graded item in that course (assignments, quizzes,
    participation) with grade, range, percentage, weight and feedback."""
    names = course_names()
    if not course_id:
        d = call("gradereport_overview_get_course_grades", userid=user_id())
        return [{"course": names.get(g.get("courseid"), g.get("courseid")),
                 "course_id": g.get("courseid"),
                 "grade": g.get("grade") or "-",
                 "rank": g.get("rank")} for g in d.get("grades", [])]
    d = call("gradereport_user_get_grade_items", courseid=course_id, userid=user_id())
    users = d.get("usergrades") or []
    out = []
    for it in (users[0].get("gradeitems", []) if users else []):
        name = it.get("itemname") or ("Course total" if it.get("itemtype") == "course" else
                                      "Category total" if it.get("itemtype") == "category" else "")
        row = {"item": name,
               "type": it.get("itemmodule") or it.get("itemtype"),
               "grade": it.get("gradeformatted") or "-",
               "range": it.get("rangeformatted"),
               "percentage": it.get("percentageformatted"),
               "weight": it.get("weightformatted")}
        fb = clean(it.get("feedback"), 800)
        if fb:
            row["feedback"] = fb
        out.append({k: v for k, v in row.items() if v not in (None, "")})
    return out


@tool
def my_groups(course_id: int | None = None, include_members: bool = True) -> list[dict]:
    """My groups in each course (or one course), with the exact group name as
    eLearn shows it, and the other members' names and emails. Use for group
    projects and forms that ask for the group name."""
    names = course_names()
    d = call("core_group_get_course_user_groups", courseid=course_id or 0, userid=user_id())
    out = []
    for g in d.get("groups", []):
        row = {"course": names.get(g.get("courseid"), g.get("courseid")),
               "group": g.get("name"), "group_id": g.get("id")}
        if include_members:
            try:
                people = call("core_enrol_get_enrolled_users", courseid=g["courseid"], options=[
                    {"name": "groupid", "value": g["id"]},
                    {"name": "userfields", "value": "id,fullname,email"}])
                row["members"] = [{"name": p.get("fullname"), "email": p.get("email")} for p in people]
            except Exception as e:
                row["members_error"] = f"eLearn didn't share the member list ({str(e)[:120]})"
        out.append(row)
    if not out:
        return [{"note": "No groups found. Group lists are sometimes posted as a file instead; "
                         "try find_files with 'group'."}]
    return out


@tool
def quizzes(course_id: int | None = None) -> list[dict]:
    """Quizzes with open and close times, time limit, attempts allowed,
    my attempts so far and my best grade. Leave course_id empty for every course."""
    names = course_names()
    d = call("mod_quiz_get_quizzes_by_courses", courseids=_course_ids(course_id))
    out = []
    for q in sorted(d.get("quizzes", []), key=lambda q: q.get("timeclose") or 10**12):
        row = {"course": names.get(q.get("course"), q.get("course")),
               "quiz": q.get("name"),
               "opens": when(q.get("timeopen")),
               "closes": when(q.get("timeclose")),
               "time_limit": f"{q['timelimit'] // 60} min" if q.get("timelimit") else "none",
               "attempts_allowed": q.get("attempts") or "unlimited",
               "instructions": clean(q.get("intro"), 800)}
        try:
            att = call("mod_quiz_get_user_attempts", quizid=q["id"], status="all")
            row["my_attempts"] = len(att.get("attempts", []))
            best = call("mod_quiz_get_user_best_grade", quizid=q["id"])
            if best.get("hasgrade"):
                row["best_grade"] = f"{best.get('grade'):g} / {q.get('grade', 0):g}"
        except Exception:
            pass
        out.append(row)
    return out


@tool
def calendar(days: int = 7, course_id: int | None = None) -> list[dict]:
    """Everything on my eLearn calendar in the next N days: due dates, quiz
    windows, course events, sessions and personal events, with location if set."""
    names = course_names()
    now = int(time.time())
    d = call("core_calendar_get_calendar_events",
             events={"courseids": _course_ids(course_id)},
             options={"userevents": True, "siteevents": True,
                      "timestart": now - 3600, "timeend": now + days * 86400})
    out = []
    for e in sorted(d.get("events", []), key=lambda e: e.get("timestart", 0)):
        row = {"name": e.get("name"),
               "course": names.get(e.get("courseid")) or ("Personal" if e.get("eventtype") == "user" else None),
               "type": e.get("modulename") or e.get("eventtype"),
               "starts": when(e.get("timestart"))}
        if e.get("timeduration"):
            row["ends"] = when(e["timestart"] + e["timeduration"])
        if e.get("location"):
            row["location"] = e["location"]
        desc = clean(e.get("description"), 300)
        if desc:
            row["details"] = desc
        out.append({k: v for k, v in row.items() if v})
    return out


# =====================================================================
# Finding files across courses
# =====================================================================

def _walk_files(course_ids: list[int]):
    """Yield (course, section, item, file) for every file-like thing."""
    names = course_names()
    for cid in course_ids:
        try:
            sections = contents_of(cid)
        except Exception:
            continue
        for s in sections:
            for m in s.get("modules", []):
                files = [c for c in m.get("contents", []) if c.get("type") == "file"]
                if not files and m.get("modname") in ("filewithwatermark", "resource", "url",
                                                      "page", "assign", "forum") and m.get("url"):
                    files = [{"filename": m.get("name"), "fileurl": m["url"]}]
                for f in files:
                    yield names.get(cid, cid), s.get("name"), m, f


@tool
def find_files(query: str, course_id: int | None = None, max_results: int = 25) -> list[dict]:
    """Search file and activity names across all my courses (or one course).
    Every word of the query must appear in the course, section, activity or
    file name, e.g. 'mkts group project' or 'case jsw'. Returns file_urls for read_file."""
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    hits = []
    for course, section, m, f in _walk_files(_course_ids(course_id)):
        hay = " ".join(str(x) for x in (course, section, m.get("name"), f.get("filename"),
                                        clean(m.get("description"), 300))).lower()
        if all(w in hay for w in words):
            hits.append({"course": course, "section": section, "activity": m.get("name"),
                         "file": f.get("filename"), "file_url": f.get("fileurl"),
                         "added": when(f.get("timecreated") or f.get("timemodified"))
                         if (f.get("timecreated") or f.get("timemodified")) else None})
            if len(hits) >= max_results:
                break
    return hits or [{"note": f"Nothing matched '{query}'. Try fewer or different words."}]


@tool
def recent_files(days: int = 7, course_id: int | None = None) -> list[dict]:
    """Files added or updated in the last N days across my courses, newest
    first: new slides, cases, readings and briefs. (Watermarked files carry no
    date, so check course_contents for those.)"""
    since = time.time() - days * 86400
    out = []
    for course, section, m, f in _walk_files(_course_ids(course_id)):
        ts = f.get("timemodified") or f.get("timecreated")
        if ts and ts >= since:
            out.append({"_ts": ts, "course": course, "section": section, "activity": m.get("name"),
                        "file": f.get("filename"), "file_url": f.get("fileurl"), "updated": when(ts)})
    out.sort(key=lambda r: r["_ts"], reverse=True)
    for r in out:
        r.pop("_ts")
    return out or [{"note": f"No dated files changed in the last {days} days."}]


if __name__ == "__main__":
    if SERVE:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
