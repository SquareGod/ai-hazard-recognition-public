export type Project = { id: string; name: string };

export const DEFAULT_PROJECT: Project = { id: "demo-construction-project", name: "示例建设项目" };
const STORAGE_KEY = "hazard-console.project-id";
const EVENT_NAME = "hazard-console:project-change";

export function getSelectedProjectId() {
  if (typeof window === "undefined") return DEFAULT_PROJECT.id;
  return window.localStorage.getItem(STORAGE_KEY) || DEFAULT_PROJECT.id;
}

export function selectProject(projectId: string) {
  window.localStorage.setItem(STORAGE_KEY, projectId);
  window.dispatchEvent(new CustomEvent<Project>(EVENT_NAME, { detail: { id: projectId, name: "" } }));
}

export function onProjectChange(listener: (project: Project) => void) {
  const handler = (event: Event) => listener((event as CustomEvent<Project>).detail);
  window.addEventListener(EVENT_NAME, handler);
  return () => window.removeEventListener(EVENT_NAME, handler);
}
