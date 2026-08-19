const RELOAD_MARKER = "omnigent:stale-asset-reload";

interface RecoveryOptions {
  target?: Window;
  storage?: Storage | null;
  buildId?: string;
  reload?: () => void;
}

const pageReloadAttempts = new WeakMap<Window, Set<string>>();

function availableStorage(target: Window, configured?: Storage | null): Storage | null {
  if (configured !== undefined) return configured;
  try {
    return target.sessionStorage;
  } catch {
    return null;
  }
}

function wasReloadAttempted(storage: Storage | null, buildId: string): boolean {
  if (!storage) return false;
  try {
    return storage.getItem(RELOAD_MARKER) === buildId;
  } catch {
    return false;
  }
}

function rememberReloadAttempt(storage: Storage | null, buildId: string): void {
  if (!storage) return;
  try {
    storage.setItem(RELOAD_MARKER, buildId);
  } catch {
    // Storage can be disabled even when obtaining the object succeeds.
  }
}

function loadedEntryBundle(target: Window): string {
  const entries = Array.from(
    target.document.querySelectorAll<HTMLScriptElement>('script[type="module"][src]'),
    (script) => script.src,
  );
  return entries.join("\n") || target.location.pathname;
}

/** Reload once when a deployment removes a chunk referenced by this tab. */
export function installStaleAssetRecovery(options: RecoveryOptions = {}): () => void {
  const target = options.target ?? window;
  const storage = availableStorage(target, options.storage);
  const buildId = options.buildId ?? loadedEntryBundle(target);
  const reload = options.reload ?? (() => target.location.reload());
  let attempts = pageReloadAttempts.get(target);
  if (!attempts) {
    attempts = new Set();
    pageReloadAttempts.set(target, attempts);
  }

  const onPreloadError = (event: Event) => {
    if (attempts.has(buildId) || wasReloadAttempted(storage, buildId)) return;
    // Suppress only the failure recovered by reloading. Later failures must surface.
    event.preventDefault();
    attempts.add(buildId);
    rememberReloadAttempt(storage, buildId);
    reload();
  };

  target.addEventListener("vite:preloadError", onPreloadError);
  return () => target.removeEventListener("vite:preloadError", onPreloadError);
}
