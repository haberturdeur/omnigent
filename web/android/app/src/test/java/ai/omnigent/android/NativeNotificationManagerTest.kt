package ai.omnigent.android

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowNotificationManager

@RunWith(RobolectricTestRunner::class)
class NativeNotificationManagerTest {
    private lateinit var context: Application
    private lateinit var manager: NativeNotificationManager
    private lateinit var shadow: ShadowNotificationManager

    // The reserved badge-summary notification id (NativeNotificationManager's
    // BADGE_NOTIFICATION_ID is private; the contract is "id 1").
    private val badgeId = 1

    @Before
    fun setUp() {
        SessionMonitorStore.appVisible = false
        context = ApplicationProvider.getApplicationContext()
        context
            .getSharedPreferences(
                "ai.omnigent.android.notification_deliveries",
                Context.MODE_PRIVATE,
            ).edit()
            .clear()
            .commit()
        manager = NativeNotificationManager(context)
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    private fun badgeNotification() = shadow.getNotification(badgeId)

    @Test
    fun `foreground web notifications and badge summaries are suppressed`() {
        manager.setBadgeCount(2, navigatePath = "/inbox")
        assertNotNull(badgeNotification())

        SessionMonitorStore.appVisible = true
        manager.setBadgeCount(2, navigatePath = "/inbox")
        manager.notify("Duplicate", "body", "/c/session-a")

        assertNull(badgeNotification())
        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `badge posts a summary notification with the count and tap intent`() {
        manager.setBadgeCount(2, navigatePath = "/inbox", title = "t", body = "b")

        val posted = badgeNotification()
        assertNotNull(posted)
        assertEquals(2, posted!!.number)
        assertNotNull(posted.contentIntent)
    }

    @Test
    fun `badge count zero cancels the summary notification`() {
        manager.setBadgeCount(3, navigatePath = "/inbox")
        assertNotNull(badgeNotification())

        manager.setBadgeCount(0)

        // The count surface must not linger as a stale, still-tappable
        // "sessions need your attention" once nothing is pending.
        assertNull(badgeNotification())
    }

    @Test
    fun `badge without a path posts with no tap intent`() {
        manager.setBadgeCount(1)
        val posted = badgeNotification()
        assertNotNull(posted)
        assertNull(posted!!.contentIntent)
    }

    @Test
    fun `replayBadge re-posts the badge dropped while notifications were disabled`() {
        // The API 33+ permission dialog is still open: posts drop silently.
        shadow.setNotificationsEnabled(false)
        manager.setBadgeCount(4, navigatePath = "/inbox", title = "t", body = "b")
        assertNull(badgeNotification())

        // Grant lands: MainActivity replays the cached state.
        shadow.setNotificationsEnabled(true)
        manager.replayBadge()

        val posted = badgeNotification()
        assertNotNull(posted)
        assertEquals(4, posted!!.number)
    }

    @Test
    fun `replayBadge of a zero state clears rather than posts`() {
        manager.setBadgeCount(2)
        manager.setBadgeCount(0)
        manager.replayBadge()
        assertNull(badgeNotification())
    }

    @Test
    fun `a new path replaces the badge tap intent extras`() {
        manager.setBadgeCount(1, navigatePath = "/c/conv_a")
        manager.setBadgeCount(2, navigatePath = "/inbox")

        // FLAG_UPDATE_CURRENT on a fixed requestCode must refresh the extras —
        // a stale path would route the tap to the wrong destination.
        val intent = shadowOf(badgeNotification()!!.contentIntent).savedIntent
        assertEquals(
            "/inbox",
            intent.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
    }

    @Test
    fun `session push tap carries its source server and session path`() {
        manager.notify(
            SessionAttentionEvent(
                MonitoredSession("session-a", "A", "idle", 0),
                SessionAttentionEvent.Kind.COMPLETED,
            ),
            serverUrl = "https://one.example",
        )

        val posted = shadow.getNotification("session:session-a", 3)
        val intent = shadowOf(posted.contentIntent).savedIntent
        assertEquals(NotificationRouterActivity::class.java.name, intent.component!!.className)
        assertEquals(
            "/c/session-a",
            intent.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
        assertEquals(
            "https://one.example",
            intent.getStringExtra(NativeNotificationManager.EXTRA_SERVER_URL),
        )
    }

    @Test
    fun `binary approval push exposes approve always and reject actions`() {
        manager.notify(
            SessionAttentionEvent(
                MonitoredSession("session-a", "A", "idle", 1),
                SessionAttentionEvent.Kind.NEEDS_INPUT,
                NotificationApproval(
                    instance = "server-instance",
                    sessionId = "session-a",
                    elicitationId = "elicit-a",
                    description = "Codex wants to run git status",
                    persistent = NotificationApproval.PersistentAction.REMEMBER,
                ),
            ),
        )

        val posted = shadow.getNotification("session:session-a", 3)
        assertEquals(
            "Codex wants to run git status",
            posted.extras.getString(Notification.EXTRA_TEXT),
        )
        assertEquals(
            "Codex wants to run git status",
            posted.extras.getString(Notification.EXTRA_BIG_TEXT),
        )
        assertEquals(
            Notification.FLAG_ONLY_ALERT_ONCE,
            posted.flags and Notification.FLAG_ONLY_ALERT_ONCE,
        )
        assertEquals(
            listOf("Approve", "Always allow", "Reject"),
            posted.actions.map { it.title.toString() },
        )
        val approveIntent = shadowOf(posted.actions[0].actionIntent).savedIntent
        assertEquals(
            ApprovalActionReceiver.ACTION_APPROVE,
            approveIntent.getStringExtra(ApprovalActionReceiver.EXTRA_ACTION),
        )
        assertEquals(
            "server-instance",
            approveIntent.getStringExtra(ApprovalActionReceiver.EXTRA_INSTANCE),
        )
        assertEquals(
            "elicit-a",
            approveIntent.getStringExtra(ApprovalActionReceiver.EXTRA_ELICITATION_ID),
        )
    }

    @Test
    fun `duplicate session event keeps the first posted notification`() {
        val first =
            SessionAttentionEvent(
                MonitoredSession("session-a", "First", "idle", 1),
                SessionAttentionEvent.Kind.NEEDS_INPUT,
            )
        val duplicate =
            SessionAttentionEvent(
                MonitoredSession("session-a", "Duplicate", "idle", 1),
                SessionAttentionEvent.Kind.NEEDS_INPUT,
            )

        manager.notify(first)
        manager.notify(duplicate)

        assertEquals(
            "First",
            shadow.getNotification("session:session-a", 3).extras.getString(Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `resolved notification id stays dismissed while a new id can post`() {
        val resolved =
            SessionAttentionEvent(
                MonitoredSession("session-a", "Resolved", "idle", 1),
                SessionAttentionEvent.Kind.NEEDS_INPUT,
                notificationId = "approval:one",
            )
        manager.notify(resolved)
        assertNotNull(shadow.getNotification("notification:approval:one", 3))

        manager.dismissSessionNotification("session-a", "approval:one")
        assertNull(shadow.getNotification("notification:approval:one", 3))

        manager.notify(resolved)
        assertNull(shadow.getNotification("notification:approval:one", 3))

        manager.notify(
            resolved.copy(
                session = resolved.session.copy(title = "New approval"),
                notificationId = "approval:two",
            ),
        )
        assertEquals(
            "New approval",
            shadow
                .getNotification("notification:approval:two", 3)
                .extras
                .getString(Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `opening a session dismisses every exact notification for it`() {
        val manager = NativeNotificationManager(context)
        manager.notify(
            SessionAttentionEvent(
                MonitoredSession("session-a", "First", "idle", 0),
                SessionAttentionEvent.Kind.COMPLETED,
                notificationId = "event:one",
            ),
        )
        manager.notify(
            SessionAttentionEvent(
                MonitoredSession("session-a", "Second", "idle", 0),
                SessionAttentionEvent.Kind.COMPLETED,
                notificationId = "event:two",
            ),
        )

        assertNotNull(shadow.getNotification("notification:event:one", 3))
        assertNotNull(shadow.getNotification("notification:event:two", 3))
        manager.dismissSessionNotification("session-a", null)
        assertNull(shadow.getNotification("notification:event:one", 3))
        assertNull(shadow.getNotification("notification:event:two", 3))
    }

    @Test
    fun `two exact approvals in one session remain independently visible`() {
        val base =
            SessionAttentionEvent(
                MonitoredSession("session-a", "First", "idle", 1),
                SessionAttentionEvent.Kind.NEEDS_INPUT,
                notificationId = "approval:one",
            )

        manager.notify(base)
        manager.notify(
            base.copy(
                session = base.session.copy(title = "Second"),
                notificationId = "approval:two",
            ),
        )

        assertNotNull(shadow.getNotification("notification:approval:one", 3))
        assertNotNull(shadow.getNotification("notification:approval:two", 3))
        manager.dismissSessionNotification("session-a", "approval:one")
        assertNull(shadow.getNotification("notification:approval:one", 3))
        assertNotNull(shadow.getNotification("notification:approval:two", 3))
    }

    @Test
    fun `delivery tombstones expire and remain size bounded`() {
        val prefs =
            context.getSharedPreferences(
                "ai.omnigent.android.notification_deliveries",
                Context.MODE_PRIVATE,
            )
        val now = 10L * 24 * 60 * 60 * 1_000
        val edit = prefs.edit()
        edit.putLong("delivered.id:expired", 1L)
        edit.putStringSet("session.old", setOf("expired"))
        repeat(2_100) { index ->
            edit.putLong("delivered.id:current-$index", now - index)
        }
        edit.commit()

        manager.pruneDeliveryState(now)

        val deliveryKeys = prefs.all.keys.filter { it.startsWith("delivered.") }
        assertEquals(2_048, deliveryKeys.size)
        assertEquals(false, prefs.contains("delivered.id:expired"))
        assertEquals(false, prefs.contains("session.old"))
    }
}
