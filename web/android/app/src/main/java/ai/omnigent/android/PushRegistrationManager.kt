package ai.omnigent.android

import android.app.Activity
import android.content.Context
import android.webkit.CookieManager
import android.widget.Toast
import org.json.JSONObject
import org.unifiedpush.android.connector.UnifiedPush
import org.unifiedpush.android.connector.data.PushEndpoint
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.Executors

/** Maintains one standards-based Web Push registration per Omnigent server. */
class PushRegistrationManager(
    context: Context,
) {
    private val appContext = context.applicationContext
    private val prefs = appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    val enabled: Boolean
        get() = prefs.getBoolean(key(KEY_ENABLED, currentInstance()), false)

    val registered: Boolean
        get() {
            val instance = currentInstance()
            return prefs.getBoolean(key(KEY_ENABLED, instance), false) &&
                prefs.contains(key(KEY_ENDPOINT, instance)) &&
                prefs.contains(key(KEY_P256DH, instance)) &&
                prefs.contains(key(KEY_AUTH, instance))
        }

    fun enable(activity: Activity) {
        val serverUrl = currentServerUrl()
        val instance = instanceFor(serverUrl)
        prefs.edit().putString(key(KEY_SERVER, instance), serverUrl).apply()
        EXECUTOR.execute {
            val vapid = runCatching { fetchVapidKey(serverUrl) }.getOrNull()
            if (vapid == null) {
                activity.runOnUiThread {
                    Toast.makeText(activity, R.string.push_server_unavailable, Toast.LENGTH_LONG)
                        .show()
                }
                return@execute
            }
            activity.runOnUiThread {
                UnifiedPush.tryUseCurrentOrDefaultDistributor(activity) { available ->
                    if (available) {
                        UnifiedPush.register(
                            appContext,
                            instance = instance,
                            messageForDistributor = "Omnigent · ${URL(serverUrl).host}",
                            vapid = vapid,
                        )
                    } else {
                        Toast.makeText(
                            activity,
                            R.string.push_distributor_unavailable,
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            }
        }
    }

    fun disable() {
        val instance = currentInstance()
        incrementEpoch(instance)
        prefs.edit()
            .putBoolean(key(KEY_ENABLED, instance), false)
            .putBoolean(key(KEY_PENDING, instance), false)
            .apply()
        deleteFromServer(instance)
        UnifiedPush.unregister(appContext, instance)
    }

    fun storeAndUpload(
        endpoint: PushEndpoint,
        instance: String,
    ): Boolean {
        val keys = endpoint.pubKeySet
        if (keys == null || serverUrlFor(instance) == null) {
            markRegistrationFailed(instance)
            return false
        }
        prefs.edit()
            .putString(key(KEY_ENDPOINT, instance), endpoint.url)
            .putString(key(KEY_P256DH, instance), keys.pubKey)
            .putString(key(KEY_AUTH, instance), keys.auth)
            .putBoolean(key(KEY_PENDING, instance), true)
            .apply()
        uploadStoredEndpoint(instance, incrementEpoch(instance))
        return true
    }

    /** Retry the current server's endpoint upload after login/page readiness. */
    fun uploadStoredEndpoint() {
        val instance = currentInstance()
        val shouldUpload =
            prefs.getBoolean(key(KEY_PENDING, instance), false) ||
                prefs.getBoolean(key(KEY_ENABLED, instance), false)
        if (!shouldUpload) return
        prefs.edit().putBoolean(key(KEY_PENDING, instance), true).apply()
        uploadStoredEndpoint(instance, incrementEpoch(instance))
    }

    fun serverUrlFor(instance: String): String? =
        prefs.getString(key(KEY_SERVER, instance), null)?.let(::normalizeServerUrl)

    fun isEnabled(instance: String): Boolean =
        prefs.getBoolean(key(KEY_ENABLED, instance), false)

    /** Ignore old-account pushes until the authenticated page reclaims this endpoint. */
    fun suspendForAuthentication() {
        val instance = currentInstance()
        if (!prefs.getBoolean(key(KEY_ENABLED, instance), false)) return
        incrementEpoch(instance)
        prefs.edit()
            .putBoolean(key(KEY_ENABLED, instance), false)
            .putBoolean(key(KEY_PENDING, instance), true)
            .apply()
    }

    fun markRegistrationFailed(instance: String) {
        incrementEpoch(instance)
        prefs.edit()
            .putBoolean(key(KEY_ENABLED, instance), false)
            .putBoolean(key(KEY_PENDING, instance), false)
            .apply()
    }

    fun markUnregistered(instance: String) {
        incrementEpoch(instance)
        deleteFromServer(instance)
        prefs.edit()
            .putBoolean(key(KEY_ENABLED, instance), false)
            .putBoolean(key(KEY_PENDING, instance), false)
            .remove(key(KEY_ENDPOINT, instance))
            .remove(key(KEY_P256DH, instance))
            .remove(key(KEY_AUTH, instance))
            .apply()
    }

    private fun uploadStoredEndpoint(instance: String, epoch: Int) {
        val serverUrl = serverUrlFor(instance) ?: return
        val endpoint = prefs.getString(key(KEY_ENDPOINT, instance), null) ?: return
        val p256dh = prefs.getString(key(KEY_P256DH, instance), null) ?: return
        val auth = prefs.getString(key(KEY_AUTH, instance), null) ?: return
        EXECUTOR.execute {
            val accepted =
                request(
                    method = "PUT",
                    url = subscriptionUrl(serverUrl),
                    serverUrl = serverUrl,
                    body = subscriptionJson(endpoint, p256dh, auth),
                )
            if (
                accepted &&
                prefs.getInt(key(KEY_EPOCH, instance), 0) == epoch &&
                prefs.getBoolean(key(KEY_PENDING, instance), false)
            ) {
                prefs.edit()
                    .putBoolean(key(KEY_ENABLED, instance), true)
                    .putBoolean(key(KEY_PENDING, instance), false)
                    .apply()
                SessionMonitorService.stop(appContext)
            }
        }
    }

    private fun fetchVapidKey(serverUrl: String): String? {
        val connection = connection("GET", "$serverUrl/v1/push/config", serverUrl)
        return try {
            if (connection.responseCode !in 200..299) return null
            JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
                .getString("vapid_public_key")
        } finally {
            connection.disconnect()
        }
    }

    private fun deleteFromServer(instance: String) {
        val serverUrl = serverUrlFor(instance) ?: return
        EXECUTOR.execute {
            request("DELETE", subscriptionUrl(serverUrl), serverUrl, null)
        }
    }

    private fun subscriptionUrl(serverUrl: String) =
        "$serverUrl/v1/push/subscriptions/${deviceId()}"

    private fun currentServerUrl() = ServerStore(appContext).currentServerUrl().trimEnd('/')

    private fun currentInstance() = instanceFor(currentServerUrl())

    private fun connection(
        method: String,
        url: String,
        serverUrl: String,
    ): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = NETWORK_TIMEOUT_MS
            readTimeout = NETWORK_TIMEOUT_MS
            setRequestProperty("Accept", "application/json")
            CookieManager.getInstance().getCookie(serverUrl)?.takeIf { it.isNotBlank() }?.let {
                setRequestProperty("Cookie", it)
            }
        }

    private fun request(
        method: String,
        url: String,
        serverUrl: String,
        body: String?,
    ): Boolean {
        val connection = connection(method, url, serverUrl)
        try {
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.bufferedWriter().use { it.write(body) }
            }
            return connection.responseCode in 200..299
        } catch (_: Exception) {
            // Retried when the authenticated page next becomes ready.
            return false
        } finally {
            connection.disconnect()
        }
    }

    private fun subscriptionJson(endpoint: String, p256dh: String, auth: String) =
        JSONObject()
            .put("endpoint", endpoint)
            .put("keys", JSONObject().put("p256dh", p256dh).put("auth", auth))
            .toString()

    private fun deviceId(): String =
        prefs.getString(KEY_DEVICE_ID, null)
            ?: UUID.randomUUID().toString().also {
                prefs.edit().putString(KEY_DEVICE_ID, it).apply()
            }

    private fun instanceFor(serverUrl: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(serverUrl.toByteArray())
        return "server-" + digest.take(12).joinToString("") { "%02x".format(it) }
    }

    private fun key(base: String, instance: String) = "$base.$instance"

    private fun incrementEpoch(instance: String): Int =
        synchronized(PREFS_LOCK) {
            val value = prefs.getInt(key(KEY_EPOCH, instance), 0) + 1
            prefs.edit().putInt(key(KEY_EPOCH, instance), value).commit()
            value
        }

    private companion object {
        const val PREFS = "ai.omnigent.android.push"
        const val KEY_ENABLED = "enabled"
        const val KEY_PENDING = "pending"
        const val KEY_EPOCH = "epoch"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_SERVER = "server"
        const val KEY_ENDPOINT = "endpoint"
        const val KEY_P256DH = "p256dh"
        const val KEY_AUTH = "auth"
        const val NETWORK_TIMEOUT_MS = 10_000
        val EXECUTOR = Executors.newSingleThreadExecutor()
        val PREFS_LOCK = Any()
    }
}
