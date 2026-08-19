package ai.omnigent.android

import android.content.Context
import android.view.LayoutInflater
import android.widget.ScrollView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ServerSettingsLayoutTest {
    @Test
    fun `server settings actions are hosted in a scroll view`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val view = LayoutInflater.from(context).inflate(R.layout.dialog_server_settings, null)

        assertTrue(view is ScrollView)
        assertNotNull(view.findViewById(R.id.server_settings_actions))
        assertNotNull(view.findViewById(R.id.server_settings_close))
    }
}
