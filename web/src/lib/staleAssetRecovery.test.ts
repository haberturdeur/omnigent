import { beforeEach, describe, expect, it, vi } from "vitest";
import { installStaleAssetRecovery } from "./staleAssetRecovery";

describe("installStaleAssetRecovery", () => {
  beforeEach(() => sessionStorage.clear());

  it("keys recovery to the currently loaded module entry", () => {
    const entry = document.createElement("script");
    entry.type = "module";
    entry.src = "/assets/index-deployment-a.js";
    document.head.appendChild(entry);
    const reload = vi.fn();
    const remove = installStaleAssetRecovery({ reload });

    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));

    expect(reload).toHaveBeenCalledOnce();
    remove();
    entry.remove();
  });

  it("reloads and suppresses the first stale dynamic-import failure", () => {
    const reload = vi.fn();
    const remove = installStaleAssetRecovery({ reload, buildId: "index-old.js" });
    const event = new Event("vite:preloadError", { cancelable: true });
    Object.assign(event, {
      payload: new TypeError(
        "error loading dynamically imported module: /assets/MonacoCodeEditor-old.js",
      ),
    });

    window.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(reload).toHaveBeenCalledOnce();
    remove();
  });

  it("surfaces a second failure without reloading the same bundle again", () => {
    const reload = vi.fn();
    const remove = installStaleAssetRecovery({ reload, buildId: "index-broken.js" });
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    reload.mockClear();
    const second = new Event("vite:preloadError", { cancelable: true });

    window.dispatchEvent(second);

    expect(second.defaultPrevented).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    remove();
  });

  it("allows one recovery reload for a later deployment", () => {
    const firstReload = vi.fn();
    const removeFirst = installStaleAssetRecovery({
      reload: firstReload,
      buildId: "index-prior-deployment.js",
    });
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    removeFirst();

    const nextReload = vi.fn();
    const removeNext = installStaleAssetRecovery({
      reload: nextReload,
      buildId: "index-later-deployment.js",
    });
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));

    expect(firstReload).toHaveBeenCalledOnce();
    expect(nextReload).toHaveBeenCalledOnce();
    removeNext();
  });

  it("falls back to a page guard when storage operations are denied", () => {
    const reload = vi.fn();
    const deniedStorage = {
      getItem: vi.fn(() => {
        throw new DOMException("Access denied", "SecurityError");
      }),
      setItem: vi.fn(() => {
        throw new DOMException("Access denied", "SecurityError");
      }),
    } as unknown as Storage;
    const remove = installStaleAssetRecovery({
      reload,
      buildId: "index-storage-denied.js",
      storage: deniedStorage,
    });

    const first = new Event("vite:preloadError", { cancelable: true });
    const second = new Event("vite:preloadError", { cancelable: true });
    expect(() => {
      window.dispatchEvent(first);
      window.dispatchEvent(second);
    }).not.toThrow();
    expect(first.defaultPrevented).toBe(true);
    expect(second.defaultPrevented).toBe(false);
    expect(reload).toHaveBeenCalledOnce();
    remove();
  });

  it("does not abort installation when acquiring session storage is denied", () => {
    const reload = vi.fn();
    const eventTarget = new EventTarget();
    Object.defineProperty(eventTarget, "sessionStorage", {
      get: () => {
        throw new DOMException("Access denied", "SecurityError");
      },
    });
    const target = eventTarget as unknown as Window;

    const remove = installStaleAssetRecovery({
      target,
      reload,
      buildId: "index-opaque-context.js",
    });
    const first = new Event("vite:preloadError", { cancelable: true });
    const second = new Event("vite:preloadError", { cancelable: true });
    eventTarget.dispatchEvent(first);
    eventTarget.dispatchEvent(second);

    expect(first.defaultPrevented).toBe(true);
    expect(second.defaultPrevented).toBe(false);
    expect(reload).toHaveBeenCalledOnce();
    remove();
  });
});
