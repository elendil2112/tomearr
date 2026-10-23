function Cover({ id, title }) {
  if (!id) return <div className="cover blank" aria-hidden="true" />;
  return (
    <img
      className="cover"
      loading="lazy"
      alt={`Cover of ${title}`}
      src={`https://covers.openlibrary.org/b/id/${id}-S.jpg`}
      onError={(e) => {
        e.currentTarget.replaceWith(Object.assign(document.createElement("div"), { className: "cover blank" }));
      }}
    />
  );
}

export default function WorkList({ works, showAuthors = false }) {
  return (
    <ul className="rows">
      {works.map((w) => (
        <li key={w.ol_key} className="row work">
          <Cover id={w.cover_id} title={w.title} />
          <div className="work-body">
            <div className="row-title">
              {w.title}
              {w.subtitle && <span className="subtitle">: {w.subtitle}</span>}
            </div>
            <div className="row-meta">
              {showAuthors &&
                w.authors?.map((a, i) => (
                  <span key={a.ol_key}>
                    {i > 0 && ", "}
                    <a href={`#/author/${a.ol_key}`}>{a.name}</a>
                  </span>
                ))}
              {showAuthors && w.authors?.length > 0 && w.first_publish_year && " · "}
              {w.first_publish_year}
            </div>
            {w.subjects && <div className="row-subjects">{w.subjects.split(" | ").slice(0, 5).join(" · ")}</div>}
          </div>
          <a className="mono ol-link" href={`https://openlibrary.org/works/${w.ol_key}`} target="_blank" rel="noreferrer">
            {w.ol_key}
          </a>
        </li>
      ))}
    </ul>
  );
}
