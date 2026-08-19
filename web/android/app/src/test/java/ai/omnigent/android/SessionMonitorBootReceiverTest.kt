package ai.omnigent.android

import android.app.Application
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf

@RunWith(RobolectricTestRunner::class)
class SessionMonitorBootReceiverTest {
    @Test
    fun `boot restarts an enabled polling fallback`() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        SessionMonitorStore(context).enabled = true

        SessionMonitorBootReceiver().onReceive(context, Intent(Intent.ACTION_BOOT_COMPLETED))

        assertEquals(
            SessionMonitorService::class.java.name,
            shadowOf(context).nextStartedService.component!!.className,
        )
    }
}
