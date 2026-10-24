import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";
import { Cover } from "../components/WorkList.jsx";

const QUEUES = [
  ["queue", "To review"],
  ["skipped", "Skipped"],
  ["accepted", "Accepted"],
  ["not_book", "Not a book"],
  ["matched", "Auto-matched"],
];

const KEYS = [
  ["Enter / A", "accept the highlighted match"],
  ["↑ ↓ / J K", "move between matches"],
  ["1 – 9", "accept that match"],
  ["N", "not a book"],
  ["S", "skip for now"],
  ["E", "edit the details, then Enter to find matches"],
  ["/", "search the catalog"],
  ["U", "undo the last decision"],
  ["← →", "previous / next file"],
  ["?", "show or hide these keys"],
];

const SOURCE_LABEL = { "your edit": "edited", "metadata.opf": "opf", file: "file", "matched reading": "name" };

function Field({ label, value, source }) {
  if (value == null || value === "" || (Array.isArray(value) && !value.length)) return null;
  return (
    <div className="rv-field">
      <span className="rv-label">{label}</span>
      <span>{Array.isArray(value) ? value.join(", ") : String(value)}</span>
      {source && <span className="rv-src">{SOURCE_LABEL[source] || source}</span>}
    </div>
  );
}

function Candidate({ c, n, selected, onPick, onSelect }) {
  const pct = Math.round(c.score || 0);
  return (
    <li
      className={`rv-cand ${selected ? "on" : ""}`}
      onClick={() => onSelect()}
      onDoubleClick={() => onPick()}
      aria-selected={selected}
    >
      <span className="rv-num">{n <= 9 ? n : ""}</span>
      <Cover id={c.cover_id} title={c.title} />
      <div className="rv-cand-body">
        <div className="row-title">
          {c.title}
          {c.subtitle && <span className="subtitle">: {c.subtitle}</span>}
          {c.owned && <span className={`owned ${c.owned.includes("EPUB") ? "epub" : ""}`}>Owned{c.owned.length ? ` · ${c.owned.join(", ")}` : ""}</span>}
        </div>
        <div className="row-meta">
          {c.authors.map((a) => a.name).join(", ") || "no author"}
          {c.first_publish_year ? ` · ${c.first_publish_year}` : ""}
          {" · "}
          <a href={`#/work/${c.ol_key}`} onClick={(e) => e.stopPropagation()} className="mono">
            {c.ol_key}
          </a>
        </div>
        <div className="rv-why">
          {c.why.map((w) => (
            <span key={w} className={w.startsWith("ISBN") ? "good" : ""}>
              {w}
            </span>
          ))}
        </div>
      </div>
      <span className={`rv-score ${pct >= 90 ? "hi" : pct >= 60 ? "mid" : "lo"}`}>{pct}%</span>
    </li>
  );
}

export default function Review({ fileId }) {
  const [which, setWhich] = useState("queue");
  const [queue, setQueue] = useState(null);
  const [file, setFile] = useState(null);
  const [cands, setCands] = useState([]);
  const [sel, setSel] = useState(0);
  const [mode, setMode] = useState("view"); // view | edit | search
  const [edit, setEdit] = useState({});
  const [q, setQ] = useState("");
  const lastSearch = useRef(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [help, setHelp] = useState(false);
  const [done, setDone] = useState(0);
  const [minConf, setMinConf] = useState(80);
  const [bulkPreview, setBulkPreview] = useState(null);
  const searchRef = useRef(null);
  const firstEditRef = useRef(null);
  const cache = useRef(new Map());

  const loadQueue = useCallback(async (w = which) => {
    const r = await api.reviewQueue(w);
    setQueue(r);
    return r;
  }, [which]);

  const fetchFile = useCallback(async (id) => {
    if (cache.current.has(id)) return cache.current.get(id);
    const p = api.reviewFile(id);
    cache.current.set(id, p);
    p.catch(() => cache.current.delete(id));
    return p;
  }, []);

  const show = useCallback(
    async (id) => {
      setErr("");
      if (id == null) {
        setFile(null);
        setCands([]);
        return;
      }
      try {
        const f = await fetchFile(id);
        // A new file always starts with the keyboard on the shortcuts, never inside a text box.
        if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) document.activeElement.blur();
        setFile(f);
        setCands(f.candidates);
        const i = f.candidates.findIndex((c) => c.ol_key === f.selected);
        setSel(i >= 0 ? i : 0);
        setMode("view");
        setEdit({
          title: f.reading.title || "",
          authors: (f.reading.authors || []).join(" & "),
          series: f.reading.series || "",
          series_index: f.reading.series_index ?? "",
          isbn: (f.reading.isbns || [])[0] || "",
          language: f.reading.language || "",
        });
        setQ(f.reading.title || "");
        lastSearch.current = null;
        if (window.location.hash !== `#/review/${id}`) history.replaceState(null, "", `#/review/${id}`);
      } catch (e) {
        setErr(e.message);
      }
    },
    [fetchFile],
  );

  // initial load / queue change
  useEffect(() => {
    let live = true;
    (async () => {
      const r = await loadQueue(which).catch((e) => (setErr(e.message), null));
      if (!live || !r) return;
      const start = fileId && which === "queue" && !queue ? Number(fileId) : r.ids[0];
      show(start ?? null);
    })();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [which]);

  // prefetch the next file so moving on feels instant
  const pos = file && queue ? queue.ids.indexOf(file.id) : -1;
  useEffect(() => {
    if (queue && pos >= 0 && queue.ids[pos + 1] != null) fetchFile(queue.ids[pos + 1]).catch(() => {});
  }, [queue, pos, fetchFile]);

  const refreshBulk = useCallback(() => {
    api.bulk({ min_confidence: minConf, dry_run: true }).then(setBulkPreview).catch(() => setBulkPreview(null));
  }, [minConf]);
  useEffect(() => {
    const t = setTimeout(refreshBulk, 250);
    return () => clearTimeout(t);
  }, [refreshBulk]);

  const flash = (m) => {
    setMsg(m);
    setTimeout(() => setMsg((x) => (x === m ? "" : x)), 2500);
  };

  const editedOverrides = () => {
    if (!file) return null;
    const r = file.reading;
    const o = {};
    if (edit.title && edit.title !== (r.title || "")) o.title = edit.title;
    if (edit.authors !== (r.authors || []).join(" & ") && edit.authors) o.authors = edit.authors;
    if (edit.series && edit.series !== (r.series || "")) o.series = edit.series;
    if (String(edit.series_index) !== String(r.series_index ?? "") && edit.series_index !== "") o.series_index = edit.series_index;
    if (edit.isbn && edit.isbn !== ((r.isbns || [])[0] || "")) o.isbn = edit.isbn;
    if (edit.language && edit.language !== (r.language || "")) o.language = edit.language;
    return Object.keys(o).length ? o : null;
  };

  const act = async (action, candidate) => {
    if (!file || busy) return;
    setBusy(true);
    setErr("");
    try {
      const wk = action === "accept" ? (candidate || cands[sel])?.ol_key : null;
      const r = await api.decide(file.id, action, wk, action === "accept" ? editedOverrides() : null);
      cache.current.delete(file.id);
      if (action !== "reset") setDone((d) => d + 1);
      flash(
        action === "accept" ? `Accepted as ${(candidate || cands[sel])?.title}` :
        action === "not_book" ? "Marked not a book" : action === "skip" ? "Skipped" : "Reset",
      );
      const qr = await loadQueue();
      refreshBulk();
      // in the main queue, go to the next undecided file; elsewhere, the next one in the list
      const nextId = which === "queue" ? r.next : qr.ids[Math.min(pos, qr.ids.length - 1)] ?? null;
      show(nextId);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    try {
      const r = await api.undo();
      r.file_ids.forEach((id) => cache.current.delete(id));
      setDone((d) => Math.max(0, d - r.undone));
      flash(r.undone > 1 ? `Undid a bulk action (${r.undone} files)` : "Undone");
      await loadQueue();
      refreshBulk();
      show(r.file_ids[r.file_ids.length - 1]);
    } catch (e) {
      setErr(e.message === "nothing to undo" ? "Nothing to undo" : e.message);
    }
  };

  const runSearch = async () => {
    if (!file || !q.trim()) return;
    try {
      lastSearch.current = q;
      setCands(await api.reviewSearch(file.id, q));
      setSel(0);
      setMode("view");
      searchRef.current?.blur(); // so Enter now accepts the highlighted result instead of searching again
    } catch (e) {
      setErr(e.message);
    }
  };

  const runRematch = async () => {
    if (!file) return;
    try {
      const c = await api.rematch(file.id, { title: edit.title, authors: edit.authors, isbn: edit.isbn || null });
      setCands(c.length ? c : cands);
      if (!c.length) flash("No matches for those details");
      setSel(0);
      setMode("view");
    } catch (e) {
      setErr(e.message);
    }
  };

  const bulkAccept = async (body, label) => {
    const preview = await api.bulk({ ...body, dry_run: true });
    if (!preview.count) return flash("Nothing to accept");
    const r = await api.bulk({ ...body, dry_run: false });
    cache.current.clear();
    setDone((d) => d + r.count);
    flash(`Accepted ${r.count} ${label} · U to undo`);
    const qr = await loadQueue();
    refreshBulk();
    show(qr.ids[0] ?? null);
  };

  const step = (d) => {
    if (!queue || pos < 0) return;
    const id = queue.ids[pos + d];
    if (id != null) show(id);
  };

  // keyboard
  useEffect(() => {
    const onKey = (e) => {
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
      if (typing) {
        if (e.key === "Escape") {
          e.target.blur();
          setMode("view");
        } else if (e.key === "Enter" && mode === "search") {
          e.preventDefault();
          // Enter searches; Enter again (same words) accepts the highlighted result.
          if (lastSearch.current === q && cands[sel] && file?.can_accept) {
            e.target.blur();
            act("accept");
          } else runSearch();
        } else if (e.key === "Enter" && mode === "edit") {
          e.preventDefault();
          e.target.blur();
          runRematch();
        }
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "?") return setHelp((h) => !h);
      if (!file) {
        if (k === "u") undo();
        return;
      }
      if (k === "arrowdown" || k === "j") {
        e.preventDefault();
        setSel((s) => Math.min(cands.length - 1, s + 1));
      } else if (k === "arrowup" || k === "k") {
        e.preventDefault();
        setSel((s) => Math.max(0, s - 1));
      } else if (k === "enter" || k === "a") {
        e.preventDefault();
        if (file.can_accept && cands[sel]) act("accept");
      } else if (/^[1-9]$/.test(k)) {
        const c = cands[Number(k) - 1];
        if (c && file.can_accept) {
          setSel(Number(k) - 1);
          act("accept", c);
        }
      } else if (k === "n") act("not_book");
      else if (k === "s") act("skip");
      else if (k === "u") undo();
      else if (k === "arrowright") step(1);
      else if (k === "arrowleft") step(-1);
      else if (k === "/") {
        e.preventDefault();
        setMode("search");
        setTimeout(() => searchRef.current?.select(), 0);
      } else if (k === "e") {
        e.preventDefault();
        setMode("edit");
        setTimeout(() => firstEditRef.current?.focus(), 0);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const selected = cands[sel];
  const authorOf = selected?.authors?.[0];
  const counts = queue?.counts || {};
  const r = file?.reading;

  const fileName = useMemo(() => {
    if (!file) return ["", ""];
    const i = file.path.lastIndexOf("/");
    return [i >= 0 ? file.path.slice(0, i + 1) : "", file.path.slice(i + 1)];
  }, [file]);

  return (
    <section className="review">
      <div className="rv-head">
        <h1>Review</h1>
        <div className="rv-progress">
          {queue && file && pos >= 0 && (
            <span>
              {formatNumber(pos + 1)} of {formatNumber(queue.ids.length)}
            </span>
          )}
          {done > 0 && <span className="muted"> · {formatNumber(done)} decided this session</span>}
          <button className="link" onClick={() => setHelp((h) => !h)}>
            keys (?)
          </button>
        </div>
      </div>

      <div className="tabs" role="tablist">
        {QUEUES.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={which === id} className={which === id ? "on" : ""} onClick={() => setWhich(id)}>
            {label} <span className="count">{formatNumber(counts[id])}</span>
          </button>
        ))}
      </div>

      {help && (
        <div className="card rv-help">
          {KEYS.map(([k, d]) => (
            <div key={k}>
              <kbd>{k}</kbd> {d}
            </div>
          ))}
        </div>
      )}

      {which === "queue" && (counts.queue || 0) > 0 && (
        <div className="rv-bulk">
          <span>Accept every match at</span>
          <input type="number" min="60" max="100" value={minConf} onChange={(e) => setMinConf(Number(e.target.value) || 60)} aria-label="Minimum confidence" />
          <span>% or better</span>
          <button disabled={!bulkPreview?.count} onClick={() => bulkAccept({ min_confidence: minConf }, `at ${minConf}%+`)}>
            Accept {formatNumber(bulkPreview?.count || 0)}
          </button>
          {authorOf && (
            <button onClick={() => bulkAccept({ author_key: authorOf.ol_key }, `by ${authorOf.name}`)}>
              Accept all matched to {authorOf.name}
            </button>
          )}
        </div>
      )}

      {err && <div className="notice error">{err}</div>}
      {msg && <div className="rv-toast" role="status">{msg}</div>}

      {!file ? (
        queue && (
          <div className="card rv-empty">
            <h2>{which === "queue" ? "All clear" : "Nothing here"}</h2>
            <p className="muted">
              {which === "queue"
                ? `Nothing left to review. ${counts.skipped ? `${counts.skipped} skipped file${counts.skipped === 1 ? "" : "s"} can be revisited under Skipped.` : ""}`
                : "No files in this list."}{" "}
              <a href="#/ingest">Back to Ingest</a>
            </p>
          </div>
        )
      ) : (
        <div className="rv-card">
          <div className="rv-file card">
            <div className="rv-path">
              <span className={`fmt ${file.format === "EPUB" ? "epub" : ""}`}>{file.format}</span>{" "}
              <span className="muted">{fileName[0]}</span>
              <strong>{fileName[1]}</strong>
            </div>
            <div className="muted small-print">
              {formatBytes(file.size)} · {file.bucket}
              {file.decision ? ` · ${file.decision.replace("_", " ")}` : ""}
              {file.match_confidence ? ` · auto match ${file.match_confidence}%` : ""}
            </div>
            {file.drm && <div className="issue">DRM{file.drm_detail ? `: ${file.drm_detail}` : ""}. Can't be imported.</div>}
            {file.duplicate_of && <div className="issue warn">Duplicate of {file.duplicate_of.replace(/^\w+:/, "")}</div>}
            {file.error && <div className="issue">{file.error}</div>}

            {mode !== "edit" ? (
              <div className="rv-fields">
                <Field label="Title" value={r.title} source={r.source.title} />
                <Field label="Author" value={r.authors} source={r.source.authors} />
                <Field label="ISBN" value={r.isbns} source={r.source.isbns} />
                <Field label="Series" value={r.series ? `${r.series}${r.series_index != null ? ` #${r.series_index}` : ""}` : null} source={r.source.series} />
                <Field label="Language" value={r.language} source={r.source.language} />
                <Field label="Year" value={r.year} source={r.source.year} />
                <Field label="Publisher" value={r.publisher} source={r.source.publisher} />
                <button className="link" onClick={() => { setMode("edit"); setTimeout(() => firstEditRef.current?.focus(), 0); }}>
                  edit details (E)
                </button>
              </div>
            ) : (
              <form className="rv-edit" onSubmit={(e) => { e.preventDefault(); runRematch(); }}>
                {[
                  ["title", "Title"],
                  ["authors", "Authors (A & B)"],
                  ["series", "Series"],
                  ["series_index", "Series #"],
                  ["isbn", "ISBN"],
                  ["language", "Language"],
                ].map(([k, label], i) => (
                  <label key={k} className="field">
                    <span>{label}</span>
                    <input
                      ref={i === 0 ? firstEditRef : undefined}
                      autoFocus={i === 0}
                      value={edit[k]}
                      onChange={(e) => setEdit({ ...edit, [k]: e.target.value })}
                      spellCheck={false}
                    />
                  </label>
                ))}
                <p className="muted small-print">Enter finds matches for these details. Your edits are saved when you accept.</p>
              </form>
            )}
          </div>

          <div className="rv-matches card">
            <div className="rv-search">
              <input
                ref={searchRef}
                className="search compact"
                placeholder="Search the catalog (/)"
                value={q}
                onFocus={() => setMode("search")}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            {cands.length === 0 ? (
              <p className="muted">No candidates. Search the catalog or edit the details (E).</p>
            ) : (
              <ul className="rv-cands">
                {cands.map((c, i) => (
                  <Candidate key={c.ol_key} c={c} n={i + 1} selected={i === sel} onSelect={() => setSel(i)} onPick={() => file.can_accept && act("accept", c)} />
                ))}
              </ul>
            )}
            <div className="actions rv-actions">
              <button className="primary" disabled={busy || !file.can_accept || !selected} onClick={() => act("accept")}>
                Accept <kbd>Enter</kbd>
              </button>
              <button disabled={busy} onClick={() => act("not_book")}>
                Not a book <kbd>N</kbd>
              </button>
              <button disabled={busy} onClick={() => act("skip")}>
                Skip <kbd>S</kbd>
              </button>
              <button disabled={busy} onClick={undo}>
                Undo <kbd>U</kbd>
              </button>
              {file.decision && (
                <button disabled={busy} onClick={() => act("reset")}>
                  Back to queue
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
