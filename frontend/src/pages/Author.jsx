import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import WorkList from "../components/WorkList.jsx";

export const MONITOR_OPTIONS = [
  ["all", "All books", "Every book that passes your profile and isn’t in Calibre is wanted."],
  ["future", "Future books only", "Only books that appear in the catalog from now on."],
  ["selected", "Selected books only", "Only the books you mark Want."],
  ["none", "Not monitored", ""],
];

const SHOW = [
  ["all", "All"],
  ["wanted", "Wanted"],
  ["in_ingest", "In ingest"],
  ["downloading", "Downloading"],
  ["owned", "Owned"],
  ["ignored", "Ignored"],
  ["filtered", "Filtered"],
];

export default function Author({ olKey }) {
  const [author, setAuthor] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("year");
  const [show, setShow] = useState("all");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.author(olKey).then(setAuthor).catch((e) => setError(e.message)), [olKey]);

  useEffect(() => {
    setAuthor(null);
    setError("");
    setShow("all");
    load();
  }, [load]);

  const setMonitor = async (mode) => {
    setBusy(true);
    try {
      if (mode === "none") await api.unmonitor(olKey);
      else await api.setMonitor(olKey, mode);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const onState = async (work, state) => {
    try {
      await api.setWorkState(work.ol_key, state);
      await load();
    } catch (e) {
      setError(e.message);
    }
  };

  const works = useMemo(() => {
    if (!author) return [];
    const f = filter.trim().toLowerCase();
    let list = f ? author.works.filter((w) => `${w.title} ${w.subtitle || ""}`.toLowerCase().includes(f)) : author.works;
    if (show !== "all") list = list.filter((w) => w.status === show);
    return sort === "popular" ? [...list].sort((a, b) => (b.popularity || 0) - (a.popularity || 0)) : list;
  }, [author, filter, sort, show]);

  if (error && !author) return <div className="notice error">{error}</div>;
  if (!author) return <p className="muted">Loading…</p>;

  const alts = author.alt_names ? author.alt_names.split(" | ") : [];
  const mode = author.monitor?.monitor || "none";
  const c = author.counts;

  return (
    <section>
      <a className="back" href="#/search">← Back to search</a>
      <h1>{author.name}</h1>
      <p className="lede">
        {(author.birth_date || author.death_date) && <span>{author.birth_date || "?"} – {author.death_date || ""} · </span>}
        {author.works.length} {author.works.length === 1 ? "work" : "works"} in the catalog · <span className="mono">{author.ol_key}</span>
      </p>
      {alts.length > 0 && <p className="muted small-print">Also known as: {alts.slice(0, 8).join(", ")}</p>}
      {error && <div className="notice error">{error}</div>}

      <div className="card monitor-card">
        {mode === "none" ? (
          <div className="actions" style={{ marginTop: 0 }}>
            <button className="primary" disabled={busy} onClick={() => setMonitor("all")}>
              Monitor this author
            </button>
          </div>
        ) : (
          <label className="field" style={{ margin: 0 }}>
            <span>Monitoring</span>
            <select value={mode} disabled={busy} onChange={(e) => setMonitor(e.target.value)}>
              {MONITOR_OPTIONS.map(([v, label]) => (
                <option key={v} value={v}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}
        <div>
          <div className="counts">
            {SHOW.map(([k, label]) => {
              const n = k === "all" ? author.works.length : c[k];
              if (k !== "all" && !n) return null;
              return (
                <button key={k} className={show === k ? "on" : ""} onClick={() => setShow(k)}>
                  <b>{n}</b> {label.toLowerCase()}
                </button>
              );
            })}
          </div>
          <p className="muted small-print" style={{ margin: "6px 0 0" }}>
            {mode === "none"
              ? "Monitoring adds this author’s missing books to your Wanted list. You can still mark single books Want."
              : MONITOR_OPTIONS.find(([v]) => v === mode)[2]}
            {mode === "future" && author.monitor?.monitor_from && ` (since ${author.monitor.monitor_from.slice(0, 10)})`}
          </p>
        </div>
      </div>

      <div className="search-bar">
        <input className="search compact" placeholder="Filter this author’s works" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <select className="lang-select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort works">
          <option value="year">Oldest first</option>
          <option value="popular">Most popular</option>
        </select>
      </div>
      {works.length ? <WorkList works={works} onState={onState} searchable onGrabbed={load} /> : <p className="muted">No works match.</p>}
    </section>
  );
}
