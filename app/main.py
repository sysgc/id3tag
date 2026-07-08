"""The id3tag web server: HTTP API + static file serving, built on FastAPI.

If you're new to Python web servers: this file defines a FastAPI `app`
object, and each function decorated with `@app.get(...)` or
`@app.post(...)` becomes an HTTP endpoint the browser-side JavaScript
(`static/app.js`) calls. FastAPI automatically turns Python type hints into
request validation and turns returned dicts/lists into JSON responses — you
won't see any manual JSON encoding in this file.

The moving pieces, and where to look for each:

- **Reading/writing actual tag data in files** — `tagging.py`.
- **Searching iTunes/AcoustID for candidate matches** — `matchers.py`
  (MusicBrainz was removed — see that module's docstring for why).
- **Everything in this file** — gluing those two together into an HTTP
  API, plus keeping track of what files exist in the library (the `FILES`
  dict below) and where they live in the artist/album folder structure.

There is no real database here — `FILES` is a plain Python dict kept in
memory while the server runs, refreshed by walking the music directory
(see `scan_library`). It's also written to a JSON file on disk
(`SCAN_CACHE_PATH`) purely so a server restart doesn't have to re-read
every single file's tags from scratch; that's not a "real" database
either, just a cache.
"""
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import matchers
import tagging

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
"""Root folder of the user's music library. In the Docker setup this is a
bind-mount into the container (see `docker-compose.yml`'s `MUSIC_DIR`
environment variable) — from the app's point of view it's just a normal
folder on disk, expected to look like `MUSIC_DIR/Artist/Album/track.ext`."""

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
"""Folder for this app's own persistent data (currently just the scan
cache) — kept separate from `MUSIC_DIR` so we never accidentally treat our
own bookkeeping files as music. Bind-mounted from the host (see
`docker-compose.yml`'s `APP_DATA_DIR`), so it survives container
restarts/rebuilds even though it's not the user's music folder.

This app intentionally does **not** keep backup copies of your audio
files before editing them — with a large library that would mean
duplicating your entire collection's worth of disk space just to guard
against a mistake. Instead, tag writes are additive/non-destructive by
design (see `tagging.write_tags` — a field with no new value is left
alone, never blanked), and the file being renamed/retagged is still the
same file, still on the same disk, still yours to inspect with any other
tool. If you want a safety net, keep your own backup of `MUSIC_DIR`
however you already back up the rest of your files."""

SCAN_CACHE_PATH = DATA_DIR / "library_scan.json"
"""Where the results of the last library scan are saved as JSON, so the
next time the server starts (or a browser tab loads the page) it doesn't
have to re-walk the whole music folder and re-read every file's tags —
see `_save_cache` / `_load_cache`."""

ALLOWED_EXT = {".mp3", ".flac", ".m4a"}
"""File extensions this app knows how to read/write tags for (must match
what `tagging.py` supports). Anything else found while scanning the music
folder — cover art images, `.nfo` files, playlists, etc. — is silently
skipped."""

ID_NAMESPACE = uuid.NAMESPACE_URL
"""A fixed "namespace" UUID used to derive stable per-file IDs — see
`scan_library` for why this matters (it's what lets a re-scan recognize
"this is the same file as before" instead of creating a duplicate entry)."""

NO_ALBUM = "(no album folder)"
"""Placeholder album name used for files that aren't inside an album
subfolder (e.g. sitting directly in `MUSIC_DIR`, or one level under it
with no separate album folder). Shown in the UI, and also used as part of
the URL path when the browser asks for that "album"'s matches — see
`_files_in_album`."""

NO_ARTIST = "Unknown Artist"
"""Placeholder artist name used for files with no artist-level subfolder,
same idea as `NO_ALBUM`."""

app = FastAPI(title="id3tag")
"""The FastAPI application object. Every `@app.get(...)`/`@app.post(...)`
function below registers one HTTP endpoint on this object; at the very
bottom of the file, `app.mount(...)` also attaches the browser UI's static
files (HTML/CSS/JS) and this project's generated documentation."""

# in-memory registry of library files, keyed by a stable id derived from
# their path relative to MUSIC_DIR (so re-scans upsert instead of duplicate)
FILES: dict[str, dict] = {}
"""The in-memory "database" of every audio file the last scan found.
Keyed by a stable file ID (see `scan_library`); each value is a dict with
keys like `id`, `filename`, `relpath`, `path`, `ext`, `tags`, `status`,
`artist_hint`, `album_hint` (and, once a match has been applied,
`applied`). This is rebuilt from disk by `scan_library()` and is what
every other endpoint in this file reads from/writes to — there's no
separate database engine involved."""

LAST_SCAN_AT: str | None = None
"""ISO-8601 timestamp string of when `scan_library()` last actually ran
(as opposed to when the cache was merely loaded from disk), or `None` if
no scan has happened yet this run. Shown in the UI so the user can tell
whether what they're looking at might be stale — see `scan_status`."""


class ApplyRequest(BaseModel):
    """Request body shape for the "apply a match" endpoints.

    FastAPI uses this Pydantic model to automatically validate incoming
    JSON and convert it into a Python object — if the browser sends a
    request body that isn't `{"candidate": {...}}`, FastAPI rejects it
    with a validation error before our code even runs.

    Attributes:
        candidate: The chosen match, as one of the candidate dicts
            originally returned by `matchers.find_candidates` or
            `matchers.find_album_candidates` (whatever the user clicked
            "Approve" on in the UI).
    """
    candidate: dict


class AlbumEditRequest(BaseModel):
    """Request body shape for manually editing an album's artist/album name.

    Attributes:
        artist: New artist name to write to every file in the album, or
            `None`/omitted to leave the artist tag untouched.
        album: New album name to write to every file in the album, or
            `None`/omitted to leave the album tag untouched.
    """
    artist: str | None = None
    album: str | None = None


class RenameRequest(BaseModel):
    """Request body shape for manually renaming a single file.

    Attributes:
        filename: The desired filename, e.g. `"03 - Yesterday.mp3"`. Any
            extension the caller includes is ignored — see
            `_sanitize_manual_filename` — the file's actual extension
            (`record["ext"]`) is always preserved unchanged.
    """
    filename: str


def _hints_from_path(rel_parts: tuple) -> tuple[str | None, str | None]:
    """Derive (artist, album) hints from a file's path relative to MUSIC_DIR.

    Expects the common `Artist/Album/track.ext` layout (`rel_parts` has 3+
    segments): first segment is the artist, the segment right before the
    filename is the album. Falls back to treating a single enclosing
    folder as the album (old flat `Album/track.ext` layout), or no hint at
    all if the file sits directly in `MUSIC_DIR`.

    Args:
        rel_parts: The file's path relative to `MUSIC_DIR`, already split
            into path components by `pathlib` — e.g. for
            `MUSIC_DIR/Beatles/Abbey Road/01 Come Together.mp3`, this
            would be `("Beatles", "Abbey Road", "01 Come Together.mp3")`.

    Returns:
        A `(artist_hint, album_hint)` tuple. Either or both may be `None`
        depending on how deep the file is nested. Non-`None` values have
        already been passed through `matchers.clean_album_hint` to tidy
        up common filesystem-safe-name substitutions.
    """
    n = len(rel_parts)
    if n >= 3:
        return matchers.clean_album_hint(rel_parts[0]), matchers.clean_album_hint(rel_parts[-2])
    if n == 2:
        return None, matchers.clean_album_hint(rel_parts[0])
    return None, None


def scan_library() -> list[dict]:
    """Walk `MUSIC_DIR` on disk and rebuild the in-memory `FILES` registry.

    This is the only function that actually reads the filesystem
    structure and every file's tags from scratch — it's relatively slow
    (one tag-read per file) which is exactly why the app tries to avoid
    calling it more than necessary (see `SCAN_CACHE_PATH` and
    `list_files`).

    For each supported audio file found, this either creates a new entry
    in `FILES` or updates an existing one *in place* — existing entries
    keep their `status` (e.g. `"tagged"`) and any previously-fetched
    `candidates` list, since re-scanning shouldn't throw away work the
    user already did, it should just refresh what's actually on disk.
    Files that no longer exist on disk are removed from `FILES`.

    Side effects:
        - Mutates the module-level `FILES` dict.
        - Mutates the module-level `LAST_SCAN_AT` timestamp.
        - Writes `SCAN_CACHE_PATH` (see `_save_cache`) so this scan's
          results survive a server restart.

    Returns:
        The updated `FILES` entries as a list, sorted by `relpath` (i.e.
        alphabetically by folder then filename).
    """
    found_ids = set()
    for root, _dirs, filenames in os.walk(MUSIC_DIR):
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXT:
                continue
            full_path = Path(root) / name
            rel_parts = full_path.relative_to(MUSIC_DIR).parts
            relpath = str(Path(*rel_parts))
            # A UUID derived deterministically from the file's relative
            # path: the same file always gets the same ID across scans
            # (uuid5 is a hash, not random), so re-scanning updates the
            # existing FILES entry instead of creating a duplicate one.
            file_id = str(uuid.uuid5(ID_NAMESPACE, relpath))
            found_ids.add(file_id)

            artist_hint, album_hint = _hints_from_path(rel_parts)

            tags = tagging.read_tags(str(full_path))
            existing = FILES.get(file_id, {})
            FILES[file_id] = {
                "id": file_id,
                "filename": name,
                "relpath": relpath,
                "path": str(full_path),
                "ext": ext,
                "tags": tags,
                "candidates": existing.get("candidates", []),
                "status": existing.get("status", "scanned"),
                "artist_hint": artist_hint,
                "album_hint": album_hint,
            }

    # Anything that was in FILES before this scan but wasn't seen this
    # time around has been deleted/moved out of MUSIC_DIR — drop it.
    for stale_id in set(FILES) - found_ids:
        del FILES[stale_id]

    global LAST_SCAN_AT
    LAST_SCAN_AT = datetime.now(timezone.utc).isoformat()
    _save_cache()
    return sorted(FILES.values(), key=lambda r: r["relpath"])


def _save_cache() -> None:
    """Write the current `FILES` registry (plus scan timestamp) to disk as JSON.

    This is what makes `SCAN_CACHE_PATH` useful across server restarts —
    every dict value in `FILES` is already plain JSON-serializable data
    (strings, numbers, nested dicts/lists), so this is a straightforward
    dump, no custom serialization needed.

    Side effects:
        Creates/overwrites the file at `SCAN_CACHE_PATH`.
    """
    SCAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_CACHE_PATH, "w") as f:
        json.dump({"scanned_at": LAST_SCAN_AT, "files": FILES}, f)


def _load_cache() -> bool:
    """Load a previously-saved scan result from disk into `FILES`, if one exists.

    This avoids re-walking the whole music folder (and re-reading every
    file's tags) on every server start or page load — see `list_files`,
    which only falls back to a real `scan_library()` if there's no cache
    to load yet.

    Side effects:
        Mutates the module-level `FILES` dict and `LAST_SCAN_AT` if a
        cache file was found.

    Returns:
        `True` if a cache file existed and was loaded, `False` if there
        was nothing to load (e.g. very first run, before any scan has
        ever completed).
    """
    if not SCAN_CACHE_PATH.exists():
        return False
    with open(SCAN_CACHE_PATH) as f:
        data = json.load(f)
    global LAST_SCAN_AT
    LAST_SCAN_AT = data.get("scanned_at")
    FILES.update(data.get("files", {}))
    return True


@app.post("/api/scan")
def scan():
    """API endpoint: force a fresh library scan.

    Bound to the UI's "Scan library" button. Unlike `list_files`, this
    *always* re-walks the disk — it's the explicit "I added/removed files,
    please check again" action, as opposed to the normal page-load path
    which prefers the cache for speed.

    Returns:
        A list of every file's public info (see `_public_view`), freshly
        read from disk.
    """
    return [_public_view(r) for r in scan_library()]


@app.get("/api/files")
def list_files():
    """API endpoint: list every audio file currently known to the app.

    Called when the web page first loads. Prefers whatever's already in
    memory or in the on-disk cache over doing a full disk walk, so opening
    the page is fast even for a large library — only does a real
    `scan_library()` if there's truly nothing to load yet (very first run).

    Returns:
        A list of every file's public info (see `_public_view`), sorted
        by `relpath`.
    """
    if not FILES and not _load_cache():
        scan_library()
    return [_public_view(v) for v in sorted(FILES.values(), key=lambda r: r["relpath"])]


@app.get("/api/scan-status")
def scan_status():
    """API endpoint: report when the library was last scanned, and how big it is.

    Purely informational — powers the "N file(s) · last scanned: ..." text
    next to the "Scan library" button, so the user can tell if the view
    might be stale.

    Returns:
        A dict with `scanned_at` (ISO-8601 string, or `None` if never
        scanned) and `file_count` (how many files are currently tracked).
    """
    return {"scanned_at": LAST_SCAN_AT, "file_count": len(FILES)}


@app.get("/api/files/{file_id}/matches")
def get_matches(file_id: str):
    """API endpoint: search for candidate matches for one specific song.

    Args:
        file_id: The file's stable ID (see `scan_library`), taken from
            the URL path, e.g. `GET /api/files/<file_id>/matches`.

    Returns:
        A list of candidate dicts from `matchers.find_candidates`.

    Raises:
        HTTPException: 404 if `file_id` isn't a known file (see `_get`).
    """
    record = _get(file_id)
    candidates = matchers.find_candidates(record["path"], record["tags"], record.get("album_hint"))
    record["candidates"] = candidates
    return candidates


@app.post("/api/files/{file_id}/apply")
def apply_match(file_id: str, req: ApplyRequest):
    """API endpoint: approve a match for one specific song and rewrite its tags.

    Doubles as the endpoint behind the UI's manual "edit song metadata"
    inline form (the pencil icon on a song row) — there's nothing
    search-specific about this endpoint, `req.candidate` just needs to be
    a dict with whichever of `title`/`artist`/`album`/`date`/`track` the
    caller wants written; a manual edit is exactly as valid a "candidate"
    as one that came from `matchers.find_candidates`. Either way, the file
    is renamed to match if `_build_filename` has enough information
    (`track` and `title`) to do so.

    Args:
        file_id: The file's stable ID, from the URL path.
        req: The chosen candidate, as a JSON request body (see
            `ApplyRequest`).

    Returns:
        The file's updated public info (see `_public_view`), including
        its freshly-re-read tags so the UI can immediately show what
        actually ended up in the file.

    Raises:
        HTTPException: 404 if `file_id` isn't a known file.
    """
    record = _get(file_id)
    return _apply_to_record(record, req.candidate)


@app.post("/api/files/{file_id}/rename")
def rename_file(file_id: str, req: RenameRequest):
    """API endpoint: manually rename one file, independent of any tag match.

    For the UI's "edit filename" inline form (pencil icon next to a song's
    filename) — lets the user set a filename directly rather than only
    ever getting one from `_build_filename`'s `"<track> - <title>
    (qualifier)"` scheme. Useful for old/rare files with no metadata
    service match, where the user still knows what the file should be
    called.

    Args:
        file_id: The file's stable ID, from the URL path.
        req: The desired filename (see `RenameRequest`).

    Returns:
        The file's updated public info (see `_public_view`).

    Raises:
        HTTPException: 404 if `file_id` isn't a known file, 400 if the
            given filename is empty/unusable after sanitizing, 409 if
            another file already has that name.
    """
    record = _get(file_id)
    new_name = _sanitize_manual_filename(req.filename, record["ext"])
    if not new_name:
        raise HTTPException(400, "filename is empty or has no usable characters")

    old_path = Path(record["path"])
    if new_name == record["filename"]:
        return _public_view(record)

    new_path = old_path.parent / new_name
    if new_path.exists():
        raise HTTPException(409, "a file with that name already exists")

    old_path.rename(new_path)

    old_relpath = Path(record["relpath"])
    new_relpath = new_name if old_relpath.parent == Path(".") else str(old_relpath.parent / new_name)
    record["path"] = str(new_path)
    record["relpath"] = new_relpath
    record["filename"] = new_name
    return _public_view(record)


@app.get("/api/albums/{artist}/{album}/matches")
def get_album_matches(
    artist: str, album: str,
    q_artist: str | None = None, q_album: str | None = None, q_year: str | None = None,
):
    """API endpoint: search for candidate album (release) matches.

    Args:
        artist: The folder-derived artist name for this album (or
            `NO_ARTIST` if there's no artist subfolder) — from the URL
            path. This identifies *which files* this request is about; it
            does not change based on what's typed into the search box.
        album: The folder-derived album name (or `NO_ALBUM`) — from the
            URL path. Same idea as `artist`: identifies the file set.
        q_artist: Optional query-string override for the *search* artist
            text (e.g. `?q_artist=The%20Beatles`) — lets the UI's editable
            search form try different search terms without changing which
            files the album maps to. If omitted, falls back to the
            folder-derived artist, or (if there isn't one) the most common
            artist tag among the album's files (see `_most_common_artist`).
        q_album: Same idea as `q_artist`, for the album title.
        q_year: Optional release year to narrow the search (see
            `matchers.find_album_candidates`).

    Returns:
        A list of candidate dicts from `matchers.find_album_candidates`.

    Raises:
        HTTPException: 404 if no files match this artist/album pair.
    """
    file_ids = _files_in_album(artist, album)
    if not file_ids:
        raise HTTPException(404, "album not found")

    # q_artist/q_album/q_year let the UI override the search text (e.g. the
    # user fixing a folder-derived guess) without changing which files this
    # album's file set (and any later apply) resolves to.
    if q_artist is not None:
        artist_hint = q_artist or None
    else:
        artist_hint = (None if artist == NO_ARTIST else artist) or _most_common_artist(file_ids)

    album_name = q_album if q_album is not None else (None if album == NO_ALBUM else album)
    return matchers.find_album_candidates(album_name or "", artist_hint, q_year or None)


@app.post("/api/albums/{artist}/{album}/apply")
def apply_album_match(artist: str, album: str, req: ApplyRequest):
    """API endpoint: approve an album match and rewrite tags for every song in it.

    Unlike the single-song `apply_match`, this updates *every file that
    can be confirmed to belong to this album* in one request, applying the
    shared album-level fields (album title, artist, date, cover) to each.
    Each file keeps its own existing `title` tag untouched — there's no
    per-track tracklist to draw a corrected title from (that required a
    MusicBrainz release lookup, which this app no longer does; see
    `matchers.py`'s module docstring).

    Track numbers come from `matchers.find_track_number` when possible — a
    per-file, fuzzy-matched iTunes song search keyed on the file's own
    title plus the confirmed album/artist. iTunes' `entity=song` search
    returns a real `trackNumber` per song even though the `entity=album`
    search used to find this candidate doesn't. Two outcomes when that
    lookup can't confirm a track number:

    - The file has **no local `title`** to search by at all: it's
      **skipped entirely** — no tag write, no rename. There's nothing to
      confirm it against the release with and nothing to build a filename
      from, so this is also the one case that protects against a bonus
      track or stray non-album file sharing the folder getting mistagged.
    - The file **already has a local title** (the common case for a
      library that's been at least loosely tagged before): falls back to
      whatever track number is already on the file. A file the user can
      see already has a title and track filled in is, in practice, a file
      they already trust belongs here — refusing to write the shared
      album fields to it just because iTunes' own text doesn't
      byte-for-byte match a possibly non-English or annotated local title
      (`"(Version 1)"`, transliteration spelling, etc.) made the core
      "approve an album match" feature silently do almost nothing on
      real-world libraries. See `find_track_number`'s docstring for the
      fuzzy-matching thresholds that now also reduce how often this
      fallback is needed in the first place.

    This deliberately does *not* renumber sequentially by local folder
    position (removed — it overwrote real iTunes numbers with a file's
    position among whichever local files happened to exist, wrong whenever
    the folder is missing tracks). `_track_sort_key` is only used to order
    iteration/results, not to assign track numbers.

    Args:
        artist: Folder-derived artist name, from the URL path — identifies
            the file set (see `get_album_matches` for the same pattern).
        album: Folder-derived album name, from the URL path.
        req: The chosen album-level candidate (see `ApplyRequest`).

    Returns:
        A list of public file info dicts (see `_public_view`), one per
        song in the album. Files that got tagged (whether iTunes confirmed
        the track number or the local-title fallback applied — see above)
        have freshly-written tags and, if renamed, an updated `filename`;
        the one skipped case (no local title at all) is returned unchanged,
        exactly as it was before this request.

    Raises:
        HTTPException: 404 if no files match this artist/album pair.
    """
    file_ids = _files_in_album(artist, album)
    if not file_ids:
        raise HTTPException(404, "album not found")

    ordered = sorted(file_ids, key=lambda fid: _track_sort_key(FILES[fid]))
    results = []
    for fid in ordered:
        record = FILES[fid]
        title = record["tags"].get("title")
        track = matchers.find_track_number(title, req.candidate.get("artist"), req.candidate.get("title"))
        if track is None:
            if not title:
                # No local title to search by at all — nothing to confirm
                # this file against the release with, and nothing to build
                # a filename from either. Leave it completely untouched
                # rather than guess it belongs.
                results.append(_public_view(record))
                continue
            # Has a local title already, but iTunes' per-song search
            # couldn't confidently confirm it against this release (common
            # for non-English/transliterated titles, or a local
            # "(Version 1)"-style suffix iTunes' own listing doesn't have).
            # The file already carries a title and (usually) a track
            # number a human entered/trusted, so fall back to that rather
            # than refuse to tag a file the user can plainly see belongs
            # here — this is different from the "no title at all" case
            # above, where there's nothing to fall back to.
            track = record["tags"].get("track")
        candidate = {
            "title": title,
            "artist": req.candidate.get("artist"),
            "album": req.candidate.get("title"),
            "date": req.candidate.get("date"),
            "track": track,
            "cover_url": req.candidate.get("cover_url"),
        }
        results.append(_apply_to_record(record, candidate))
    return results


@app.post("/api/albums/{artist}/{album}/edit")
def edit_album_metadata(artist: str, album: str, req: AlbumEditRequest):
    """API endpoint: manually set an album's artist/album name on every file in it.

    For the UI's "edit album" inline form (pencil icon next to the album
    header) — a manual, human-typed correction, not a metadata-service
    match. Unlike `apply_album_match`, this writes to **every** file in
    the folder unconditionally, with no `matchers.find_track_number`
    belonging-check: that check exists to protect against an *automated*
    iTunes-driven bulk apply guessing wrong on a bonus/stray file, but here
    the user has explicitly told this app what the whole folder's
    artist/album is, which is exactly the kind of override the
    belonging-check would otherwise get in the way of (e.g. old/rare
    albums iTunes has no listing for at all, and so could never confirm
    any file against). No renaming happens here — filenames aren't derived
    from artist/album, only from `track`/`title` (see `_build_filename`),
    neither of which this endpoint touches.

    Args:
        artist: Folder-derived artist name, from the URL path — identifies
            the file set (see `_files_in_album`).
        album: Folder-derived album name, from the URL path.
        req: The new artist/album text to write (see `AlbumEditRequest`);
            either may be omitted to leave that field untouched.

    Returns:
        A list of updated public file info dicts (see `_public_view`), one
        per song in the album.

    Raises:
        HTTPException: 404 if no files match this artist/album pair, 400
            if neither `artist` nor `album` was given.
    """
    file_ids = _files_in_album(artist, album)
    if not file_ids:
        raise HTTPException(404, "album not found")

    fields = {k: v for k, v in {"artist": req.artist, "album": req.album}.items() if v}
    if not fields:
        raise HTTPException(400, "nothing to update")

    results = []
    for fid in file_ids:
        record = FILES[fid]
        tagging.write_tags(record["path"], fields)
        record["tags"] = tagging.read_tags(record["path"])
        record["status"] = "tagged"
        results.append(_public_view(record))
    return results


def _apply_to_record(record: dict, candidate: dict) -> dict:
    """Rewrite tags and rename to match — the shared core of both apply endpoints.

    This writes straight to the file at `record["path"]` — there is no
    backup copy made first (see the note on `DATA_DIR` for why). Tag
    writes only ever add/overwrite fields the candidate actually has a
    value for (see `tagging.write_tags`), never blank out existing ones.

    If the candidate has a `cover_url`, this also downloads that image
    (over plain HTTP(S), synchronously) so it can be embedded in the file.
    A failed image download doesn't stop the rest of the tag update — cover
    art is a nice-to-have, not worth blocking the whole operation over.

    After the tags are written, the file is also renamed to
    `"<track> - <title> (qualifier).<ext>"` (see `_build_filename`) so
    filenames stay in track order and match what's actually tagged — the
    file's *content* (its extension) is never changed, only its name.

    Args:
        record: One entry from the `FILES` dict (must have `path` and
            `relpath` keys).
        candidate: The chosen match dict — passed straight through to
            `tagging.write_tags`, so see that function for which keys it
            reads.

    Side effects:
        - Overwrites the audio file at `record["path"]` with new tags.
        - May rename the file on disk (see `_rename_to_match`).
        - Mutates `record` in place: `tags` is always refreshed from disk
          (even if `_verify_write` then raises — it reflects whatever
          actually ended up on disk, which is worth keeping either way),
          and if renamed, so are `path`/`relpath`/`filename`. `status` and
          `applied` are only set to `"tagged"`/the candidate if
          `_verify_write` doesn't raise.

    Returns:
        The updated record's public info (see `_public_view`).

    Raises:
        HTTPException: 500, via `_verify_write`, if a field this call
            asked to write still reads back empty afterward — see that
            function's docstring.
    """
    path = record["path"]

    cover_bytes = None
    cover_url = candidate.get("cover_url")
    if cover_url:
        try:
            r = requests.get(cover_url, timeout=10)
            if r.ok:
                cover_bytes = r.content
        except requests.RequestException:
            pass

    tagging.write_tags(path, candidate, cover_bytes)
    _rename_to_match(record, candidate)

    # Re-read the file from disk rather than trusting `candidate` as-is —
    # this is the "force a re-read" step that guarantees what the UI shows
    # next is what's actually in the file, not just what we asked to write.
    record["tags"] = tagging.read_tags(record["path"])
    _verify_write(record["path"], candidate, record["tags"])
    record["status"] = "tagged"
    record["applied"] = candidate
    return _public_view(record)


def _tag_value_matches(wanted, got) -> bool:
    """Loosely compare a requested tag value against what got read back.

    Not a byte-for-byte equality check on purpose: a genuinely successful
    write can still read back slightly differently formatted (a `track`
    of `"3"` written, then read back as `"3"` on mp3/flac but iTunes-style
    `"3/12"` was never sent so that's moot; an ID3 `TDRC` timestamp that
    mutagen normalizes). Case/whitespace-insensitive equality, plus a
    "one starts with the other" fallback, covers those benign cases while
    still catching the actual failure shape this exists to catch: the
    field reading back as the *old* value because the write silently did
    nothing (a plain non-empty check alone would miss that — an old,
    still-truthy value looks "written" too).

    Args:
        wanted: The value from `candidate` that was supposed to be written
            (may be `None`/falsy, meaning "nothing was requested here").
        got: The corresponding value from `tagging.read_tags` after the
            write.

    Returns:
        `True` if `wanted` is falsy (nothing to check) or `got` matches it
        closely enough to trust; `False` otherwise.
    """
    if not wanted:
        return True
    if not got:
        return False
    w, g = str(wanted).strip().casefold(), str(got).strip().casefold()
    return w == g or g.startswith(w) or w.startswith(g)


def _verify_write(path: str, candidate: dict, tags_after: dict) -> None:
    """Raise if a field we just asked to write doesn't actually show up on disk.

    `_apply_to_record` used to mark a file `"tagged"` purely because
    `tagging.write_tags` didn't raise and a re-read succeeded — but neither
    of those actually proves the new values landed on disk. Reported as:
    the UI says "tagged", but an external tool (Plex) never picked up the
    change. An earlier version of this check only confirmed the field
    wasn't *empty* after the write — which missed the exact failure shape
    that matters most: a field that already had an old value stays exactly
    that old value because the write silently no-opped, and "old truthy
    value" passes an empty-check just fine. This compares against what was
    actually requested instead (see `_tag_value_matches`).

    Args:
        path: The file path, for the error message only.
        candidate: The dict passed to `tagging.write_tags` — same keys
            (`title`, `artist`, `album`, `date`, `track`) it recognizes.
        tags_after: The dict from `tagging.read_tags`, read immediately
            after the write (and any rename) completed.

    Raises:
        HTTPException: 500, if any field `candidate` had a truthy value
            for doesn't closely match what's now on disk — surfaces as a
            real "Apply failed" error in the UI instead of a silent,
            incorrect "tagged" status.
    """
    mismatched = [k for k in ("title", "artist", "album", "date", "track")
                  if not _tag_value_matches(candidate.get(k), tags_after.get(k))]
    if mismatched:
        raise HTTPException(
            500,
            f"wrote {', '.join(mismatched)} to {path} but re-reading the file back doesn't "
            "show the new value — the write did not actually take effect on disk",
        )


_LEADING_TRACK_NUM_RE = re.compile(r"^\s*\d{1,3}[.\-)]?\s+")
"""Matches a leading track-number-like prefix on a title, e.g. `"03. "`,
`"3 - "`, `"03) "` — stripped defensively before building a filename so we
never end up with the track number appearing twice (see `_build_filename`).
Titles from iTunes essentially never have this, but it costs nothing to
guard against it."""

_TRAILING_PAREN_RE = re.compile(r"^(.*\S)\s*\(([^()]+)\)\s*$")
"""Matches `"<base title> (<parenthetical>)"`, splitting off a trailing
parenthetical qualifier — e.g. `"Yesterday (Remastered 2009)"` splits into
base title `"Yesterday"` and qualifier `"Remastered 2009"`. Used by
`_build_filename` to keep the qualifier — e.g. (Remastered), (Remix),
(Reprise), (Duet), (Mono), (Stereo), whatever the title actually says — in
its own place in the filename rather than jammed into the track name."""

_ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
"""Characters that are illegal (or awkward) in filenames on at least one
major OS (Windows disallows all of these; macOS/Linux only truly disallow
`/`, but the rest cause enough real-world grief — e.g. exFAT-formatted
external drives — that we strip them everywhere for safety)."""


def _sanitize_filename_part(s: str) -> str:
    """Make a string safe to use as part of a filename, on any common OS.

    Args:
        s: Raw text (a track title or qualifier) that might contain
            characters filenames can't have.

    Returns:
        `s` with illegal characters removed and whitespace collapsed/
        trimmed.
    """
    s = _ILLEGAL_FILENAME_CHARS_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _sanitize_manual_filename(raw: str, ext: str) -> str | None:
    """Turn user-typed filename text into a safe, extension-correct filename.

    Used by the `rename_file` endpoint (the UI's manual "edit filename"
    form) — unlike `_build_filename`, this doesn't assume any
    `"<track> - <title>"` structure, it just takes whatever the user typed
    for the name part.

    Args:
        raw: The filename text the user submitted, e.g. `"My Old Song"` or
            `"My Old Song.mp3"`.
        ext: The file's actual extension, including the dot (e.g.
            `".mp3"`) — always wins over whatever extension (if any)
            appears in `raw`, so a rename can never change a file's actual
            format, only what it's called. See `_rename_to_match` for the
            same rule applied to metadata-match-driven renames.

    Returns:
        A filename like `"My Old Song.mp3"`, or `None` if `raw` is
        empty or made entirely of characters that aren't legal in a
        filename (see `_sanitize_filename_part`).
    """
    raw = raw.strip()
    # Only strip a trailing extension if it's *exactly* this file's real
    # extension (case-insensitive) — using Path().suffix instead would
    # misfire on a name like "Mr. Smith" (Path treats ".Smith" as a
    # suffix), silently mangling a title that just happens to contain a
    # period.
    if raw.lower().endswith(ext.lower()):
        raw = raw[: -len(ext)]
    stem = _sanitize_filename_part(raw)
    if not stem:
        return None
    return f"{stem}{ext}"


def _build_filename(candidate: dict, ext: str) -> str | None:
    """Build the `"<track> - <title> (qualifier).<ext>"` filename for an applied match.

    Args:
        candidate: The match being applied — needs `track` (a track
            number, e.g. `"3"` or `"3/12"`) and `title` (the song name,
            optionally with a trailing parenthetical qualifier like
            `"(Remastered 2009)"`).
        ext: The file's original extension, including the dot (e.g.
            `".mp3"`) — always preserved unchanged, we only ever rename,
            never convert a file to a different format.

    Returns:
        A filename like `"03 - Yesterday (Remastered 2009).mp3"`, or
        `"03 - Yesterday.mp3"` if the title has no parenthetical
        qualifier. Returns `None` if there isn't enough information to
        build a sensible name (no track number, no title, or the title is
        empty after stripping illegal characters) — callers should leave
        the file's existing name alone in that case rather than rename it
        to something meaningless.
    """
    track_value = candidate.get("track")
    title = candidate.get("title")
    if not track_value or not title:
        return None

    track_match = re.match(r"^\s*(\d+)", str(track_value))
    if not track_match:
        return None
    track_num = f"{int(track_match.group(1)):02d}"  # zero-padded to 2 digits, e.g. "01", "12"

    title = _LEADING_TRACK_NUM_RE.sub("", title, count=1)
    qualifier_match = _TRAILING_PAREN_RE.match(title)
    if qualifier_match:
        base_title, qualifier = qualifier_match.group(1), qualifier_match.group(2)
    else:
        base_title, qualifier = title, None

    base_title = _sanitize_filename_part(base_title)
    if not base_title:
        return None

    name = f"{track_num} - {base_title}"
    if qualifier:
        qualifier = _sanitize_filename_part(qualifier)
        if qualifier:
            name += f" ({qualifier})"
    return f"{name}{ext}"


def _rename_to_match(record: dict, candidate: dict) -> None:
    """Rename a file on disk to match its newly-applied tags, if possible.

    Renames within the same folder only — this never moves a file to a
    different album/artist directory, it just changes the filename itself
    (see `_build_filename` for the naming scheme). If a file with the
    target name already exists (and isn't just this same file), the
    rename is skipped so we never silently overwrite an unrelated file;
    the tag write from `_apply_to_record` still applies either way.

    Args:
        record: One entry from the `FILES` dict. Mutated in place on
            success.
        candidate: The match just applied (see `_build_filename`).

    Side effects:
        On success, renames the file on disk and updates `record["path"]`,
        `record["relpath"]`, and `record["filename"]` to the new name.
        Does nothing if `_build_filename` can't construct a name, the new
        name is the same as the current one, or the target name is
        already taken by a different file.
    """
    new_name = _build_filename(candidate, record["ext"])
    if not new_name or new_name == record["filename"]:
        return

    old_path = Path(record["path"])
    new_path = old_path.parent / new_name
    if new_path.exists() and new_path != old_path:
        return  # name collision with an unrelated file — leave this one as-is

    old_path.rename(new_path)

    old_relpath = Path(record["relpath"])
    new_relpath = new_name if old_relpath.parent == Path(".") else str(old_relpath.parent / new_name)
    record["path"] = str(new_path)
    record["relpath"] = new_relpath
    record["filename"] = new_name


def _files_in_album(artist: str, album: str) -> list[str]:
    """Find every file ID belonging to a given folder-derived artist/album pair.

    Args:
        artist: Folder-derived artist name to match, or `NO_ARTIST`.
        album: Folder-derived album name to match, or `NO_ALBUM`.

    Returns:
        A list of file IDs (keys into `FILES`) whose `artist_hint`/
        `album_hint` match the given values (falling back to the
        `NO_ARTIST`/`NO_ALBUM` placeholders for files with no such hint).
    """
    return [
        fid for fid, r in FILES.items()
        if (r.get("artist_hint") or NO_ARTIST) == artist and (r.get("album_hint") or NO_ALBUM) == album
    ]


def _most_common_artist(file_ids: list[str]) -> str | None:
    """Guess an album's artist from its files' existing tags, by majority vote.

    Used as a fallback in `get_album_matches` when there's no
    artist-level subfolder to derive a hint from directly — if most of
    the files in an "album" already have an `artist` tag, that's a
    reasonable guess even without folder structure to lean on.

    Args:
        file_ids: List of file IDs (keys into `FILES`) to look at.

    Returns:
        The most common non-empty `artist` tag value among those files,
        or `None` if none of them have one set.
    """
    artists = [FILES[fid]["tags"].get("artist") for fid in file_ids if FILES[fid]["tags"].get("artist")]
    return Counter(artists).most_common(1)[0][0] if artists else None


_TRACK_NUM_RE = re.compile(r"^(\d+)")
"""Matches one or more digits at the very start of a string — used by
`_track_sort_key` to pull a leading track number out of values like
`"3"` or `"3/12"` (ignoring the "/12" part)."""


def _track_sort_key(record: dict):
    """Sort key for ordering an album's files into track order.

    Used when applying an album match, to assign each local file a track
    number/position (see `apply_album_match`).

    Args:
        record: One entry from the `FILES` dict.

    Returns:
        A tuple that sorts numbered tracks before un-numbered ones, and
        numbered tracks in numeric (not alphabetical) order:

        - `(0, track_number)` if the file's `track` tag starts with a
          number — e.g. `(0, 3)` for track `"3"` or `"3/12"`.
        - `(1, filename)` otherwise, so files with no track number sort
          after all numbered ones, alphabetically by filename among
          themselves.
    """
    track = record["tags"].get("track") or ""
    m = _TRACK_NUM_RE.match(track.strip())
    if m:
        return (0, int(m.group(1)))
    return (1, record["filename"])


def _get(file_id: str) -> dict:
    """Look up one file's record by ID, or raise a 404 if it doesn't exist.

    Args:
        file_id: The file's stable ID.

    Returns:
        The matching entry from `FILES`.

    Raises:
        HTTPException: 404, if `file_id` isn't a key in `FILES`.
    """
    record = FILES.get(file_id)
    if not record:
        raise HTTPException(404, "file not found")
    return record


def _public_view(record: dict) -> dict:
    """Strip internal-only fields from a `FILES` entry before sending it to the browser.

    Right now this just removes `path` — the *absolute server-side
    filesystem path* to the file. There's no need for (and no reason to
    expose) that server-local detail to the browser; `relpath` (a path
    relative to `MUSIC_DIR`) is what the UI actually needs and uses.

    Args:
        record: One entry from the `FILES` dict.

    Returns:
        A shallow copy of `record` with the `path` key removed.
    """
    return {k: v for k, v in record.items() if k != "path"}


# Serves this project's generated API/code reference (see the "docs" build
# step in the Dockerfile, which runs `pdoc` to turn every docstring above
# into browsable HTML) at /reference. Deliberately not called "/docs" —
# FastAPI already serves its own interactive API explorer there by default.
app.mount("/reference", StaticFiles(directory="docs", html=True), name="reference")

# Catch-all: serves the browser UI itself (index.html/app.js/style.css from
# the static/ folder). Must be mounted *last* — Starlette checks routes in
# the order they were registered, and this one matches almost any path, so
# anything mounted after it here would never be reached.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
