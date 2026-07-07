#!/usr/bin/env python3
"""
Reddit Saved Archiver
=====================
A macOS tkinter app that downloads the Reddit posts you've saved (other
people's posts, not your own submissions) with full metadata, media, and
comment trees.

Two sources, used together:
  1. The live API listing of your saved items. Reddit caps this listing at
     roughly your 1,000 most recent saves.
  2. saved_posts.csv from Reddit's data export (Settings -> Request my data),
     which lists everything you have EVER saved. The app fetches those older
     posts by ID, 100 at a time.

Requires:  pip3 install praw
Optional:  yt-dlp + ffmpeg (for v.redd.it and external videos)

Run:  python3 reddit_saved_archiver.py
"""

import csv
import html
import json
import os
import queue
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

try:
    import praw
    import prawcore
    import requests  # dependency of praw/prawcore
except ImportError:
    praw = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
APP_NAME = "Reddit Saved Archiver"
APP_VERSION = "1.3"
CONFIG_DIR = Path.home() / "Library" / "Application Support" / "RedditSavedArchiver"
CONFIG_PATH = CONFIG_DIR / "config.json"

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
OAUTH_SCOPES = ["identity", "history", "read"]

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PERMALINK_ID_RE = re.compile(r"/comments/([a-z0-9]{4,10})", re.I)
COMMENT_REF_RE = re.compile(r"/comments/([a-z0-9]{4,10})/[^/]+/([a-z0-9]{4,10})", re.I)
BARE_ID_RE = re.compile(r"^(t3_)?([a-z0-9]{4,8})$", re.I)
HEADER_WORDS = {"id", "permalink", "direction", "subreddit", "title", "post",
                "link", "href", "url", "name", "date", "created", "author"}

DIRECT_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm")

COMMENT_MODES = {
    "Top-loaded (fast, 1 request/post)": "loaded",
    "Full threads (slow, many requests)": "full",
    "Skip comments": "skip",
}
COMMENT_MODE_LABELS = {v: k for k, v in COMMENT_MODES.items()}

INDEX_FIELDS = ["id", "archived_at", "created_utc", "subreddit", "author",
                "score", "num_comments", "title", "folder", "media_count",
                "first_media", "save_order", "save_source", "saved_via",
                "saved_comment_ids", "status"]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
VIDEO_EXTS = (".mp4", ".webm")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "client_id": "",
    "client_secret": "",
    "username": "",
    "auth_mode": "oauth",          # "oauth" or "password"
    "remember_password": False,
    "password": "",
    "refresh_token": "",
    "output_dir": str(Path.home() / "RedditSaved"),
    "csv_path": "",
    "comments_csv_path": "",
    "include_saved_comments": True,
    "use_listing": True,
    "download_media": True,
    "external_media": True,
    "comments_mode": "loaded",
    "skip_existing": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out = dict(cfg)
    if not out.get("remember_password"):
        out["password"] = ""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_date(created_utc):
    try:
        return datetime.fromtimestamp(float(created_utc), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "0000-00-00"


def ts_to_iso(created_utc):
    try:
        return datetime.fromtimestamp(float(created_utc), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def sanitize(text, maxlen=50):
    """Filesystem-safe slug from arbitrary text."""
    text = (text or "").strip()
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-.")
    return text[:maxlen].rstrip("-.") or "untitled"


def gk(obj, name, default=None):
    """getattr/dict.get that works on both praw objects and raw dicts."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def find_binary(name):
    """Locate a binary, checking PATH plus common Homebrew locations."""
    p = shutil.which(name)
    if p:
        return p
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def extract_post_ids_from_csv(path):
    """Pull base36 post IDs out of Reddit's saved_posts.csv (or anything
    containing permalinks / IDs). Returns a de-duplicated, ordered list."""
    ids, seen = [], set()
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            found = None
            for cell in row:
                m = PERMALINK_ID_RE.search((cell or "").strip())
                if m:
                    found = m.group(1).lower()
                    break
            if not found:
                for cell in row:
                    m = BARE_ID_RE.match((cell or "").strip())
                    if m and m.group(2).lower() not in HEADER_WORDS:
                        found = m.group(2).lower()
                        break
            if found and found not in seen:
                seen.add(found)
                ids.append(found)
    return ids


def extract_saved_comment_refs_from_csv(path):
    """Pull (post_id, comment_id) pairs out of Reddit's saved_comments.csv.
    Returns an ordered, de-duplicated list."""
    refs, seen = [], set()
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            for cell in row:
                m = COMMENT_REF_RE.search((cell or "").strip())
                if m:
                    pair = (m.group(1).lower(), m.group(2).lower())
                    if pair not in seen:
                        seen.add(pair)
                        refs.append(pair)
                    break
    return refs


def mark_saved_comments(tree, saved_ids):
    """Flag nodes in a serialized comment tree whose id is in saved_ids.
    Returns the set of ids that were found."""
    found = set()

    def walk(nodes):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if n.get("id") in saved_ids:
                n["saved_by_user"] = True
                found.add(n["id"])
            walk(n.get("replies", []))
    walk(tree or [])
    return found


def find_comment_node(tree, cid):
    for n in tree or []:
        if not isinstance(n, dict):
            continue
        if n.get("id") == cid:
            return n
        hit = find_comment_node(n.get("replies", []), cid)
        if hit:
            return hit
    return None


# --------------------------------------------------------------------------
# Reddit auth
# --------------------------------------------------------------------------
def make_user_agent(username):
    who = username or "unknown"
    return f"macos:reddit-saved-archiver:v{APP_VERSION} (by /u/{who})"


def build_reddit(cfg):
    """Create an authenticated praw.Reddit from config."""
    common = dict(
        client_id=cfg["client_id"].strip(),
        client_secret=cfg["client_secret"].strip(),
        user_agent=make_user_agent(cfg.get("username", "").strip()),
        check_for_async=False,
        ratelimit_seconds=600,
    )
    if cfg.get("auth_mode") == "password":
        return praw.Reddit(username=cfg["username"].strip(),
                           password=cfg["password"],
                           **common)
    token = cfg.get("refresh_token", "").strip()
    if not token:
        raise RuntimeError("No refresh token saved yet - click 'Authorize in browser' first, "
                           "or switch to password mode.")
    return praw.Reddit(refresh_token=token, **common)


def obtain_refresh_token(client_id, client_secret, log, stop_event):
    """Authorization-code flow: open the browser, catch the redirect on
    localhost, exchange the code for a permanent refresh token."""
    reddit = praw.Reddit(client_id=client_id.strip(),
                         client_secret=client_secret.strip(),
                         redirect_uri=REDIRECT_URI,
                         user_agent=make_user_agent(""),
                         check_for_async=False)
    state = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    url = reddit.auth.url(scopes=OAUTH_SCOPES, state=state, duration="permanent")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("localhost", REDIRECT_PORT))
    except OSError:
        server.close()
        raise RuntimeError(f"Port {REDIRECT_PORT} is in use. Close whatever is using it and try again.")
    server.listen(1)
    server.settimeout(2.0)

    log("Opening Reddit in your browser - approve the app there...")
    webbrowser.open(url)

    deadline = time.time() + 300
    conn = None
    try:
        while time.time() < deadline:
            if stop_event.is_set():
                raise RuntimeError("Authorization cancelled.")
            try:
                conn, _ = server.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            raise RuntimeError("Timed out waiting for the browser redirect (5 min).")

        data = conn.recv(8192).decode("utf-8", errors="ignore")
        request_line = data.split("\r\n", 1)[0]
        parts = request_line.split(" ")
        qs = parse_qs(urlparse(parts[1]).query) if len(parts) >= 2 else {}

        def respond(msg):
            body = (f"<html><body style='font-family:sans-serif;padding:2em'>"
                    f"<h2>{msg}</h2><p>You can close this tab and return to "
                    f"{APP_NAME}.</p></body></html>")
            conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                          f"Content-Length: {len(body)}\r\n\r\n{body}").encode())

        if "error" in qs:
            respond("Authorization was denied.")
            raise RuntimeError(f"Reddit returned error: {qs['error'][0]}")
        if qs.get("state", [""])[0] != state:
            respond("State mismatch - please try again.")
            raise RuntimeError("OAuth state mismatch (possible stale request). Try again.")
        code = qs.get("code", [""])[0]
        if not code:
            respond("No authorization code received.")
            raise RuntimeError("No authorization code in the redirect.")
        respond("Authorized!")
    finally:
        if conn:
            conn.close()
        server.close()

    token = reddit.auth.authorize(code)
    if not token:
        raise RuntimeError("Reddit did not return a refresh token (check the app's redirect URI "
                           f"is exactly {REDIRECT_URI}).")
    return token


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------
def submission_to_dict(s):
    """Everything Reddit returned for the post, JSON-safe."""
    d = {}
    for k, v in vars(s).items():
        if k.startswith("_"):
            continue
        if k in ("author", "subreddit"):
            d[k] = str(v) if v is not None else None
        else:
            d[k] = v
    return d


def serialize_comment(c):
    author = getattr(c, "author", None)
    return {
        "id": getattr(c, "id", None),
        "author": str(author) if author else None,
        "score": getattr(c, "score", None),
        "created_utc": getattr(c, "created_utc", None),
        "edited": bool(getattr(c, "edited", False)),
        "is_submitter": getattr(c, "is_submitter", False),
        "stickied": getattr(c, "stickied", False),
        "distinguished": getattr(c, "distinguished", None),
        "body": getattr(c, "body", ""),
        "body_html": getattr(c, "body_html", None),
        "replies": [serialize_comment(r) for r in getattr(c, "replies", [])],
    }


def fetch_comment_tree(s, mode, log):
    """Returns (list_of_comment_dicts, count) or (None, 0) when skipped."""
    if mode == "skip":
        return None, 0
    try:
        s.comment_sort = "top"
        limit = None if mode == "full" else 0
        s.comments.replace_more(limit=limit)
        tree = [serialize_comment(c) for c in s.comments]

        def count(nodes):
            return sum(1 + count(n["replies"]) for n in nodes)
        return tree, count(tree)
    except Exception as e:  # noqa: BLE001 - archive what we can, note the rest
        log(f"    comments unavailable: {e.__class__.__name__}: {e}")
        return [{"error": f"{e.__class__.__name__}: {e}"}], 0


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------
def render_markdown(d, comments, max_top=15, max_depth=3):
    author = d.get("author") or "[deleted]"
    lines = [
        f"# {d.get('title', '(no title)')}",
        "",
        f"- **Subreddit:** r/{d.get('subreddit')}",
        f"- **Author:** u/{author}",
        f"- **Posted:** {ts_to_iso(d.get('created_utc'))}",
        f"- **Score:** {d.get('score')} ({d.get('upvote_ratio', '?')} upvoted) | "
        f"**Comments:** {d.get('num_comments')}",
        f"- **Link:** {d.get('url')}",
        f"- **Permalink:** https://www.reddit.com{d.get('permalink', '')}",
    ]
    if d.get("link_flair_text"):
        lines.append(f"- **Flair:** {d['link_flair_text']}")
    if d.get("removed_by_category"):
        lines.append(f"- **Note:** post was removed ({d['removed_by_category']})")
    _ar = d.get("_archive") or {}
    if _ar.get("saved_comment_ids"):
        lines.append(f"- **You saved {len(_ar['saved_comment_ids'])} comment(s) in this thread** "
                     f"(ids: {', '.join(_ar['saved_comment_ids'])})")
    lines.append("")

    selftext = d.get("selftext") or ""
    if selftext and selftext not in ("[removed]", "[deleted]"):
        lines += ["---", "", selftext, ""]
    elif selftext:
        lines += ["---", "", f"*(selftext {selftext})*", ""]

    if comments:
        lines += ["---", "", f"## Comments (top {max_top}, depth {max_depth} - full tree in comments.json)", ""]

        def walk(node, depth):
            if depth > max_depth or "body" not in node:
                return
            indent = "  " * depth
            who = node.get("author") or "[deleted]"
            body = (node.get("body") or "").replace("\r", "")
            body = body.replace("\n", f"\n{indent}  ")
            lines.append(f"{indent}- **u/{who}** ({node.get('score')} pts): {body}")
            for r in node.get("replies", []):
                walk(r, depth + 1)

        for top in comments[:max_top]:
            walk(top, 0)
            lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# HTML viewer rendering (offline, file:// friendly - no server needed)
# --------------------------------------------------------------------------
_POST_CSS = """
*{box-sizing:border-box}body{margin:0;background:#dae0e6;font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a1b}
a{color:#0079d3;text-decoration:none}a:hover{text-decoration:underline}
.topbar{position:sticky;top:0;background:#fff;border-bottom:1px solid #edeff1;padding:10px 16px;display:flex;gap:16px;align-items:center;z-index:5}
.topbar .sub{font-weight:700}
.wrap{max-width:860px;margin:16px auto;padding:0 12px}
.card{background:#fff;border:1px solid #ccc;border-radius:6px;padding:14px 16px;margin-bottom:14px}
h1{font-size:21px;margin:.2em 0 .4em}
.meta{color:#787c7e;font-size:12.5px;margin-bottom:10px}.meta b{color:#1a1a1b}
.badge{font-size:11px;border-radius:10px;padding:1px 7px;margin-left:6px;vertical-align:1px}
.flair{background:#edeff1;color:#1a1a1b}.nsfw{background:#ff585b;color:#fff}.rm{background:#ffb000;color:#1a1a1b}
.score{color:#ff4500;font-weight:700}
.media img,.media video{max-width:100%;border-radius:4px;display:block;margin:6px 0}
.linkrow{font-size:13px;margin:8px 0;word-break:break-all}
.md p{margin:.5em 0}.md blockquote{border-left:4px solid #c5c1ad;margin:.5em 0;padding:0 8px;color:#4f5355}
.md pre{background:#f6f7f8;padding:8px;border-radius:4px;overflow-x:auto}
.md code{background:#f6f7f8;padding:1px 4px;border-radius:3px;font-size:13px}
.md table{border-collapse:collapse}.md th,.md td{border:1px solid #edeff1;padding:3px 8px}
h2{font-size:15px;color:#555;margin:.2em 0 .6em}
details.c{margin:6px 0}
details.c>summary{cursor:pointer;list-style:none;font-size:12.5px;color:#787c7e}
details.c>summary::before{content:'[\\2013] ';color:#a5a4a4;font-family:monospace}
details.c:not([open])>summary::before{content:'[+] '}
.cu{font-weight:700;color:#1a1a1b}
.op{background:#0079d3;color:#fff}.mod{background:#46d160;color:#fff}.pin{background:#edeff1}
.sv{background:#f5c518;color:#1a1a1b;font-weight:700}
.svhl{background:#fffdf0;border-left-color:#f5c518 !important}
.cb{margin:2px 0 4px 6px;padding-left:12px;border-left:2px solid #edeff1}
.cb:hover{border-left-color:#b8bcc0}
.cbody{font-size:14px}
"""

_INDEX_CSS = """
*{box-sizing:border-box}body{margin:0;background:#dae0e6;font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a1b}
a{color:inherit;text-decoration:none}
.topbar{position:sticky;top:0;background:#fff;border-bottom:1px solid #edeff1;padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:5}
.topbar h1{font-size:16px;margin:0 8px 0 0}
input,select{font:inherit;padding:5px 8px;border:1px solid #ccc;border-radius:4px;background:#fff}
input[type=search]{width:230px}
#count{color:#787c7e;font-size:12.5px;margin-left:auto}
.wrap{max-width:960px;margin:14px auto;padding:0 12px}
.row{display:flex;gap:12px;background:#fff;border:1px solid #ccc;border-radius:5px;padding:10px 12px;margin-bottom:8px;align-items:center}
.row:hover{border-color:#898989}
.thumb{width:72px;height:54px;object-fit:cover;border-radius:4px;background:#edeff1;flex:none}
.noimg{width:72px;height:54px;border-radius:4px;background:#edeff1;flex:none;display:flex;align-items:center;justify-content:center;color:#9aa0a6;font-size:20px}
.title{font-size:15px;font-weight:600;margin-bottom:2px}
.sub{color:#787c7e;font-size:12.5px}
.sub b{color:#1a1a1b}
.badge{font-size:11px;border-radius:10px;padding:1px 7px;margin-left:6px}
.rm{background:#ffb000}.del{background:#edeff1}.fail{background:#ff585b;color:#fff}
.sv{background:#f5c518}
"""


def _md_block(html_field, plain):
    """Prefer Reddit's own rendered HTML (selftext_html / body_html);
    fall back to escaped plain text with preserved line breaks."""
    if html_field:
        s = str(html_field)
        if "&lt;" in s and "<" not in s:
            s = html.unescape(s)
        return s
    if plain:
        return f'<div class="md"><p style="white-space:pre-wrap">{html.escape(plain)}</p></div>'
    return ""


def _comment_html(node):
    if not isinstance(node, dict) or "body" not in node:
        err = node.get("error", "") if isinstance(node, dict) else ""
        return f'<div class="meta">(comments unavailable: {html.escape(str(err))})</div>'
    who = html.escape(node.get("author") or "[deleted]")
    badges = ""
    if node.get("is_submitter"):
        badges += '<span class="badge op">OP</span>'
    if node.get("distinguished") == "moderator":
        badges += '<span class="badge mod">MOD</span>'
    if node.get("stickied"):
        badges += '<span class="badge pin">pinned</span>'
    if node.get("saved_by_user"):
        badges += '<span class="badge sv">\U0001f4be SAVED</span>'
    body = _md_block(node.get("body_html"), node.get("body"))
    kids = "".join(_comment_html(r) for r in node.get("replies", []))
    hl = " svhl" if node.get("saved_by_user") else ""
    return (f'<details class="c" open><summary><span class="cu">u/{who}</span>{badges} '
            f'<span class="score">{node.get("score")}</span> points \u00b7 '
            f'{ts_to_iso(node.get("created_utc"))}</summary>'
            f'<div class="cb{hl}"><div class="cbody">{body}</div>{kids}</div></details>')


def render_post_html(d, comments, media_files):
    esc = html.escape
    title = esc(d.get("title") or "(no title)")
    sub = esc(str(d.get("subreddit") or ""))
    author = esc(d.get("author") or "[deleted]")

    badges = ""
    if d.get("link_flair_text"):
        badges += f'<span class="badge flair">{esc(str(d["link_flair_text"]))}</span>'
    if d.get("over_18"):
        badges += '<span class="badge nsfw">NSFW</span>'
    if d.get("removed_by_category"):
        badges += f'<span class="badge rm">removed: {esc(str(d["removed_by_category"]))}</span>'

    media_html = ""
    for m in media_files or []:
        low = m.lower()
        src = esc(f"media/{m}")
        if low.endswith(IMAGE_EXTS):
            media_html += f'<img src="{src}" loading="lazy">'
        elif low.endswith(VIDEO_EXTS):
            media_html += f'<video src="{src}" controls preload="metadata"></video>'
    if media_html:
        media_html = f'<div class="media">{media_html}</div>'

    selftext = d.get("selftext")
    body = _md_block(d.get("selftext_html"),
                     None if selftext in ("[removed]", "[deleted]") else selftext)

    linkrow = ""
    url = d.get("url") or ""
    if url and not d.get("is_self"):
        linkrow = (f'<div class="linkrow">\U0001f517 '
                   f'<a href="{esc(url)}" target="_blank">{esc(url[:110])}</a></div>')

    ar = d.get("_archive") or {}
    save_note = ""
    if ar.get("save_order"):
        save_note = f' \u00b7 save order #{ar["save_order"]} ({ar.get("save_source", "")})'

    saved_nodes = ar.get("saved_comments") or []
    saved_card = ""
    if saved_nodes:
        inner = "".join(_comment_html(n) for n in saved_nodes)
        note = "" if all(n.get("in_main_tree") for n in saved_nodes if isinstance(n, dict)) \
            else ' <span class="meta">(some were fetched individually - not reached by the main tree)</span>'
        saved_card = (f'<div class="card"><h2>\U0001f4be Comments you saved in this thread'
                      f'{note}</h2>{inner}</div>')

    if comments is None:
        chtml = '<div class="meta">(comments were skipped for this post)</div>'
    else:
        chtml = "".join(_comment_html(c) for c in comments) or '<div class="meta">(no comments)</div>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} : r/{sub}</title><style>{_POST_CSS}</style></head><body>
<div class="topbar"><a href="../../index.html">\u2190 archive</a><span class="sub">r/{sub}</span>
<span style="flex:1"></span>
<a href="https://www.reddit.com{esc(d.get('permalink') or '')}" target="_blank">view on reddit \u2197</a></div>
<div class="wrap">
<div class="card">
<h1>{title}</h1>
<div class="meta">Posted by <b>u/{author}</b> \u00b7 {ts_to_iso(d.get('created_utc'))} \u00b7
<span class="score">{d.get('score')}</span> points ({d.get('upvote_ratio', '?')} upvoted) \u00b7
{d.get('num_comments')} comments{badges}<br>
archived {esc(str(ar.get('archived_at', '')))}{save_note}</div>
{media_html}{body}{linkrow}
</div>
{saved_card}<div class="card"><h2>Comments \u00b7 sorted by top \u00b7 snapshot at archive time</h2>{chtml}</div>
</div></body></html>"""


def render_index_html(rows):
    clean = [{k: (v if v is not None else "") for k, v in r.items() if isinstance(k, str)}
             for r in rows]
    data = json.dumps(clean, ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reddit Saved Archive</title><style>""" + _INDEX_CSS + """</style></head><body>
<div class="topbar">
<h1>Reddit Saved Archive</h1>
<input type="search" id="q" placeholder="Search title / author / subreddit">
<select id="sub"><option value="">All subreddits</option></select>
<select id="sort">
<option value="created_desc">Newest posted</option>
<option value="created_asc">Oldest posted</option>
<option value="archived_desc">Recently archived</option>
<option value="score_desc">Highest score</option>
<option value="save_asc">Save order</option>
</select>
<select id="status">
<option value="">Any status</option><option value="ok">OK</option>
<option value="removed">Removed/deleted</option><option value="failed">Failed</option>
</select>
<select id="kind">
<option value="">All saves</option>
<option value="post">Directly saved posts</option>
<option value="comment">Via saved comments</option>
</select>
<span id="count"></span>
</div>
<div class="wrap" id="list"></div>
<script>const POSTS = """ + data + """;</script>
<script>
const $ = s => document.querySelector(s);
const subCounts = {};
POSTS.forEach(p => { const s = p.subreddit || "?"; subCounts[s] = (subCounts[s] || 0) + 1; });
Object.entries(subCounts).sort((a, b) => b[1] - a[1]).forEach(([s, n]) => {
  const o = document.createElement("option");
  o.value = s; o.textContent = "r/" + s + " (" + n + ")";
  $("#sub").appendChild(o);
});
function matches(p, q, sub, st, kd) {
  if (sub && p.subreddit !== sub) return false;
  if (kd && !((p.saved_via || "post").includes(kd))) return false;
  const status = p.status || "";
  if (st === "ok" && status !== "ok") return false;
  if (st === "removed" && !(status.startsWith("removed") || status === "deleted")) return false;
  if (st === "failed" && !status.startsWith("failed")) return false;
  if (q) {
    const hay = ((p.title || "") + " " + (p.author || "") + " " + (p.subreddit || "")).toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
const sorters = {
  created_desc: (a, b) => (b.created_utc || "").localeCompare(a.created_utc || ""),
  created_asc:  (a, b) => (a.created_utc || "").localeCompare(b.created_utc || ""),
  archived_desc:(a, b) => (b.archived_at || "").localeCompare(a.archived_at || ""),
  score_desc:   (a, b) => (Number(b.score) || 0) - (Number(a.score) || 0),
  save_asc:     (a, b) => (Number(a.save_order) || 1e9) - (Number(b.save_order) || 1e9),
};
function render() {
  const q = $("#q").value.trim().toLowerCase(), sub = $("#sub").value,
        st = $("#status").value, sort = $("#sort").value, kd = $("#kind").value;
  const shown = POSTS.filter(p => matches(p, q, sub, st, kd)).sort(sorters[sort]);
  $("#count").textContent = shown.length + " / " + POSTS.length + " posts";
  const list = $("#list"); list.textContent = "";
  shown.forEach(p => {
    const a = document.createElement(p.folder ? "a" : "div");
    a.className = "row";
    if (p.folder) a.href = encodeURI(p.folder) + "/post.html";
    if (p.first_media) {
      const img = document.createElement("img");
      img.className = "thumb"; img.loading = "lazy";
      img.src = encodeURI(p.folder) + "/media/" + encodeURIComponent(p.first_media);
      a.appendChild(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "noimg";
      ph.textContent = Number(p.media_count) > 0 ? "\\uD83C\\uDFAC" : "\\uD83D\\uDCC4";
      a.appendChild(ph);
    }
    const txt = document.createElement("div");
    const t = document.createElement("div"); t.className = "title";
    t.textContent = p.title || "(no title)";
    if ((p.saved_via || "").includes("comment")) {
      const sb = document.createElement("span");
      sb.className = "badge sv"; sb.textContent = "\uD83D\uDCBE comment";
      t.appendChild(sb);
    }
    const status = p.status || "";
    if (status && status !== "ok") {
      const b = document.createElement("span");
      b.className = "badge " + (status.startsWith("failed") ? "fail" : status === "deleted" ? "del" : "rm");
      b.textContent = status;
      t.appendChild(b);
    }
    const m = document.createElement("div"); m.className = "sub";
    m.innerHTML = "<b>r/" + (p.subreddit || "?") + "</b> \\u00b7 u/" + (p.author || "?") +
      " \\u00b7 " + (p.created_utc || "").slice(0, 10) + " \\u00b7 " + (p.score || 0) +
      " pts \\u00b7 " + (p.num_comments || 0) + " comments";
    txt.appendChild(t); txt.appendChild(m); a.appendChild(txt);
    list.appendChild(a);
  });
}
["q", "sub", "sort", "status", "kind"].forEach(id => {
  $("#" + id).addEventListener("input", render);
  $("#" + id).addEventListener("change", render);
});
render();
</script></body></html>"""


def rebuild_html(root, log):
    """Regenerate index.html and any missing post.html pages from what's on
    disk. Pure local operation - no Reddit connection needed."""
    root = Path(root).expanduser()
    index_path = root / "index.csv"
    if not index_path.exists():
        log("No index.csv in that folder yet - run an archive first.")
        return
    with open(index_path, newline="", encoding="utf-8") as f:
        rows = [{k: (v or "") for k, v in r.items() if isinstance(k, str)}
                for r in csv.DictReader(f)]
    built = 0
    for row in rows:
        rel = (row.get("folder") or "").strip()
        if not rel:
            continue
        folder = root / rel
        pj = folder / "post.json"
        if not folder.is_dir() or not pj.exists():
            continue
        media_dir = folder / "media"
        media = sorted(p.name for p in media_dir.iterdir()) if media_dir.is_dir() else []
        if not row.get("first_media"):
            row["first_media"] = next((m for m in media if m.lower().endswith(IMAGE_EXTS)), "")
        if not (folder / "post.html").exists():
            try:
                d = json.loads(pj.read_text(encoding="utf-8"))
                cj = folder / "comments.json"
                comments = json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else None
                (folder / "post.html").write_text(render_post_html(d, comments, media),
                                                  encoding="utf-8")
                built += 1
            except Exception as e:  # noqa: BLE001
                log(f"  viewer: couldn't build {rel}: {e.__class__.__name__}: {e}")
    (root / "index.html").write_text(render_index_html(rows), encoding="utf-8")
    log(f"Viewer updated: index.html covers {len(rows)} posts"
        + (f", built {built} missing post pages" if built else "") + ".")
    log(f"Browse the archive: open {root / 'index.html'}")


# --------------------------------------------------------------------------
# Media download
# --------------------------------------------------------------------------
def guess_ext(url, default=".jpg"):
    path = urlparse(url).path.lower()
    for ext in DIRECT_MEDIA_EXTS:
        if path.endswith(ext):
            return ext
    return default


def download_url(url, dest: Path, log):
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60, stream=True)
        if r.status_code != 200:
            log(f"    media HTTP {r.status_code}: {url[:90]}")
            return False
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        return True
    except requests.RequestException as e:
        log(f"    media download failed ({e.__class__.__name__}): {url[:90]}")
        return False


def ytdlp_download(url, folder: Path, log, binaries):
    """Best-effort yt-dlp fetch into folder. Returns list of new filenames."""
    ytdlp = binaries.get("yt-dlp")
    if not ytdlp:
        return []
    before = set(p.name for p in folder.iterdir())
    cmd = [ytdlp, "--no-playlist", "--no-warnings", "--restrict-filenames",
           "-f", "bv*+ba/b", "-o", str(folder / "%(id)s.%(ext)s"), url]
    ff = binaries.get("ffmpeg")
    if ff:
        cmd[1:1] = ["--ffmpeg-location", str(Path(ff).parent)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"    yt-dlp failed to run: {e.__class__.__name__}")
        return []
    new = [p.name for p in folder.iterdir()
           if p.name not in before and not p.name.endswith((".part", ".ytdl"))]
    if proc.returncode != 0 and not new:
        err = (proc.stderr or "").strip().splitlines()
        log(f"    yt-dlp: no media ({err[-1][:100] if err else 'no output'})")
    return new


def archive_media(target, folder: Path, opts, log, binaries, _is_crosspost=False):
    """Download whatever media belongs to the post. Works on a praw Submission
    or a raw crosspost-parent dict. Returns list of saved filenames."""
    saved = []
    url = html.unescape(gk(target, "url") or "")
    domain = urlparse(url).netloc.lower()
    post_id = gk(target, "id", "post")

    # 1) Reddit gallery ----------------------------------------------------
    media_meta = gk(target, "media_metadata")
    if gk(target, "is_gallery") and media_meta:
        gdata = gk(target, "gallery_data") or {}
        order = [it.get("media_id") for it in gdata.get("items", [])] or list(media_meta)
        for idx, mid in enumerate(order, 1):
            m = media_meta.get(mid) if isinstance(media_meta, dict) else None
            if not m or m.get("status") != "valid":
                continue
            src = m.get("s") or {}
            u = src.get("mp4") or src.get("gif") or src.get("u")
            if not u:
                continue
            u = html.unescape(u)
            if src.get("mp4"):
                ext = ".mp4"
            else:
                mime = m.get("m", "image/jpg")
                ext = "." + mime.split("/")[-1].replace("jpeg", "jpg")
            name = f"{idx:02d}_{mid}{ext}"
            if download_url(u, folder / name, log):
                saved.append(name)
        if saved:
            return saved

    # 2) Reddit-hosted video ----------------------------------------------
    media = gk(target, "secure_media") or gk(target, "media")
    rv = media.get("reddit_video") if isinstance(media, dict) else None
    if gk(target, "is_video") and rv:
        permalink = gk(target, "permalink")
        new = ytdlp_download(f"https://www.reddit.com{permalink}", folder, log, binaries) \
            if permalink else []
        if new:
            return new
        fb = (rv.get("fallback_url") or "").split("?")[0]
        if fb:
            name = f"{post_id}_video_noaudio.mp4"
            if download_url(fb, folder / name, log):
                log("    saved fallback video (no audio track - install yt-dlp + ffmpeg for full video)")
                return [name]

    # 3) Direct file link (i.redd.it, i.imgur.com, ...) --------------------
    if url and (guess_ext(url, "") in DIRECT_MEDIA_EXTS or domain == "i.redd.it"):
        ext = guess_ext(url)
        name = f"{post_id}{ext}"
        if download_url(url, folder / name, log):
            saved.append(name)
            # reddit "gif" posts often have a better mp4 in the preview
            return saved

    # 4) Crosspost parent ---------------------------------------------------
    cpl = gk(target, "crosspost_parent_list")
    if not saved and cpl and not _is_crosspost:
        saved = archive_media(cpl[0], folder, opts, log, binaries, _is_crosspost=True)
        if saved:
            return saved

    # 5) External link, best effort via yt-dlp ------------------------------
    if (not saved and opts.get("external_media") and url
            and not gk(target, "is_self")
            and domain not in ("www.reddit.com", "reddit.com", "old.reddit.com")):
        saved = ytdlp_download(url, folder, log, binaries)
        if saved:
            return saved

    # 6) Last resort: the preview image Reddit cached ------------------------
    preview = gk(target, "preview")
    if not saved and isinstance(preview, dict):
        try:
            src = preview["images"][0]["source"]["url"]
            name = f"{post_id}_preview{guess_ext(src)}"
            if download_url(html.unescape(src), folder / name, log):
                log("    saved Reddit's cached preview image (original media not retrievable)")
                saved.append(name)
        except (KeyError, IndexError, TypeError):
            pass
    return saved


# --------------------------------------------------------------------------
# Archiver
# --------------------------------------------------------------------------
class StopRequested(Exception):
    pass


class Archiver:
    def __init__(self, reddit, cfg, log, progress, stop_event):
        self.reddit = reddit
        self.cfg = cfg
        self.log = log
        self.progress = progress
        self.stop = stop_event
        self.root = Path(cfg["output_dir"]).expanduser()
        self.index_path = self.root / "index.csv"
        self.binaries = {"yt-dlp": find_binary("yt-dlp"), "ffmpeg": find_binary("ffmpeg")}
        self._listing_comment_objs = {}

    # -- helpers ------------------------------------------------------------
    def _check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def _archived_map(self):
        """{post_id: set(saved_comment_ids already recorded)} for every
        successfully archived post."""
        amap = {}
        if self.index_path.exists():
            with open(self.index_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("status", "").startswith(("ok", "removed", "deleted")):
                        cids = set(filter(None, (row.get("saved_comment_ids") or "").split(";")))
                        amap[row.get("id", "")] = amap.get(row.get("id", ""), set()) | cids
        return amap

    @staticmethod
    def _needs_archive(pid, save_meta, archived_map):
        """A post needs (re-)archiving if it was never archived, or if new
        saved-comment ids have appeared that the archived copy doesn't mark."""
        if pid not in archived_map:
            return True
        new_cids = set((save_meta.get(pid) or {}).get("saved_comment_ids") or [])
        return not new_cids <= archived_map[pid]

    def _index_write(self, row):
        new = not self.index_path.exists()
        with open(self.index_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def _migrate_index(self):
        """Upgrade an index.csv written by an older version to the current
        column set (missing columns become empty)."""
        if not self.index_path.exists():
            return
        with open(self.index_path, newline="", encoding="utf-8") as f:
            first = f.readline().strip()
        if first == ",".join(INDEX_FIELDS):
            return
        with open(self.index_path, newline="", encoding="utf-8") as f:
            rows = [{k: (v or "") for k, v in r.items() if isinstance(k, str)}
                    for r in csv.DictReader(f)]
        with open(self.index_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        self.log("index.csv upgraded to the current schema.")

    # -- gathering ------------------------------------------------------------
    def gather(self, archived_map):
        """Collect Submission objects from the live listing, the posts CSV,
        and the parents of saved comments (listing + comments CSV).
        Returns (submissions, save_meta); save_meta[post_id] records save
        order, source, how the post was saved ("post", "comment", or
        "post+comment"), and any saved comment ids inside it. Posts whose
        archived copy is already complete are excluded from metadata fetches.
        (Reddit exposes save ORDER, never save timestamps.)"""
        subs, have = [], set()
        save_meta = {}
        self._listing_comment_objs = {}
        pending_parents = []          # ordered post ids needed for saved comments
        skipped_comments = 0
        include_comments = self.cfg.get("include_saved_comments", True)

        def meta(pid):
            return save_meta.setdefault(pid, {"save_order": None, "save_source": None,
                                              "saved_via": set(), "saved_comment_ids": []})

        if self.cfg.get("use_listing"):
            self.log("Fetching your saved listing from the API (up to ~1,000 items)...")
            pos = 0
            for item in self.reddit.user.me().saved(limit=None):
                self._check_stop()
                pos += 1
                if isinstance(item, praw.models.Submission):
                    m = meta(item.id)
                    m["saved_via"].add("post")
                    if m["save_order"] is None:
                        m["save_order"], m["save_source"] = pos, "listing"
                    if item.id not in have:
                        have.add(item.id)
                        subs.append(item)
                elif isinstance(item, praw.models.Comment) and include_comments:
                    pid = item.link_id.split("_", 1)[-1]
                    m = meta(pid)
                    m["saved_via"].add("comment")
                    if item.id not in m["saved_comment_ids"]:
                        m["saved_comment_ids"].append(item.id)
                    if m["save_order"] is None:
                        m["save_order"], m["save_source"] = pos, "listing"
                    self._listing_comment_objs[item.id] = item
                    if pid not in have:
                        pending_parents.append(pid)
                else:
                    skipped_comments += 1
                if pos % 100 == 0:
                    self.log(f"  ...{pos} saved items walked")
            n_saved_c = len(self._listing_comment_objs)
            self.log(f"Listing done: {len(subs)} saved posts"
                     + (f", {n_saved_c} saved comments (parent posts will be archived)"
                        if n_saved_c else "")
                     + (f", {skipped_comments} saved comments skipped (option off)"
                        if skipped_comments else "")
                     + " - order 1 = most recently saved")

        # -- comments CSV: (post_id, comment_id) pairs -------------------------
        ccsv = (self.cfg.get("comments_csv_path") or "").strip()
        if ccsv and include_comments:
            self.log(f"Reading saved-comments CSV: {Path(ccsv).name}")
            refs = extract_saved_comment_refs_from_csv(ccsv)
            added = 0
            for n, (pid, cid) in enumerate(refs, 1):
                m = meta(pid)
                m["saved_via"].add("comment")
                if cid not in m["saved_comment_ids"]:
                    m["saved_comment_ids"].append(cid)
                    added += 1
                if m["save_order"] is None:
                    m["save_order"], m["save_source"] = n, "csv"
                if pid not in have:
                    pending_parents.append(pid)
            self.log(f"  {len(refs)} saved comments listed; {added} new comment references.")

        # -- posts CSV ----------------------------------------------------------
        to_fetch = []
        csv_path = (self.cfg.get("csv_path") or "").strip()
        if csv_path:
            self.log(f"Reading export CSV: {Path(csv_path).name}")
            all_ids = extract_post_ids_from_csv(csv_path)
            already = 0
            for n, pid in enumerate(all_ids, 1):
                m = meta(pid)
                m["saved_via"].add("post")
                if m["save_order"] is None:
                    m["save_order"], m["save_source"] = n, "csv"
                if pid in have:
                    continue
                if not self._needs_archive(pid, save_meta, archived_map):
                    already += 1
                    continue
                to_fetch.append(pid)
            self.log(f"  CSV lists {len(all_ids)} posts; {already} already archived, "
                     f"{len(to_fetch)} to fetch (safe to leave this field filled).")

        # -- batch-fetch metadata for CSV posts + saved-comment parents ---------
        for pid in pending_parents:
            if pid not in have and pid not in to_fetch                     and self._needs_archive(pid, save_meta, archived_map):
                to_fetch.append(pid)
        seen_fetch, ordered = set(), []
        for pid in to_fetch:
            if pid not in seen_fetch:
                seen_fetch.add(pid)
                ordered.append(pid)
        fetched = 0
        for start in range(0, len(ordered), 100):
            self._check_stop()
            chunk = ordered[start:start + 100]
            got = 0
            for thing in self.reddit.info(fullnames=[f"t3_{i}" for i in chunk]):
                if isinstance(thing, praw.models.Submission) and thing.id not in have:
                    have.add(thing.id)
                    subs.append(thing)
                    got += 1
            fetched += got
            self.log(f"  metadata batch {start // 100 + 1}/{(len(ordered) + 99) // 100}: +{got}")
        if len(ordered) - fetched > 0:
            self.log(f"  {len(ordered) - fetched} items are no longer retrievable "
                     "(fully deleted or purged).")

        for m in save_meta.values():
            m["saved_via"] = "+".join(sorted(m["saved_via"])) if m["saved_via"] else ""
        subs.sort(key=lambda s: getattr(s, "created_utc", 0), reverse=True)
        return subs, save_meta

    def _capture_saved_comments(self, saved_cids, tree):
        """Mark saved comments inside the fetched tree, and guarantee a full
        copy of each saved comment in the returned list - fetching any that
        the tree didn't reach (costs 1 request per missing comment)."""
        found = mark_saved_comments(tree, set(saved_cids)) if tree else set()
        out = []
        for cid in saved_cids:
            if cid in found:
                node = dict(find_comment_node(tree, cid) or {})
                node["saved_by_user"] = True
                node["in_main_tree"] = True
                out.append(node)
                continue
            try:
                obj = self._listing_comment_objs.get(cid) or self.reddit.comment(cid)
                try:
                    obj.refresh()
                except Exception:  # noqa: BLE001 - fall back to whatever we hold
                    pass
                node = serialize_comment(obj)
                node["saved_by_user"] = True
                node["in_main_tree"] = False
                out.append(node)
            except Exception as e:  # noqa: BLE001
                self.log(f"    saved comment {cid} unretrievable: {e.__class__.__name__}")
                out.append({"id": cid, "saved_by_user": True,
                            "error": f"{e.__class__.__name__}: {e}"})
        return out

    # -- one post -------------------------------------------------------------
    def archive_one(self, s, save_meta):
        d = submission_to_dict(s)
        am = save_meta.get(d.get("id"), {})
        saved_cids = list(am.get("saved_comment_ids") or [])
        d["_archive"] = {
            "archived_at": now_iso(),
            "app_version": APP_VERSION,
            "save_order": am.get("save_order"),
            "save_source": am.get("save_source"),
            "saved_via": am.get("saved_via", "post"),
            "saved_comment_ids": saved_cids,
            "save_date_note": "Reddit does not expose save timestamps; save_order is the "
                              "position within its source, and archived_at bounds the save "
                              "date for posts saved after archiving began.",
        }
        folder_name = "_".join([
            ts_to_date(d.get("created_utc")),
            sanitize(str(d.get("subreddit")), 24),
            d.get("id", "x"),
            sanitize(d.get("title", ""), 48),
        ])
        folder = self.root / "posts" / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        comments, n_comments = fetch_comment_tree(s, self.cfg.get("comments_mode", "loaded"), self.log)
        if saved_cids:
            d["_archive"]["saved_comments"] = self._capture_saved_comments(saved_cids, comments)
        if comments is not None:
            with open(folder / "comments.json", "w", encoding="utf-8") as f:
                json.dump(comments, f, indent=2, ensure_ascii=False, default=str)

        media = []
        if self.cfg.get("download_media"):
            media_dir = folder / "media"
            media_dir.mkdir(exist_ok=True)
            media = archive_media(s, media_dir, self.cfg, self.log, self.binaries)
            if not any(media_dir.iterdir()):
                media_dir.rmdir()
        first_media = next((m for m in media if m.lower().endswith(IMAGE_EXTS)), "")

        with open(folder / "post.json", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False, default=str)
        with open(folder / "post.md", "w", encoding="utf-8") as f:
            f.write(render_markdown(d, comments))
        with open(folder / "post.html", "w", encoding="utf-8") as f:
            f.write(render_post_html(d, comments, media))

        status = "ok"
        if d.get("removed_by_category"):
            status = f"removed:{d['removed_by_category']}"
        elif d.get("author") is None:
            status = "deleted"

        with open(folder / ".archived_ok", "w", encoding="utf-8") as f:
            json.dump({"archived_at": now_iso(), "media": media,
                       "comments_archived": n_comments}, f)

        self._index_write({
            "id": d.get("id"), "archived_at": now_iso(),
            "created_utc": ts_to_iso(d.get("created_utc")),
            "subreddit": str(d.get("subreddit")), "author": d.get("author") or "[deleted]",
            "score": d.get("score"), "num_comments": d.get("num_comments"),
            "title": (d.get("title") or "")[:180],
            "folder": str(folder.relative_to(self.root)),
            "media_count": len(media), "first_media": first_media,
            "save_order": am.get("save_order", ""), "save_source": am.get("save_source", ""),
            "saved_via": am.get("saved_via", "post"),
            "saved_comment_ids": ";".join(saved_cids),
            "status": status,
        })
        return len(media), status

    # -- main loop ------------------------------------------------------------
    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "posts").mkdir(exist_ok=True)

        if self.cfg.get("download_media"):
            for tool in ("yt-dlp", "ffmpeg"):
                if not self.binaries[tool]:
                    self.log(f"Note: {tool} not found - videos will be limited. "
                             f"(brew install {tool})")

        self._migrate_index()
        archived_map = self._archived_map() if self.cfg.get("skip_existing") else {}
        items, save_meta = self.gather(archived_map)
        todo = [s for s in items if self._needs_archive(s.id, save_meta, archived_map)]
        skipped = len(items) - len(todo)
        if skipped:
            self.log(f"Skipping {skipped} posts already archived (uncheck "
                     "'Skip already-archived' to redo them).")
        total = len(todo)
        self.log(f"Archiving {total} posts -> {self.root}")
        self.progress(0, max(total, 1))

        ok = failed = media_total = 0
        for i, s in enumerate(todo, 1):
            self._check_stop()
            try:
                title = (getattr(s, "title", "") or "")[:60]
                self.log(f"[{i}/{total}] r/{s.subreddit} - {title}")
                n_media, _ = self.archive_one(s, save_meta)
                media_total += n_media
                ok += 1
            except StopRequested:
                raise
            except Exception as e:  # noqa: BLE001 - keep going, report at end
                failed += 1
                self.log(f"    FAILED: {e.__class__.__name__}: {e}")
                try:
                    self._index_write({"id": getattr(s, "id", "?"), "archived_at": now_iso(),
                                       "title": (getattr(s, "title", "") or "")[:180],
                                       "status": f"failed:{e.__class__.__name__}"})
                except OSError:
                    pass
            self.progress(i, total)
            time.sleep(0.05)

        self.log("-" * 60)
        self.log(f"Done. {ok} archived, {failed} failed, {skipped} skipped, "
                 f"{media_total} media files. Index: {self.index_path}")
        try:
            rebuild_html(self.root, self.log)
        except Exception as e:  # noqa: BLE001
            self.log(f"Viewer rebuild failed: {e.__class__.__name__}: {e}")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.minsize(860, 700)
        self.cfg = load_config()
        self.ui_q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self._build()
        self.after(100, self._poll)

    # -- layout ---------------------------------------------------------------
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        hint_fg = "#777777"

        # Credentials ---------------------------------------------------------
        cred = ttk.LabelFrame(self, text="  Reddit app credentials  ")
        cred.pack(fill="x", **pad)
        cred.columnconfigure(1, weight=1)
        cred.columnconfigure(3, weight=1)

        self.v_client_id = tk.StringVar(value=self.cfg["client_id"])
        self.v_secret = tk.StringVar(value=self.cfg["client_secret"])
        self.v_username = tk.StringVar(value=self.cfg["username"])
        self.v_password = tk.StringVar(value=self.cfg.get("password", ""))
        self.v_remember = tk.BooleanVar(value=self.cfg.get("remember_password", False))
        self.v_auth = tk.StringVar(value=self.cfg.get("auth_mode", "oauth"))

        ttk.Label(cred, text="Client ID").grid(row=0, column=0, sticky="e", padx=6, pady=3)
        ttk.Entry(cred, textvariable=self.v_client_id).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(cred, text="Client secret").grid(row=0, column=2, sticky="e", padx=6)
        ttk.Entry(cred, textvariable=self.v_secret).grid(row=0, column=3, sticky="ew", padx=(0, 8))
        ttk.Label(cred, text="From reddit.com/prefs/apps - create a 'script' app with redirect URI "
                            f"{REDIRECT_URI}", foreground=hint_fg).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(cred, text="Username").grid(row=2, column=0, sticky="e", padx=6, pady=3)
        ttk.Entry(cred, textvariable=self.v_username).grid(row=2, column=1, sticky="ew", pady=3)

        auth_row = ttk.Frame(cred)
        auth_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        ttk.Radiobutton(auth_row, text="Authorize in browser (recommended - works with 2FA and "
                        "Google sign-in)", variable=self.v_auth, value="oauth").pack(anchor="w", padx=6)
        oauth_line = ttk.Frame(auth_row)
        oauth_line.pack(anchor="w", padx=28, pady=(2, 6))
        ttk.Button(oauth_line, text="Authorize in browser…",
                   command=self.on_authorize).pack(side="left")
        self.token_status = ttk.Label(
            oauth_line,
            text=("Refresh token saved ✓" if self.cfg.get("refresh_token") else "No token yet"),
            foreground=("#2a7f2a" if self.cfg.get("refresh_token") else hint_fg))
        self.token_status.pack(side="left", padx=10)

        ttk.Radiobutton(auth_row, text="Reddit password (only if the account has a password and no 2FA)",
                        variable=self.v_auth, value="password").pack(anchor="w", padx=6)
        pw_line = ttk.Frame(auth_row)
        pw_line.pack(anchor="w", padx=28, pady=2)
        ttk.Label(pw_line, text="Password").pack(side="left")
        ttk.Entry(pw_line, textvariable=self.v_password, show="•", width=24).pack(side="left", padx=6)
        ttk.Checkbutton(pw_line, text="Remember password (stored in plain text)",
                        variable=self.v_remember).pack(side="left", padx=6)

        # Sources --------------------------------------------------------------
        src = ttk.LabelFrame(self, text="  What to archive  ")
        src.pack(fill="x", **pad)
        src.columnconfigure(1, weight=1)

        self.v_listing = tk.BooleanVar(value=self.cfg.get("use_listing", True))
        self.v_csv = tk.StringVar(value=self.cfg.get("csv_path", ""))
        self.v_ccsv = tk.StringVar(value=self.cfg.get("comments_csv_path", ""))
        self.v_inc_comments = tk.BooleanVar(value=self.cfg.get("include_saved_comments", True))

        ttk.Checkbutton(src, text="Current saved listing (your ~1,000 most recent saves)",
                        variable=self.v_listing).grid(row=0, column=0, columnspan=3,
                                                      sticky="w", padx=6, pady=3)
        ttk.Checkbutton(src, text="Archive parent posts of comments I saved (marks the saved comment)",
                        variable=self.v_inc_comments).grid(row=1, column=0, columnspan=3,
                                                           sticky="w", padx=6, pady=3)
        ttk.Label(src, text="Posts CSV").grid(row=2, column=0, sticky="e", padx=6)
        ttk.Entry(src, textvariable=self.v_csv).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Button(src, text="Browse…", command=self.on_browse_csv).grid(row=2, column=2, padx=8)
        ttk.Label(src, text="Comments CSV").grid(row=3, column=0, sticky="e", padx=6)
        ttk.Entry(src, textvariable=self.v_ccsv).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Button(src, text="Browse…", command=self.on_browse_ccsv).grid(row=3, column=2, padx=8)
        ttk.Label(src, text="saved_posts.csv / saved_comments.csv from Reddit's data export - cover "
                            "saves older than the 1,000-item listing cap. Leave empty to skip.",
                  foreground=hint_fg).grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 4))

        # Options ---------------------------------------------------------------
        opt = ttk.LabelFrame(self, text="  Options  ")
        opt.pack(fill="x", **pad)
        opt.columnconfigure(1, weight=1)

        self.v_outdir = tk.StringVar(value=self.cfg.get("output_dir"))
        self.v_media = tk.BooleanVar(value=self.cfg.get("download_media", True))
        self.v_external = tk.BooleanVar(value=self.cfg.get("external_media", True))
        self.v_skip = tk.BooleanVar(value=self.cfg.get("skip_existing", True))
        self.v_comments = tk.StringVar(
            value=COMMENT_MODE_LABELS.get(self.cfg.get("comments_mode", "loaded")))

        ttk.Label(opt, text="Save to").grid(row=0, column=0, sticky="e", padx=6)
        ttk.Entry(opt, textvariable=self.v_outdir).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(opt, text="Choose…", command=self.on_browse_out).grid(row=0, column=2, padx=8)

        checks = ttk.Frame(opt)
        checks.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(checks, text="Download media", variable=self.v_media).pack(side="left")
        ttk.Checkbutton(checks, text="Try external links with yt-dlp",
                        variable=self.v_external).pack(side="left", padx=12)
        ttk.Checkbutton(checks, text="Skip already-archived posts",
                        variable=self.v_skip).pack(side="left", padx=12)

        crow = ttk.Frame(opt)
        crow.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(crow, text="Comments:").pack(side="left")
        ttk.Combobox(crow, textvariable=self.v_comments, state="readonly", width=36,
                     values=list(COMMENT_MODES)).pack(side="left", padx=8)

        # Run -----------------------------------------------------------------
        run = ttk.Frame(self)
        run.pack(fill="x", **pad)
        self.btn_start = ttk.Button(run, text="Start archiving", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(run, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(run, text="Rebuild viewer (offline)",
                   command=self.on_rebuild).pack(side="left", padx=8)
        self.status = ttk.Label(run, text="Idle")
        self.status.pack(side="right")
        self.pbar = ttk.Progressbar(self, mode="determinate")
        self.pbar.pack(fill="x", padx=10)

        # Log ------------------------------------------------------------------
        self.logbox = ScrolledText(self, height=16, state="disabled",
                                   font=("Menlo", 11), wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self._log(f"{APP_NAME} v{APP_VERSION} ready. Config: {CONFIG_PATH}")
        if praw is None:
            self._log("praw is not installed - run:  pip3 install praw")

    # -- UI plumbing ----------------------------------------------------------
    def _log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{stamp}] {msg}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, *rest = self.ui_q.get_nowait()
                if kind == "log":
                    self._log(rest[0])
                elif kind == "progress":
                    cur, total = rest
                    self.pbar.configure(maximum=total, value=cur)
                    self.status.configure(text=f"{cur} / {total}")
                elif kind == "token":
                    self.cfg["refresh_token"] = rest[0]
                    save_config(self.cfg)
                    self.token_status.configure(text="Refresh token saved ✓",
                                                foreground="#2a7f2a")
                    self._log("Authorization complete - refresh token saved.")
                elif kind == "done":
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status.configure(text="Idle")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _collect_cfg(self):
        self.cfg.update({
            "client_id": self.v_client_id.get().strip(),
            "client_secret": self.v_secret.get().strip(),
            "username": self.v_username.get().strip(),
            "auth_mode": self.v_auth.get(),
            "password": self.v_password.get(),
            "remember_password": self.v_remember.get(),
            "output_dir": self.v_outdir.get().strip(),
            "csv_path": self.v_csv.get().strip(),
            "comments_csv_path": self.v_ccsv.get().strip(),
            "include_saved_comments": self.v_inc_comments.get(),
            "use_listing": self.v_listing.get(),
            "download_media": self.v_media.get(),
            "external_media": self.v_external.get(),
            "comments_mode": COMMENT_MODES.get(self.v_comments.get(), "loaded"),
            "skip_existing": self.v_skip.get(),
        })
        return self.cfg

    # -- button handlers --------------------------------------------------------
    def on_browse_csv(self):
        p = filedialog.askopenfilename(title="Choose saved_posts.csv",
                                       filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if p:
            self.v_csv.set(p)

    def on_browse_ccsv(self):
        p = filedialog.askopenfilename(title="Choose saved_comments.csv",
                                       filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if p:
            self.v_ccsv.set(p)

    def on_browse_out(self):
        p = filedialog.askdirectory(title="Choose output folder")
        if p:
            self.v_outdir.set(p)

    def on_authorize(self):
        if praw is None:
            messagebox.showerror(APP_NAME, "praw is not installed.\n\nRun:  pip3 install praw")
            return
        cfg = self._collect_cfg()
        if not cfg["client_id"] or not cfg["client_secret"]:
            messagebox.showerror(APP_NAME, "Enter the Client ID and Client secret first.\n\n"
                                 "Create them at reddit.com/prefs/apps ('script' type, redirect URI "
                                 f"{REDIRECT_URI}).")
            return
        save_config(cfg)
        self.stop_event.clear()

        def work():
            try:
                token = obtain_refresh_token(cfg["client_id"], cfg["client_secret"],
                                             lambda m: self.ui_q.put(("log", m)),
                                             self.stop_event)
                self.ui_q.put(("token", token))
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("log", f"Authorization failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def on_start(self):
        if self.worker and self.worker.is_alive():
            return
        if praw is None:
            messagebox.showerror(APP_NAME, "praw is not installed.\n\nRun:  pip3 install praw")
            return
        cfg = self._collect_cfg()

        problems = []
        if not cfg["client_id"] or not cfg["client_secret"]:
            problems.append("Client ID and secret are required.")
        if cfg["auth_mode"] == "password" and (not cfg["username"] or not cfg["password"]):
            problems.append("Password mode needs username and password.")
        if cfg["auth_mode"] == "oauth" and not cfg.get("refresh_token"):
            problems.append("Click 'Authorize in browser' first (or switch to password mode).")
        if not cfg["output_dir"]:
            problems.append("Choose an output folder.")
        if not cfg["use_listing"] and not cfg["csv_path"] and not cfg["comments_csv_path"]:
            problems.append("Pick at least one source: the saved listing and/or an export CSV.")
        for key, label in (("csv_path", "posts"), ("comments_csv_path", "comments")):
            if cfg[key] and not Path(cfg[key]).exists():
                problems.append(f"The {label} CSV path doesn't exist.")
        if problems:
            messagebox.showerror(APP_NAME, "\n".join(problems))
            return

        save_config(cfg)
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pbar.configure(value=0)

        def work():
            log = lambda m: self.ui_q.put(("log", m))  # noqa: E731
            progress = lambda c, t: self.ui_q.put(("progress", c, t))  # noqa: E731
            try:
                log("Connecting to Reddit...")
                reddit = build_reddit(cfg)
                me = reddit.user.me()
                log(f"Authenticated as u/{me}")
                Archiver(reddit, cfg, log, progress, self.stop_event).run()
            except StopRequested:
                log("Stopped by user. Re-run any time - already-archived posts are skipped.")
            except prawcore.exceptions.ResponseException as e:
                code = getattr(getattr(e, "response", None), "status_code", "?")
                log(f"Reddit rejected the credentials (HTTP {code}).")
                if code == 401:
                    log("Check the Client ID / secret, and that the app type is 'script'. "
                        "For password mode with 2FA, use 'password:123456' (current code) "
                        "or switch to browser authorization.")
            except prawcore.exceptions.OAuthException as e:
                log(f"Login failed: {e}. Check username/password, or use browser authorization.")
            except Exception as e:  # noqa: BLE001
                log(f"Error: {e.__class__.__name__}: {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
            finally:
                self.ui_q.put(("done",))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_stop(self):
        self.stop_event.set()
        self._log("Stopping after the current post...")

    def on_rebuild(self):
        if self.worker and self.worker.is_alive():
            self._log("An archive run is in progress - the viewer rebuilds automatically when it finishes.")
            return
        outdir = self.v_outdir.get().strip()
        if not outdir or not Path(outdir).expanduser().exists():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        self.cfg["output_dir"] = outdir
        save_config(self._collect_cfg())

        def work():
            try:
                rebuild_html(Path(outdir), lambda m: self.ui_q.put(("log", m)))
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("log", f"Viewer rebuild failed: {e.__class__.__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()


def main():
    if praw is None:
        # Still open the window so the user sees the install hint.
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
