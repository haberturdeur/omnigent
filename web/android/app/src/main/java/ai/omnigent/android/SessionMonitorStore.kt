package ai.omnigent.android

import android.content.Context

/** Persistent user preference and process-local visibility for the monitor. */
class SessionMonitorStore(
    context: Context,
) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    var enabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(value) {
            prefs.edit().putBoolean(KEY_ENABLED, value).apply()
        }

    companion object {
        @Volatile
        var appVisible: Boolean = false

        private const val PREFS = "ai.omnigent.android.session_monitor"
        private const val KEY_ENABLED = "enabled"
    }
}
