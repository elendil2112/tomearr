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
  searchWorks: (q) => request(`/api/search/works?q=${encodeURIComponent(q)}`),
  author: (key) => request(`/api/authors/${encodeURIComponent(key)}`),
};

export function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export const formatNumber = (n) => (n ?? 0).toLocaleString();
