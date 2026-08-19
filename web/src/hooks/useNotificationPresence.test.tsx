import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchMock, dismissMock } = vi.hoisted(() => ({
  fetchMock: vi.fn(),
  dismissMock: vi.fn(),
}));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));
vi.mock("@/lib/nativeBridge", () => ({
  dismissNativeSessionNotifications: dismissMock,
  isAndroidShell: () => false,
  isElectronShell: () => false,
  isIOSShell: () => false,
}));

import { useNotificationPresence } from "./useNotificationPresence";

describe("useNotificationPresence", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    dismissMock.mockReset();
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    window.localStorage.clear();
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  it("reports active desktop presence and acknowledges the open session", async () => {
    const { unmount } = renderHook(() => useNotificationPresence("session-1"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const activity = fetchMock.mock.calls.find(([url]) => String(url).includes("/push/activity/"));
    expect(JSON.parse(String(activity?.[1]?.body))).toEqual({
      platform: "web",
      foreground: true,
      active: true,
      mobile_delay_seconds: 60,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/push/acknowledgements",
      expect.objectContaining({ body: JSON.stringify({ session_id: "session-1" }) }),
    );
    expect(dismissMock).toHaveBeenCalledWith("session-1");
    unmount();
  });

  it("retries a failed acknowledgement and keeps a bounded visible heartbeat", async () => {
    vi.useFakeTimers();
    let acknowledgementAttempts = 0;
    fetchMock.mockImplementation((url: string) => {
      if (url === "/v1/push/acknowledgements") {
        acknowledgementAttempts += 1;
        if (acknowledgementAttempts === 1) return Promise.reject(new Error("offline"));
      }
      return Promise.resolve(new Response(null, { status: 204 }));
    });

    const { unmount } = renderHook(() => useNotificationPresence("session-1"));
    await act(async () => Promise.resolve());
    expect(acknowledgementAttempts).toBe(1);

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(acknowledgementAttempts).toBe(2);

    await act(async () => vi.advanceTimersByTimeAsync(25_000));
    expect(acknowledgementAttempts).toBe(3);
    expect(dismissMock).toHaveBeenCalledTimes(3);

    unmount();
    vi.useRealTimers();
  });
});
