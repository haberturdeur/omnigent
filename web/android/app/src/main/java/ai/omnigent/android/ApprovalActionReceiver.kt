package ai.omnigent.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.webkit.CookieManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/** Resolves an elicitation when the user taps a notification action. */
class ApprovalActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val instance = intent.getStringExtra(EXTRA_INSTANCE) ?: return
        val sessionId = intent.getStringExtra(EXTRA_SESSION_ID) ?: return
        val elicitationId = intent.getStringExtra(EXTRA_ELICITATION_ID) ?: return
        val action = intent.getStringExtra(EXTRA_ACTION) ?: return
        val body = requestBody(action) ?: return
        val serverUrl = PushRegistrationManager(context).serverUrlFor(instance) ?: return
        val pendingResult = goAsync()
        EXECUTOR.execute {
            try {
                if (submit(serverUrl, sessionId, elicitationId, body)) {
                    NativeNotificationManager(context).dismissSessionNotification(
                        sessionId,
                        "approval:$elicitationId",
                    )
                }
            } finally {
                pendingResult.finish()
            }
        }
    }

    private fun submit(
        serverUrl: String,
        sessionId: String,
        elicitationId: String,
        body: String,
    ): Boolean {
        val path =
            "$serverUrl/v1/sessions/${Uri.encode(sessionId)}/elicitations/" +
                "${Uri.encode(elicitationId)}/resolve"
        val connection = URL(path).openConnection() as HttpURLConnection
        return try {
            connection.requestMethod = "POST"
            connection.connectTimeout = NETWORK_TIMEOUT_MS
            connection.readTimeout = NETWORK_TIMEOUT_MS
            connection.doOutput = true
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Content-Type", "application/json")
            CookieManager.getInstance().getCookie(serverUrl)?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Cookie", it)
            }
            connection.outputStream.bufferedWriter().use { it.write(body) }
            connection.responseCode in 200..299
        } catch (_: Exception) {
            false
        } finally {
            connection.disconnect()
        }
    }

    internal companion object {
        const val EXTRA_INSTANCE = "ai.omnigent.android.approval.INSTANCE"
        const val EXTRA_SESSION_ID = "ai.omnigent.android.approval.SESSION_ID"
        const val EXTRA_ELICITATION_ID = "ai.omnigent.android.approval.ELICITATION_ID"
        const val EXTRA_ACTION = "ai.omnigent.android.approval.ACTION"
        const val ACTION_APPROVE = "approve"
        const val ACTION_ALLOW_ALL_EDITS = "allow_all_edits"
        const val ACTION_REMEMBER = "remember"
        const val ACTION_REJECT = "reject"
        private const val NETWORK_TIMEOUT_MS = 10_000
        private val EXECUTOR = Executors.newSingleThreadExecutor()

        fun requestBody(action: String): String? {
            val result = JSONObject()
            when (action) {
                ACTION_APPROVE -> result.put("action", "accept")
                ACTION_ALLOW_ALL_EDITS ->
                    result
                        .put("action", "accept")
                        .put("content", JSONObject().put("allow_all_edits", true))
                ACTION_REMEMBER ->
                    result
                        .put("action", "accept")
                        .put("content", JSONObject().put("remember", true))
                ACTION_REJECT -> result.put("action", "decline")
                else -> return null
            }
            return result.toString()
        }
    }
}
