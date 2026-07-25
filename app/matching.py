import re

DURATION_TOLERANCE = 3  # seconds; fuzzy-search candidates outside this are a different recording

# Words that mark a track as a distinct recording of the "same" song. A fuzzy
# text search for "Song Title" will happily return "Song Title (Acoustic)" or
# vice versa - same title match, completely different timing. Two titles are
# only considered the same recording if they carry the same subset of these.
VERSION_KEYWORDS = (
    "acoustic", "live", "remix", "demo", "instrumental", "extended",
    "unplugged", "session", "sessions", "orchestral", "piano", "cover",
    "reprise", "edit", "mix", "version", "remaster", "remastered",
)
_VERSION_RE = re.compile(r"\b(" + "|".join(VERSION_KEYWORDS) + r")\b", re.IGNORECASE)


def version_tags(text: str) -> frozenset:
    return frozenset(m.lower() for m in _VERSION_RE.findall(text or ""))


def duration_matches(candidate_seconds, target_seconds) -> bool:
    if candidate_seconds is None or target_seconds is None:
        return False
    return abs(candidate_seconds - target_seconds) <= DURATION_TOLERANCE
