package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * User-enabled fallback for devices without a UnifiedPush distributor.
 *
 * This foreground service polls the configured self-hosted Omnigent server
 * with the WebView's authenticated cookie. Its persistent low-priority
 * notification makes the battery/network cost visible.
 */
class SessionMonitorService : Service() {
    private lateinit var executor: ScheduledExecutorService
    private lateinit var cookieManager: CookieManager
    private val tracker = SessionTransitionTracker()
    private var monitoredServer: String? = null

    override fun onCreate() {
        super.onCreate()
        cookieManager = CookieManager.getInstance()
        createMonitorChannel()
        val notification = monitorNotification(getString(R.string.monitor_status_active))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                MONITOR_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(MONITOR_NOTIFICATION_ID, notification)
        }
        executor = Executors.newSingleThreadScheduledExecutor()
        executor.scheduleWithFixedDelay(::pollSafely, 0, POLL_INTERVAL_SECONDS, TimeUnit.SECONDS)
    }

    override fun onStartCommand(
        intent: Intent?,
        flags: Int,
        startId: Int,
    ): Int {
        if (intent?.action == ACTION_STOP || !SessionMonitorStore(this).enabled) {
            if (intent?.action == ACTION_STOP) SessionMonitorStore(this).enabled = false
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onDestroy() {
        if (::executor.isInitialized) executor.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun pollSafely() {
        if (!SessionMonitorStore(this).enabled) {
            stopSelf()
            return
        }
        try {
            poll()
            updateMonitorNotification(getString(R.string.monitor_status_active))
        } catch (_: SecurityException) {
            updateMonitorNotification(getString(R.string.monitor_status_sign_in))
        } catch (_: Exception) {
            updateMonitorNotification(getString(R.string.monitor_status_unreachable))
        }
    }

    private fun poll() {
        val server = ServerStore(this).currentServerUrl().trimEnd('/')
        if (server != monitoredServer) {
            monitoredServer = server
            tracker.reset()
        }
        val connection =
            URL("$server/v1/sessions?limit=1000&sort_by=updated_at&order=desc")
                .openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = NETWORK_TIMEOUT_MS
            connection.readTimeout = NETWORK_TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            cookieManager.getCookie(server)?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Cookie", it)
            }
            val status = connection.responseCode
            if (status == HttpURLConnection.HTTP_UNAUTHORIZED || status == HttpURLConnection.HTTP_FORBIDDEN) {
                throw SecurityException("Authentication required")
            }
            if (status !in 200..299) throw IllegalStateException("Server returned HTTP $status")
            val body = connection.inputStream.bufferedReader().use { it.readText() }
            val sessions = parseSessions(body)
            val events = tracker.observe(sessions)
            if (!SessionMonitorStore.appVisible) {
                val notifications = NativeNotificationManager(applicationContext)
                for (event in events) notifications.notify(event)
            }
        } finally {
            connection.disconnect()
        }
    }

    private fun parseSessions(body: String): List<MonitoredSession> {
        val data = JSONObject(body).getJSONArray("data")
        return buildList(data.length()) {
            for (index in 0 until data.length()) {
                val item = data.getJSONObject(index)
                add(
                    MonitoredSession(
                        id = item.getString("id"),
                        title =
                            if (item.isNull("title")) {
                                null
                            } else {
                                item.optString("title").takeIf { it.isNotBlank() }
                            },
                        status = item.getString("status"),
                        pendingElicitations = item.optInt("pending_elicitations_count", 0),
                    ),
                )
            }
        }
    }

    private fun createMonitorChannel() {
        val channel =
            NotificationChannel(
                MONITOR_CHANNEL_ID,
                getString(R.string.monitor_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.monitor_channel_description)
                setShowBadge(false)
            }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun monitorNotification(status: String): Notification {
        val openApp =
            PendingIntent.getActivity(
                this,
                MONITOR_NOTIFICATION_ID,
                Intent(this, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        val stop =
            PendingIntent.getService(
                this,
                MONITOR_NOTIFICATION_ID,
                Intent(this, SessionMonitorService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        return NotificationCompat
            .Builder(this, MONITOR_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.monitor_notification_title))
            .setContentText(status)
            .setContentIntent(openApp)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .addAction(0, getString(R.string.monitor_stop), stop)
            .build()
    }

    private fun updateMonitorNotification(status: String) {
        val manager = NotificationManagerCompat.from(this)
        if (!manager.areNotificationsEnabled()) return
        try {
            manager.notify(MONITOR_NOTIFICATION_ID, monitorNotification(status))
        } catch (_: SecurityException) {
            // Permission can be revoked while the foreground service is alive.
        }
    }

    companion object {
        private const val ACTION_STOP = "ai.omnigent.android.STOP_SESSION_MONITOR"
        private const val MONITOR_CHANNEL_ID = "omnigent.monitor"
        private const val MONITOR_NOTIFICATION_ID = 2
        private const val POLL_INTERVAL_SECONDS = 10L
        private const val NETWORK_TIMEOUT_MS = 10_000

        fun start(context: android.content.Context) {
            SessionMonitorStore(context).enabled = true
            ContextCompat.startForegroundService(context, Intent(context, SessionMonitorService::class.java))
        }

        fun stop(context: android.content.Context) {
            SessionMonitorStore(context).enabled = false
            context.stopService(Intent(context, SessionMonitorService::class.java))
        }
    }
}
