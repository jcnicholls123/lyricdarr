# Lyricdarr

A small self-hosted app that does for **lyrics** what Bazarr does for **subtitles**:
it scans your music library, checks each track against [LRCLIB](https://lrclib.net)
(a free, open synced-lyrics database), and saves a `.lrc` file next to each track,
named to match the audio file — e.g. `01 - Song Title.mp3` → `01 - Song Title.lrc`.

This is a working MVP, not a polished production project — treat it as a solid
starting point you can extend (more providers, embedded lyrics, Lidarr webhook
triggers, etc).

## Features

- Recursively scans a music folder (`.mp3`, `.flac`, `.m4a`, `.ogg`, `.opus`, `.wav`, `.wma`, `.aac`)
- Reads artist/title/album/duration tags via `mutagen`
- Queries LRCLIB for an exact match, falls back to fuzzy search
- Saves synced `.lrc` lyrics matching the track's filename
- Tracks status per-track in a small SQLite DB (pending / found / not found /
  instrumental / pending review / error)
- Web dashboard with real-time scan/fetch progress, browsable by Artist / Album / Song
  with a completion tick at each level, search, pagination, per-track retry, and a
  preview-before-you-approve review queue for lower-confidence fuzzy matches
- "Refresh library" (scan) automatically removes tracks whose audio file has been
  deleted from disk, alongside picking up newly added ones
- Manual delete buttons on artists/albums/tracks in the dashboard, for untracking
  entries yourself (doesn't touch anything on disk — just Lyricdarr's own record of it)
- "Verify" action to re-check which `.lrc` files are actually on disk without a full rescan
- In-app settings panel to change the auto-scan interval or turn it off, no restart needed
- Auto re-scans and re-fetches on a schedule (default every 6 hours), like Bazarr's automatic search

## Getting this onto GitHub (one-time setup)

This repo includes a GitHub Actions workflow that automatically builds a Docker
image and publishes it to GitHub Container Registry (GHCR) every time you push.
Once that's done, TrueNAS's Apps GUI can just pull the image by name — no
building on the NAS itself required.

1. Create a new **empty** repo on GitHub (e.g. `lyricdarr`) — don't add a README/license
   there, since this folder already has them.
2. From inside this folder, run:

   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/lyricdarr.git
   git push -u origin main
   ```

3. On GitHub, go to your new repo → **Actions** tab. You should see the "Build
   and publish Docker image" workflow run automatically (takes 1-2 minutes).
4. Once it's green, go to your GitHub profile → **Packages** — you'll see a
   package named `lyricdarr`. By default GitHub packages are private; open the
   package → **Package settings** → change visibility to **Public** (simplest,
   since there's nothing sensitive in the image) — otherwise TrueNAS will need
   a GHCR login token to pull it.
5. Your image is now available at:
   ```
   ghcr.io/YOUR_GITHUB_USERNAME/lyricdarr:latest
   ```
   (all lowercase, matching your GitHub username). Any time you push a change
   to `main`, this image rebuilds and updates automatically.

## Installing on TrueNAS SCALE via the Apps GUI

1. In the TrueNAS web UI, go to **Apps** → **Discover Apps** → **Custom App**
   (top-right, sometimes called "Install via YAML" depending on your SCALE version).
2. Fill in:
   - **Application Name**: `lyricdarr`
   - **Image repository**: `ghcr.io/YOUR_GITHUB_USERNAME/lyricdarr`
   - **Image tag**: `latest`
3. **Container Port**: `8686` → map to a host port of your choice (e.g. `8686`)
4. **Storage / Host Path Volumes** — add two:
   - Host path `/mnt/your-pool/media/music` → Mount path `/music` (read-write —
     this needs to be the *same* library path Lidarr/Plex use)
   - Host path `/mnt/your-pool/apps/lyricdarr/config` → Mount path `/config`
     (create this dataset/folder first so settings persist across restarts)
5. **Environment Variables** (optional):
   - `SCAN_INTERVAL_HOURS` = `6` (or whatever cadence you want)
6. Click **Install**.
7. Once running, open `http://<your-truenas-ip>:8686`, click **Refresh library**,
   then **Download missing lyrics**.

After that, it re-scans and re-fetches automatically on the interval you set —
so newly added albums from Lidarr get lyrics without you doing anything.

### Updating later
Since the image is rebuilt automatically on every GitHub push, updating just
means: **Apps → lyricdarr → Edit → re-pull image** (or delete and reinstall
pointing at the same `ghcr.io/...:latest` tag) whenever you've pushed a change.

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `/music` | Path inside the container to scan (map your library volume here) |
| `SCAN_INTERVAL_HOURS` | `6` | How often to auto re-scan + fetch missing lyrics |

## API

Since it's FastAPI under the hood, you get a free interactive API explorer at
`http://<host>:8686/docs` if you want to script against it (e.g. trigger a scan
right after Lidarr imports something, via a webhook/custom script).

Key endpoints:
- `POST /api/scan` — rescan the library for new files, and remove tracks whose file
  is no longer on disk (blocking, returns a summary including `removed_tracks`)
- `POST /api/fetch` — attempt to fetch lyrics for anything not yet found (blocking)
- `POST /api/scan-and-fetch` — do both in one call (blocking)
- `POST /api/verify/start` — re-check existing tracks' `.lrc` files on disk without a
  full rescan (picks up manually added/removed lyrics)
- `POST /api/scan/start`, `POST /api/fetch/start`, `POST /api/scan-and-fetch/start` —
  same as above but run in the background; returns immediately with `{"started": true}`
  (409 if a job is already running). Pair with `/api/progress` or the SSE stream below
  for live status — this is what the dashboard UI uses.
- `GET /api/progress` — current background job status (phase, running, processed/total, current item)
- `GET /api/progress/stream` — Server-Sent Events stream of the same, pushed in real time
- `GET /api/tracks?status=not_found&page=1&page_size=100` — paginated track list
- `GET /api/library/tree?group_by=artist|album|song&search=` — library grouped for
  browsing, with a completion tally at each level
- `POST /api/tracks/{id}/retry` — retry a single track
- `POST /api/tracks/{id}/approve` — write a pending fuzzy-matched lyric candidate to disk
- `POST /api/tracks/{id}/reject` — discard a pending fuzzy-matched candidate without writing it
- `DELETE /api/tracks/{id}` — untrack a single track (no files touched on disk)
- `DELETE /api/library/album?artist=&album=` — untrack every track in an album
- `DELETE /api/library/artist?artist=` — untrack every track by an artist
- `GET /api/settings` / `POST /api/settings` — read or update `scan_interval_hours`,
  `auto_scan_enabled`, and `netease_fallback_enabled` (persisted, applied immediately)
- `POST /api/webhook/lidarr` — see **Lidarr integration** below

## Lyrics sources

LRCLIB is tried first (`/get` exact match, then `/search` fuzzy match). If neither
finds anything, and the "Use NetEase Cloud Music as a fallback" setting is on
(default: on), it's queried as a second source — it's an unofficial endpoint (the
same one NetEase's own web player calls, no API key needed) but has good synced-lyrics
coverage, including plenty of non-Chinese/Western tracks. Toggle it off in Settings if
you'd rather only ever use LRCLIB.

An exact LRCLIB `/get` hit is written straight to disk — it's a confident match.
Anything from a fuzzy text search (LRCLIB `/search` or NetEase) goes through
duration, version-qualifier (acoustic/live/remix/etc.), artist, and — for NetEase —
CJK-language consistency checks first, but a text search can still surface the wrong
recording. Those results are held as `pending_review` instead of being written
automatically: the dashboard shows a `[review]` count, and each one has a **Review**
button that previews the candidate lyrics before you **Approve & save** or **Reject** it.

Musixmatch was considered but skipped: their free developer API only returns a ~30%
lyrics snippet, not full synced lyrics, unless you have a paid commercial license — not
much of a fallback. If you have that kind of Musixmatch access, `app/netease.py` is a
small, self-contained example of the provider shape (`search_lyrics(artist, title,
duration) -> {"synced", "plain", "instrumental"} | None`) to copy for your own
provider module.

## Lidarr integration

To fetch lyrics right after Lidarr imports a new album instead of waiting for the
next scheduled scan:

1. In Lidarr, go to **Settings → Connect → +** → **Webhook**.
2. **URL**: `http://<lyricdarr-host>:8787/api/webhook/lidarr`
3. **Method**: `POST`
4. Trigger on **On Import** and **On Upgrade** (or whichever import events you want).
5. Save. Lidarr will now kick off a scan-and-fetch pass on this app every time it
   imports something — new tracks show up without waiting on the schedule.

Optional: set `WEBHOOK_TOKEN` in the container's environment to require a shared
secret on that endpoint (anyone else on your LAN could otherwise trigger it). With it
set, append `?token=<value>` to the webhook URL in Lidarr's connect settings (Lidarr's
built-in webhook connector doesn't support custom headers, so the query param is the
practical way to pass it).

## Notes / limitations (things to extend if you want to go further)

- No auth on the web UI — fine on an internal home network, but put it behind
  your reverse proxy / VPN like you would Sonarr or Radarr if exposing it further.
- Tag reading assumes reasonably well-tagged files. Poorly tagged tracks (missing
  artist/title) will get worse match rates from either lyrics source.
- There's no per-user auth, HTTPS, or rate-limit handling for LRCLIB/NetEase — they're
  free public endpoints, so it's polite to not hammer them on very large libraries; the
  scheduled job naturally spreads load out over time as your library grows.
