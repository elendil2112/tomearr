import { useEffect, useState } from "react";
import { api, isbnFromQuery, LANGUAGES } from "../api.js";
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
  const [lang, setLang] = useState(null); // null until settings load
  const [isbnHit, setIsbnHit] = useState(null);

  useEffect(() => {
    api.status().then((s) => setReady(s.searchable)).catch(() => {});
    api.settings().then((s) => setLang(s.language)).catch(() => setLang("eng"));
  }, []);

  const changeLang = (value) => {
    setLang(value);
    api.saveSettings({ language: value }).catch(() => {});
  };

  useEffect(() => {
    try {
      sessionStorage.setItem("q", q);
    } catch {
      /* storage unavailable */
    }
    if (!q.trim() || lang === null) {
      setWorks([]);
      setAuthors([]);
      setIsbnHit(null);
      setSearched(false);
      return;
    }
    let live = true;
    const t = setTimeout(async () => {
      try {
        const isbn = isbnFromQuery(q);
        if (isbn) {
          const hit = await api.isbn(isbn).catch(() => null);
          if (!live) return;
          if (hit) {
            setIsbnHit(hit);
            setWorks(hit.works);
            setAuthors([]);
            setTab("works");
            setSearched(true);
            return;
          }
        }
        const [w, a] = await Promise.all([api.searchWorks(q, lang), api.searchAuthors(q)]);
        if (!live) return;
        setIsbnHit(null);
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
  }, [q, lang]);

  return (
    <section>
      <h1>Search the catalog</h1>
      <div className="search-bar">
        <input
          className="search"
          autoFocus
          placeholder="Title, author or ISBN, e.g. “tolkien hobbit”"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          className="lang-select"
          value={lang ?? "eng"}
          onChange={(e) => changeLang(e.target.value)}
          aria-label="Book language"
          title="Only show books with an edition in this language (books with no known language are always shown)"
        >
          <option value="all">All languages</option>
          {LANGUAGES.map(([code, name]) => (
            <option key={code} value={code}>
              {name}
            </option>
          ))}
        </select>
      </div>

      {!ready && (
        <div className="notice">
          The catalog hasn’t been built yet. <a href="#/catalog">Go to Catalog</a> to download and import it.
        </div>
      )}

      {searched && isbnHit && (
        <p className="muted small-print">
          ISBN <span className="mono">{isbnHit.isbn}</span>:{" "}
          {isbnHit.works.length
            ? `${isbnHit.editions.length} ${isbnHit.editions.length === 1 ? "edition" : "editions"} found`
            : "no edition in the catalog has this ISBN"}
        </p>
      )}
      {searched && isbnHit && isbnHit.works.length > 0 && <WorkList works={isbnHit.works} showAuthors />}

      {searched && !isbnHit && (
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
