package ai.omnigent.android

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import androidx.appcompat.app.AppCompatDelegate
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowNotificationManager

/**
 * Bridge JSON parsing, asserted end to end through the real
 * [NativeNotificationManager] into Robolectric's shadow notification
 * service — the same wiring [MainActivity] installs.
 */
@RunWith(RobolectricTestRunner::class)
class OmnigentBridgeListenerTest {
    private lateinit var context: Application
    private lateinit var listener: OmnigentBridgeListener
    private lateinit var shadow: ShadowNotificationManager

    private val badgeId = 1
    private var serverSettingsOpened = false

    @Before
    fun setUp() {
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM)
        context = ApplicationProvider.getApplicationContext()
        serverSettingsOpened = false
        listener =
            OmnigentBridgeListener(
                notifications = NativeNotificationManager(context),
                blobSaver = BlobSaver(context),
                onOpenServerSettings = { serverSettingsOpened = true },
            )
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    @Test
    fun `openServerSettings delegates to the activity`() {
        listener.handle("""{"method":"openServerSettings"}""")

        assertTrue(serverSettingsOpened)
    }

    @Test
    fun `setColorScheme light sets night mode no`() {
        listener.handle("""{"method":"setColorScheme","scheme":"light"}""")
        assertEquals(AppCompatDelegate.MODE_NIGHT_NO, AppCompatDelegate.getDefaultNightMode())
    }

    @Test
    fun `setColorScheme dark sets night mode yes`() {
        listener.handle("""{"method":"setColorScheme","scheme":"dark"}""")
        assertEquals(AppCompatDelegate.MODE_NIGHT_YES, AppCompatDelegate.getDefaultNightMode())
    }

    @Test
    fun `setColorScheme system follows system`() {
        listener.handle("""{"method":"setColorScheme","scheme":"system"}""")
        assertEquals(
            AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM,
            AppCompatDelegate.getDefaultNightMode(),
        )
    }

    @Test
    fun `setColorScheme rejects missing and unsupported schemes`() {
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)

        listener.handle("""{"method":"setColorScheme"}""")
        listener.handle("""{"method":"setColorScheme","scheme":"auto"}""")
        listener.handle("""{"method":"setColorScheme","scheme":123}""")

        assertEquals(AppCompatDelegate.MODE_NIGHT_NO, AppCompatDelegate.getDefaultNightMode())
    }

    @Test
    fun `setBadgeCount message posts the badge with parsed fields`() {
        listener.handle(
            """{"method":"setBadgeCount","count":3,"navigatePath":"/inbox","title":"T","body":"B"}""",
        )

        val posted = shadow.getNotification(badgeId)
        assertNotNull(posted)
        assertEquals(3, posted!!.number)
        assertEquals(
            "/inbox",
            shadowOf(posted.contentIntent).savedIntent.getStringExtra(
                NativeNotificationManager.EXTRA_NAVIGATE_PATH,
            ),
        )
    }

    @Test
    fun `setBadgeCount zero clears the badge`() {
        listener.handle("""{"method":"setBadgeCount","count":2,"navigatePath":"/inbox"}""")
        listener.handle("""{"method":"setBadgeCount","count":0}""")
        assertNull(shadow.getNotification(badgeId))
    }

    @Test
    fun `legacy setBadgeCount without options still posts`() {
        // Older web builds send only the count; fields default to absent.
        listener.handle("""{"method":"setBadgeCount","count":1}""")
        val posted = shadow.getNotification(badgeId)
        assertNotNull(posted)
        assertNull(posted!!.contentIntent)
    }

    @Test
    fun `notify message posts a per-session toast with tap routing`() {
        listener.handle(
            """{"method":"notify","params":{"title":"done","body":"b","navigatePath":"/c/x"}}""",
        )

        // Toasts allocate ids above the reserved badge id.
        assertEquals(1, shadow.allNotifications.size)
        assertNull(shadow.getNotification(badgeId))
    }

    @Test
    fun `notify without a title is dropped`() {
        listener.handle("""{"method":"notify","params":{"body":"b"}}""")
        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `malformed and unknown messages are dropped without crashing`() {
        listener.handle("not json at all")
        listener.handle("""{"method":"unknownThing","count":5}""")
        listener.handle("""{"count":5}""")
        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `rejected registration rolls back pending biometric credential`() {
        val credentials = FakeProfileCredentialStore(ProfileCredential("wrong", "registration-1"))
        val listener = biometricListener(credentials, Result.failure(Exception("rejected")))
        var reply: JSONObject? = null

        listener.handleProfilePasscode("https://example.test", "request-1", "private", "wrong") {
            reply = JSONObject(it)
        }

        assertEquals(listOf("registration-1"), credentials.rolledBack)
        assertTrue(credentials.confirmed.isEmpty())
        assertEquals("rejected", reply?.getString("error"))
    }

    @Test
    fun `accepted registration commits pending biometric credential`() {
        val credentials = FakeProfileCredentialStore(ProfileCredential("correct", "registration-2"))
        val listener = biometricListener(credentials, Result.success("profile-token"))
        var reply: JSONObject? = null

        listener.handleProfilePasscode("https://example.test", "request-2", "private", "correct") {
            reply = JSONObject(it)
        }

        assertEquals(listOf("registration-2"), credentials.confirmed)
        assertTrue(credentials.rolledBack.isEmpty())
        assertEquals("profile-token", reply?.getString("token"))
    }

    @Test
    fun `automatic biometric unlock rejection does not remove existing credential`() {
        val credentials = FakeProfileCredentialStore(ProfileCredential("stored-correct"))
        val listener = biometricListener(credentials, Result.failure(Exception("expired")))

        listener.handleProfilePasscode("https://example.test", "request-3", "private", null) {}

        assertNull(credentials.lastPasscode)
        assertTrue(credentials.confirmed.isEmpty())
        assertTrue(credentials.rolledBack.isEmpty())
        assertTrue(credentials.removed.isEmpty())
    }

    @Test
    fun `biometric exchange preserves the configured server base path`() {
        val credentials = FakeProfileCredentialStore(ProfileCredential("correct"))
        var exchangeBase: String? = null
        val listener =
            OmnigentBridgeListener(
                notifications = NativeNotificationManager(context),
                blobSaver = BlobSaver(context),
                profileBiometrics = credentials,
                profileTokenExchange =
                    ProfileTokenExchange { base, _, _, callback ->
                        exchangeBase = base
                        callback(Result.success("token"))
                    },
                serverBaseUrl = { "https://example.test/omnigent" },
            )

        listener.handleProfilePasscode(
            "https://example.test",
            "request-path",
            "private",
            "correct",
        ) {}

        assertEquals("https://example.test/omnigent", exchangeBase)
    }

    @Test
    fun `native downloads forward only approved auth and shard headers`() {
        val raw =
            JSONObject()
                .put("X-Omnigent-Profile-Unlock", "unlock")
                .put("X-Forwarded-Email", "me@example.test")
                .put("X-Databricks-Omnigent-Slice-Key", "host-1")
                .put("Authorization", "must-not-pass")

        assertEquals(
            mapOf(
                "X-Omnigent-Profile-Unlock" to "unlock",
                "X-Forwarded-Email" to "me@example.test",
                "X-Databricks-Omnigent-Slice-Key" to "host-1",
                "Cookie" to "session=cookie",
            ),
            allowedDownloadHeaders(raw, "session=cookie"),
        )
    }

    @Test
    fun `bridge dispatch accepts only the configured mount descendants`() {
        val base = "https://example.test/omnigent"

        assertTrue(isTrustedBridgeMessage("$base/c/one", "https://example.test", base))
        assertFalse(
            isTrustedBridgeMessage(
                "https://example.test/omnigent-evil",
                "https://example.test",
                base,
            ),
        )
        assertFalse(
            isTrustedBridgeMessage(
                "https://example.test/other",
                "https://example.test",
                base,
            ),
        )
    }

    private fun biometricListener(
        credentials: FakeProfileCredentialStore,
        exchangeResult: Result<String>,
    ): OmnigentBridgeListener =
        OmnigentBridgeListener(
            notifications = NativeNotificationManager(context),
            blobSaver = BlobSaver(context),
            profileBiometrics = credentials,
            profileTokenExchange =
                ProfileTokenExchange { _, _, _, callback ->
                    callback(exchangeResult)
                },
        )

    private class FakeProfileCredentialStore(
        private val credential: ProfileCredential,
    ) : ProfileCredentialStore {
        val confirmed = mutableListOf<String>()
        val rolledBack = mutableListOf<String>()
        val removed = mutableListOf<String>()
        var lastPasscode: String? = "not-called"

        override fun authenticate(
            serverOrigin: String,
            profileId: String,
            passcode: String?,
            callback: (Result<ProfileCredential>) -> Unit,
        ) {
            lastPasscode = passcode
            callback(Result.success(credential))
        }

        override fun confirmRegistration(
            serverOrigin: String,
            profileId: String,
            registrationId: String,
        ) {
            confirmed += registrationId
        }

        override fun rollbackRegistration(
            serverOrigin: String,
            profileId: String,
            registrationId: String,
        ) {
            rolledBack += registrationId
        }

        override fun remove(
            serverOrigin: String,
            profileId: String,
        ) {
            removed += profileId
        }
    }
}
