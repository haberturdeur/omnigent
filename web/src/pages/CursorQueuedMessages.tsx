import { ArrowUpIcon, ClockIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useCursorQueue } from "@/lib/cursorQueue";
import { cn } from "@/lib/utils";

export function CursorQueuedMessages({
  sessionId,
  widthClassName,
  readOnly = false,
}: {
  sessionId: string;
  widthClassName?: string;
  readOnly?: boolean;
}) {
  const { query, update } = useCursorQueue(sessionId);
  const queue = query.data;

  // Retrying an initial failure becomes pending before availability is known.
  if (query.isError || (query.isPending && query.errorUpdatedAt > 0)) {
    return (
      <p role="status" className="text-center text-sm text-muted-foreground">
        Cursor’s queue is unavailable.
      </p>
    );
  }
  if (!queue?.messages.length) return null;

  return (
    <div
      data-testid="cursor-queued-strip"
      className={cn(
        "mx-auto -mb-4 flex w-full flex-col rounded-t-2xl bg-tray/40 px-4 pt-1.5 pb-5.5",
        widthClassName,
      )}
    >
      <div
        className="flex max-h-32 flex-col gap-1 overflow-y-auto"
        aria-label="Cursor queued messages"
      >
        {queue.messages.map((message) => (
          <div
            key={message.index}
            className="flex items-center gap-1.5 text-sm text-muted-foreground"
          >
            <ClockIcon className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate">{message.text}</span>
            {!readOnly && message.index === 0 && queue.can_steer && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label="Steer queued message"
                disabled={update.isPending}
                onClick={() =>
                  update.mutate(
                    { action: "steer_queue", revision: queue.revision, index: message.index },
                    {
                      onError: (error) => toast.error(error.message),
                    },
                  )
                }
              >
                <ArrowUpIcon className="size-3.5" aria-hidden="true" />
                Steer
              </Button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
