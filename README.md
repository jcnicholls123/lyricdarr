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
- Tracks status per-track in a small SQLite DB (pending / found / not found / instrumental / error)
- Web dashboard to see stats, filter/search tracks, retry individual failures
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
7. Once running, open `http://<your-truenas-ip>:8686`, click **Scan library**,
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
- `POST /api/scan` — rescan the library for new files
- `POST /api/fetch` — attempt to fetch lyrics for anything not yet found
- `POST /api/scan-and-fetch` — do both in one call
- `GET /api/tracks?status=not_found` — list tracks by status
- `POST /api/tracks/{id}/retry` — retry a single track

## Notes / limitations (things to extend if you want to go further)

- Only LRCLIB is used as a source right now. It has good coverage but isn't
  exhaustive — you could add a second provider (e.g. Musixmatch, NetEase) as a
  fallback for the `not_found` bucket.
- No auth on the web UI — fine on an internal home network, but put it behind
  your reverse proxy / VPN like you would Sonarr or Radarr if exposing it further.
- Tag reading assumes reasonably well-tagged files. Poorly tagged tracks (missing
  artist/title) will get worse match rates from LRCLIB — same limitation LRCGET has.
- There's no per-user auth, HTTPS, or rate-limit handling for LRCLIB — it's a
  small free API, so it's polite to not hammer it on very large libraries; the
  scheduled job naturally spreads load out over time as your library grows.
