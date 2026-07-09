# id3tag

A small self-hosted web app that scans your local music library, looks up
each song/album against iTunes (with optional AcoustID audio
fingerprinting), and lets you review and approve a match before it rewrites
the file's tags — nothing is changed without your say-so.

Runs entirely on your own machine/server via Docker; no data leaves your
network except the metadata lookups themselves (iTunes/AcoustID API calls).

## Features

- **Reads and writes mp3, flac, m4a (AAC/ALAC), ogg (Vorbis), opus, wma,
  wav, and aiff** tags (via [mutagen](https://mutagen.readthedocs.io/)) —
  title, artist, album artist, album, year, track number, and cover art,
  regardless of which of those formats a given file is.
- **Artist and album artist are separate fields**, correctly: on a
  compilation or soundtrack, the album can be credited to one name (a
  composer, or the headline artist) while each song has its own performer.
  Approving an album match only ever writes the album's own credit into
  `album_artist`; a track's `artist` tag is only touched when this app can
  independently confirm that specific song's own performer via a
  per-track iTunes search — it's never overwritten with the album's
  artist just because that's what's easiest.
- **Two ways to find a match**: text search against iTunes, and (optional)
  AcoustID audio fingerprinting for files with missing or wrong tags. (See
  [Why no MusicBrainz](#why-no-musicbrainz) — it was tried and removed.)
- **Album-aware**: understands an `Artist/Album/track.ext` folder layout,
  can match/apply a whole album at once, and tolerates folder names that
  are missing punctuation due to filesystem restrictions.
- **You approve every change** — search results are shown side by side with
  the file's current tags; nothing gets written until you click Approve.
  No backup copies are kept (see [Why no backups](#why-no-backups)) — writes
  only ever add/fill in fields, never blank an existing one out.
- **In place, no upload/download** — point it at your music folder and it
  edits files directly.
- **Renames files to match**, once approved: `"<track> - <title>
  (qualifier).<ext>"`, e.g. `"03 - Yesterday (Remastered 2009).mp3"` — track
  number zero-padded so files sort correctly, extension always left
  unchanged. If there's not enough information to build a sensible name
  (no track number, no title), the file is left as-is rather than guessed at.
- **Editable search** — if the folder-derived artist/album/year guess is
  wrong, fix it right in the UI and re-search.
- **Manual metadata/filename edits** — a pencil icon next to the album
  header, each song's tags, and each song's filename opens an inline form
  to type a correction directly, bypassing iTunes/AcoustID entirely. For
  old/rare music no matching service has ever heard of, this is the way to
  fix it — approving a match is one way to write tags, manually typing the
  right values is another.
- Every function in the codebase is documented; browse it at `/reference`
  in the running app (see [Code reference](#code-reference) below).

## Requirements

- Docker + Docker Compose
- A folder of mp3/flac/m4a/ogg/opus/wma/wav/aiff files, ideally laid out as
  `Artist/Album/track.ext`
- (optional) a free [AcoustID API key](https://acoustid.org/api-key), for
  audio-fingerprint matching

## Quick start

```
git clone https://github.com/sysgc/id3tag.git
cd id3tag
cp .env.example .env
```

Edit `.env`:

```
MUSIC_DIR=/path/to/your/music     # required
APP_DATA_DIR=./data                # where the scan cache is kept
HOST_PORT=8080                     # what port to open the app on
ACOUSTID_API_KEY=                  # optional, enables audio-fingerprint matching
```

```
docker compose up --build
```

Open http://localhost:8080 (or whatever `HOST_PORT` you set) and click
"Scan library".

## Configuration

All configuration is via `.env` (copy `.env.example` to start) — nothing is
hardcoded, so there's nothing to edit in the source to point this at your
own setup.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MUSIC_DIR` | yes | — | Host path to your music library. Bind-mounted read-write into the container. |
| `APP_DATA_DIR` | no | `./data` | Host path for this app's own data: just the scan cache. Bind-mounted, so it's a normal folder you can browse directly. |
| `HOST_PORT` | no | `8080` | Port on the host that the app (uvicorn) listens on directly. |
| `ACOUSTID_API_KEY` | no | *(empty)* | Enables AcoustID audio-fingerprint matching. Without it, matching still works via iTunes text search alone. |

## How it works

```
browser  <-->  uvicorn/FastAPI app (HOST_PORT)  <-->  iTunes / AcoustID
                        |
                    MUSIC_DIR (your files, read + written in place)
                    APP_DATA_DIR (scan cache only)
```

- `app/main.py` — the HTTP API and the in-memory registry of what's in your
  library (rebuilt by scanning `MUSIC_DIR`, cached to `APP_DATA_DIR` so
  restarts don't require a full re-scan).
- `app/matchers.py` — all the iTunes/AcoustID search logic.
- `app/tagging.py` — the only code that actually reads/writes tags in a
  file, via `mutagen`.
- `app/static/` — the browser UI (plain HTML/CSS/JS, no build step).

### Why no reverse proxy

Earlier versions put nginx in front of the app; it's been removed.
Uvicorn (the ASGI server FastAPI runs on) serves the app directly on
`HOST_PORT` — there's no separate proxy hop, one less container, one less
config file to keep in sync.

More importantly, this app is built around a **synchronous, one-at-a-time,
human-in-the-loop workflow**: you scan, you look at a candidate match, you
approve it, the file gets rewritten, you move to the next one. It is
deliberately *not* built for unattended bulk/concurrent processing of an
entire library — every route handler in `main.py` is a plain (blocking)
Python function, not `async def`, and there's no background task queue or
worker pool. That's a conscious choice, not a gap to fill in later: adding
async concurrency or auto-processing would work against the whole point of
this tool, which is that a person reviews and approves every single change.
If you're running this for yourself on a home network, a bare uvicorn
process is already more than enough — there's no meaningful load to justify
a reverse proxy in front of it. If you later expose this beyond your own
network (a real domain, HTTPS, several people using it), put whatever
reverse proxy you prefer in front yourself; it's just not bundled by
default anymore.

### Why no MusicBrainz

Earlier versions also matched against [MusicBrainz](https://musicbrainz.org)
— it's a great, free, structured music database, and for a single lookup
now and then it works fine. It was removed because it doesn't hold up at
real library scale: MusicBrainz's unauthenticated API enforces a strict
**1 request per second per IP**. A single "find matches" click for one
album could need several MusicBrainz calls (trying progressively looser
searches when an exact-phrase query came back empty); go through a library
of any real size and you're constantly bumping into that limit, with
requests coming back throttled — which looked exactly like "no match
found," not like a rate-limit error, since there was nothing surfacing the
actual cause.

iTunes' Search API has no such restriction and was already returning good
results on its own, so MusicBrainz was adding latency, complexity, and
flakiness without reliably adding matches. If you're curious what it looked
like, or want to bring a self-hosted/mirrored/authenticated MusicBrainz
lookup back for your own use, `matchers.py`'s module docstring and
`CLAUDE.md` have the details on what was removed and why.

### Why no backups

This app writes tag changes and renames directly to the file in
`MUSIC_DIR` — it does not keep a backup copy of your original audio files
anywhere. That's deliberate: a backup-before-write scheme means storing a
second full copy of every file you ever touch, which for a real music
library quickly adds up to gigabytes of duplicated audio sitting in
`APP_DATA_DIR` for no ongoing benefit, and turns "deploy this app" into
"provision storage for two copies of my library." Not worth it for what
this tool actually needs to protect against:

- Tag writes are additive, not destructive — a field the candidate has no
  value for is left alone, never cleared (see `tagging.write_tags`).
- You review every change before it happens; there's no "oops, applied the
  wrong match to 200 files" scenario this is meant to guard against, by
  design (see [Why no reverse proxy](#why-no-reverse-proxy) above).
- Renaming only ever changes the filename, never the folder or the file's
  actual audio content.

If you want a safety net anyway, back up `MUSIC_DIR` the same way you'd
back up any other files you care about (whatever your existing backup
routine is) — that's a better fit than this app maintaining its own
shadow copy of your entire library.

## Code reference

Every function has a docstring explaining what it does, its inputs/outputs,
and any side effects — written for someone new to the codebase (or to
Python) to be able to safely tweak or extend it. These are built into
browsable HTML with [pdoc](https://pdoc.dev) at Docker build time and served
at `/reference` in the running app (linked as "Docs" in the header).

## Deployment

For deploying to a dedicated server rather than running locally, see
[deployment.md](deployment.md).

## Notes

- Expected library layout: `MUSIC_DIR/Artist/Album/track.ext` — the
  immediate parent folder is used as the album hint, its parent as the
  artist hint. A flat `MUSIC_DIR/Album/track.ext` (no artist level) still
  works, just without an artist hint. Since OS-safe folder names often drop
  characters like `: ' ? *`, hints are fuzzy-matched (punctuation stripped)
  rather than compared exactly. The UI's album column groups/searches by
  artist then album, and same-named albums under different artists are kept
  separate.
- Matching: fingerprint (AcoustID, if key set) + text search (iTunes),
  merged and sorted by confidence. Genre is intentionally never looked up,
  matched, or written — out of scope for this tool. MusicBrainz is
  intentionally not used at all — see [Why no MusicBrainz](#why-no-musicbrainz).
- Apply writes ID3v2.4 for mp3/wav/aiff, Vorbis comments for flac/ogg/opus,
  MP4 atoms for m4a, and ASF attributes for wma — each format's native tag
  system, not a lowest-common-denominator subset. Cover art is embedded the
  same way: natively where the format supports it, and via the
  Picard/foobar2000-style `metadata_block_picture` convention for ogg/opus,
  which have no native picture block of their own. Then renames the file to
  `"<track> - <title> (qualifier).<ext>"` (e.g.
  `"03 - Yesterday (Remastered 2009).mp3"`) if there's enough information to
  build a sensible name; otherwise the filename is left as-is rather than
  guessed at. Applying a whole album checks each file's local title against
  iTunes via a fuzzy per-song search to recover its real track number *and*
  that specific song's own artist credit; a file with **no local title at
  all** is left completely untouched (no tag write, no rename) — so a
  bonus track or a stray non-album file sharing the folder won't get
  mistagged — while a file that already has a local title falls back to
  whatever track number it already has if iTunes can't confidently confirm
  one (a file the user can already see is tagged is one they've
  effectively already confirmed belongs there), but its `artist` tag is
  left alone either way unless iTunes actually confirmed a per-song
  credit — the album's own artist only ever goes into `album_artist`,
  never stamped onto every track's `artist` as a guess.
- Every apply (song or album) verifies the write actually landed before
  reporting success: it re-reads the file straight from disk and checks
  the new values are really there, not just that `mutagen` didn't raise.
  If a field doesn't show up, the request fails loudly instead of the UI
  quietly claiming "tagged" while the file (and anything reading it, like
  Plex) still shows the old data.
- Manual edits (pencil icons on the album header, a song's tags, or a
  song's filename) skip iTunes/AcoustID entirely and write exactly what
  you type — no confirmation search, no belonging-check. That's the point:
  they're for the cases the automated matchers can't handle (an old/rare
  release no metadata service lists), so the human typing the value *is*
  the confirmation. A manual filename edit still always keeps the file's
  real extension, no matter what you type.
- No backup copies are kept — see [Why no backups](#why-no-backups).
- The scan result is cached to `APP_DATA_DIR/library_scan.json` so page
  loads don't re-walk the disk and re-read every file's tags each time.
  "Scan library" forces a fresh walk (new files picked up, removed files
  drop out, already-tagged files keep their status) and refreshes the cache.

## License

[MIT](LICENSE)
