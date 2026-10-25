async function request(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return res.json();
}

const post = (path, body = {}) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const api = {
  status: () => request("/api/status"),
  jobs: () => request("/api/jobs?limit=12"),
  build: (opts) => post("/api/catalog/build", opts),
  download: (opts) => post("/api/catalog/download", opts),
  importDumps: (opts) => post("/api/catalog/import", opts),
  reindex: () => post("/api/catalog/reindex"),
  cancel: (id) => post(`/api/jobs/${id}/cancel`),
  searchAuthors: (q) => request(`/api/search/authors?q=${encodeURIComponent(q)}`),
  searchWorks: (q, lang) =>
    request(`/api/search/works?q=${encodeURIComponent(q)}${lang ? `&lang=${encodeURIComponent(lang)}` : ""}`),
  author: (key) => request(`/api/authors/${encodeURIComponent(key)}`),
  work: (key) => request(`/api/works/${encodeURIComponent(key)}`),
  isbn: (value) => request(`/api/isbn/${encodeURIComponent(value)}`),
  library: ({ q = "", filter = "all", offset = 0, limit = 200 } = {}) =>
    request(`/api/library?q=${encodeURIComponent(q)}&filter=${filter}&offset=${offset}&limit=${limit}`),
  libraryStats: () => request("/api/library/stats"),
  librarySync: (rematch = false) => post("/api/library/sync", { rematch }),
  libraryTest: (path) => post("/api/library/test", { path }),
  setMatch: (id, workKey) =>
    request(`/api/library/${id}/match`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ work_key: workKey }),
    }),
  clearMatch: (id) => request(`/api/library/${id}/match`, { method: "DELETE" }),
  ingest: ({ bucket = "all", q = "", offset = 0, limit = 200 } = {}) =>
    request(`/api/ingest?bucket=${bucket}&q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`),
  ingestStats: () => request("/api/ingest/stats"),
  ingestScan: (opts = {}) => post("/api/ingest/scan", opts),
  ingestTest: (path) => post("/api/ingest/test", { path }),
  reviewQueue: (which = "queue") => request(`/api/review/queue?which=${which}`),
  reviewFile: (id) => request(`/api/review/${id}`),
  decide: (id, action, workKey, overrides) =>
    post(`/api/review/${id}/decide`, { action, work_key: workKey || null, overrides: overrides || null }),
  rematch: (id, body) => post(`/api/review/${id}/rematch`, body),
  reviewSearch: (id, q) => request(`/api/review/${id}/search?q=${encodeURIComponent(q)}`),
  undo: () => post("/api/review/undo"),
  bulk: (body) => post("/api/review/bulk", body),
  calibreTest: () => post("/api/calibre/test"),
  importPlan: (library = false) => request(`/api/import/plan${library ? "?library=true" : ""}`),
  importRun: (keys, library = false) => post("/api/import/run", { keys, library }),
  importHistory: () => request("/api/import/history"),
  importUndo: (id) => post(`/api/import/${id}/undo`),
  importUndoBatch: (batch) => post(`/api/import/batch/${batch}/undo`),
  pdf: (id, action) => post(`/api/import/pdf/${id}`, { action }),
  pdfPreview: (id) => request(`/api/import/pdf/${id}/preview`),
  compare: (fileId) => request(`/api/import/compare/${fileId}`),
  compareChoice: (fileId, choice) => post(`/api/import/compare/${fileId}/choice`, { choice }),
  coversMissing: () => request("/api/covers/missing"),
  coversFix: () => post("/api/covers/fix"),
  settings: () => request("/api/settings"),
  saveSettings: (body) =>
    request("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
};

/** The search text as a bare ISBN (digits/X only), or null. The server does the checksum validation. */
export function isbnFromQuery(q) {
  const s = (q || "").trim().replace(/^isbn(?:-?1[03])?\s*:?\s*/i, "");
  if (!s || /[^0-9Xx\-\s]/.test(s)) return null;
  const bare = s.replace(/[^0-9Xx]/g, "");
  return bare.length === 10 || bare.length === 13 ? bare : null;
}

// MARC language codes, as Open Library uses them. The most common ones in the catalog, roughly.
export const LANGUAGES = [
  ["eng", "English"], ["spa", "Spanish"], ["fre", "French"], ["ger", "German"], ["ita", "Italian"],
  ["por", "Portuguese"], ["dut", "Dutch"], ["rus", "Russian"], ["pol", "Polish"], ["swe", "Swedish"],
  ["dan", "Danish"], ["nor", "Norwegian"], ["fin", "Finnish"], ["jpn", "Japanese"], ["chi", "Chinese"],
  ["kor", "Korean"], ["ara", "Arabic"], ["heb", "Hebrew"], ["lat", "Latin"], ["gre", "Greek, Modern"],
];
export const languageName = (code) => LANGUAGES.find(([c]) => c === code)?.[1] || code;

export function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export const formatNumber = (n) => (n ?? 0).toLocaleString();
