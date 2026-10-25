import { useCallback, useEffect, useState } from "react";
import { api, formatNumber } from "../api.js";
import WorkList from "../components/WorkList.jsx";
import { MONITOR_OPTIONS } from "./Author.jsx";

const TABS = [
  ["wanted", "Wanted"],
  ["authors", "Monitored authors"],
  ["new", "New releases"],
];

export default function Wanted({ tab: initial }) {
  const [tab, setTab] = useState(TABS.some(([t]) => t === initial) ? initial : "wanted");
  return (
    <section>
      <h1>Wanted</h1>
      <p className="lede">
        Books you don’t have yet, from the authors you monitor and the single books you marked Want. Searching indexers
        for them comes in the next phase.
      </p>
      <div className="tabs">
        {TABS.map(([t, label]) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>
      {tab === "wanted" && <WantedList />}
      {tab === "authors" && <AuthorList />}
      {tab === "new" && <NewReleases />}
    </section>
  );
}

function useList(fetcher, deps) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(() => fetcher().then(setRows).catch((e) => setError(e.message)), deps);
  useEffect(() => {
    load();
  }, [load]);
  return { rows, error, setError, load };
}

function WantedList() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQuery(q), 250);
    return () => clearTimeout(t);
  }, [q]);
  const { rows, error, setError, load } = useList(() => api.wanted(query), [query]);

  const onState = async (work, state) => {
    try {
      await api.setWorkState(work.ol_key, state);
      await load();
    } catch (e) {
      setError(e.message);
    }
  };

  if (error) return <div className="notice error">{error}</div>;
  if (!rows) return <p className="muted">Loading…</p>;
  const waiting = rows.filter((w) => w.status === "in_ingest").length;
  return (
    <>
      <div className="search-bar">
        <input className="search compact" placeholder="Filter by title or author" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {rows.length === 0 && !query ? (
        <p className="muted">
          Nothing wanted yet. Open an author (from <a href="#/search">Search</a>) and choose <b>Monitor this author</b>,
          or mark single books <b>Want</b>.
        </p>
      ) : (
        <>
          <p className="muted small-print">
            {formatNumber(rows.length)} {rows.length === 1 ? "book" : "books"}
            {waiting > 0 && `, ${formatNumber(waiting)} with a file waiting in the ingest folder`}.
          </p>
          {rows.length ? <WorkList works={rows} showAuthors onState={onState} /> : <p className="muted">No books match.</p>}
        </>
      )}
    </>
  );
}

function AuthorList() {
  const { rows, error, setError, load } = useList(() => api.monitored(), []);
  const change = async (key, mode) => {
    try {
      if (mode === "none") await api.unmonitor(key);
      else await api.setMonitor(key, mode);
      await load();
    } catch (e) {
      setError(e.message);
    }
  };
  if (error) return <div className="notice error">{error}</div>;
  if (!rows) return <p className="muted">Loading…</p>;
  if (!rows.length) return <p className="muted">You aren’t monitoring anyone yet.</p>;
  return (
    <ul className="rows">
      {rows.map((a) => (
        <li key={a.author_key} className="row">
          <div className="work-body">
            <a className="row-title" href={`#/author/${a.author_key}`}>
              {a.name}
            </a>
            <div className="row-meta">
              {a.counts.owned} owned · {a.counts.wanted} wanted
              {a.counts.in_ingest > 0 && ` · ${a.counts.in_ingest} in ingest`}
              {a.counts.ignored > 0 && ` · ${a.counts.ignored} ignored`} · {a.counts.filtered} filtered out
            </div>
          </div>
          <select className="lang-select" value={a.monitor} onChange={(e) => change(a.author_key, e.target.value)} aria-label={`Monitoring for ${a.name}`}>
            {MONITOR_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </li>
      ))}
    </ul>
  );
}

function NewReleases() {
  const [years, setYears] = useState(2);
  const { rows, error, setError, load } = useList(() => api.wantedNew(years), [years]);
  const onState = async (work, state) => {
    try {
      await api.setWorkState(work.ol_key, state);
      await load();
    } catch (e) {
      setError(e.message);
    }
  };
  if (error) return <div className="notice error">{error}</div>;
  if (!rows) return <p className="muted">Loading…</p>;
  return (
    <>
      <div className="search-bar">
        <select className="lang-select" value={years} onChange={(e) => setYears(Number(e.target.value))} aria-label="How far back">
          <option value={0}>This year</option>
          <option value={1}>Since last year</option>
          <option value={2}>Last 2 years</option>
          <option value={5}>Last 5 years</option>
        </select>
      </div>
      <p className="muted small-print">
        Books by monitored authors first published recently. Open Library only knows about a book once it’s in a
        monthly dump, so rebuild the catalog now and then to see new ones.
      </p>
      {rows.length ? <WorkList works={rows} showAuthors onState={onState} /> : <p className="muted">Nothing recent.</p>}
    </>
  );
}
