import { withBasePath } from "./basePath";

const KEY = `omnigent.activeProfile:${withBasePath("/")}`;

export function activeProfileId(): string {
  const fromUrl = new URLSearchParams(window.location.search).get("profile_id");
  if (fromUrl) {
    try {
      sessionStorage.setItem(KEY, fromUrl);
    } catch {
      /* URL selection remains available. */
    }
    return fromUrl;
  }
  try {
    return sessionStorage.getItem(KEY) || "default";
  } catch {
    return "default";
  }
}

export function profileRoute(id: string): string {
  return `${withBasePath("/")}?profile_id=${encodeURIComponent(id)}`;
}

export function selectProfile(id: string): void {
  try {
    sessionStorage.setItem(KEY, id);
  } catch {
    /* The URL still preserves selection. */
  }
  window.location.assign(profileRoute(id));
}
