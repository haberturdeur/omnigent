package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.util.concurrent.atomic.AtomicInteger

/**
 * Local (foreground) notifications + best-effort badge, mirroring the iOS
 * `NativeNotificationManager`. Tap routing forwards the notification's
 * `navigatePath` back into the SPA: the tap launches [MainActivity] with the
 * path as an intent extra, which the activity replays via
 * `window.__omnigentNativeEmitNotificationActivated`.
 *
 * Posting tolerates a missing `POST_NOTIFICATIONS` grant (requested by
 * [MainActivity] on API 33+): [post] drops silently if disabled or revoked, so
 * the web layer keeps working without OS toasts.
 */
class NativeNotificationManager(
    private val context: Context,
) {
    private val manager = NotificationManagerCompat.from(context)

    // IDs 1 and 2 are reserved for the badge summary and foreground monitor.
    private val nextId = AtomicInteger(FIRST_TRANSIENT_NOTIFICATION_ID)

    // Last badge state from the web layer, kept so a grant of the API 33+
    // notification permission can replay a badge that was computed (and
    // deduped web-side) while the permission dialog was still open.
    private data class BadgeState(
        val count: Int,
        val navigatePath: String?,
        val title: String?,
        val body: String?,
    )

    @Volatile
    private var lastBadge: BadgeState? = null

    init {
        val channel =
            NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_HIGH,
            )
        manager.createNotificationChannel(channel)
    }

    fun notify(
        title: String,
        body: String?,
        navigatePath: String?,
    ) {
        if (webNotificationsSuppressed()) return
        val id = nextId.getAndIncrement()
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body.orEmpty())
                .setAutoCancel(true)
                .setDefaults(NotificationCompat.DEFAULT_ALL)

        if (navigatePath != null && navigatePath.startsWith("/")) {
            builder.setContentIntent(activationIntent(navigatePath, id))
        }

        post(id, builder.build())
    }

    /** Post or replace the background monitor's notification for one session. */
    fun notify(
        event: SessionAttentionEvent,
        serverUrl: String? = null,
    ) {
        val session = event.session
        val exactId = event.notificationId
        val deliveryKey =
            exactId?.let { "id:$it" }
                ?: "fallback:${session.id}:${event.kind.name}"
        val deliveryWindow = if (exactId == null) DELIVERY_DEDUPE_MS else EXACT_ID_RETENTION_MS
        val deliveryClaim = claimDelivery(deliveryKey, deliveryWindow) ?: return
        val body =
            event.approval?.description
                ?: context.getString(
                    when (event.kind) {
                        SessionAttentionEvent.Kind.NEEDS_INPUT -> R.string.notification_needs_input
                        SessionAttentionEvent.Kind.COMPLETED -> R.string.notification_completed
                        SessionAttentionEvent.Kind.FAILED -> R.string.notification_failed
                    },
                )
        val requestCode = (session.id.hashCode() and 0x3fffffff) + FIRST_SESSION_REQUEST_CODE
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(session.title ?: context.getString(R.string.notification_session))
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setContentIntent(
                    activationIntent(
                        "/c/${session.id}",
                        requestCode,
                        serverUrl = serverUrl,
                    ),
                )
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setDefaults(NotificationCompat.DEFAULT_ALL)
        event.approval?.let { approval ->
            builder.addAction(
                R.drawable.ic_notification,
                context.getString(R.string.approval_action_approve),
                approvalIntent(approval, ApprovalActionReceiver.ACTION_APPROVE),
            )
            approval.persistent?.let { persistent ->
                val action =
                    when (persistent) {
                        NotificationApproval.PersistentAction.ALLOW_ALL_EDITS ->
                            ApprovalActionReceiver.ACTION_ALLOW_ALL_EDITS
                        NotificationApproval.PersistentAction.REMEMBER ->
                            ApprovalActionReceiver.ACTION_REMEMBER
                    }
                builder.addAction(
                    R.drawable.ic_notification,
                    context.getString(R.string.approval_action_always),
                    approvalIntent(approval, action),
                )
            }
            builder.addAction(
                R.drawable.ic_notification,
                context.getString(R.string.approval_action_reject),
                approvalIntent(approval, ApprovalActionReceiver.ACTION_REJECT),
            )
        }
        if (!post("session:${session.id}", EVENT_NOTIFICATION_ID, builder.build())) {
            releaseDelivery(deliveryKey, deliveryClaim)
        }
    }

    /**
     * Android has no universal numeric icon badge, so the count is surfaced as a
     * lightweight summary notification (its `setNumber()` is shown by some
     * launchers; AOSP shows only a dot). Because that notification is often the
     * ONLY thing the user sees, it must be actionable and descriptive: when the
     * web layer supplies a [navigatePath] the tap opens the app and routes there
     * (one waiting session → that session; several → the inbox), and [title] /
     * [body] describe what's waiting instead of a bare "N pending". Older web
     * builds omit these, so we fall back to the app name + "N pending" and no
     * tap intent — the prior behavior.
     *
     * A count of 0 withdraws the summary: the badge notification is the count
     * surface, so once nothing is pending it must not linger as a stale,
     * still-tappable "N sessions need your attention" routing to resolved work.
     */
    fun setBadgeCount(
        count: Int,
        navigatePath: String? = null,
        title: String? = null,
        body: String? = null,
    ) {
        lastBadge = BadgeState(count, navigatePath, title, body)
        if (webNotificationsSuppressed()) {
            manager.cancel(BADGE_NOTIFICATION_ID)
            return
        }
        if (count <= 0) {
            manager.cancel(BADGE_NOTIFICATION_ID)
            return
        }
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title ?: context.getString(R.string.app_name))
                .setContentText(
                    body ?: context.resources.getQuantityString(R.plurals.badge_text, count, count),
                ).setNumber(count)
                .setSilent(true)
                .setOngoing(false)
        if (navigatePath != null && navigatePath.startsWith("/")) {
            // Tap opens the app and routes. Deliberately NOT setAutoCancel: this
            // is an ambient count, not a one-off event — clearing it on tap would
            // drop the only Android count surface while sessions are still
            // pending, and a later poll with the same count won't repost it.
            builder.setContentIntent(activationIntent(navigatePath, BADGE_NOTIFICATION_ID))
        }
        post(BADGE_NOTIFICATION_ID, builder.build())
    }

    /**
     * Re-post the last badge the web layer sent. Called when the user grants
     * the notification permission: a badge posted before the grant was
     * silently dropped, and the web side won't resend an unchanged state.
     */
    fun replayBadge() {
        val badge = lastBadge ?: return
        setBadgeCount(badge.count, badge.navigatePath, badge.title, badge.body)
    }

    internal fun dismissSessionNotification(
        sessionId: String,
        notificationId: String?,
    ) {
        manager.cancel("session:$sessionId", EVENT_NOTIFICATION_ID)
        notificationId?.let { rememberDelivery("id:$it") }
        rememberDelivery("fallback:$sessionId:${SessionAttentionEvent.Kind.NEEDS_INPUT.name}")
    }

    private fun webNotificationsSuppressed(): Boolean =
        SessionMonitorStore.appVisible || PushRegistrationManager(context).registered

    /**
     * Post a notification, tolerating a missing notification grant. The
     * `POST_NOTIFICATIONS` permission is revocable on API 33+, so `notify` can
     * throw `SecurityException` even after `areNotificationsEnabled()` — we drop
     * silently rather than crash.
     */
    private fun post(
        id: Int,
        notification: Notification,
    ) {
        if (!manager.areNotificationsEnabled()) return
        try {
            manager.notify(id, notification)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted — drop; web falls back.
        }
    }

    private fun post(
        tag: String,
        id: Int,
        notification: Notification,
    ): Boolean {
        if (!manager.areNotificationsEnabled()) return false
        return try {
            manager.notify(tag, id, notification)
            true
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted — drop; web falls back.
            false
        }
    }

    private fun claimDelivery(
        key: String,
        dedupeWindowMs: Long,
    ): Long? {
        if (!manager.areNotificationsEnabled()) return null
        synchronized(DELIVERY_LOCK) {
            val prefs = context.getSharedPreferences(DELIVERY_PREFS, Context.MODE_PRIVATE)
            val now = System.currentTimeMillis()
            val storedKey = "$DELIVERY_PREFIX$key"
            val previous = prefs.getLong(storedKey, 0L)
            if (previous > 0L && now - previous in 0 until dedupeWindowMs) return null
            prefs.edit().putLong(storedKey, now).commit()
            return now
        }
    }

    private fun rememberDelivery(key: String) {
        synchronized(DELIVERY_LOCK) {
            context
                .getSharedPreferences(DELIVERY_PREFS, Context.MODE_PRIVATE)
                .edit()
                .putLong("$DELIVERY_PREFIX$key", System.currentTimeMillis())
                .commit()
        }
    }

    private fun releaseDelivery(key: String, claim: Long) {
        synchronized(DELIVERY_LOCK) {
            val prefs = context.getSharedPreferences(DELIVERY_PREFS, Context.MODE_PRIVATE)
            val storedKey = "$DELIVERY_PREFIX$key"
            if (prefs.getLong(storedKey, 0L) == claim) prefs.edit().remove(storedKey).commit()
        }
    }

    // requestCode is the notification's own id, so each notification gets a
    // distinct PendingIntent — otherwise FLAG_UPDATE_CURRENT would let two paths
    // with colliding hashes overwrite each other's extras and mis-route a tap.
    private fun activationIntent(
        navigatePath: String,
        requestCode: Int,
        serverUrl: String? = null,
    ): PendingIntent {
        val intent =
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_NAVIGATE_PATH, navigatePath)
                serverUrl?.let { putExtra(EXTRA_SERVER_URL, it) }
            }
        return PendingIntent.getActivity(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun approvalIntent(
        approval: NotificationApproval,
        action: String,
    ): PendingIntent {
        val intent =
            Intent(context, ApprovalActionReceiver::class.java).apply {
                this.action = "ai.omnigent.android.approval.$action"
                putExtra(ApprovalActionReceiver.EXTRA_INSTANCE, approval.instance)
                putExtra(ApprovalActionReceiver.EXTRA_SESSION_ID, approval.sessionId)
                putExtra(ApprovalActionReceiver.EXTRA_ELICITATION_ID, approval.elicitationId)
                putExtra(ApprovalActionReceiver.EXTRA_ACTION, action)
            }
        val requestCode = ("${approval.elicitationId}:$action".hashCode() and 0x3fffffff)
        return PendingIntent.getBroadcast(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or
                PendingIntent.FLAG_IMMUTABLE or
                PendingIntent.FLAG_ONE_SHOT,
        )
    }

    companion object {
        const val EXTRA_NAVIGATE_PATH = "ai.omnigent.android.NAVIGATE_PATH"
        const val EXTRA_SERVER_URL = "ai.omnigent.android.SERVER_URL"
        private const val CHANNEL_ID = "omnigent.sessions"
        private const val BADGE_NOTIFICATION_ID = 1
        private const val EVENT_NOTIFICATION_ID = 3
        private const val FIRST_TRANSIENT_NOTIFICATION_ID = 4
        private const val FIRST_SESSION_REQUEST_CODE = 1_000
        private const val DELIVERY_PREFS = "ai.omnigent.android.notification_deliveries"
        private const val DELIVERY_PREFIX = "delivered."
        private const val DELIVERY_DEDUPE_MS = 30_000L
        private const val EXACT_ID_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000L
        private val DELIVERY_LOCK = Any()
    }
}
