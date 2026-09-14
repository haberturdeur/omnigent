import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "./identity";

export interface CursorQueue {
  revision: string;
  messages: { index: number; text: string }[];
  can_steer: boolean;
}

export interface CursorQueueAction {
  action: "steer_queue";
  revision: string;
  index: number;
}

async function queueRequest<T>(sessionId: string, action?: CursorQueueAction): Promise<T> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/cursor-queue`,
    action
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(action),
        }
      : undefined,
  );
  const body = await response.json();
  if (!response.ok) throw new Error(body.error ?? body.detail ?? "Cursor queue is unavailable");
  return body;
}

export function useCursorQueue(sessionId: string) {
  const client = useQueryClient();
  const queryKey = ["cursor-queue", sessionId];
  const query = useQuery({
    queryKey,
    queryFn: () => queueRequest<CursorQueue>(sessionId),
    refetchInterval: 1000,
    retry: false,
  });
  const update = useMutation({
    mutationFn: (action: CursorQueueAction) => queueRequest<void>(sessionId, action),
    onSettled: () => client.invalidateQueries({ queryKey }),
  });
  return { query, update };
}
