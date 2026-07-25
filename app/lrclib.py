import logging
import requests

logger = logging.getLogger("lyricdarr.lrclib")

BASE_URL = "https://lrclib.net/api"
HEADERS = {"User-Agent": "Lyricdarr/0.1.0 (self-hosted lyrics fetcher)"}
TIMEOUT = 15


def get_lyrics(artist: str, title: str, album: str = None, duration: int = None):
    """Query LRCLIB's /get endpoint for an exact match.

    Returns dict with 'synced' (str or None), 'plain' (str or None), 'instrumental' (bool)
    or None if no match found at all.
    """
    params = {"artist_name": artist or "", "track_name": title or ""}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration

    try:
        resp = requests.get(f"{BASE_URL}/get", params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"LRCLIB request failed for {artist} - {title}: {e}")
        return None

    if resp.status_code == 200:
        data = resp.json()
        return {
            "synced": data.get("syncedLyrics") or None,
            "plain": data.get("plainLyrics") or None,
            "instrumental": bool(data.get("instrumental")),
        }

    if resp.status_code == 404:
        return None

    logger.warning(f"LRCLIB returned {resp.status_code} for {artist} - {title}")
    return None


DURATION_TOLERANCE = 3  # seconds; fuzzy-search candidates outside this are a different recording


def search_lyrics(artist: str, title: str, duration: int = None):
    """Fallback fuzzy search via /search endpoint when /get finds no exact match.

    Since this is a text search rather than an exact lookup, results can include
    covers, live versions, or other recordings of the "same" song with different
    timing. When duration is known, candidates whose reported duration doesn't
    match are rejected so we don't attach lyrics timed to a different recording.

    Returns the best candidate dict (same shape as get_lyrics) or None.
    """
    params = {"artist_name": artist or "", "track_name": title or ""}
    try:
        resp = requests.get(f"{BASE_URL}/search", params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"LRCLIB search failed for {artist} - {title}: {e}")
        return None

    if resp.status_code != 200:
        return None

    results = resp.json()
    if not results:
        return None

    if duration:
        results = [
            r for r in results
            if r.get("duration") is not None
            and abs(r["duration"] - duration) <= DURATION_TOLERANCE
        ]
        if not results:
            return None

    # Prefer the first result that has synced lyrics
    for r in results:
        if r.get("syncedLyrics"):
            return {
                "synced": r.get("syncedLyrics"),
                "plain": r.get("plainLyrics") or None,
                "instrumental": bool(r.get("instrumental")),
            }

    # Otherwise fall back to the first result (may only have plain lyrics)
    r = results[0]
    return {
        "synced": r.get("syncedLyrics") or None,
        "plain": r.get("plainLyrics") or None,
        "instrumental": bool(r.get("instrumental")),
    }
