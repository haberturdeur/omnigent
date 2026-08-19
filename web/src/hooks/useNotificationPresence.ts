import { useEffect, useRef } from "react";
import { authenticatedFetch } from "@/lib/identity";
import {
  dismissNativeSessionNotifications,
  isAndroidShell,
  isElectronShell,
  isIOSShell,
} from "@/lib/nativeBridge";

const DEVICE_ID_KEY = "omnigent:notification-device-id";
const MOBILE_DELAY_SECONDS = 60;
const HEARTBEAT_MS = 25_000;
const ACK_RETRY_INITIAL_MS = 1_000;

type NotificationPlatform = "web" | "electron" | "android" | "ios";
let volatileDeviceId: string | undefined;

function platform(): NotificationPlatform {
  if (isAndroidShell()) return "android";
  if (isIOSShell()) return "ios";
  if (isElectronShell()) return "electron";
  return "web";
}

function deviceId(): string {
  try {
    const existing = window.localStorage.getItem(DEVICE_ID_KEY);
    if (existing) return existing;
    const value = `web-${crypto.randomUUID()}`;
    window.localStorage.setItem(DEVICE_ID_KEY, value);
    return value;
  } catch {
    volatileDeviceId ??= `web-${crypto.randomUUID()}`;
    return volatileDeviceId;
  }
}

async function post(path: string, body: unknown): Promise<boolean> {
  try {
    const response = await authenticatedFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Keep the server informed about the foreground client that currently owns attention. */
export function useNotificationPresence(activeSessionId?: string): void {
  const id = useRef<string | undefined>(undefined);
  if (id.current === undefined) id.current = deviceId();

  useEffect(() => {
    const sendActivity = (forceInactive = false, activityPulse = false) => {
      const foreground = !forceInactive && document.visibilityState === "visible";
      void post(`/v1/push/activity/${encodeURIComponent(id.current!)}`, {
        platform: platform(),
        foreground,
        active: foreground && document.hasFocus() && activityPulse,
        mobile_delay_seconds: MOBILE_DELAY_SECONDS,
      });
    };
    const interacted = () => {
      sendActivity(false, true);
    };
    const visibilityChanged = () => sendActivity(false, document.visibilityState === "visible");
    const pageHidden = () => sendActivity(true);

    sendActivity(false, true);
    const interval = window.setInterval(sendActivity, HEARTBEAT_MS);
    window.addEventListener("focus", interacted);
    window.addEventListener("pointerdown", interacted, { passive: true });
    window.addEventListener("keydown", interacted);
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("pagehide", pageHidden);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", interacted);
      window.removeEventListener("pointerdown", interacted);
      window.removeEventListener("keydown", interacted);
      document.removeEventListener("visibilitychange", visibilityChanged);
      window.removeEventListener("pagehide", pageHidden);
      pageHidden();
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    let retryMs = ACK_RETRY_INITIAL_MS;
    let timer: number | undefined;

    const eligible = () =>
      Boolean(activeSessionId) && document.visibilityState === "visible" && document.hasFocus();
    const schedule = (delay: number) => {
      if (stopped || !eligible()) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(acknowledge, delay);
    };
    const acknowledge = () => {
      timer = undefined;
      const sessionId = activeSessionId;
      if (!sessionId || !eligible() || inFlight) return;
      inFlight = true;
      dismissNativeSessionNotifications(sessionId);
      void post("/v1/push/acknowledgements", { session_id: sessionId }).then((ok) => {
        inFlight = false;
        if (stopped || !eligible()) return;
        if (ok) {
          retryMs = ACK_RETRY_INITIAL_MS;
          schedule(HEARTBEAT_MS);
          return;
        }
        schedule(retryMs);
        retryMs = Math.min(retryMs * 2, HEARTBEAT_MS);
      });
    };
    const stateChanged = () => {
      retryMs = ACK_RETRY_INITIAL_MS;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
      acknowledge();
    };
    acknowledge();
    window.addEventListener("focus", stateChanged);
    document.addEventListener("visibilitychange", stateChanged);
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      window.removeEventListener("focus", stateChanged);
      document.removeEventListener("visibilitychange", stateChanged);
    };
  }, [activeSessionId]);
}
