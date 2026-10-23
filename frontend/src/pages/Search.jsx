import { useEffect, useState } from "react";
import { api } from "../api.js";
import WorkList from "../components/WorkList.jsx";

function lifespan(a) {
  if (!a.birth_date && !a.death_date) return "";
  return `${a.birth_date || "?"} – ${a.death_date || ""}`.trim();
}

export default function Search() {
  const [q, setQ] = useState(() => sessionStorage.getItem("q") || "");
  const [tab, setTab] = useState("works");
  const [works, setWorks] = useState([]);
  const [authors, setAuthors] = useState([]);
  const [searched, setSearched] = useState(false);
  const [ready, setReady] = useState(true);

  useEffect(() => {
    api.status().then((s) => setReady(s.searchable)).catch(() => {});
  }, []);

  useEffect(() => {
    try {
      sessionStorage.setItem("q", q);
    } catch {
      /* storage unavailable */
    }
    if (!q.trim()) {
      setWorks([]);
      setAuthors([]);
      setSearched(false);
      return;
    }
    let live = true;
    const t = setTimeout(async () => {
      try {
        const [w, a] = await Promise.all([api.searchWorks(q), api.searchAuthors(q)]);
        if (!live) return;
        setWorks(w);
        setAuthors(a);
        setSearched(true);
      } catch {
        /* keep previous results on a transient failure */
      }
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q]);

  return (
    <section>
      <h1>Search the catalog</h1>
      <input
        className="search"
        autoFocus
        placeholder="Title or author, e.g. “tolkien hobbit”"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />

      {!ready && (
        <div className="notice">
          The catalog hasn’t been built yet. <a href="#/catalog">Go to Catalog</a> to download and import it.
        </div>
      )}

      {searched && (
        <>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "works"} className={tab === "works" ? "on" : ""} onClick={() => setTab("works")}>
              Books <span className="count">{works.length}</span>
            </button>
            <button role="tab" aria-selected={tab === "authors"} className={tab === "authors" ? "on" : ""} onClick={() => setTab("authors")}>
              Authors <span className="count">{authors.length}</span>
            </button>
          </div>

          {tab === "works" && (works.length ? <WorkList works={works} showAuthors /> : <p className="muted">No books matched.</p>)}

          {tab === "authors" &&
            (authors.length ? (
              <ul className="rows">
                {authors.map((a) => (
                  <li key={a.ol_key}>
                    <a className="row" href={`#/author/${a.ol_key}`}>
                      <span className="row-title">{a.name}</span>
                      <span className="row-meta">{lifespan(a)}</span>
                      <span className="row-count">{a.work_count} {a.work_count === 1 ? "work" : "works"}</span>
                    </a>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No authors matched.</p>
            ))}
        </>
      )}
    </section>
  );
}
