import logging
import requests

from app import matching

logger = logging.getLogger("lyricdarr.netease")

SEARCH_URL = "https://music.163.com/api/search/get"
LYRIC_URL = "https://music.163.com/api/song/lyric"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}
TIMEOUT = 15

# NetEase marks pure-instrumental tracks with this line instead of real lyrics.
INSTRUMENTAL_MARKER = "纯音乐，请欣赏"


def search_lyrics(artist: str, title: str, duration: int = None):
    """Search NetEase Cloud Music for a track and fetch its lyrics.

    This is an unofficial endpoint (the one NetEase's own web player calls) -
    no API key needed, but it's not a documented public API. Used as a
    fallback when LRCLIB has no match. Since this is a fuzzy text search
    against a primarily Chinese-language catalogue, the top hit can easily be
    a same-titled track by a completely different (often Chinese) artist -
    candidates whose artist doesn't loosely match the local tag are skipped,
    as are candidates whose title carries different version qualifiers (e.g.
    local track is "(Acoustic)" but the candidate isn't), and when duration
    is known, candidates whose reported length doesn't match are skipped
    too. As a last check, if the local artist/title have no CJK characters
    but the fetched lyrics do, that's also treated as a mismatch and
    rejected. Returns a dict with 'synced' (str or None), 'plain' (str or
    None), 'instrumental' (bool), or None if nothing usable was found.
    """
    query = f"{title} {artist}".strip() if artist else (title or "")
    if not query:
        return None

    try:
        resp = requests.post(
            SEARCH_URL,
            data={"s": query, "type": 1, "limit": 5, "offset": 0},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        songs = resp.json().get("result", {}).get("songs", [])
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"NetEase search failed for {artist} - {title}: {e}")
        return None

    if not songs:
        return None

    songs = [
        s for s in songs
        if matching.artist_matches(artist, [a.get("name") for a in (s.get("artists") or [])])
    ]
    if not songs:
        return None

    wanted_tags = matching.version_tags(title)
    songs = [s for s in songs if matching.version_tags(s.get("name")) == wanted_tags]
    if not songs:
        return None

    if duration:
        songs = [
            s for s in songs
            if s.get("duration") is not None
            and matching.duration_matches(round(s["duration"] / 1000), duration)
        ]
        if not songs:
            return None

    song_id = songs[0].get("id")
    if not song_id:
        return None

    try:
        resp = requests.get(
            LYRIC_URL,
            params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"NetEase lyric fetch failed for {artist} - {title} (id={song_id}): {e}")
        return None

    lrc = (data.get("lrc") or {}).get("lyric")
    if not lrc:
        return None

    if INSTRUMENTAL_MARKER in lrc:
        return {"synced": None, "plain": None, "instrumental": True}

    if matching.has_cjk(lrc) and not matching.has_cjk(artist) and not matching.has_cjk(title):
        logger.info(f"NetEase result for {artist} - {title} looked like a wrong-song CJK match, skipping")
        return None

    return {"synced": lrc, "plain": None, "instrumental": False}
