"""Publish Buzzora posts directly to Instagram and YouTube — free, no third-party scheduler.

Why: Metricool's free plan caps at ~20 posts/month. Instagram's own Content Publishing API
allows 25–100 API posts per rolling 24h (query the real number), and YouTube's videos.insert
has its own bucket of ~100 uploads/day. Both APIs are free; media is served from the public
buzzora-media repo, which is also free.

Credentials live in E:/Buzzora/.secrets/publish.json (git-ignored, never printed). See PUBLISHING.md.

Usage:
  python tools/publish.py check                          # token + quota health, posts nothing
  python tools/publish.py queue editions/<date>          # build queue.json from that edition
  python tools/publish.py run [--due-only] [--dry-run]   # publish what's due
  python tools/publish.py one editions/<date> R1         # publish a single item now
"""
import json, os, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

# Windows consoles default to cp1252; captions carry ₹ and other non-latin1 text, and a print
# that raises here would otherwise abort the run *after* a post went out. Never let output kill a publish.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = os.path.join(ROOT, ".secrets", "publish.json")
QUEUE = os.path.join(ROOT, ".secrets", "queue.json")
LOG = os.path.join(ROOT, ".secrets", "published.json")
MEDIA_BASE = "https://raw.githubusercontent.com/patelviral2001-cmyk/buzzora-media/main"
IST = timezone(timedelta(hours=5, minutes=30))
GRAPH = "https://graph.facebook.com/v21.0"


# ---------------------------------------------------------------- plumbing
def _req(url, data=None, headers=None, method=None, raw=False, timeout=120):
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return body if raw else json.loads(body or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:600]
        raise SystemExit(f"HTTP {e.code} on {url.split('?')[0]}\n{detail}")


def load(path, default):
    return json.load(open(path, encoding="utf8")) if os.path.exists(path) else default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf8"), indent=2, ensure_ascii=False)


ENV_KEYS = ("ig_user_id", "ig_access_token", "fb_page_id", "meta_app_id", "meta_app_secret",
            "yt_client_id", "yt_client_secret", "yt_refresh_token", "token_kind")


def creds():
    """Local runs read .secrets/publish.json; CI reads BUZZORA_* environment variables.

    On GitHub Actions there is no secrets file - each value arrives as a repository secret, so the
    same code path works in both places without the workflow having to write a file to disk.
    """
    c = load(SECRETS, {}) if os.path.exists(SECRETS) else {}
    env = {k: os.environ[f"BUZZORA_{k.upper()}"] for k in ENV_KEYS
           if os.environ.get(f"BUZZORA_{k.upper()}")}
    c.update(env)
    if not c:
        raise SystemExit(f"No credentials. Create {SECRETS} (see PUBLISHING.md) or set BUZZORA_* env vars.")
    if not (c.get("ig_user_id") and c.get("ig_access_token")):
        raise SystemExit("Instagram is not configured yet (no ig_user_id / ig_access_token). "
                         "Run: python tools/ig_setup.py <USER_ACCESS_TOKEN>   (see PUBLISHING.md step 1)")
    return c


# ---------------------------------------------------------------- instagram
def ig_limit(c):
    d = _req(f"{GRAPH}/{c['ig_user_id']}/content_publishing_limit?fields=quota_usage,config"
             f"&access_token={c['ig_access_token']}")
    row = (d.get("data") or [{}])[0]
    return row.get("quota_usage", 0), (row.get("config") or {}).get("quota_total", 25)


def _ig_container(c, params):
    params["access_token"] = c["ig_access_token"]
    return _req(f"{GRAPH}/{c['ig_user_id']}/media", data=params)["id"]


def _ig_wait(c, cid, tries=40):
    """Reels are transcoded asynchronously; publishing early fails."""
    for _ in range(tries):
        s = _req(f"{GRAPH}/{cid}?fields=status_code,status&access_token={c['ig_access_token']}")
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") == "ERROR":
            raise SystemExit(f"Instagram rejected the media: {s.get('status')}")
        time.sleep(15)
    raise SystemExit("Instagram container never finished processing")


def _ig_publish(c, cid):
    return _req(f"{GRAPH}/{c['ig_user_id']}/media_publish",
                data={"creation_id": cid, "access_token": c["ig_access_token"]})["id"]


def ig_post(c, item, base):
    kind, files, caption = item["kind"], item["files"], item["text"]
    if kind == "reel":
        cid = _ig_container(c, {"media_type": "REELS", "video_url": f"{base}/{files[0]}",
                                "caption": caption, "share_to_feed": "true"})
        _ig_wait(c, cid)
    elif kind == "carousel":
        kids = []
        for f in files:
            k = _ig_container(c, {"image_url": f"{base}/{f}", "is_carousel_item": "true"})
            kids.append(k)
        for k in kids:
            _ig_wait(c, k, tries=20)
        cid = _ig_container(c, {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption})
        _ig_wait(c, cid, tries=20)
    else:
        cid = _ig_container(c, {"image_url": f"{base}/{files[0]}", "caption": caption})
        _ig_wait(c, cid, tries=20)
    return _ig_publish(c, cid)


def ig_refresh_token(c):
    """Keep the token alive. How depends on which login route configured it (see tools/ig_setup.py).

    facebook_page  - Page tokens derived from a long-lived user token do not expire. Confirm the token
                     is still valid and push the horizon out; nothing to exchange.
    facebook_user  - re-exchange the long-lived user token for a fresh 60 days.
    instagram      - Instagram-login tokens refresh at graph.instagram.com.
    """
    kind = c.get("token_kind", "instagram")
    if kind == "facebook_page":
        info = _req(f"{GRAPH}/debug_token?input_token={c['ig_access_token']}"
                    f"&access_token={c['meta_app_id']}|{c['meta_app_secret']}").get("data", {})
        if not info.get("is_valid"):
            raise SystemExit("Page token is no longer valid - re-run: python tools/ig_setup.py <USER_TOKEN>")
        c["ig_token_expires"] = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    elif kind == "facebook_user":
        got = _req(f"{GRAPH}/oauth/access_token?grant_type=fb_exchange_token"
                   f"&client_id={c['meta_app_id']}&client_secret={c['meta_app_secret']}"
                   f"&fb_exchange_token={c['ig_access_token']}")
        c["ig_access_token"] = got["access_token"]
        c["ig_token_expires"] = (datetime.now(timezone.utc)
                                 + timedelta(seconds=int(got.get("expires_in", 5184000)))).isoformat()
    else:
        got = _req("https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token"
                   f"&access_token={c['ig_access_token']}")
        c["ig_access_token"] = got["access_token"]
        c["ig_token_expires"] = (datetime.now(timezone.utc)
                                 + timedelta(seconds=int(got.get("expires_in", 5184000)))).isoformat()
    save(SECRETS, c)
    return c


# ---------------------------------------------------------------- facebook page
def fb_post(c, item):
    """Publish the same package to the Facebook Page.

    Videos go to /videos by file_url (the Reels endpoint needs a 3-phase upload; a vertical video
    on the Page feed is the same asset and far less brittle). Multi-image carousels are uploaded
    unpublished, then attached to one feed post.
    """
    page, tok = c["fb_page_id"], c["ig_access_token"]
    kind, files, caption = item["kind"], item["files"], item["text"]
    base = item["media_base"]
    if kind == "reel":
        return _req(f"{GRAPH}/{page}/videos",
                    data={"file_url": f"{base}/{files[0]}", "description": caption,
                          "access_token": tok})["id"]
    if kind == "carousel":
        ids = []
        for f in files:
            ids.append(_req(f"{GRAPH}/{page}/photos",
                            data={"url": f"{base}/{f}", "published": "false",
                                  "access_token": tok})["id"])
        params = {"message": caption, "access_token": tok}
        for i, mid in enumerate(ids):
            params[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
        return _req(f"{GRAPH}/{page}/feed", data=params)["id"]
    return _req(f"{GRAPH}/{page}/photos",
                data={"url": f"{base}/{files[0]}", "caption": caption, "access_token": tok})["id"]


# ---------------------------------------------------------------- youtube
def yt_access_token(c):
    d = _req("https://oauth2.googleapis.com/token", data={
        "client_id": c["yt_client_id"], "client_secret": c["yt_client_secret"],
        "refresh_token": c["yt_refresh_token"], "grant_type": "refresh_token"})
    return d["access_token"]


def yt_upload(c, item, local_video):
    token = yt_access_token(c)
    meta = {"snippet": {"title": item["yt_title"], "description": item["text"],
                        "tags": item.get("tags", []), "categoryId": "24"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    size = os.path.getsize(local_video)
    # resumable upload: initiate, then PUT the bytes at the returned Location
    r = urllib.request.Request("https://www.googleapis.com/upload/youtube/v3/videos"
                               "?uploadType=resumable&part=snippet,status",
                               data=json.dumps(meta).encode(),
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                        "X-Upload-Content-Type": "video/mp4",
                                        "X-Upload-Content-Length": str(size)})
    with urllib.request.urlopen(r, timeout=120) as resp:
        location = resp.headers.get("Location")
    if not location:
        raise SystemExit("YouTube did not return a resumable upload URL")
    body = open(local_video, "rb").read()
    put = urllib.request.Request(location, data=body, method="PUT",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "video/mp4", "Content-Length": str(size)})
    with urllib.request.urlopen(put, timeout=900) as resp:
        return json.loads(resp.read())["id"]


# ---------------------------------------------------------------- queue
def build_queue(edition_dir):
    date = os.path.basename(edition_dir.rstrip("/\\")).split("-daily")[0]
    caps = load(os.path.join(ROOT, edition_dir, "captions.json"), None)
    if caps is None:
        raise SystemExit(f"No captions.json in {edition_dir}")
    q = load(QUEUE, [])
    # Re-queuing an edition must never resurrect an item that already went out: published.json is the
    # durable record, so anything logged there stays 'published' even though its row is rebuilt.
    already = {e["id"]: e for e in load(LOG, []) if e.get("id")}
    prev = {x["id"]: x for x in q if x["edition"] == edition_dir}
    q = [x for x in q if x["edition"] != edition_dir]
    for key, item in caps.items():
        q.append({"id": f"{date}-{key}", "edition": edition_dir, "key": key,
                  "due_ist": f"{date}T{item['slot']}:00", "kind": item["kind"],
                  "files": item["files"], "text": item["text"],
                  "yt_title": item.get("yt_title"), "tags": item.get("tags", []),
                  "media_base": f"{MEDIA_BASE}/{date}", "status": "queued"})
        row = q[-1]
        old = prev.get(row["id"], {})
        done = already.get(row["id"]) or (old.get("status") == "published" and old)
        if done:
            row["status"] = "published"
            row["result"] = done.get("result")
            row["published_at"] = done.get("published_at")
        elif old.get("result"):
            # partly sent: carry the per-network ids across so the retry does not repeat them
            row["status"] = "partial"
            row["result"] = old["result"]
    q.sort(key=lambda x: x["due_ist"])
    save(QUEUE, q)
    return q


def due(q, now=None):
    now = now or datetime.now(IST)
    out = []
    for x in q:
        if x["status"] not in ("queued", "partial"):
            continue
        t = datetime.fromisoformat(x["due_ist"]).replace(tzinfo=IST)
        if t <= now:
            out.append(x)
    return out


def local_video(x):
    """Path to the reel's MP4, fetching it from the public media repo when it is not on disk.

    YouTube needs the bytes, not a URL. A checkout in CI has no editions/ folder, so fall back to
    the same raw.githubusercontent URL Instagram and Facebook are already given.
    """
    path = os.path.join(ROOT, x["edition"], "video", x["files"][0])
    if os.path.exists(path):
        return path
    url = f"{x['media_base']}/{x['files'][0]}"
    tmp = os.path.join(tempfile.gettempdir(), x["files"][0])
    if not os.path.exists(tmp):
        print(f"    fetching {url}")
        with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
            f.write(r.read())
    return tmp


def publish_item(c, x, dry=False):
    """Publish to every configured network, resuming rather than repeating.

    Each network's id is written back onto the item as soon as it succeeds, and a network already
    present in the result is skipped. So if Facebook fails after Instagram succeeded, the retry
    posts only the missing half - it can never post the same thing twice.
    """
    if dry:
        nets = ["instagram"] + (["facebook"] if c.get("fb_page_id") else []) + \
               (["youtube"] if x["kind"] == "reel" and c.get("yt_refresh_token") else [])
        done = list((x.get("result") or {}).keys())
        todo = [n for n in nets if n not in done]
        print(f"DRY {x['id']}  {x['kind']:<8} {x['files'][0]}  -> {', '.join(todo) or 'nothing left'}")
        return {"dry": True}
    res = dict(x.get("result") or {})
    if "instagram" not in res:
        res["instagram"] = ig_post(c, x, x["media_base"])
        x["result"] = dict(res)
    if c.get("fb_page_id") and "facebook" not in res:
        res["facebook"] = fb_post(c, x)
        x["result"] = dict(res)
    if x["kind"] == "reel" and c.get("yt_refresh_token") and "youtube" not in res:
        res["youtube"] = yt_upload(c, x, local_video(x))
        x["result"] = dict(res)
    return res


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    dry = "--dry-run" in sys.argv

    if cmd == "check":
        c = creds()
        used, total = ig_limit(c)
        print(f"Instagram: {used}/{total} API posts used in the last 24h")
        exp = c.get("ig_token_expires")
        if exp:
            left = (datetime.fromisoformat(exp) - datetime.now(timezone.utc)).days
            print(f"Instagram token: {left} days left" + ("  → refreshing" if left < 20 else ""))
            if left < 20:
                ig_refresh_token(c)
                print("Instagram token refreshed")
        print("YouTube:", "configured" if c.get("yt_refresh_token") else "not configured")
        q = load(QUEUE, [])
        print(f"Queue: {sum(1 for x in q if x['status']=='queued')} queued, {len(due(q))} due now")

    elif cmd == "queue":
        q = build_queue(sys.argv[2])
        print(f"{len(q)} items queued. Next: {q[0]['id']} at {q[0]['due_ist']} IST" if q else "empty")

    elif cmd in ("run", "one"):
        c = None if dry else creds()
        q = load(QUEUE, [])
        if cmd == "one":
            edition, key = sys.argv[2], sys.argv[3]
            items = [x for x in q if x["edition"] == edition and x["key"] == key] or \
                    [x for x in build_queue(edition) if x["key"] == key]
        else:
            items = due(q)
        if not items:
            print("nothing due")
            return
        if not dry:
            used, total = ig_limit(c)
            if used + len(items) > total:
                print(f"Instagram 24h limit: {used}/{total} used — publishing only what fits")
                items = items[: max(0, total - used)]
        log = load(LOG, [])
        try:
            for x in items:
                try:
                    res = publish_item(c, x, dry)
                    x["status"] = "published" if not dry else "queued"
                    x["result"] = res
                    x["published_at"] = datetime.now(IST).isoformat()
                    log.append({k: x[k] for k in ("id", "kind", "published_at", "result") if k in x})
                    print(f"OK  {x['id']} -> {res}")
                except SystemExit as e:
                    # Something already went out? Keep it as 'partial' so the retry resumes the
                    # missing networks instead of treating the whole item as unsent.
                    part = bool(x.get("result"))
                    x["status"] = "partial" if part else "failed"
                    x["error"] = str(e)
                    print(f"{'PARTIAL' if part else 'FAIL'} {x['id']}: {e}")
                except Exception as e:  # noqa: BLE001 - an unexpected error must not hide a live post
                    x["status"] = "unknown"
                    x["error"] = f"{type(e).__name__}: {e}"
                    print(f"ERROR {x['id']}: {x['error']} - VERIFY on the account before re-running")
        finally:
            # Must run even if the loop raises: an unsaved queue means a post that already went out
            # is still marked 'queued' and gets published a second time on the next run.
            if not dry:
                save(QUEUE, q)
                save(LOG, log)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
