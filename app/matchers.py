"""Look up candidate matches for songs and albums from iTunes and AcoustID.

This module never touches a file on disk (that's `tagging.py`'s job) and
never talks to the web UI directly (that's `main.py`'s job). All it does is:
take some text (a song title, an artist, an album/folder name) or an audio
file (for fingerprinting), send requests to external metadata services, and
hand back a list of "candidate" dicts — possible matches, ranked by how
confident we are in each one.

External services used, and why each one exists:

- **iTunes Search API** (https://itunes.apple.com) — Apple's public,
  no-API-key-required search. No rate limiting that matters at the scale
  this app operates at.
- **AcoustID** (https://acoustid.org) — an "audio fingerprinting" service:
  given a short digital fingerprint computed from the actual audio (via
  the `fpcalc` tool, wrapped by the `acoustid` Python package), it can
  identify a recording even when the file has no useful tags at all. Only
  used if the user has set an `ACOUSTID_API_KEY` (it's optional).

**MusicBrainz was removed.** An earlier version of this app also queried
`musicbrainz.org`'s web service directly (both text search and full
tracklist lookups). It was taken back out: MusicBrainz enforces a strict
1-request-per-second-per-IP rate limit on unauthenticated use, which a
real-sized library runs into constantly (a single "find matches" click for
an album could need 4+ MusicBrainz calls across fallback search tiers) —
results would silently come back empty from throttling, indistinguishable
from "no match found." iTunes has no such restriction and was already
returning good results, so MusicBrainz added cost (complexity, latency,
flakiness) without reliably adding matches. See README's "Why no
MusicBrainz" section and `CLAUDE.md` for the full reasoning — don't add
`musicbrainz.org` API calls back into this module without checking with
the user first, this was a deliberate removal.

One MusicBrainz-shaped thing *does* remain: `fingerprint_match` still
returns a `mb_recording_id` when AcoustID has one, because AcoustID's own
database is built on top of MusicBrainz recording IDs and hands us one for
free as part of its normal response — that's not an extra API call to
`musicbrainz.org`, just a piece of data AcoustID already gave us.

Every candidate dict returned by any function in this module (whatever the
source) shares a common shape so the rest of the app can treat them
interchangeably — see the docstring on `find_candidates` for the full list
of keys.
"""
import difflib
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

ITUNES_SEARCH = "https://itunes.apple.com/search"
"""Base URL for Apple's iTunes Search API. No API key needed."""

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "").strip()
"""AcoustID API key, read once at import time from the `ACOUSTID_API_KEY`
environment variable (see `docker-compose.yml` / `.env`). If this is an
empty string, `fingerprint_match()` just returns an empty list — audio
fingerprinting is an optional feature, not a hard requirement."""

# OS-safe folder names often swap illegal chars (: / * ? " < > | ') for "_" or "-",
# or drop them outright. Undo the common substitutions before using the name as a
# search term; the fuzzy comparison in album_similarity() handles the rest.
_HINT_SEPARATORS = re.compile(r"[_]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_album_hint(name: str) -> str:
    """Turn a raw folder name into a cleaner search term.

    Folder names are often not quite the same as the "real" album title —
    for example a filesystem might store `"Some_Album_Name"` because
    spaces or punctuation aren't allowed. This just replaces runs of
    underscores with a single space and collapses any repeated
    whitespace, so `"Some_Album__Name"` becomes `"Some Album Name"`.

    This is intentionally light-touch — it does *not* try to guess or
    restore punctuation that was stripped entirely (like an apostrophe or
    colon). That's instead handled by comparing text loosely rather than
    exactly; see `album_similarity`.

    Args:
        name: A raw folder name, e.g. `"Some_Album_Name"`.

    Returns:
        The cleaned-up name, e.g. `"Some Album Name"`.
    """
    name = _HINT_SEPARATORS.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


# Folder names commonly carry edition/remaster decoration ("Album (Deluxe
# Edition)", "Album [2011 Remaster]") that isn't part of the canonical release
# title iTunes indexes on — an exact search including it can come back empty
# or with a worse match. Used only as a search-query fallback, never for display.
_EDITION_SUFFIX_RE = re.compile(
    r"\s*[\(\[][^()\[\]]*\b(edition|remaster(ed)?|deluxe|bonus|anniversary|"
    r"expanded|explicit|mono|stereo|version)\b[^()\[\]]*[\)\]]\s*",
    re.IGNORECASE,
)


def strip_edition_suffix(name: str) -> str:
    """Remove edition/remaster decoration from an album name, for searching.

    Example: `"Abbey Road (Remastered 2009)"` becomes `"Abbey Road"`.
    This only matters as a *search query* fallback (see
    `find_album_candidates`) — we never use the stripped version for
    anything shown to the user, since the original folder name might be
    exactly right and we don't want to silently change what gets written
    to a file.

    Args:
        name: An album name that might have edition/remaster text in
            parentheses or brackets.

    Returns:
        The name with any such decoration removed, whitespace-normalized.
        If there's no decoration to strip, returns the name unchanged
        (aside from whitespace cleanup).
    """
    return re.sub(r"\s+", " ", _EDITION_SUFFIX_RE.sub(" ", name)).strip()


def normalize_for_match(s: str | None) -> str:
    """Reduce a string to lowercase alphanumeric words, for fuzzy comparison.

    All punctuation (colons, apostrophes, parentheses, ...) is replaced
    with spaces and the result is lowercased. This is the key trick that
    lets us match a folder name like `"Guns N Roses"` (apostrophe missing
    because the filesystem couldn't store it) against the real title
    `"Guns N' Roses"` — once you strip out everything that isn't a letter
    or digit, both become `"guns n roses"`.

    Args:
        s: Any string, or `None`.

    Returns:
        The normalized string (lowercase, punctuation replaced by single
        spaces, leading/trailing whitespace trimmed). Returns `""` if `s`
        is `None` or empty.
    """
    if not s:
        return ""
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def album_similarity(hint: str | None, candidate_album: str | None) -> float:
    """Score how similar two album names are, ignoring punctuation/case.

    Used to boost the confidence of search results whose album title
    closely matches the folder-derived name, even if they're not
    byte-for-byte identical (see `normalize_for_match` for why exact
    comparison would fail on OS-mangled folder names).

    Args:
        hint: The album name derived from the folder structure (may be
            `None` if there's no folder-derived hint available).
        candidate_album: The album title from a search result (may be
            `None`).

    Returns:
        A float from `0.0` (completely different) to `1.0` (identical
        after normalization), using Python's built-in
        `difflib.SequenceMatcher` "ratio" — a general string-similarity
        measure. Returns `0.0` if either input is missing/empty.
    """
    if not hint or not candidate_album:
        return 0.0
    a, b = normalize_for_match(hint), normalize_for_match(candidate_album)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def search_itunes(title: str, artist: str, album: str | None, limit=5) -> list[dict]:
    """Search the iTunes Search API for a single song, by text.

    Joins whichever of artist/title we have into one free-text `term` and
    lets Apple's search do its own fuzzy matching (iTunes has no
    structured field-by-field query syntax the way MusicBrainz did).

    Args:
        title: Song title to search for.
        artist: Artist name to search for.
        album: Not currently used in the iTunes query itself (iTunes'
            `entity=song` search doesn't support filtering by album), but
            kept in the function signature so callers can pass the same
            arguments to every search function uniformly.
        limit: Maximum number of results to fetch from iTunes.

    Returns:
        A list of candidate dicts, one per matching track, in the order
        iTunes returns them. Returns `[]` if `title` and `artist` are both
        empty, or if the request fails.
    """
    term = " ".join(p for p in (artist, title) if p)
    if not term:
        return []
    try:
        r = requests.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "song", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("iTunes song search failed: %s", e)
        return []

    candidates = []
    for item in r.json().get("results", []):
        artwork = item.get("artworkUrl100")
        if artwork:
            # iTunes' default artwork URL is a tiny 100x100 thumbnail;
            # swapping the size in the URL gets us a much bigger image
            # for free, no extra request needed.
            artwork = artwork.replace("100x100bb", "600x600bb")
        candidates.append({
            "source": "itunes",
            "confidence": 60,  # iTunes gives no relevance score of its own, so we use a flat mid-range default
            "title": item.get("trackName"),
            "artist": item.get("artistName"),
            "album": item.get("collectionName"),
            "date": (item.get("releaseDate") or "")[:10] or None,
            "track": item.get("trackNumber"),
            # genre is deliberately not looked up, matched, or written back
            "mb_recording_id": None,
            "mb_release_id": None,
            "mb_artist_id": None,
            "itunes_id": item.get("trackId"),
            "itunes_url": item.get("collectionViewUrl"),
            "cover_url": artwork,
            "raw": item,
        })
    return candidates


_TRACK_TITLE_MATCH_THRESHOLD = 0.72
"""Minimum `album_similarity`-style ratio between a file's local title and
an iTunes song search result's title for `find_track_info` to trust that
result. Loose exact-equality matching used to live here, but real-world
titles routinely differ from iTunes' own text in ways that are still
obviously "the same song" — a `"(Version 1)"`/`"(Remastered)"` suffix a
local tagger added, transliteration spelling variance on non-English
titles, curly vs straight apostrophes, etc. A fuzzy threshold catches those
while still rejecting an actually-different song."""

_TRACK_ALBUM_MATCH_THRESHOLD = 0.45
"""Same idea as `_TRACK_TITLE_MATCH_THRESHOLD`, but looser, for the album
name — album titles vary even more between a local tag and iTunes'
listing (edition/soundtrack-label suffixes, punctuation), and the title
match above is already doing most of the work of confirming this is the
right song; the album check here is just a sanity check against confusing
this song with a same-named track on a *different* release."""


def find_track_info(title: str | None, album_artist: str | None, album: str | None) -> dict | None:
    """Look up a song's real iTunes track number *and its own performing artist*.

    `search_release_itunes` (the album search) only gets a `trackCount`
    from iTunes' `entity=album` results, not a per-track listing — there's
    no MusicBrainz tracklist lookup anymore to draw individual track
    numbers from (see the module docstring). But iTunes' `entity=song`
    search (`search_itunes`) *does* return a `trackNumber` and an
    `artistName` per matching song, so a per-file song search can still
    recover both — this is what `main.py`'s `apply_album_match` calls to
    correct a local file's track number and set its *track* artist
    (the performer/singer), as distinct from the *album* artist (see
    `main.py` for why those two are different fields).

    This distinction matters a lot for compilations and soundtracks: an
    album's iTunes "artist" credit is often a composer or the billed
    headline artist (e.g. `"Ilaiyaraaja"` for a film score), while each
    individual song can have a completely different performer (e.g.
    `"S.P. Balasubrahmanyam & K.S. Chithra"`). Blindly writing the album's
    artist onto every track's `artist` tag overwrites correct,
    song-specific performer credits with an incorrect one — this function
    exists specifically so `apply_album_match` doesn't have to guess: it
    asks iTunes for *this song's own* artist credit, not the album's.

    Matching is fuzzy (see `_TRACK_TITLE_MATCH_THRESHOLD`/
    `_TRACK_ALBUM_MATCH_THRESHOLD`), not exact-equality — an earlier version
    required a byte-for-byte match (after `normalize_for_match`), which
    real-world tags routinely failed on (a `"(Version 1)"` suffix, a
    transliteration spelling difference on a non-English title, an edition
    suffix on the album) even when the song was unambiguously the right
    one, causing most of an album's files to get silently skipped by
    `apply_album_match`'s belonging-check instead of tagged.

    Args:
        title: The song's title (from its own local tag) to search for.
        album_artist: The confirmed album's artist, used only to steer the
            search query (see `search_itunes`) — never assumed to be this
            individual song's own artist.
        album: The confirmed album title, used to deprioritize results
            from a different release of the same song (e.g. a compilation)
            that would carry a different track number/artist.

    Returns:
        A `{"track": ..., "artist": ...}` dict from the best-title-matching
        iTunes result, or `None` if `title` is empty, the search fails, or
        no result's title (and, loosely, album) is a close enough fuzzy
        match to trust. `artist` here is that specific song's own
        `artistName` from iTunes — not necessarily the same as
        `album_artist`.
    """
    if not title:
        return None
    best, best_score = None, 0.0
    for r in search_itunes(title, album_artist, album, limit=5):
        title_score = album_similarity(title, r.get("title"))
        if title_score < _TRACK_TITLE_MATCH_THRESHOLD:
            continue
        if album and r.get("album"):
            album_score = album_similarity(strip_edition_suffix(album), strip_edition_suffix(r["album"]))
            if album_score < _TRACK_ALBUM_MATCH_THRESHOLD:
                continue
        if title_score > best_score:
            best, best_score = {"track": r.get("track"), "artist": r.get("artist")}, title_score
    return best


def fingerprint_match(filepath: str) -> list[dict]:
    """Identify a song from its audio content via AcoustID fingerprinting.

    This doesn't look at the file's tags at all — it computes an "audio
    fingerprint" (a compact summary of what the recording actually sounds
    like) and asks the AcoustID service to match it against its database.
    This means it can identify a file even if its tags are completely
    missing or wrong, unlike the text-based `search_itunes` above.

    AcoustID's own database cross-references MusicBrainz recordings, so a
    match includes a MusicBrainz recording ID (`mb_recording_id` below) at
    no extra cost — but that's the only MusicBrainz-derived data in this
    candidate; we don't make a separate call to `musicbrainz.org` to
    enrich it with release/date/cover info the way an earlier version of
    this app did (see the module docstring).

    This is an *optional* feature: if no `ACOUSTID_API_KEY` was configured
    (see the module-level `ACOUSTID_API_KEY` constant), this immediately
    returns an empty list rather than trying and failing.

    Args:
        filepath: Path to the local audio file to fingerprint.

    Returns:
        A list of candidate dicts, one per AcoustID match, sorted
        best-first by AcoustID's own match score (converted to our 0-100
        `confidence` scale). `album`, `date`, and `cover_url` are always
        `None` here (see above — no MusicBrainz enrichment call is made in
        *this* function) — `find_candidates` backfills them with a
        follow-up iTunes text search before returning candidates to the
        caller, so callers of `find_candidates` will usually see them
        filled in. Returns `[]` if fingerprinting isn't configured, or if
        it fails for any reason (e.g. the `fpcalc` binary isn't installed,
        or the file is silent/too short to fingerprint).
    """
    if not ACOUSTID_API_KEY:
        return []
    import acoustid
    candidates = []
    try:
        results = acoustid.match(ACOUSTID_API_KEY, filepath, parse=True)
    except acoustid.AcoustidError as e:
        logger.warning("AcoustID fingerprint match failed for %s: %s", filepath, e)
        return []
    for score, recording_id, title, artist in results:
        candidates.append({
            "source": "acoustid",
            "confidence": round(score * 100),  # score is AcoustID's own 0.0-1.0 confidence
            "title": title,
            "artist": artist,
            "album": None,
            "date": None,
            "track": None,
            "mb_recording_id": recording_id,
            "mb_release_id": None,
            "mb_artist_id": None,
            "cover_url": None,
            "itunes_url": None,
            "raw": {"acoustid_score": score, "recording_id": recording_id,
                    "title": title, "artist": artist},
        })
    return candidates


def search_release_itunes(album: str, artist: str | None = None, limit=5) -> list[dict]:
    """Search the iTunes Search API for an album, by text.

    Args:
        album: Album title to search for.
        artist: Optional artist name, joined into the same free-text
            search term (see `search_itunes` for why iTunes uses one
            combined term rather than separate fields).
        limit: Maximum number of results to fetch.

    Returns:
        A list of candidate dicts, one per matching album (`entity=album`
        in iTunes' API). Returns `[]` if both `album` and `artist` are
        empty, or if the request fails.
    """
    term = " ".join(p for p in (artist, album) if p)
    if not term:
        return []
    try:
        r = requests.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "album", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("iTunes album search failed: %s", e)
        return []

    candidates = []
    for item in r.json().get("results", []):
        artwork = item.get("artworkUrl100")
        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb")
        candidates.append({
            "source": "itunes",
            "confidence": 60,
            "title": item.get("collectionName"),
            "artist": item.get("artistName"),
            "date": (item.get("releaseDate") or "")[:10] or None,
            "track_count": item.get("trackCount"),
            "mb_release_id": None,
            "itunes_collection_id": item.get("collectionId"),
            "itunes_url": item.get("collectionViewUrl"),
            "cover_url": artwork,
            "raw": item,
        })
    return candidates


def find_album_candidates(album: str, artist_hint: str | None = None, year_hint: str | None = None) -> list[dict]:
    """Find candidate album matches from iTunes, trying progressively looser searches.

    This is the main entry point used for the UI's "Find matches (album)"
    button. A folder name is rarely byte-for-byte identical to the real
    album title (missing punctuation, an added "(Remastered)", a slightly
    different artist spelling, ...), so instead of a single search this
    tries a few increasingly loose variations, stopping as soon as one of
    them finds something:

    1. The album/artist exactly as given.
    2. Same, but with any edition/remaster suffix stripped
       (`strip_edition_suffix`) — only tried if that actually changes the
       album text.
    3. Album only, dropping the artist constraint entirely (handles cases
       where the folder-derived artist name doesn't match iTunes' own
       spelling of it).

    Args:
        album: The album title/name to search for (typically the
            cleaned-up folder name).
        artist_hint: Optional artist name to narrow the search.
        year_hint: Optional release year (e.g. `"1975"`) — not sent as a
            search parameter (iTunes' API doesn't support filtering by
            year), but used afterward to boost the confidence of any
            result whose returned release year matches it.

    Returns:
        A list of candidate dicts, sorted best-first by `confidence`.
        Each dict's `confidence` has been boosted based on:

        - `album_hint_similarity`: how closely the candidate's title
          matches `album` after normalizing away punctuation (see
          `album_similarity`) — added to `confidence` at 15 points per
          full point of similarity.
        - An extra +10 if `year_hint` was given and the candidate's
          release year matches it.

        Returns `[]` if nothing was found at any tier.
    """
    stripped = strip_edition_suffix(album) if album else album

    tiers = [(album, artist_hint)]
    if stripped and stripped != album:
        tiers.append((stripped, artist_hint))
    tiers.append((stripped or album, None))

    candidates = []
    for album_term, artist_term in tiers:
        candidates = search_release_itunes(album_term, artist_term)
        if candidates:
            break

    for c in candidates:
        sim = album_similarity(album, c.get("title"))
        c["album_hint_similarity"] = round(sim, 2)
        c["confidence"] = min(100, round(c["confidence"] + sim * 15))
        if year_hint and (c.get("date") or "")[:4] == year_hint[:4]:
            c["confidence"] = min(100, c["confidence"] + 10)
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def find_candidates(filepath: str, current_tags: dict, album_hint: str | None = None) -> list[dict]:
    """Find candidate song matches for one local file, combining all sources.

    This is the main entry point used for the UI's per-song "Find match"
    button. It runs both lookup strategies this module supports and merges
    their results into one ranked list:

    1. `fingerprint_match` — audio fingerprinting via AcoustID (only if
       configured). Any resulting candidate missing an `album` is
       backfilled with one more iTunes text search, keyed on the
       title/artist AcoustID identified (see the loop right after this
       call).
    2. `search_itunes` — text search using the file's existing tags.

    Args:
        filepath: Path to the local audio file (needed for fingerprinting;
            the text search only uses `current_tags`).
        current_tags: The file's current tags, as returned by
            `tagging.read_tags` — specifically `title`, `artist`, and
            `album` are used as search terms.
        album_hint: Optional album name derived from the folder structure
            (see `main.py`'s `_hints_from_path`), used as a fallback
            search term when the file's own `album` tag is empty, and to
            boost confidence of results whose album matches this hint.

    Returns:
        A list of candidate dicts from both sources combined, sorted
        best-first by `confidence`. If `album_hint` was given, each
        candidate's confidence is boosted based on how closely its album
        matches the hint (see `album_similarity`), same idea as in
        `find_album_candidates`.
    """
    album_for_query = current_tags.get("album") or album_hint
    candidates = fingerprint_match(filepath)

    # fingerprint_match never fills in album/date/cover_url/itunes_url (see
    # its docstring — no MusicBrainz release lookup). Back them in with a
    # follow-up iTunes text search keyed on the title/artist AcoustID
    # identified, not the file's own (possibly wrong) tags — this is the
    # case fingerprinting exists for: a file whose local tags can't be
    # trusted to search by. Approving an AcoustID candidate should still
    # be able to fix the album tag, not silently leave it untouched.
    for c in candidates:
        if c.get("album") or not (c.get("title") and c.get("artist")):
            continue
        enrichment = search_itunes(c["title"], c["artist"], None, limit=1)
        if enrichment:
            best = enrichment[0]
            c["album"] = best.get("album")
            c["date"] = best.get("date")
            c["cover_url"] = best.get("cover_url")
            c["itunes_url"] = best.get("itunes_url")

    candidates += search_itunes(current_tags.get("title"), current_tags.get("artist"), album_for_query)

    if album_hint:
        for c in candidates:
            sim = album_similarity(album_hint, c.get("album"))
            c["album_hint_similarity"] = round(sim, 2)
            c["confidence"] = min(100, round(c["confidence"] + sim * 15))

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates
