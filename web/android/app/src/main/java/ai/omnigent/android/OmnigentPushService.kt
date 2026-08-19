package ai.omnigent.android

import android.widget.Toast
import org.json.JSONObject
import org.unifiedpush.android.connector.FailedReason
import org.unifiedpush.android.connector.PushService
import org.unifiedpush.android.connector.data.PushEndpoint
import org.unifiedpush.android.connector.data.PushMessage

/** Receives decrypted RFC 8291 payloads from the installed UnifiedPush distributor. */
class OmnigentPushService : PushService() {
    override fun onMessage(message: PushMessage, instance: String) {
        if (!message.decrypted) return
        val registration = PushRegistrationManager(this)
        if (!registration.isEnabled(instance)) return
        runCatching {
            val payload = JSONObject(message.content.toString(Charsets.UTF_8))
            val type = payload.getString("type")
            val notificationId = payload.optString("notification_id").ifBlank { null }
            if (type == "notification.dismissed") {
                NativeNotificationManager(applicationContext).dismissSessionNotification(
                    sessionId = payload.getString("session_id"),
                    notificationId = notificationId,
                )
                return
            }
            if (SessionMonitorStore.appVisible) return
            val kind =
                when (type) {
                    "session.needs_input" -> SessionAttentionEvent.Kind.NEEDS_INPUT
                    "session.completed" -> SessionAttentionEvent.Kind.COMPLETED
                    "session.failed" -> SessionAttentionEvent.Kind.FAILED
                    else -> return
                }
            val approval =
                if (kind == SessionAttentionEvent.Kind.NEEDS_INPUT) {
                    payload.optJSONObject("approval")?.let { parseApproval(it, instance) }
                } else {
                    null
                }
            NativeNotificationManager(applicationContext).notify(
                SessionAttentionEvent(
                    MonitoredSession(
                        id = payload.getString("session_id"),
                        title = payload.optString("title").ifBlank { null },
                        status = "idle",
                        pendingElicitations =
                            if (kind == SessionAttentionEvent.Kind.NEEDS_INPUT) 1 else 0,
                    ),
                    kind = kind,
                    approval = approval,
                    notificationId = notificationId,
                ),
                serverUrl = registration.serverUrlFor(instance),
            )
        }
    }

    override fun onNewEndpoint(endpoint: PushEndpoint, instance: String) {
        if (!PushRegistrationManager(this).storeAndUpload(endpoint, instance)) {
            Toast.makeText(this, R.string.push_webpush_required, Toast.LENGTH_LONG).show()
        }
    }

    override fun onRegistrationFailed(reason: FailedReason, instance: String) {
        PushRegistrationManager(this).markRegistrationFailed(instance)
        Toast.makeText(this, R.string.push_registration_failed, Toast.LENGTH_LONG).show()
    }

    override fun onUnregistered(instance: String) {
        PushRegistrationManager(this).markUnregistered(instance)
    }

    private fun parseApproval(value: JSONObject, instance: String): NotificationApproval? {
        val sessionId = value.optString("session_id").takeIf { it.isNotBlank() } ?: return null
        val elicitationId =
            value.optString("elicitation_id").takeIf { it.isNotBlank() } ?: return null
        val persistent =
            when (value.optString("persistent")) {
                "allow_all_edits" -> NotificationApproval.PersistentAction.ALLOW_ALL_EDITS
                "remember" -> NotificationApproval.PersistentAction.REMEMBER
                else -> null
            }
        return NotificationApproval(
            instance = instance,
            sessionId = sessionId,
            elicitationId = elicitationId,
            description = value.optString("description").ifBlank { null },
            persistent = persistent,
        )
    }
}
