"""Read and write audio file tags (mp3, flac, m4a/alac, ogg, opus, wma, wav, aiff).

This module is the only place in the app that touches the actual tag data
inside an audio file. Everything else (matching against MusicBrainz/iTunes,
the web API, the UI) works with the plain Python `dict` that `read_tags()`
returns — it never has to know that mp3/wav/aiff use "ID3 frames", flac/ogg/
opus use "Vorbis comments", m4a uses "MP4 atoms", and wma uses ASF
attributes, each with its own field names.

Three format "families" share code here, grouped by how mutagen exposes
their tags rather than by file extension:

- **ID3-frame formats** (mp3, wav, aiff): `_mp3_frame`/`_all_mp3_tags` read
  any mutagen object with an ID3-compatible `.tags` (`getall`/`setall`,
  4-letter frame codes like `TIT2`). wav/aiff route through the shared
  `_read_id3_container`/`_write_id3_container` since they're otherwise
  identical to mp3's own `_read_mp3`/`_write_mp3` — mutagen's `WAVE`/`AIFF`
  classes just find the ID3 chunk inside the RIFF/IFF container for you.
- **Vorbis-comment formats** (flac, ogg, opus): simple lowercase
  `key -> [values]` dicts. flac keeps its own functions (native
  `Picture`/`add_picture` cover art support); ogg/opus share
  `_read_vorbis_comment`/`_write_vorbis_comment` and store cover art as a
  base64-encoded FLAC `Picture` block under the `metadata_block_picture`
  key — the same convention Picard/foobar2000 use, since neither Ogg
  container format has a native picture block of its own.
- **ASF format** (wma): its own `_read_wma`/`_write_wma` — attribute names
  (`Title`, `Author`, `WM/AlbumTitle`, ...) and cover art (a hand-packed
  `WM/Picture` byte blob, no mutagen helper class for it) don't resemble
  either family above.

m4a (AAC or ALAC — mutagen's tag-level API doesn't distinguish the two,
since ALAC-in-MP4 uses the same atom structure as AAC-in-MP4, just a
different codec fourcc the tag layer never touches) uses MP4 atoms,
handled by `_read_m4a`/`_write_m4a`.

We use the third-party library `mutagen` (https://mutagen.readthedocs.io/)
to do the actual byte-level reading/writing for each format. If you're new
to Python: `mutagen` is just an import like any other, installed via
`requirements.txt` and `pip install`.

Public functions:

- `read_tags(path)` — returns a dict of the current tags on a file.
- `write_tags(path, candidate, cover_bytes)` — writes new tags to a file.

Everything else in this module (names starting with `_`) is a private
helper used internally by the two functions above; you shouldn't need to
call them directly.
"""
import base64
import os
import struct
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC, TXXX, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.asf import ASF, ASFByteArrayAttribute

MB_TRACK_FRAME = "MusicBrainz Track Id"
"""Name of the custom ID3 "TXXX" frame we use to store a MusicBrainz recording ID on mp3/wav/aiff files."""

MB_ALBUM_FRAME = "MusicBrainz Album Id"
"""Name of the custom ID3 "TXXX" frame we use to store a MusicBrainz release (album) ID on mp3/wav/aiff files."""

MB_ARTIST_FRAME = "MusicBrainz Artist Id"
"""Name of the custom ID3 "TXXX" frame we use to store a MusicBrainz artist ID on mp3/wav/aiff files."""

MP4_MB_TRACK_ATOM = "----:com.apple.iTunes:MusicBrainz Track Id"
"""m4a/MP4 equivalent of `MB_TRACK_FRAME`. MP4 "freeform" atoms are always named
`----:<reverse-domain>:<name>`; iTunes/MusicBrainz-compatible tools recognize this exact name."""

MP4_MB_ALBUM_ATOM = "----:com.apple.iTunes:MusicBrainz Album Id"
"""m4a/MP4 equivalent of `MB_ALBUM_FRAME`."""

MP4_MB_ARTIST_ATOM = "----:com.apple.iTunes:MusicBrainz Artist Id"
"""m4a/MP4 equivalent of `MB_ARTIST_FRAME`."""

VORBIS_MB_TRACK_FIELD = "MUSICBRAINZ_TRACKID"
"""Vorbis-comment equivalent of `MB_TRACK_FRAME`, for ogg/opus (flac uses this same
literal field name directly in `_write_flac` — a de-facto standard other tools also use)."""

VORBIS_MB_ALBUM_FIELD = "MUSICBRAINZ_ALBUMID"
"""Vorbis-comment equivalent of `MB_ALBUM_FRAME`, for ogg/opus."""

VORBIS_MB_ARTIST_FIELD = "MUSICBRAINZ_ARTISTID"
"""Vorbis-comment equivalent of `MB_ARTIST_FRAME`, for ogg/opus."""

VORBIS_PICTURE_FIELD = "metadata_block_picture"
"""Vorbis-comment field ogg/opus cover art lives in — neither container format
has a native picture block the way flac does, so tools that support cover art
on them (this app, Picard, foobar2000, ...) all use this same convention: a
base64-encoded flac-style `Picture` block stored as a regular comment field."""

(WM_TITLE_ATTR, WM_ARTIST_ATTR, WM_ALBUM_ARTIST_ATTR, WM_ALBUM_ATTR,
 WM_YEAR_ATTR, WM_TRACK_ATTR, WM_GENRE_ATTR) = (
    "Title", "Author", "WM/AlbumArtist", "WM/AlbumTitle", "WM/Year", "WM/TrackNumber", "WM/Genre",
)
"""ASF (wma) attribute names for the fields this app understands. `Title`/`Author`
are ASF's own generic content-description attributes; the rest are Microsoft's
documented `WM/...` extended attributes, the same ones Windows Media Player/
Picard read and write."""

WM_PICTURE_ATTR = "WM/Picture"
"""ASF attribute cover art lives in. Unlike ID3's `APIC` or flac's `Picture`,
mutagen has no dedicated helper class for this one — it's a single binary
blob you pack/unpack by hand (see `_build_wm_picture`/`_parse_wm_picture`)."""

WM_MB_TRACK_ATTR = "MusicBrainz/Track Id"
"""ASF equivalent of `MB_TRACK_FRAME`. Not part of the official ASF spec, but
the attribute name Picard (and this app) uses — ASF allows arbitrary custom
attribute names, unlike, say, MP4's fixed atom set."""

WM_MB_ALBUM_ATTR = "MusicBrainz/Album Id"
"""ASF equivalent of `MB_ALBUM_FRAME`."""

WM_MB_ARTIST_ATTR = "MusicBrainz/Artist Id"
"""ASF equivalent of `MB_ARTIST_FRAME`."""


def read_tags(path: str) -> dict:
    """Read the current tags of an audio file into a plain dict.

    This looks at the file's extension (`.mp3`, `.flac`, `.m4a`, `.ogg`/
    `.oga`, `.opus`, `.wma`, `.wav`, or `.aiff`/`.aif`) and delegates to the
    matching format-specific reader below. That reader always returns a
    dict with the same set of keys, regardless of format, so the rest of
    the app never has to care which audio format it's dealing with.

    Args:
        path: Absolute filesystem path to the audio file, e.g.
            `"/music/Artist/Album/01 Track.mp3"`.

    Returns:
        A dict with these keys:

        - `title` (str | None): track title.
        - `artist` (str | None): track artist — the performer of *this
          specific song* (e.g. a featured singer), which on a compilation
          or soundtrack can differ from `album_artist` below.
        - `album_artist` (str | None): the album's own artist credit (e.g.
          a composer, or the headline artist a variety-of-performers
          release is filed under). Distinct from `artist` on purpose — see
          `main.py`'s `apply_album_match` for why conflating the two was a
          real bug.
        - `album` (str | None): album name.
        - `date` (str | None): release date/year, as stored in the file
          (format varies — could be `"2020"` or `"2020-05-01"`).
        - `track` (str | None): track number, as stored in the file
          (sometimes `"3"`, sometimes `"3/12"` for "track 3 of 12").
        - `genre` (str | None): genre, read for display only — this app
          never searches or writes genre (see `write_tags`).
        - `duration` (int): length of the audio in whole seconds.
        - `has_art` (bool): whether the file already has embedded cover art.
        - `all_tags` (dict): every other raw tag found in the file (see the
          format-specific `_all_*_tags` helpers), useful for showing the
          user "everything in this file" without us having to know every
          possible tag name in advance.

    Raises:
        ValueError: if the file extension isn't one this app supports.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return _read_mp3(path)
    if ext == ".flac":
        return _read_flac(path)
    if ext == ".m4a":
        return _read_m4a(path)
    if ext in (".ogg", ".oga"):
        return _read_vorbis_comment(path, OggVorbis)
    if ext == ".opus":
        return _read_vorbis_comment(path, OggOpus)
    if ext == ".wma":
        return _read_wma(path)
    if ext == ".wav":
        return _read_id3_container(path, WAVE)
    if ext in (".aiff", ".aif"):
        return _read_id3_container(path, AIFF)
    raise ValueError(f"unsupported file type: {ext}")


def _read_mp3(path: str) -> dict:
    """Read tags from an mp3 file using ID3 frames.

    mp3 files store tags as a list of "frames", each identified by a
    4-letter code (e.g. `TIT2` for title). A brand new/untagged mp3 may
    have no ID3 data at all, which mutagen signals by raising
    `ID3NoHeaderError` — we catch that and treat it the same as "no tags
    yet" rather than crashing.

    Args:
        path: Path to the `.mp3` file.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = None
    audio = MP3(path)
    tags = {
        "title": _mp3_frame(id3, "TIT2"),
        "artist": _mp3_frame(id3, "TPE1"),
        "album_artist": _mp3_frame(id3, "TPE2"),
        "album": _mp3_frame(id3, "TALB"),
        "date": _mp3_frame(id3, "TDRC"),
        "track": _mp3_frame(id3, "TRCK"),
        "genre": _mp3_frame(id3, "TCON"),
        "duration": round(audio.info.length),
        "has_art": bool(id3 and id3.getall("APIC")),
        "all_tags": _all_mp3_tags(id3),
    }
    return tags


def _mp3_frame(id3, frame_id):
    """Read a single ID3 frame's text value out of a mutagen `ID3` object.

    Args:
        id3: A mutagen `ID3` instance, or `None` if the file has no ID3
            tag at all (see `_read_mp3`).
        frame_id: The 4-letter ID3 frame code to look up, e.g. `"TIT2"`
            (title) or `"TPE1"` (artist).

    Returns:
        The frame's value as a plain string, or `None` if `id3` is `None`
        or the file doesn't have that particular frame.
    """
    if id3 is None:
        return None
    frames = id3.getall(frame_id)
    return str(frames[0]) if frames else None


BINARY_FRAME_IDS = ("APIC", "GEOB", "PIC")
"""ID3 frame codes that hold binary data (embedded pictures, general binary
objects) rather than text. `_all_mp3_tags` skips these — dumping raw image
bytes into the UI's "all tags" table wouldn't be useful and could be huge."""


def _all_mp3_tags(id3) -> dict:
    """Build a dict of *every* text tag in an mp3's ID3 data, for display.

    This powers the "all tags (raw)" panel in the UI — it's meant to show
    the user everything that's actually in the file, not just the
    handful of fields (`title`/`artist`/...) this app understands.

    Args:
        id3: A mutagen `ID3` instance, or `None` if the file has no tags.

    Returns:
        A dict mapping each frame's key (e.g. `"TIT2"`, or
        `"TXXX:MusicBrainz Track Id"` for custom frames) to its text
        value. Binary frames (see `BINARY_FRAME_IDS`) are left out.
    """
    if id3 is None:
        return {}
    result = {}
    for key, frame in id3.items():
        frame_id = getattr(frame, "FrameID", key.split(":")[0])
        if frame_id in BINARY_FRAME_IDS:
            continue
        try:
            result[key] = str(frame)
        except Exception:
            continue
    return result


def _read_flac(path: str) -> dict:
    """Read tags from a flac file using Vorbis comments.

    Unlike mp3's ID3 frames, flac tags ("Vorbis comments") are simple
    `key -> [list of string values]` pairs with lowercase field names
    like `"title"`, `"artist"`, `"tracknumber"`.

    Args:
        path: Path to the `.flac` file.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    audio = FLAC(path)

    def first(key):
        v = audio.get(key)
        return v[0] if v else None

    return {
        "title": first("title"),
        "artist": first("artist"),
        "album_artist": first("albumartist"),
        "album": first("album"),
        "date": first("date"),
        "track": first("tracknumber"),
        "genre": first("genre"),
        "duration": round(audio.info.length),
        "has_art": bool(audio.pictures),
        "all_tags": {k: ", ".join(v) for k, v in audio.tags.items()} if audio.tags else {},
    }


def _read_m4a(path: str) -> dict:
    """Read tags from an m4a/MP4 file using MP4 "atoms".

    MP4-based files (m4a is just AAC audio inside an MP4 container) use
    short 4-character atom names for standard fields, several of which
    are non-ASCII, e.g. `"\\xa9nam"` for title (`\\xa9` is the "©" symbol
    Apple uses as a prefix for its standard fields) and `"trkn"` for track
    number. Unlike mp3/flac, the track number isn't a string — it's a
    tuple like `(3, 12)` meaning "track 3 of 12", so we pull just the
    track-number part out and convert it to a string for consistency with
    the other formats.

    Args:
        path: Path to the `.m4a` file.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    audio = MP4(path)
    tags = audio.tags or {}

    def first(key):
        v = tags.get(key)
        return str(v[0]) if v else None

    track = None
    if tags.get("trkn"):
        num = tags["trkn"][0][0]
        track = str(num) if num else None

    return {
        "title": first("\xa9nam"),
        "artist": first("\xa9ART"),
        "album_artist": first("aART"),
        "album": first("\xa9alb"),
        "date": first("\xa9day"),
        "track": track,
        "genre": first("\xa9gen"),
        "duration": round(audio.info.length),
        "has_art": bool(tags.get("covr")),
        "all_tags": _all_m4a_tags(tags),
    }


def _all_m4a_tags(tags) -> dict:
    """Build a dict of every tag in an m4a file's MP4 atoms, for display.

    Same purpose as `_all_mp3_tags`, adapted for MP4's atom format. Cover
    art (the `"covr"` atom) is skipped since it's binary image data, not
    something useful to show as text.

    Args:
        tags: The `.tags` attribute of a mutagen `MP4` object (may be
            `None` if the file has no tags at all).

    Returns:
        A dict mapping each atom name to a string representation of its
        value. Multi-value atoms are joined with `", "`.
    """
    result = {}
    for key, value in tags.items():
        if key == "covr":
            continue
        try:
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            result[key] = str(value)
        except Exception:
            continue
    return result


def write_tags(path: str, candidate: dict, cover_bytes: bytes | None = None) -> None:
    """Write new tags to an audio file, in place, overwriting old values.

    This is called after the user approves a match: `candidate` is the
    metadata chosen from MusicBrainz/iTunes (see `matchers.py`), and this
    function is what actually rewrites the file on disk to match it. It
    dispatches to the right format-specific writer based on file
    extension, the same way `read_tags` does for reading.

    Note this **modifies the file at `path` directly** — the caller
    (`main.py`) is responsible for backing up the original first if that
    matters, since this function has no concept of backups.

    Args:
        path: Absolute filesystem path to the audio file to update.
        candidate: Dict of new metadata to write. Recognized keys:
            `title`, `artist`, `album_artist`, `album`, `date`, `track`,
            `mb_recording_id`, `mb_release_id`, `mb_artist_id`. `artist`
            and `album_artist` are deliberately separate fields — see
            `main.py`'s `apply_album_match` for why writing an album's
            artist onto every track's `artist` tag is a bug, not a
            simplification. Any key
            that's missing or falsy (`None`, `""`) is simply left
            untouched — this function only ever adds/overwrites fields
            it has a real value for, it never blanks out a field.
            `genre` is deliberately ignored even if present in this dict
            (see the module-level note about genre being out of scope).
        cover_bytes: Raw JPEG image bytes to embed as cover art, or `None`
            to leave existing cover art untouched.

    Raises:
        ValueError: if the file extension isn't one this app supports.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        _write_mp3(path, candidate, cover_bytes)
    elif ext == ".flac":
        _write_flac(path, candidate, cover_bytes)
    elif ext == ".m4a":
        _write_m4a(path, candidate, cover_bytes)
    elif ext in (".ogg", ".oga"):
        _write_vorbis_comment(path, OggVorbis, candidate, cover_bytes)
    elif ext == ".opus":
        _write_vorbis_comment(path, OggOpus, candidate, cover_bytes)
    elif ext == ".wma":
        _write_wma(path, candidate, cover_bytes)
    elif ext == ".wav":
        _write_id3_container(path, WAVE, candidate, cover_bytes)
    elif ext in (".aiff", ".aif"):
        _write_id3_container(path, AIFF, candidate, cover_bytes)
    else:
        raise ValueError(f"unsupported file type: {ext}")


def _write_mp3(path: str, c: dict, cover_bytes: bytes | None) -> None:
    """Write new ID3 tags to an mp3 file.

    Args:
        path: Path to the `.mp3` file to update.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()

    def set_text(frame_id, cls, value):
        # Only touch this frame if we actually have a value for it —
        # this is what makes writes "additive": a candidate that has no
        # track number, say, won't erase an existing one.
        if value:
            id3.setall(frame_id, [cls(encoding=3, text=str(value))])

    set_text("TIT2", TIT2, c.get("title"))
    set_text("TPE1", TPE1, c.get("artist"))
    set_text("TPE2", TPE2, c.get("album_artist"))
    set_text("TALB", TALB, c.get("album"))
    set_text("TDRC", TDRC, c.get("date"))
    set_text("TRCK", TRCK, c.get("track"))
    # genre is deliberately never written — out of scope for this tool

    # MusicBrainz IDs don't have their own standard ID3 frame, so they're
    # stored as custom "TXXX" (user-defined text) frames, each identified
    # by a description string (MB_TRACK_FRAME etc.) instead of a 4-letter code.
    for frame_desc, key in ((MB_TRACK_FRAME, "mb_recording_id"),
                            (MB_ALBUM_FRAME, "mb_release_id"),
                            (MB_ARTIST_FRAME, "mb_artist_id")):
        val = c.get(key)
        if val:
            id3.add(TXXX(encoding=3, desc=frame_desc, text=[val]))

    if cover_bytes:
        id3.delall("APIC")  # remove any existing cover art frame(s) first
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))

    id3.save(path, v2_version=4)


def _write_flac(path: str, c: dict, cover_bytes: bytes | None) -> None:
    """Write new Vorbis comment tags to a flac file.

    Args:
        path: Path to the `.flac` file to update.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    audio = FLAC(path)

    def set_field(key, value):
        if value:
            audio[key] = str(value)

    set_field("title", c.get("title"))
    set_field("artist", c.get("artist"))
    set_field("albumartist", c.get("album_artist"))
    set_field("album", c.get("album"))
    set_field("date", c.get("date"))
    set_field("tracknumber", c.get("track"))
    # genre is deliberately never written — out of scope for this tool
    # MUSICBRAINZ_*ID are the de-facto standard Vorbis comment field names
    # other music tools (Picard, etc.) also use for these IDs.
    set_field("MUSICBRAINZ_TRACKID", c.get("mb_recording_id"))
    set_field("MUSICBRAINZ_ALBUMID", c.get("mb_release_id"))
    set_field("MUSICBRAINZ_ARTISTID", c.get("mb_artist_id"))

    if cover_bytes:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3  # "3" is the standard code for "front cover" in the FLAC/ID3 spec
        pic.mime = "image/jpeg"
        pic.data = cover_bytes
        audio.add_picture(pic)

    audio.save()


def _write_m4a(path: str, c: dict, cover_bytes: bytes | None) -> None:
    """Write new MP4 atom tags to an m4a file.

    Args:
        path: Path to the `.m4a` file to update.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    def set_field(atom, value):
        if value:
            tags[atom] = [str(value)]

    set_field("\xa9nam", c.get("title"))
    set_field("\xa9ART", c.get("artist"))
    set_field("aART", c.get("album_artist"))
    set_field("\xa9alb", c.get("album"))
    set_field("\xa9day", c.get("date"))
    # genre is deliberately never written — out of scope for this tool

    track = c.get("track")
    if track:
        try:
            # "trkn" wants a (track_number, total_tracks) tuple; we don't
            # know the total, so 0 (mutagen/iTunes convention for "unknown").
            tags["trkn"] = [(int(str(track).split("/")[0]), 0)]
        except ValueError:
            pass  # candidate's track value wasn't a plain number — skip it rather than crash

    # Same idea as the ID3 TXXX frames in _write_mp3: MP4 has no built-in
    # atom for MusicBrainz IDs, so they go in "freeform" (----) atoms,
    # encoded as raw bytes (hence .encode()).
    for atom, key in ((MP4_MB_TRACK_ATOM, "mb_recording_id"),
                      (MP4_MB_ALBUM_ATOM, "mb_release_id"),
                      (MP4_MB_ARTIST_ATOM, "mb_artist_id")):
        val = c.get(key)
        if val:
            tags[atom] = [MP4FreeForm(val.encode())]

    if cover_bytes:
        tags["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def _read_id3_container(path: str, audio_cls) -> dict:
    """Read ID3 frames out of a non-mp3 container that still uses them (wav, aiff).

    wav and aiff can carry the exact same ID3v2 frames mp3 does, just
    tucked inside a RIFF/IFF chunk instead of sitting at the start of the
    file — mutagen's `WAVE`/`AIFF` classes handle finding that chunk and
    expose the result as `.tags`, an object with the same `getall` API
    `_mp3_frame`/`_all_mp3_tags` already know how to read. Unlike `MP3`,
    `.tags` here is `None` for a file with no ID3 chunk yet rather than
    raising `ID3NoHeaderError`.

    Args:
        path: Path to the file.
        audio_cls: `mutagen.wave.WAVE` or `mutagen.aiff.AIFF`.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    audio = audio_cls(path)
    id3 = audio.tags
    return {
        "title": _mp3_frame(id3, "TIT2"),
        "artist": _mp3_frame(id3, "TPE1"),
        "album_artist": _mp3_frame(id3, "TPE2"),
        "album": _mp3_frame(id3, "TALB"),
        "date": _mp3_frame(id3, "TDRC"),
        "track": _mp3_frame(id3, "TRCK"),
        "genre": _mp3_frame(id3, "TCON"),
        "duration": round(audio.info.length),
        "has_art": bool(id3 and id3.getall("APIC")),
        "all_tags": _all_mp3_tags(id3),
    }


def _write_id3_container(path: str, audio_cls, c: dict, cover_bytes: bytes | None) -> None:
    """Write ID3 frames into a non-mp3 container that still uses them (wav, aiff).

    Same frames, same additive-only behavior as `_write_mp3` — see there
    for the reasoning on each. The only real difference is `add_tags()`:
    unlike mp3 (where a missing tag means `ID3(path)` raised
    `ID3NoHeaderError`, handled by falling back to a fresh `ID3()`), wav/
    aiff report a missing tag as `audio.tags is None`, and mutagen wants an
    explicit `add_tags()` call to create the chunk before it can be filled in.

    Args:
        path: Path to the file.
        audio_cls: `mutagen.wave.WAVE` or `mutagen.aiff.AIFF`.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    audio = audio_cls(path)
    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags

    def set_text(frame_id, cls, value):
        if value:
            id3.setall(frame_id, [cls(encoding=3, text=str(value))])

    set_text("TIT2", TIT2, c.get("title"))
    set_text("TPE1", TPE1, c.get("artist"))
    set_text("TPE2", TPE2, c.get("album_artist"))
    set_text("TALB", TALB, c.get("album"))
    set_text("TDRC", TDRC, c.get("date"))
    set_text("TRCK", TRCK, c.get("track"))
    # genre is deliberately never written — out of scope for this tool

    for frame_desc, key in ((MB_TRACK_FRAME, "mb_recording_id"),
                            (MB_ALBUM_FRAME, "mb_release_id"),
                            (MB_ARTIST_FRAME, "mb_artist_id")):
        val = c.get(key)
        if val:
            id3.add(TXXX(encoding=3, desc=frame_desc, text=[val]))

    if cover_bytes:
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))

    audio.save()


def _read_vorbis_comment(path: str, audio_cls) -> dict:
    """Read Vorbis-comment tags out of an ogg or opus file.

    Same field names/shape as flac's Vorbis comments (`_read_flac`) —
    `OggVorbis`/`OggOpus` expose the identical lowercase `key -> [values]`
    dict interface. The one difference is cover art: neither container has
    flac's native `Picture`/`pictures` support, so it's read out of the
    `metadata_block_picture` field instead (see `VORBIS_PICTURE_FIELD`).

    Args:
        path: Path to the file.
        audio_cls: `mutagen.oggvorbis.OggVorbis` or `mutagen.oggopus.OggOpus`.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    audio = audio_cls(path)

    def first(key):
        v = audio.get(key)
        return v[0] if v else None

    return {
        "title": first("title"),
        "artist": first("artist"),
        "album_artist": first("albumartist"),
        "album": first("album"),
        "date": first("date"),
        "track": first("tracknumber"),
        "genre": first("genre"),
        "duration": round(audio.info.length),
        "has_art": bool(audio.get(VORBIS_PICTURE_FIELD)),
        "all_tags": {k: ", ".join(v) for k, v in audio.tags.items() if k != VORBIS_PICTURE_FIELD} if audio.tags else {},
    }


def _write_vorbis_comment(path: str, audio_cls, c: dict, cover_bytes: bytes | None) -> None:
    """Write Vorbis-comment tags into an ogg or opus file.

    Same fields/additive behavior as `_write_flac`. Cover art has no
    native block to write to here (see `_read_vorbis_comment`), so it's
    packed the same way any other tool that supports Ogg cover art does:
    a flac-style `Picture` block, base64-encoded, stored as a regular
    `metadata_block_picture` comment field.

    Args:
        path: Path to the file.
        audio_cls: `mutagen.oggvorbis.OggVorbis` or `mutagen.oggopus.OggOpus`.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    audio = audio_cls(path)

    def set_field(key, value):
        if value:
            audio[key] = str(value)

    set_field("title", c.get("title"))
    set_field("artist", c.get("artist"))
    set_field("albumartist", c.get("album_artist"))
    set_field("album", c.get("album"))
    set_field("date", c.get("date"))
    set_field("tracknumber", c.get("track"))
    # genre is deliberately never written — out of scope for this tool
    set_field(VORBIS_MB_TRACK_FIELD, c.get("mb_recording_id"))
    set_field(VORBIS_MB_ALBUM_FIELD, c.get("mb_release_id"))
    set_field(VORBIS_MB_ARTIST_FIELD, c.get("mb_artist_id"))

    if cover_bytes:
        pic = Picture()
        pic.type = 3  # "3" is the standard code for "front cover" in the FLAC/ID3 spec
        pic.mime = "image/jpeg"
        pic.data = cover_bytes
        audio[VORBIS_PICTURE_FIELD] = [base64.b64encode(pic.write()).decode("ascii")]

    audio.save()


def _build_wm_picture(data: bytes, mime: str = "image/jpeg", picture_type: int = 3, desc: str = "") -> bytes:
    """Pack cover art bytes into the binary layout wma's `WM/Picture` attribute expects.

    Unlike ID3's `APIC` or flac's `Picture`, mutagen has no helper class
    for this one — it's a plain byte blob you build yourself, per
    Microsoft's documented layout: a 1-byte picture type (same codes as
    ID3's `APIC` type, e.g. `3` = front cover), a 4-byte little-endian
    image size, a UTF-16LE null-terminated mime type string, a UTF-16LE
    null-terminated description string, then the raw image bytes.

    Args:
        data: Raw image bytes (JPEG).
        mime: Image MIME type.
        picture_type: ID3-style picture type code.
        desc: Optional description string.

    Returns:
        The packed byte blob, ready to wrap in an `ASFByteArrayAttribute`.
    """
    mime_bytes = mime.encode("utf-16-le") + b"\x00\x00"
    desc_bytes = desc.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<bI", picture_type, len(data)) + mime_bytes + desc_bytes + data


def _read_wma(path: str) -> dict:
    """Read ASF attributes from a wma file.

    ASF's tag interface (`mutagen.asf.ASF`) doesn't resemble either the
    ID3-frame or Vorbis-comment families: attributes are looked up by
    name (`Title`, `Author`, `WM/AlbumTitle`, ...) and each value comes
    back as a list of `ASFBaseAttribute` objects, whose `str()` gives the
    plain value.

    Args:
        path: Path to the `.wma` file.

    Returns:
        The same dict shape documented in `read_tags`.
    """
    audio = ASF(path)
    tags = audio.tags or {}

    def first(key):
        v = tags.get(key)
        return str(v[0]) if v else None

    return {
        "title": first(WM_TITLE_ATTR),
        "artist": first(WM_ARTIST_ATTR),
        "album_artist": first(WM_ALBUM_ARTIST_ATTR),
        "album": first(WM_ALBUM_ATTR),
        "date": first(WM_YEAR_ATTR),
        "track": first(WM_TRACK_ATTR),
        "genre": first(WM_GENRE_ATTR),
        "duration": round(audio.info.length),
        "has_art": bool(tags.get(WM_PICTURE_ATTR)),
        "all_tags": _all_wma_tags(tags),
    }


def _all_wma_tags(tags) -> dict:
    """Build a dict of every attribute in a wma file's ASF tags, for display.

    Same purpose as `_all_mp3_tags`/`_all_m4a_tags`, adapted for ASF's
    attribute format. `WM/Picture` (cover art) is skipped since it's
    binary image data, not something useful to show as text.

    Args:
        tags: The `.tags` attribute of a mutagen `ASF` object (an
            `ASFTags`, never `None` — an untagged file just has an empty one).

    Returns:
        A dict mapping each attribute name to a string representation of
        its value. Multi-value attributes are joined with `", "`.
    """
    result = {}
    for key, values in tags.items():
        if key == WM_PICTURE_ATTR:
            continue
        try:
            result[key] = ", ".join(str(v) for v in values)
        except Exception:
            continue
    return result


def _write_wma(path: str, c: dict, cover_bytes: bytes | None) -> None:
    """Write new ASF attributes to a wma file.

    Args:
        path: Path to the `.wma` file to update.
        c: The candidate metadata dict (see `write_tags`).
        cover_bytes: New cover art JPEG bytes, or `None` to leave as-is.
    """
    audio = ASF(path)
    tags = audio.tags

    def set_field(attr, value):
        if value:
            tags[attr] = [str(value)]

    set_field(WM_TITLE_ATTR, c.get("title"))
    set_field(WM_ARTIST_ATTR, c.get("artist"))
    set_field(WM_ALBUM_ARTIST_ATTR, c.get("album_artist"))
    set_field(WM_ALBUM_ATTR, c.get("album"))
    set_field(WM_YEAR_ATTR, c.get("date"))
    set_field(WM_TRACK_ATTR, c.get("track"))
    # genre is deliberately never written — out of scope for this tool

    for attr, key in ((WM_MB_TRACK_ATTR, "mb_recording_id"),
                      (WM_MB_ALBUM_ATTR, "mb_release_id"),
                      (WM_MB_ARTIST_ATTR, "mb_artist_id")):
        val = c.get(key)
        if val:
            set_field(attr, val)

    if cover_bytes:
        tags[WM_PICTURE_ATTR] = [ASFByteArrayAttribute(_build_wm_picture(cover_bytes))]

    audio.save()
