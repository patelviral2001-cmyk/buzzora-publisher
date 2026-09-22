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
  python tools/publish.py serve [--hours 5.75]           # cloud runner: stay up, publish each slot on time
  python tools/publish.py stage <id>                     # natively schedule one item on Facebook + YouTube
"""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
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


def _fb_find_scheduled(c, when, caption):
    """Id of an already-scheduled Page post with this slot and caption, or None.

    Guards against scheduling the same item twice (e.g. state failed to save after the API call),
    and resolves the real id when /feed answers with a placeholder like '<page>_1'.
    """
    try:
        d = _req(f"{GRAPH}/{c['fb_page_id']}/scheduled_posts?fields=id,message,scheduled_publish_time"
                 f"&limit=100&access_token={c['ig_access_token']}")
    except SystemExit:
        return None
    ts, head = int(when.timestamp()), caption[:80]
    for p in d.get("data", []):
        spt = p.get("scheduled_publish_time")
        if isinstance(spt, str):
            try:
                spt = int(datetime.fromisoformat(spt.replace("+0000", "+00:00")).timestamp())
            except ValueError:
                spt = None
        if spt == ts and (p.get("message") or "")[:80] == head:
            return p["id"]
    return None


def fb_schedule(c, item, when):
    """Hand the post to Facebook with its publish time, so it goes out on time with no runner awake.

    Same three shapes as fb_post, created unpublished with scheduled_publish_time (Facebook needs it
    10 minutes to 30 days ahead). Carousel photos are uploaded as temporary, as scheduled multi-photo
    posts require.
    """
    page, tok = c["fb_page_id"], c["ig_access_token"]
    kind, files, caption, base = item["kind"], item["files"], item["text"], item["media_base"]
    existing = _fb_find_scheduled(c, when, caption)
    if existing:
        return existing
    ts = str(int(when.timestamp()))
    if kind == "reel":
        return _req(f"{GRAPH}/{page}/videos",
                    data={"file_url": f"{base}/{files[0]}", "description": caption, "published": "false",
                          "scheduled_publish_time": ts, "access_token": tok}, timeout=600)["id"]
    if kind == "carousel":
        ids = [_req(f"{GRAPH}/{page}/photos",
                    data={"url": f"{base}/{f}", "published": "false", "temporary": "true",
                          "access_token": tok})["id"] for f in files]
        params = {"message": caption, "published": "false", "scheduled_publish_time": ts, "access_token": tok}
        for i, mid in enumerate(ids):
            params[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
        pid = _req(f"{GRAPH}/{page}/feed", data=params)["id"]
        if pid.endswith("_1"):  # placeholder answer seen on 21 Sept; look up the real id
            pid = _fb_find_scheduled(c, when, caption) or pid
        return pid
    return _req(f"{GRAPH}/{page}/photos",
                data={"url": f"{base}/{files[0]}", "caption": caption, "published": "false",
                      "scheduled_publish_time": ts, "access_token": tok})["id"]


def fb_cancel(c, fid):
    """Remove a scheduled (not yet public) Page post - used when an item is held."""
    return _req(f"{GRAPH}/{fid}?access_token={c['ig_access_token']}", method="DELETE")


# ---------------------------------------------------------------- youtube
def yt_access_token(c):
    d = _req("https://oauth2.googleapis.com/token", data={
        "client_id": c["yt_client_id"], "client_secret": c["yt_client_secret"],
        "refresh_token": c["yt_refresh_token"], "grant_type": "refresh_token"})
    return d["access_token"]


def yt_upload(c, item, local_video, publish_at=None):
    """Upload a Short. With publish_at it is uploaded private and YouTube itself makes it public then."""
    token = yt_access_token(c)
    status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    if publish_at:
        status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                  "publishAt": publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    meta = {"snippet": {"title": item["yt_title"], "description": item["text"],
                        "tags": item.get("tags", []), "categoryId": "24"},
            "status": status}
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


def yt_unschedule(c, vid):
    """Cancel a scheduled Short: it stays uploaded but private, with no publish time. Nothing is deleted."""
    token = yt_access_token(c)
    body = json.dumps({"id": vid, "status": {"privacyStatus": "private",
                                             "selfDeclaredMadeForKids": False}}).encode()
    return _req("https://www.googleapis.com/youtube/v3/videos?part=status", data=body, method="PUT",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


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
        elif old.get("status") == "held":
            # an editor pulled it (push_queue.py --hold); re-queuing must not silently put it back
            row["status"] = "held"
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


def publish_item(c, x, dry=False, skip_meta=False):
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
    # Networks natively scheduled ahead of time (see stage_item) publish themselves at the slot;
    # their ids carry across so the runner never posts them a second time.
    for net, sid in (x.get("staged") or {}).items():
        res.setdefault(net, sid)
    # Networks are independent: Meta blocking the app (22 Sept) must not also stop YouTube. Every
    # network is attempted; failures are collected and raised together after the others have gone.
    steps = []
    if not skip_meta:
        steps.append(("instagram", lambda: ig_post(c, x, x["media_base"])))
        if c.get("fb_page_id"):
            steps.append(("facebook", lambda: fb_post(c, x)))
    if x["kind"] == "reel" and c.get("yt_refresh_token"):
        steps.append(("youtube", lambda: yt_upload(c, x, local_video(x))))
    errors = ["instagram/facebook: skipped - Meta API unavailable"] if skip_meta else []
    for net, fn in steps:
        if net in res:
            continue
        try:
            res[net] = fn()
            x["result"] = dict(res)
        except SystemExit as e:
            errors.append(f"{net}: {e}")
    if errors:
        raise SystemExit(" | ".join(errors))
    return res


# ---------------------------------------------------------------- cloud runner (serve)
# The GitHub Actions cron fired once in ~10 hours on 21 Sept, so slots went out 2-5 hours late in
# bursts. `serve` removes the dependency: one job stays up ~5.75 h, sleeps until each slot and
# publishes on the minute, then the workflow hands over to a fresh job. State is the git repo itself:
# every change is re-applied on top of origin/main and pushed at once, so a hold or a new queue
# pushed from the PC mid-run is picked up, and nothing is ever published twice.
STAGE_MIN = timedelta(minutes=20)    # closer than this, just publish live at the slot
STAGE_MAX = timedelta(hours=26)      # far enough to cover tomorrow's whole edition once it is pushed
STAGE_ON = os.environ.get("BUZZORA_STAGE", "0") == "1"


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def pull_state():
    _git("fetch", "--quiet", "origin", "main")
    _git("reset", "--quiet", "--hard", "origin/main")


def commit_state(mutate, msg):
    """Apply mutate(queue, log) to the latest origin state and push it; retry if someone pushed first.

    Returns False if it could not be saved - callers then stop making API calls whose record would
    be lost (that is how a lost save turns into a duplicate post).
    """
    for attempt in range(6):
        pull_state()
        q, log = load(QUEUE, []), load(LOG, [])
        mutate(q, log)
        save(QUEUE, q)
        save(LOG, log)
        _git("add", ".secrets/queue.json", ".secrets/published.json")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return True
        _git("-c", "user.name=Buzzora", "-c", "user.email=buzzora72@gmail.com", "commit", "--quiet", "-m", msg)
        if _git("push", "--quiet", "origin", "HEAD:main").returncode == 0:
            return True
        time.sleep(3 + 2 * attempt)
    print(f"!! could not save state after retries: {msg}")
    return False


def _row(q, item_id):
    return next((r for r in q if r["id"] == item_id), None)


def _due_at(x):
    return datetime.fromisoformat(x["due_ist"]).replace(tzinfo=IST)


def stage_item(c, x, save_fn):
    """Natively schedule one item on Facebook (and YouTube for reels). Instagram still posts at the slot.

    Each network is recorded the moment it succeeds. Failures are reported and left for the slot:
    an unstaged network simply publishes live, as before.
    """
    when = _due_at(x)
    staged = dict(x.get("staged") or {})
    ok = True

    def record(net, sid):
        staged[net] = sid

        def m(q, log):
            r = _row(q, x["id"])
            if r is not None:
                r.setdefault("staged", {})[net] = sid
        return save_fn(m, f"stage {x['id']} {net}")

    if c.get("fb_page_id") and "facebook" not in staged:
        try:
            ok = record("facebook", fb_schedule(c, x, when)) and ok
            print(f"STAGED {x['id']} facebook for {when:%d %b %H:%M} IST")
        except SystemExit as e:
            print(f"stage failed {x['id']} facebook (will post live at slot): {e}")
    if ok and x["kind"] == "reel" and c.get("yt_refresh_token") and "youtube" not in staged:
        try:
            ok = record("youtube", yt_upload(c, x, local_video(x), publish_at=when)) and ok
            print(f"STAGED {x['id']} youtube for {when:%d %b %H:%M} IST")
        except SystemExit as e:
            print(f"stage failed {x['id']} youtube (will post live at slot): {e}")
    return ok


def unstage_item(c, x, save_fn):
    """Cancel an item's native schedules (it was held, or its slot/copy changed after staging)."""
    staged = dict(x.get("staged") or {})
    if "facebook" in staged:
        try:
            fb_cancel(c, staged["facebook"])
        except SystemExit as e:
            print(f"!! could not cancel facebook schedule for {x['id']}: {e}")
            return False
    if "youtube" in staged:
        try:
            yt_unschedule(c, staged["youtube"])
        except SystemExit as e:
            print(f"!! could not cancel youtube schedule for {x['id']}: {e}")
            return False

    def m(q, log):
        r = _row(q, x["id"])
        if r is not None:
            r.pop("staged", None)
            r.pop("restage", None)
    print(f"UNSTAGED {x['id']} ({', '.join(staged)})")
    return save_fn(m, f"unstage {x['id']}")


LATE_MAX = timedelta(hours=3)   # later than this, an item needs a human decision, not an auto-post


def publish_due(c, q, save_fn):
    items = due(q)
    if not items:
        return
    # Never auto-post stale items in a burst when a long outage ends: copy says "today", slots pile
    # up. Mark them 'missed'; `push_queue.py --unhold <id>` re-queues one deliberately.
    now = datetime.now(IST)
    for x in [x for x in items if now - _due_at(x) > LATE_MAX]:
        print(f"MISSED {x['id']} (due {x['due_ist'][5:16]}) - over {LATE_MAX} late, not auto-posting")

        def m(q2, log, i=x["id"]):
            r = _row(q2, i)
            if r is not None and r["status"] in ("queued", "partial"):
                r["status"] = "missed"
        save_fn(m, f"missed {x['id']}")
    items = [x for x in items if now - _due_at(x) <= LATE_MAX]
    if not items:
        return
    skip_meta = False
    try:
        used, total = ig_limit(c)
        if used + len(items) > total:
            print(f"Instagram 24h limit: {used}/{total} used - publishing only what fits")
            items = items[: max(0, total - used)]
    except SystemExit as e:
        # Meta down or blocking the app: still send what does not depend on it (YouTube Shorts).
        print(f"!! Meta unavailable, YouTube only this round: {str(e)[:200]}")
        skip_meta = True
        items = [x for x in items if x["kind"] == "reel" and "youtube" not in (x.get("result") or {})
                 and "youtube" not in (x.get("staged") or {})]
    for x in items:
        fields = {}
        try:
            res = publish_item(c, x, skip_meta=skip_meta)
            fields = {"status": "published", "result": res, "published_at": datetime.now(IST).isoformat()}
            print(f"OK  {x['id']} -> {res}")
        except SystemExit as e:
            part = bool(x.get("result"))
            fields = {"status": "partial" if part else "failed", "result": x.get("result"), "error": str(e)}
            print(f"{'PARTIAL' if part else 'FAIL'} {x['id']}: {e}")
        except Exception as e:  # noqa: BLE001 - an unexpected error must not hide a live post
            fields = {"status": "unknown", "result": x.get("result"), "error": f"{type(e).__name__}: {e}"}
            print(f"ERROR {x['id']}: {fields['error']} - VERIFY on the account")

        def m(q2, log, x=x, fields=fields):
            r = _row(q2, x["id"])
            if r is None:
                return
            r.update({k: v for k, v in fields.items() if v is not None})
            if fields.get("status") == "published" and not any(e.get("id") == x["id"] for e in log):
                log.append({k: r[k] for k in ("id", "kind", "published_at", "result") if k in r})
        if not save_fn(m, f"publish {x['id']} {fields.get('status')}"):
            raise SystemExit("state could not be saved - stopping so nothing is posted twice")


def serve(hours):
    c = creds()
    start = datetime.now(IST)
    end = start + timedelta(hours=hours)
    stage_ok = STAGE_ON
    print(f"serve: {start:%d %b %H:%M} -> {end:%H:%M} IST · native FB/YT scheduling {'ON' if STAGE_ON else 'off'}")
    while True:
        now = datetime.now(IST)
        if now >= end - timedelta(minutes=12):   # leave room for one slow reel before the job limit
            break
        try:
            pull_state()
            q = load(QUEUE, [])
            for x in q:   # holds and edits made after staging
                if x.get("staged") and (x["status"] == "held" or x.get("restage")):
                    unstage_item(c, x, commit_state)
            pull_state()
            publish_due(c, load(QUEUE, []), commit_state)
            if stage_ok:
                pull_state()
                for x in load(QUEUE, []):
                    t = _due_at(x)
                    if (x["status"] == "queued" and not x.get("staged")
                            and now + STAGE_MIN <= t <= now + STAGE_MAX):
                        if not stage_item(c, x, commit_state):
                            stage_ok = False   # a lost record could mean a duplicate: stop staging
                            break
        except SystemExit as e:
            print(f"!! {e}")
            if "state could not be saved" in str(e):
                raise
        except Exception as e:  # noqa: BLE001 - a network blip must not end the shift
            print(f"!! {type(e).__name__}: {e}")
        q = load(QUEUE, [])
        upcoming = [_due_at(x) for x in q if x["status"] in ("queued", "partial") and _due_at(x) > now]
        wake = min([now + timedelta(minutes=10), end - timedelta(minutes=12)] + upcoming)
        time.sleep(max(20, (wake - datetime.now(IST)).total_seconds() + 5))
    ran = datetime.now(IST) - start
    print(f"serve: shift over after {ran}")
    if ran < timedelta(minutes=30):
        # never hand over from a run that ended almost at once, or a fault becomes a dispatch loop
        raise SystemExit("serve ended early - not handing over")


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

    elif cmd == "serve":
        hours = float(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 5.75
        serve(hours)

    elif cmd == "stage":
        c = creds()
        pull_state()
        x = _row(load(QUEUE, []), sys.argv[2])
        if x is None:
            raise SystemExit(f"{sys.argv[2]} not in queue")
        if x.get("staged"):
            print(f"{x['id']} already staged: {x['staged']}")
            return
        if not (datetime.now(IST) + STAGE_MIN <= _due_at(x)):
            raise SystemExit(f"{x['id']} is due too soon to schedule natively")
        stage_item(c, x, commit_state)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
