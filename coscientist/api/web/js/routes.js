/** Explicit pages; a launch form is never the study catalog. */
export const studyTabs = new Set(["activity", "plan", "graph", "results", "docs", "hitl", "events", "tools", "literature", "code"]);
export function studyRoute(id, tab = "activity") {
  return `#/study/${encodeURIComponent(id)}/${studyTabs.has(tab) ? tab : "activity"}`;
}
export function parseRoute(hash) {
  if (hash === "#/new") return {page: "new"};
  if (hash === "#/settings/agents") return {page: "settings", section: "agents"};
  if (hash === "#/settings/mcp") return {page: "settings", section: "mcp"};
  const match = /^#\/study\/([^/]+)(?:\/([a-z]+))?$/.exec(hash);
  if (match) {
    try { return {page: "study", id: decodeURIComponent(match[1]), tab: studyTabs.has(match[2]) ? match[2] : "activity"}; }
    catch { return {page: "runs"}; }
  }
  return {page: "runs"};
}
