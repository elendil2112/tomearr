import { useEffect, useState } from "react";
import Catalog from "./pages/Catalog.jsx";
import Search from "./pages/Search.jsx";
import Author from "./pages/Author.jsx";
import Work from "./pages/Work.jsx";
import Library from "./pages/Library.jsx";
import Settings from "./pages/Settings.jsx";

function useHashRoute() {
  const read = () => window.location.hash.replace(/^#\/?/, "") || "search";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

const NAV = [
  { id: "search", label: "Search" },
  { id: "library", label: "Library" },
  { id: "catalog", label: "Catalog" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const route = useHashRoute();
  const [page, arg] = route.split("/");

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/search">
          <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="var(--accent)" />
            <path d="M9 8h11a3 3 0 0 1 3 3v13H12a3 3 0 0 1-3-3z" fill="none" stroke="var(--paper)" strokeWidth="2" />
            <path d="M13 13h6M13 17h6" stroke="var(--paper)" strokeWidth="2" />
          </svg>
          <span>Tomearr</span>
        </a>
        <nav>
          {NAV.map((n) => (
            <a key={n.id} href={`#/${n.id}`} className={page === n.id || (n.id === "search" && (page === "author" || page === "work")) ? "active" : ""}>
              {n.label}
            </a>
          ))}
        </nav>
      </header>
      <main className="content">
        {page === "catalog" && <Catalog />}
        {page === "author" && <Author olKey={arg} />}
        {page === "work" && <Work olKey={arg} />}
        {page === "library" && <Library />}
        {page === "settings" && <Settings />}
        {!["catalog", "author", "work", "library", "settings"].includes(page) && <Search />}
      </main>
    </div>
  );
}
