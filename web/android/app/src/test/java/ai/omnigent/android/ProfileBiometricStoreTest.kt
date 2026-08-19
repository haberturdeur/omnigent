package ai.omnigent.android

import android.content.Context
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
class ProfileBiometricStoreTest {
    private lateinit var activity: MainActivity
    private lateinit var store: ProfileBiometricStore

    @Before
    fun setUp() {
        activity = Robolectric.buildActivity(MainActivity::class.java).get()
        store = ProfileBiometricStore(activity)
    }

    @Test
    fun `rollback removes rejected pending credential without replacing active credential`() {
        val key = storageKey(ORIGIN, PROFILE_ID)
        preferences()
            .edit()
            .putString("$key.iv", "existing-iv")
            .putString("$key.value", "existing-value")
            .putString("$key.pending.id", "rejected-registration")
            .putString("$key.pending.iv", "rejected-iv")
            .putString("$key.pending.value", "rejected-value")
            .commit()

        store.rollbackRegistration(ORIGIN, PROFILE_ID, "rejected-registration")

        assertEquals("existing-iv", preferences().getString("$key.iv", null))
        assertEquals("existing-value", preferences().getString("$key.value", null))
        assertNull(preferences().getString("$key.pending.id", null))
        assertNull(preferences().getString("$key.pending.iv", null))
        assertNull(preferences().getString("$key.pending.value", null))
    }

    @Test
    fun `confirmation promotes only matching pending credential`() {
        val key = storageKey(ORIGIN, PROFILE_ID)
        preferences()
            .edit()
            .putString("$key.pending.id", "accepted-registration")
            .putString("$key.pending.iv", "accepted-iv")
            .putString("$key.pending.value", "accepted-value")
            .commit()

        store.confirmRegistration(ORIGIN, PROFILE_ID, "accepted-registration")

        assertEquals("accepted-iv", preferences().getString("$key.iv", null))
        assertEquals("accepted-value", preferences().getString("$key.value", null))
        assertFalse(preferences().contains("$key.pending.id"))
    }

    @Test
    fun `stale rollback cannot remove a newer pending registration`() {
        val key = storageKey(ORIGIN, PROFILE_ID)
        preferences()
            .edit()
            .putString("$key.pending.id", "new-registration")
            .putString("$key.pending.iv", "new-iv")
            .putString("$key.pending.value", "new-value")
            .commit()

        store.rollbackRegistration(ORIGIN, PROFILE_ID, "old-registration")

        assertEquals("new-registration", preferences().getString("$key.pending.id", null))
        assertEquals("new-value", preferences().getString("$key.pending.value", null))
    }

    private fun preferences() =
        activity.getSharedPreferences("private-profile-credentials", Context.MODE_PRIVATE)

    private fun storageKey(serverOrigin: String, profileId: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest("$serverOrigin\u0000$profileId".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    companion object {
        private const val ORIGIN = "https://example.test"
        private const val PROFILE_ID = "private"
    }
}
