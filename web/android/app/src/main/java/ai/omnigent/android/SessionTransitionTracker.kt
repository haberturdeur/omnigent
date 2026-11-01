package ai.omnigent.android

/** Minimal session-list state consumed by the background notification monitor. */
data class MonitoredSession(
    val id: String,
    val title: String?,
    val status: String,
    val pendingElicitations: Int,
)

/** Opaque identifiers needed to resolve a binary approval from a notification. */
data class NotificationApproval(
    val instance: String,
    val sessionId: String,
    val elicitationId: String,
    val description: String? = null,
    val persistent: PersistentAction? = null,
) {
    enum class PersistentAction {
        ALLOW_ALL_EDITS,
        REMEMBER,
    }
}

/** A user-visible transition detected between server snapshots. */
data class SessionAttentionEvent(
    val session: MonitoredSession,
    val kind: Kind,
    val approval: NotificationApproval? = null,
    val notificationId: String? = null,
) {
    enum class Kind {
        NEEDS_INPUT,
        COMPLETED,
        FAILED,
    }
}

/**
 * Diffs session-list snapshots without notifying for state present at startup.
 *
 * Turn completion is confirmed by a second terminal snapshot. Omnigent agents
 * can briefly move from running to idle between steps; waiting one poll keeps
 * those internal boundaries from producing a notification for every step.
 */
class SessionTransitionTracker {
    private var previous: Map<String, MonitoredSession>? = null
    private val pendingCompletions = mutableMapOf<String, MonitoredSession>()

    fun reset() {
        previous = null
        pendingCompletions.clear()
    }

    fun observe(sessions: List<MonitoredSession>): List<SessionAttentionEvent> {
        val current = sessions.associateBy(MonitoredSession::id)
        val prior = previous
        if (prior == null) {
            previous = current
            return emptyList()
        }

        val events = mutableListOf<SessionAttentionEvent>()
        val needsInputIds =
            sessions
                .filter { session ->
                    val before = prior[session.id]
                    before != null && session.pendingElicitations > before.pendingElicitations
                }.mapTo(mutableSetOf(), MonitoredSession::id)

        // Confirm completion candidates from the preceding poll. A session that
        // resumed work is no longer complete; a removed session is also pruned.
        for (id in pendingCompletions.keys.toList()) {
            val now = current[id]
            pendingCompletions.remove(id)
            if (now != null && now.status in TERMINAL_STATUSES && id !in needsInputIds) {
                events +=
                    SessionAttentionEvent(
                        session = now,
                        kind =
                            if (now.status == "failed") {
                                SessionAttentionEvent.Kind.FAILED
                            } else {
                                SessionAttentionEvent.Kind.COMPLETED
                            },
                    )
            }
        }

        for (session in sessions) {
            val before = prior[session.id] ?: continue
            val needsInput = session.id in needsInputIds
            if (needsInput) {
                // An elicitation is immediately actionable and supersedes a
                // generic completion cue for the same turn.
                pendingCompletions.remove(session.id)
                events += SessionAttentionEvent(session, SessionAttentionEvent.Kind.NEEDS_INPUT)
            } else if (before.status == "running" && session.status in TERMINAL_STATUSES) {
                pendingCompletions[session.id] = session
            }
        }

        previous = current
        return events
    }

    private companion object {
        val TERMINAL_STATUSES = setOf("idle", "failed")
    }
}
