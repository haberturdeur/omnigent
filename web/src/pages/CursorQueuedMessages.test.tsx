import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { CursorQueuedMessages } from "./CursorQueuedMessages";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
});

function mount(readOnly = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          throw new Error("Steering submitted the composer form");
        }}
      >
        <CursorQueuedMessages sessionId="cursor-session" readOnly={readOnly} />
      </form>
    </QueryClientProvider>,
  );
  return client;
}

const snapshot = {
  revision: "native-1",
  can_steer: true,
  messages: [
    { index: 0, text: "change direction" },
    { index: 1, text: "later" },
  ],
};

describe("Cursor's native queue", () => {
  it("steers the native head without resubmitting text and refreshes the native queue", async () => {
    let queue = snapshot;
    const requests: RequestInit[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (url, init) => {
      expect(url).toBe("/v1/sessions/cursor-session/cursor-queue");
      if (init?.method === "POST") {
        requests.push(init);
        queue = { revision: "native-2", can_steer: false, messages: [{ index: 0, text: "later" }] };
        return Response.json({ status: "submitted" });
      }
      return Response.json(queue);
    });
    mount();
    await screen.findByText("change direction");
    expect(screen.getAllByRole("button", { name: "Steer queued message" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Steer queued message" }));
    await waitFor(() => expect(screen.queryByText("change direction")).toBeNull());
    expect(screen.getByText("later")).toBeInTheDocument();
    expect(JSON.parse(requests[0]!.body as string)).toEqual({
      action: "steer_queue",
      index: 0,
      revision: "native-1",
    });
    expect(requests).toHaveLength(1);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("reflects terminal queue removal and never offers steering to a read-only viewer", async () => {
    let queue = snapshot;
    vi.mocked(authenticatedFetch).mockImplementation(async () => Response.json(queue));
    const client = mount(true);
    await screen.findByText("change direction");
    expect(screen.queryByRole("button")).toBeNull();
    queue = { ...snapshot, revision: "native-2", messages: [] };
    await client.invalidateQueries({ queryKey: ["cursor-queue", "cursor-session"] });
    await waitFor(() => expect(screen.queryByTestId("cursor-queued-strip")).toBeNull());
  });

  it("refreshes after a stale action is rejected and hides controls when native reads fail", async () => {
    let unavailable = false;
    vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) => {
      if (init?.method === "POST") {
        unavailable = true;
        return Response.json({ error: "Cursor queue changed" }, { status: 409 });
      }
      return unavailable
        ? Response.json({ error: "offline" }, { status: 503 })
        : Response.json(snapshot);
    });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Steer queued message" }));
    await screen.findByRole("status");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
