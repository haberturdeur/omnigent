package ai.omnigent.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restarts user-enabled polling after the device finishes booting. */
class SessionMonitorBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (SessionMonitorStore(context).enabled && !PushRegistrationManager(context).registered) {
            SessionMonitorService.startPreservingPreference(context)
        }
    }
}
