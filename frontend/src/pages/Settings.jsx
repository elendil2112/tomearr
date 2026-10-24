import { useEffect, useState } from "react";
import { api, formatNumber, LANGUAGES } from "../api.js";

export default function Settings() {
  const [settings, setSettings] = useState(null);
  const [path, setPath] = useState("");
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .settings()
      .then((s) => {
        setSettings(s);
        setPath(s.calibre_path);
      })
      .catch((e) => setError(e.message));
  }, []);

  const save = async (body, label) => {
    setError("");
    try {
      const s = await api.saveSettings(body);
      setSettings(s);
      setPath(s.calibre_path);
      setSaved(label);
      setTimeout(() => setSaved(""), 2500);
    } catch (e) {
      setError(e.message);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.libraryTest(path));
    } catch (e) {
      setTest({ ok: false, message: e.message });
    } finally {
      setTesting(false);
    }
  };

  if (!settings) return error ? <div className="notice error">{error}</div> : <p className="muted">Loading…</p>;

  return (
    <section>
      <h1>Settings</h1>
      {error && <div className="notice error">{error}</div>}

      <div className="card">
        <h2>Calibre library</h2>
        <p className="muted small-print">
          The folder that contains <code>metadata.db</code>, as the container sees it. With the default compose file
          that is <code>/calibre</code>. Tomearr copies <code>metadata.db</code> and reads the copy, so it never locks or
          changes your library, even while Calibre is running.
        </p>
        <label className="field">
          <span>Library folder</span>
          <input type="text" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} />
        </label>
        <div className="actions">
          <button disabled={testing} onClick={runTest}>
            {testing ? "Testing…" : "Test connection"}
          </button>
          <button className="primary" disabled={path === settings.calibre_path} onClick={() => save({ calibre_path: path }, "calibre")}>
            Save
          </button>
          {saved === "calibre" && <span className="ok small-print">Saved</span>}
        </div>
        {test &&
          (test.ok ? (
            <div className="notice ok-notice">
              Found {formatNumber(test.books)} books by {formatNumber(test.authors)} authors.{" "}
              {Object.entries(test.formats)
                .map(([f, n]) => `${formatNumber(n)} ${f}`)
                .join(", ")}
              .
            </div>
          ) : (
            <div className="notice error">{test.message}</div>
          ))}
      </div>

      <div className="card">
        <h2>Search</h2>
        <label className="field">
          <span>Default book language</span>
          <select value={settings.language} onChange={(e) => save({ language: e.target.value }, "language")}>
            <option value="all">All languages</option>
            {LANGUAGES.map(([code, name]) => (
              <option key={code} value={code}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {saved === "language" && <span className="ok small-print">Saved</span>}
      </div>
    </section>
  );
}
