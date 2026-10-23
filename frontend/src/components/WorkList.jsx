export function Cover({ id, title, size = "S" }) {
  const cls = size === "M" ? "cover large" : "cover";
  if (!id) return <div className={`${cls} blank`} aria-hidden="true" />;
  return (
    <img
      className={cls}
      loading="lazy"
      alt={`Cover of ${title}`}
      src={`https://covers.openlibrary.org/b/id/${id}-${size}.jpg`}
      onError={(e) => {
        e.currentTarget.replaceWith(Object.assign(document.createElement("div"), { className: `${cls} blank` }));
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
            <a className="row-title" href={`#/work/${w.ol_key}`}>
              {w.title}
              {w.subtitle && <span className="subtitle">: {w.subtitle}</span>}
            </a>
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
              {w.edition_count > 0 && (
                <span className="editions">
                  {(showAuthors && w.authors?.length > 0) || w.first_publish_year ? " · " : ""}
                  {w.edition_count} {w.edition_count === 1 ? "edition" : "editions"}
                  {w.ebook_count > 0 && `, ${w.ebook_count} ebook`}
                </span>
              )}
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
