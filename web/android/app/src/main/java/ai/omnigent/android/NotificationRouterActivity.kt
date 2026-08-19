package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity

/** Internal notification trampoline that may select the server that emitted a push. */
class NotificationRouterActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        intent.getStringExtra(NativeNotificationManager.EXTRA_SERVER_URL)
            ?.let(::normalizeServerUrl)
            ?.let { ServerStore(this).connect(it) }
        val openApp =
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                intent.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH)
                    ?.takeIf { it.startsWith("/") }
                    ?.let { putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, it) }
            }
        startActivity(openApp)
        finish()
    }
}
