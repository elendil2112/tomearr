import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import WorkList from "../components/WorkList.jsx";

export default function Author({ olKey }) {
  const [author, setAuthor] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("year");

  useEffect(() => {
    setAuthor(null);
    setError("");
    api.author(olKey).then(setAuthor).catch((e) => setError(e.message));
  }, [olKey]);

  const works = useMemo(() => {
    if (!author) return [];
    const f = filter.trim().toLowerCase();
    const list = f ? author.works.filter((w) => `${w.title} ${w.subtitle || ""}`.toLowerCase().includes(f)) : author.works;
    return sort === "popular" ? [...list].sort((a, b) => (b.popularity || 0) - (a.popularity || 0)) : list;
  }, [author, filter, sort]);

  if (error) return <div className="notice error">{error}</div>;
  if (!author) return <p className="muted">Loading…</p>;

  const alts = author.alt_names ? author.alt_names.split(" | ") : [];

  return (
    <section>
      <a className="back" href="#/search">← Back to search</a>
      <h1>{author.name}</h1>
      <p className="lede">
        {(author.birth_date || author.death_date) && <span>{author.birth_date || "?"} – {author.death_date || ""} · </span>}
        {author.work_count} {author.work_count === 1 ? "work" : "works"} in the catalog · <span className="mono">{author.ol_key}</span>
      </p>
      {alts.length > 0 && <p className="muted small-print">Also known as: {alts.slice(0, 8).join(", ")}</p>}

      <div className="search-bar">
        <input className="search compact" placeholder="Filter this author’s works" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <select className="lang-select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort works">
          <option value="year">Oldest first</option>
          <option value="popular">Most popular</option>
        </select>
      </div>
      {works.length ? <WorkList works={works} /> : <p className="muted">No works match.</p>}
    </section>
  );
}
