# Reddit Saved Archiver

A macOS tkinter app that downloads the Reddit posts you've **saved** (other people's posts) with full metadata, media, and comment trees — plus a built-in offline HTML viewer that presents everything Reddit-style. Resumable and designed for periodic re-runs.

## Scope and API usage

This is a single-user tool: it authenticates via OAuth as the account owner only, and touches nothing but that account's own saved items. Access is strictly read-only (OAuth scopes: `identity`, `history`, `read`) — the app never posts, comments, votes, messages, subscribes, or modifies anything. Request volume stays under Reddit's free-tier limit of 100 queries per minute; PRAW honors the `X-Ratelimit-*` response headers automatically. Usage is a one-time backfill of the account's saved history followed by small incremental runs. All archived data is stored locally on the user's own computer for private reference — nothing is redistributed, published, sold, or used for AI/ML training. Built for use in accordance with Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) and Data API Terms.

## Why two sources

Reddit's API caps the saved listing at roughly your **1,000 most recent** saves. Anything older is invisible to the listing but still fetchable by ID. Reddit's data export gives you a `saved_posts.csv` listing *everything* you've ever saved; the app merges both sources and de-duplicates.

**Do this first (it takes hours to a few days):** go to <https://www.reddit.com/settings/data-request>, request your data ("all time"), and wait for the email. Unzip it and point the app at `saved_posts.csv`. You can archive your recent ~1,000 right away while you wait. The CSV only needs to be supplied **once**, but it is also safe to leave the field filled — already-archived IDs are excluded before any metadata is fetched, so re-runs cost nothing extra.

## Setup

1. **Install dependencies**

   ```bash
   pip3 install praw
   brew install yt-dlp ffmpeg     # optional but recommended — needed for v.redd.it and external videos
   ```

   Your Python needs tkinter. If `python3 -c "import tkinter"` fails: `brew install python-tk`.

2. **Get Reddit API credentials** (a client ID + secret from a "script" app)

   As of late 2025, Reddit gates new app creation behind its [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) — clicking "create app" at reddit.com/prefs/apps now points you to an approval process instead of issuing keys.

   - **If you already have an app** listed at <https://www.reddit.com/prefs/apps> (created before the gate): use its client ID and secret, and edit its redirect URI to exactly `http://localhost:8765`. Done.
   - **Otherwise, request access** via the "file a ticket" link in the *Developers* section of the policy page, from your main account. Describe the use case as read-only, single-user archiving of your own account's saved posts (scopes: identity, history, read; well under 100 requests/min; no redistribution, AI training, or commercial use; redirect URI `http://localhost:8765`). Once approved and the app exists, the **client ID** is the string under the app name and the **secret** is labeled.

3. **Run it**

   ```bash
   python3 reddit_saved_archiver.py
   ```

## Using the app

- Enter the client ID and secret, plus your username.
- **Auth:** click **Authorize in browser…** (recommended — works with 2FA and Google sign-in; stores a permanent refresh token so long runs don't die when the hourly access token expires). Password mode exists as a fallback; with 2FA you'd have to enter `yourpassword:123456`, and it breaks on runs longer than an hour, so use browser auth.
- **Sources:** leave the listing checked; browse to `saved_posts.csv` once you have your export.
- **Comments:** *Top-loaded* (default) grabs everything Reddit returns in one request per post — usually a few hundred comments. *Full threads* expands every "load more" link, which can multiply the request count.
- Click **Start archiving**. When it finishes it (re)generates the viewer automatically.

## Browsing the archive (the viewer)

Open **`index.html`** in the archive root — no server needed, works straight off the filesystem.

- **index.html** — every archived post with thumbnails; live text search, subreddit filter (with counts), status filter, and sorting by date posted, date archived, score, or save order. This is your categorization workbench.
- **post.html** (one per post) — styled like Reddit: title, author, score, flair/NSFW/removed badges, inline images and videos, the selftext rendered with Reddit's own HTML, and the full comment tree as **nested, collapsible threads** (click `[–]` to fold a thread) with OP/MOD/pinned badges. Each page links back to the index and out to the live Reddit post.

The **Rebuild viewer (offline)** button regenerates `index.html` and any missing `post.html` pages from what's on disk — no Reddit connection, useful after upgrading the app or if you hand-edit anything.

## Metadata for categorizing

Every post carries (in `post.json`, `index.csv`, and the folder name):

| What | Where | Notes |
|---|---|---|
| Subreddit | everywhere | also a filter in the viewer |
| Date **posted** | `created_utc` | folder names start with it |
| Date **saved** | — | **Reddit does not expose save timestamps to anyone.** The app records the next-best things: |
| Save **order** | `save_order` + `save_source` | position within the listing (1 = most recently saved) or within the export CSV |
| How it was saved | `saved_via` | `post`, `comment`, or `post+comment` — plus `saved_comment_ids` listing the exact comments |
| Date **archived** | `archived_at` | for posts saved after you start running periodically, this bounds the save date to within your run cadence |
| Score, flair, author, NSFW, everything else | `post.json` | the complete API payload |

## How comments are stored

`comments.json` is a **nested tree**: the top-level array holds top-level comments, and every comment has a `replies: [...]` array of its children, recursively — depth is the nesting itself. Each node carries `author`, `score`, `created_utc`, `edited`, `is_submitter` (OP), `stickied`, `distinguished` (mods), the raw markdown `body`, and Reddit's rendered `body_html` (which the viewer uses, so formatting matches Reddit exactly). Comments are captured in Reddit's **top** sort. `post.md` additionally renders the top 15 threads to depth 3 for quick text reading.

## Periodic re-runs (incremental behavior)

`index.csv` is the ledger. On every run the app:

1. Walks the current saved listing (~10 cheap requests for the full 1,000).
2. Skips every ID already successfully archived — **before** spending any requests on comments or media.
3. Archives only what's new — including re-archiving a post when you've saved a *new comment* in a thread it already has, so the new save gets marked.
4. Refreshes the viewer.

So a weekly run costs seconds plus only your new saves. Notes: archived posts are **snapshots** (comments/scores aren't re-fetched — uncheck *Skip already-archived* to force a full redo); un-saving a post on Reddit does not remove it from the archive; rows with `failed:` status are retried automatically next run.

## Output layout

```
RedditSaved/
├── index.html                      # the browsable archive (open this)
├── index.csv                       # ledger: one row per post (drives skip logic + viewer)
└── posts/
    └── 2025-11-03_networking_1abc2de_Travel-router-setup/
        ├── post.html               # Reddit-style page with nested collapsible comments
        ├── post.json               # everything Reddit returned + _archive block
        ├── post.md                 # plain-text readable version
        ├── comments.json           # full archived comment tree
        ├── media/                  # images, gallery items, videos
        └── .archived_ok            # completion marker
```

## What to expect

- **Speed:** the free API tier allows 100 requests/minute. Metadata is batched 100 posts per request; comments cost 1 request per post. Ballpark: **~1,500 posts ≈ 20–30 min** plus media download time.
- **Removed/deleted posts:** archived with whatever survives (title and metadata usually do; selftext shows `[removed]`, media often 404s). The app grabs Reddit's cached preview image as a last resort and marks the status in `index.csv` and on the page.
- **Videos:** v.redd.it serves video and audio as separate streams; yt-dlp + ffmpeg mux them. Without them you get a video-only fallback file.
- **Saved comments:** when you saved a *comment*, the app archives its **parent post** with the full standard treatment, guarantees a copy of the saved comment itself (fetching it individually if the main tree didn't reach it), highlights it in gold in `post.html` with a 💾 SAVED badge, records it in `post.json` under `_archive.saved_comments`, and tags the index row `saved_via: comment` (filterable in the viewer). Feed `saved_comments.csv` from your export into the Comments CSV field for full history. Toggle the whole behavior off with the checkbox if you only want directly saved posts.
- **Stop/resume:** Stop finishes the current post cleanly; the next run picks up where it left off.

## Config & credentials

Settings persist to `~/Library/Application Support/RedditSavedArchiver/config.json` (chmod 600). The refresh token is stored there; your password is only stored if you tick "Remember password". Everything runs locally — nothing leaves your machine except API calls to Reddit.

## Troubleshooting

- **HTTP 401 on connect** — client ID/secret typo, or the app isn't type "script".
- **Browser auth fails / no token returned** — the app's redirect URI on reddit.com must be *exactly* `http://localhost:8765`.
- **Port 8765 in use** — close whatever is bound to it (only needed during the one-time authorize step).
- **yt-dlp errors on old external links** — expected; imgur/gfycat purged a lot of history. The post's `post.json` always records the original URL.

## License

MIT — see [LICENSE](LICENSE).
