const albumListEl = document.getElementById("album-list");
const albumSearchEl = document.getElementById("album-search");
const detailCol = document.getElementById("detail-col");
const matchesCol = document.getElementById("matches-col");
const artistHeaderTpl = document.getElementById("artist-header-template");
const albumItemTpl = document.getElementById("album-item-template");
const songRowTpl = document.getElementById("song-row-template");
const candidateTpl = document.getElementById("candidate-template");

// must match the sentinels in main.py exactly — they're compared as strings
const NO_ALBUM = "(no album folder)";
const NO_ARTIST = "Unknown Artist";

let files = [];        // flat list of file records from the server
let albums = [];        // grouped by artist/album, derived from `files`
let selectedKey = null;

document.getElementById("scan-btn").addEventListener("click", () => loadFiles(true));
albumSearchEl.addEventListener("input", () => renderAlbumList(albums));
loadFiles(false);

async function updateScanTimestamp() {
  const status = document.getElementById("scan-status");
  try {
    const res = await fetch("/api/scan-status");
    if (!res.ok) return;
    const data = await res.json();
    const when = data.scanned_at ? new Date(data.scanned_at).toLocaleString() : "never";
    status.textContent = `${data.file_count} file(s) · last scanned: ${when}`;
  } catch {
    // status line is a nice-to-have, don't let a failed fetch clobber the count already shown
  }
}

function capEntries(container, entrySelector, maxEntries) {
  const entries = container.querySelectorAll(entrySelector);
  if (entries.length <= maxEntries) {
    container.style.maxHeight = "";
    container.style.overflowY = "";
    return;
  }
  const containerTop = container.getBoundingClientRect().top;
  const cutoff = entries[maxEntries - 1].getBoundingClientRect().bottom;
  container.style.maxHeight = `${Math.ceil(cutoff - containerTop)}px`;
  container.style.overflowY = "auto";
}

function compositeKey(r) {
  return `${r.artist_hint || NO_ARTIST}/${r.album_hint || NO_ALBUM}`;
}

async function loadFiles(force) {
  const btn = document.getElementById("scan-btn");
  const status = document.getElementById("scan-status");
  btn.disabled = true;
  status.textContent = force ? "Scanning…" : "Loading…";

  try {
    const res = await fetch(force ? "/api/scan" : "/api/files", { method: force ? "POST" : "GET" });
    if (!res.ok) throw new Error(await res.text());
    files = await res.json();
  } catch (err) {
    status.textContent = (force ? "Scan" : "Load") + " failed: " + err.message;
    btn.disabled = false;
    return;
  }

  status.textContent = `${files.length} file(s)`;
  btn.disabled = false;
  updateScanTimestamp();

  albums = groupByArtistAlbum(files);
  renderAlbumList(albums);
  if (albums.length) {
    if (!selectedKey || !albums.some((a) => a.key === selectedKey)) selectedKey = albums[0].key;
    renderDetail(selectedKey);
  }
}

function groupByArtistAlbum(records) {
  const map = new Map();
  for (const r of records) {
    const artist = r.artist_hint || NO_ARTIST;
    const album = r.album_hint || NO_ALBUM;
    const key = `${artist}/${album}`;
    if (!map.has(key)) map.set(key, { key, artist, album, items: [] });
    map.get(key).items.push(r);
  }
  return [...map.values()].sort((a, b) => a.artist.localeCompare(b.artist) || a.album.localeCompare(b.album));
}

function renderAlbumList(allAlbums) {
  const filter = albumSearchEl.value.trim().toLowerCase();
  const visible = filter
    ? allAlbums.filter((a) => a.artist.toLowerCase().includes(filter) || a.album.toLowerCase().includes(filter))
    : allAlbums;

  albumListEl.innerHTML = "";
  if (!visible.length) {
    albumListEl.innerHTML = "<p>No matches.</p>";
    return;
  }

  let lastArtist = null;
  visible.forEach((album) => {
    // skip the header entirely for the "no artist folder" bucket — with the
    // expected Artist/Album/track layout this basically never fires, and an
    // "UNKNOWN ARTIST" label for it is just noise
    if (album.artist !== lastArtist && album.artist !== NO_ARTIST) {
      const headerNode = artistHeaderTpl.content.cloneNode(true);
      headerNode.querySelector(".artist-header").textContent = album.artist;
      albumListEl.appendChild(headerNode);
    }
    lastArtist = album.artist;

    const node = albumItemTpl.content.cloneNode(true);
    const el = node.querySelector(".album-item");
    el.querySelector(".album-item-name").textContent = album.album;
    el.querySelector(".album-item-count").textContent = `${album.items.length} track(s)`;
    if (album.key === selectedKey) el.classList.add("selected");
    el.addEventListener("click", () => {
      selectedKey = album.key;
      renderAlbumList(allAlbums);
      renderDetail(selectedKey);
      resetMatchesColumn();
    });
    albumListEl.appendChild(el);
  });

  capEntries(albumListEl, ".album-item", 25);
}

function renderDetail(key) {
  const group = albums.find((a) => a.key === key);
  const items = group ? group.items : [];
  detailCol.innerHTML = "";

  const header = document.createElement("div");
  header.className = "detail-header";
  // prefer the actual tag artist (accurate once a match's been approved) over
  // the folder-derived hint, which stays "Unknown Artist" forever if there's
  // no artist-level subfolder — and if we still have nothing, just show the
  // album name rather than an "Unknown Artist —" label nobody needs
  const displayArtist = group ? (mostCommonTagArtist(items) || group.artist) : null;
  const label = group
    ? (displayArtist && displayArtist !== NO_ARTIST ? `${displayArtist} — ${group.album}` : group.album)
    : key;

  const titleWrap = document.createElement("div");
  titleWrap.className = "detail-title-wrap";
  const h2 = document.createElement("h2");
  h2.textContent = label;
  titleWrap.appendChild(h2);
  if (group) {
    const editBtn = document.createElement("button");
    editBtn.className = "edit-btn edit-album-btn";
    editBtn.title = "Edit artist/album";
    editBtn.innerHTML = "&#9998;";
    editBtn.addEventListener("click", () => albumEditForm.classList.toggle("hidden"));
    titleWrap.appendChild(editBtn);
  }
  header.appendChild(titleWrap);

  const findAlbumBtn = document.createElement("button");
  findAlbumBtn.textContent = "Find matches (album)";
  findAlbumBtn.addEventListener("click", () => {
    findAlbumMatches(group.artist, group.album, key, label, findAlbumBtn, undefined, undefined, guessYear(items));
  });
  header.appendChild(findAlbumBtn);
  detailCol.appendChild(header);

  const albumEditForm = document.createElement("form");
  albumEditForm.className = "inline-edit-form album-edit-form hidden";
  albumEditForm.innerHTML = `
    <label>Artist <input type="text" class="edit-album-artist"></label>
    <label>Album <input type="text" class="edit-album-album"></label>
    <div class="inline-edit-actions">
      <button type="submit" class="edit-save-btn">Save</button>
      <button type="button" class="edit-cancel-btn">Cancel</button>
    </div>
  `;
  if (group) {
    albumEditForm.querySelector(".edit-album-artist").value = displayArtist && displayArtist !== NO_ARTIST ? displayArtist : "";
    albumEditForm.querySelector(".edit-album-album").value = group.album !== NO_ALBUM ? group.album : "";
    albumEditForm.querySelector(".edit-cancel-btn").addEventListener("click", () => albumEditForm.classList.add("hidden"));
    albumEditForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {
        artist: albumEditForm.querySelector(".edit-album-artist").value.trim() || null,
        album: albumEditForm.querySelector(".edit-album-album").value.trim() || null,
      };
      const saveBtn = albumEditForm.querySelector(".edit-save-btn");
      saveBtn.disabled = true;
      try {
        const res = await fetch(`/api/albums/${encodeURIComponent(group.artist)}/${encodeURIComponent(group.album)}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        const updated = await res.json();
        for (const record of updated) {
          const idx = files.findIndex((f) => f.id === record.id);
          if (idx !== -1) files[idx] = record;
        }
        albums = groupByArtistAlbum(files);
        renderAlbumList(albums);
        renderDetail(key);
      } catch (err) {
        alert("Edit failed: " + err.message);
        saveBtn.disabled = false;
      }
    });
  }
  detailCol.appendChild(albumEditForm);

  const songsContainer = document.createElement("div");
  songsContainer.id = "songs-container";
  detailCol.appendChild(songsContainer);

  items.forEach((record) => songsContainer.appendChild(renderSongRow(record)));
  capEntries(songsContainer, ".song-row", 15);
}

function mostCommonTagArtist(items) {
  const counts = new Map();
  for (const r of items) {
    const artist = r.tags.artist;
    if (!artist) continue;
    counts.set(artist, (counts.get(artist) || 0) + 1);
  }
  let best = null;
  let bestCount = 0;
  for (const [artist, count] of counts) {
    if (count > bestCount) { best = artist; bestCount = count; }
  }
  return best;
}

function guessYear(items) {
  const counts = new Map();
  for (const r of items) {
    const year = (r.tags.date || "").slice(0, 4);
    if (!/^\d{4}$/.test(year)) continue;
    counts.set(year, (counts.get(year) || 0) + 1);
  }
  let best = "";
  let bestCount = 0;
  for (const [year, count] of counts) {
    if (count > bestCount) { best = year; bestCount = count; }
  }
  return best;
}

function renderSongRow(record) {
  const node = songRowTpl.content.cloneNode(true);
  const row = node.querySelector(".song-row");
  row.dataset.id = record.id;

  row.querySelector(".filename").textContent = record.filename;
  row.querySelector(".duration").textContent = formatDuration(record.tags.duration);
  updateStatus(row, record.status);
  renderCurrentTags(row, record.tags);

  row.querySelector(".find-song-match-btn").addEventListener("click", (e) => findSongMatches(record.id, row, e.target));

  // Shared mutable holder so the filename-edit and metadata-edit forms
  // (wired separately below) always prefill from whichever one last saved,
  // rather than each closing over its own stale copy of `record`.
  const state = { record };
  row._editState = state; // let findSongMatches' onApprove keep this in sync too
  wireFilenameEdit(row, state);
  wireSongEdit(row, state);

  return row;
}

// After any manual edit or apply, patch `files`/`albums`, the shared row
// state, and this row's DOM with the freshly-returned record — same
// pattern findSongMatches uses.
function applyRecordUpdate(row, state, record) {
  if (state) state.record = record; // state is only absent if row._editState somehow wasn't set — don't hard-fail the UI update over it
  const idx = files.findIndex((f) => f.id === record.id);
  if (idx !== -1) files[idx] = record;
  albums = groupByArtistAlbum(files);
  row.dataset.id = record.id;
  row.querySelector(".filename").textContent = record.filename;
  updateStatus(row, record.status);
  renderCurrentTags(row, record.tags);
}

function wireFilenameEdit(row, state) {
  const btn = row.querySelector(".edit-filename-btn");
  const form = row.querySelector(".filename-edit-form");
  const input = form.querySelector(".edit-filename-input");
  const extLabel = form.querySelector(".edit-ext");

  btn.addEventListener("click", () => {
    const { filename, ext } = state.record;
    input.value = filename.endsWith(ext) ? filename.slice(0, -ext.length) : filename;
    extLabel.textContent = ext;
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) input.focus();
  });
  form.querySelector(".edit-cancel-btn").addEventListener("click", () => form.classList.add("hidden"));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const saveBtn = form.querySelector(".edit-save-btn");
    saveBtn.disabled = true;
    try {
      const res = await fetch(`/api/files/${state.record.id}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: input.value.trim() }),
      });
      if (!res.ok) throw new Error(await res.text());
      applyRecordUpdate(row, state, await res.json());
      form.classList.add("hidden");
    } catch (err) {
      alert("Rename failed: " + err.message);
    } finally {
      saveBtn.disabled = false;
    }
  });
}

function wireSongEdit(row, state) {
  const btn = row.querySelector(".edit-song-btn");
  const form = row.querySelector(".song-edit-form");
  const fields = {
    title: form.querySelector(".edit-title"),
    artist: form.querySelector(".edit-artist"),
    album: form.querySelector(".edit-album"),
    date: form.querySelector(".edit-date"),
    track: form.querySelector(".edit-track"),
  };

  btn.addEventListener("click", () => {
    for (const key of Object.keys(fields)) fields[key].value = state.record.tags[key] || "";
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) fields.title.focus();
  });
  form.querySelector(".edit-cancel-btn").addEventListener("click", () => form.classList.add("hidden"));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const candidate = {};
    for (const key of Object.keys(fields)) {
      const v = fields[key].value.trim();
      if (v) candidate[key] = v;
    }
    const saveBtn = form.querySelector(".edit-save-btn");
    saveBtn.disabled = true;
    try {
      const res = await fetch(`/api/files/${state.record.id}/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate }),
      });
      if (!res.ok) throw new Error(await res.text());
      applyRecordUpdate(row, state, await res.json());
      form.classList.add("hidden");
    } catch (err) {
      alert("Save failed: " + err.message);
    } finally {
      saveBtn.disabled = false;
    }
  });
}

function renderCurrentTags(row, tags) {
  const el = row.querySelector(".current-tags");
  el.innerHTML = `
    <div><strong>Title:</strong> ${tags.title || "—"}</div>
    <div><strong>Artist:</strong> ${tags.artist || "—"}</div>
    <div><strong>Album:</strong> ${tags.album || "—"}</div>
    <div><strong>Year:</strong> ${tags.date || "—"}</div>
    <div><strong>Track:</strong> ${tags.track || "—"}</div>
    <div><strong>Genre:</strong> ${tags.genre || "—"}</div>
  `;
  row.querySelector(".all-tags-body").innerHTML = renderTagTable(tags.all_tags);
}

function renderTagTable(obj) {
  if (!obj || !Object.keys(obj).length) return "<p>No raw tags found.</p>";
  const rows = Object.entries(obj)
    .map(([k, v]) => `<div class="tag-row"><span class="tag-key">${escapeHtml(k)}</span><span class="tag-val">${escapeHtml(formatValue(v))}</span></div>`)
    .join("");
  return `<div class="tag-table">${rows}</div>`;
}

function formatValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function updateStatus(row, status) {
  const badge = row.querySelector(".status-badge");
  badge.textContent = status;
  badge.className = "status-badge status-" + status;
}

async function findSongMatches(fileId, row, btn) {
  btn.disabled = true;
  btn.textContent = "Searching…";
  let candidates;
  try {
    const res = await fetch(`/api/files/${fileId}/matches`);
    if (!res.ok) throw new Error(await res.text());
    candidates = await res.json();
  } catch (err) {
    matchesCol.innerHTML = `<p class="error">Match search failed: ${escapeHtml(err.message)}</p>`;
    btn.disabled = false;
    btn.textContent = "Find match";
    return;
  }
  btn.disabled = false;
  btn.textContent = "Find match";

  renderMatchesColumn({
    title: `Song matches — ${row.querySelector(".filename").textContent}`,
    candidates,
    onApprove: async (candidate) => {
      try {
        const res = await fetch(`/api/files/${fileId}/apply`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate }),
        });
        if (!res.ok) throw new Error(await res.text());
        // also refreshes row._editState so the manual edit forms (wired in
        // renderSongRow) prefill from this newly-approved match, not stale data
        applyRecordUpdate(row, row._editState, await res.json());
      } catch (err) {
        alert("Apply failed: " + err.message);
      }
    },
  });
}

async function findAlbumMatches(artist, album, key, label, btn, searchArtist, searchAlbum, searchYear) {
  // the folder-derived artist/album always identify which files this album
  // maps to (path params); searchArtist/searchAlbum/searchYear are what
  // actually gets sent to iTunes and can be edited independently
  if (searchArtist === undefined) searchArtist = artist === NO_ARTIST ? "" : artist;
  if (searchAlbum === undefined) searchAlbum = album === NO_ALBUM ? "" : album;
  if (searchYear === undefined) searchYear = "";

  btn.disabled = true;
  btn.textContent = "Searching…";
  const base = `/api/albums/${encodeURIComponent(artist)}/${encodeURIComponent(album)}`;
  const qs = `q_artist=${encodeURIComponent(searchArtist)}&q_album=${encodeURIComponent(searchAlbum)}&q_year=${encodeURIComponent(searchYear)}`;
  let candidates;
  try {
    const res = await fetch(`${base}/matches?${qs}`);
    if (!res.ok) throw new Error(await res.text());
    candidates = await res.json();
  } catch (err) {
    matchesCol.innerHTML = `<p class="error">Match search failed: ${escapeHtml(err.message)}</p>`;
    btn.disabled = false;
    btn.textContent = "Find matches (album)";
    return;
  }
  btn.disabled = false;
  btn.textContent = "Find matches (album)";

  renderMatchesColumn({
    title: `Album matches — ${label}`,
    candidates,
    editableSearch: {
      artistValue: searchArtist,
      albumValue: searchAlbum,
      yearValue: searchYear,
      onSearch: (newArtist, newAlbum, newYear) => findAlbumMatches(artist, album, key, label, btn, newArtist, newAlbum, newYear),
    },
    onApprove: async (candidate) => {
      try {
        const res = await fetch(`${base}/apply`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate }),
        });
        if (!res.ok) throw new Error(await res.text());
        const updated = await res.json();
        for (const record of updated) {
          const idx = files.findIndex((f) => f.id === record.id);
          if (idx !== -1) files[idx] = record;
        }
        albums = groupByArtistAlbum(files); // rebuild before re-render so it reflects the just-written tags
        renderDetail(key);
      } catch (err) {
        alert("Apply failed: " + err.message);
      }
    },
  });
}

function resetMatchesColumn() {
  matchesCol.innerHTML = '<div id="matches-empty" class="placeholder">Matches will show up here once you search.</div>';
}

function renderMatchesColumn({ title, candidates, onApprove, editableSearch }) {
  matchesCol.innerHTML = "";
  const h = document.createElement("h2");
  h.textContent = title;
  matchesCol.appendChild(h);

  if (editableSearch) {
    const form = document.createElement("div");
    form.className = "search-edit-form";
    form.innerHTML = `
      <label>Artist <input type="text" class="search-edit-artist"></label>
      <label>Album <input type="text" class="search-edit-album"></label>
      <label class="search-edit-year-label">Year <input type="text" class="search-edit-year" size="4"></label>
      <button class="search-edit-btn">Search</button>
    `;
    const artistInput = form.querySelector(".search-edit-artist");
    const albumInput = form.querySelector(".search-edit-album");
    const yearInput = form.querySelector(".search-edit-year");
    artistInput.value = editableSearch.artistValue;
    albumInput.value = editableSearch.albumValue;
    yearInput.value = editableSearch.yearValue || "";
    form.querySelector(".search-edit-btn").addEventListener("click", () => {
      editableSearch.onSearch(artistInput.value.trim(), albumInput.value.trim(), yearInput.value.trim());
    });
    matchesCol.appendChild(form);
  }

  if (!candidates.length) {
    const p = document.createElement("p");
    p.textContent = "No matches found.";
    matchesCol.appendChild(p);
    return;
  }

  const candidatesList = document.createElement("div");
  candidatesList.className = "candidates-list";
  matchesCol.appendChild(candidatesList);

  candidates.forEach((c) => {
    const node = candidateTpl.content.cloneNode(true);
    const el = node.querySelector(".candidate-card");
    const img = el.querySelector(".cover");
    if (c.cover_url) {
      img.src = c.cover_url;
    } else {
      img.style.visibility = "hidden";
    }
    const hintNote = c.album_hint_similarity !== undefined ? ` · album match ${Math.round(c.album_hint_similarity * 100)}%` : "";
    el.querySelector(".source-badge").textContent = `${c.source} · ${c.confidence}%${hintNote}`;
    el.querySelector(".title").textContent = c.title || "—";
    el.querySelector(".artist").textContent = c.artist || "—";
    el.querySelector(".album-line").textContent = [c.album, c.track_count ? `${c.track_count} tracks` : null].filter(Boolean).join(" · ");
    el.querySelector(".year-line").textContent = `Year: ${(c.date || "").slice(0, 4) || "—"}`;
    const link = el.querySelector(".itunes-link");
    if (c.itunes_url) {
      link.href = c.itunes_url;
    } else {
      link.style.display = "none";
    }
    el.querySelector(".raw-tags-body").innerHTML = renderTagTable(c.raw);
    el.querySelector(".approve-btn").addEventListener("click", () => onApprove(c));
    candidatesList.appendChild(el);
  });

  capEntries(candidatesList, ".candidate-card", 15);
}
