# buzzora-publisher

Cloud runner for Buzzora's daily posting. **Private on purpose** — it holds the Meta and YouTube
credentials as Actions secrets. Media stays in the public `buzzora-media` repo and is referenced by
`raw.githubusercontent.com` URL.

## Why this exists

Instagram's Content Publishing API has **no scheduled-publish parameter** — something must call the
API at the moment each post goes out. That used to be a local task on one PC, which meant nothing
published while the machine was asleep. This repo does the same job on GitHub's schedule instead.

Facebook and YouTube *can* be scheduled natively (`scheduled_publish_time`, `publishAt`), but they
go through the same runner here so all three networks stay in one place and one state file.

## How it works

1. `.secrets/queue.json` holds the day's items and their IST slots. It carries **no credentials** —
   only captions and public media URLs.
2. `.github/workflows/publish.yml` runs every 30 minutes between **02:00–17:30 UTC**
   (07:30–23:00 IST) and calls `python tools/publish.py run`.
3. Anything whose IST slot has passed is published: Instagram + Facebook Page for every item, plus a
   YouTube Short for reels.
4. The updated `.secrets/queue.json` and `.secrets/published.json` are committed back, so the next
   run knows what already went out.

The window is deliberate: 24/7 would be ~1,440 runs/month and would exceed the 2,000 free
Actions minutes a private repo gets.

## Loading a new day

The queue is built on the workstation by the daily edition task, then pushed here:

```bash
python tools/push_queue.py        # in E:\Buzzora
```

## Secrets

| Secret | From |
|---|---|
| `BUZZORA_IG_USER_ID` | Instagram Business account id |
| `BUZZORA_IG_ACCESS_TOKEN` | Facebook **Page** token (does not expire) |
| `BUZZORA_FB_PAGE_ID` | Facebook Page id |
| `BUZZORA_META_APP_ID` / `BUZZORA_META_APP_SECRET` | Meta app *Buzzora72* |
| `BUZZORA_TOKEN_KIND` | `facebook_page` |
| `BUZZORA_YT_CLIENT_ID` / `BUZZORA_YT_CLIENT_SECRET` / `BUZZORA_YT_REFRESH_TOKEN` | Google Cloud OAuth client |

All are generated on the workstation by `tools/ig_setup.py` and `tools/yt_setup.py`. See
`PUBLISHING.md` in the main Buzzora project.

## Safety properties

- **Never double-posts.** Each network's id is recorded the instant it succeeds; a network already
  in an item's `result` is skipped. A part-failed item is `partial`, and the retry resumes only the
  missing networks.
- **No overlapping runs** (`concurrency: buzzora-publish`), so two runs cannot publish the same item.
- **State is committed even on failure**, so a crash cannot make an already-published item look unsent.

## Manual run

Actions → *Buzzora publish queue* → **Run workflow**. `dry_run` defaults to **true** — untick it to
actually publish.
