package ai.omnigent.android

import android.net.Uri
import android.webkit.CookieManager
import android.webkit.WebView
import androidx.appcompat.app.AppCompatDelegate
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.Executors

/**
 * The single web -> native bridge, installed via
 * `WebViewCompat.addWebMessageListener` with an origin allowlist of just the
 * pinned server. Unlike `addJavascriptInterface`, the injected object
 * (`window.`[JS_OBJECT_NAME]`)` is delivered ONLY to frames whose origin
 * matches the allowlist, so a sandboxed / opaque agent-HTML iframe never
 * receives it. We additionally drop non-main-frame messages — together the
 * structural equivalent of the iOS `isMainFrame` + frame-origin check that a
 * raw `addJavascriptInterface` bridge cannot express.
 *
 * [BlobSaver] offloads writes to its own worker.
 */
class OmnigentBridgeListener(
    private val notifications: NativeNotificationManager,
    private val blobSaver: BlobSaver,
    private val profileBiometrics: ProfileCredentialStore? = null,
    private val onOpenServerSettings: () -> Unit = {},
    private val profileTokenExchange: ProfileTokenExchange? = null,
    private val serverBaseUrl: () -> String? = { null },
) : WebViewCompat.WebMessageListener {
    override fun onPostMessage(
        view: WebView,
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
        replyProxy: JavaScriptReplyProxy,
    ) {
        val baseUrl = serverBaseUrl()
        if (!isMainFrame ||
            !isTrustedBridgeMessage(view.url, sourceOrigin.toString(), baseUrl)
        ) {
            return
        }
        val data = message.data ?: return
        val json = parse(data) ?: return
        if (json.optString("method") == "profilePasscode") {
            val requestId = json.optString("requestId").ifEmpty { return }
            val profileId = json.optString("profileId").ifEmpty { return }
            val passcode = if (json.has("passcode")) json.optString("passcode") else null
            val authenticator = profileBiometrics ?: return
            handleProfilePasscode(
                sourceOrigin.toString(),
                requestId,
                profileId,
                passcode,
                replyProxy::postMessage,
            )
            return
        }
        if (json.optString("method") == "removeProfileCredential") {
            val profileId = json.optString("profileId").ifEmpty { return }
            profileBiometrics?.remove(sourceOrigin.toString(), profileId)
            return
        }
        if (json.optString("method") == "downloadFile") {
            val url = json.optString("url").ifEmpty { return }
            if (!isWithinServerBase(url, baseUrl)) return
            val headers =
                allowedDownloadHeaders(
                    json.optJSONObject("headers"),
                    CookieManager.getInstance().getCookie(sourceOrigin.toString()),
                )
            blobSaver.download(
                url = url,
                headers = headers,
                mimeType = json.optString("mimeType").ifEmpty { "application/octet-stream" },
                suggestedName = json.optString("name"),
            )
            return
        }
        handle(json)
    }

    internal fun handleProfilePasscode(
        sourceOrigin: String,
        requestId: String,
        profileId: String,
        passcode: String?,
        postReply: (String) -> Unit,
    ) {
        val authenticator = profileBiometrics ?: return
        authenticator.authenticate(sourceOrigin, profileId, passcode) { result ->
            val response = JSONObject().put("requestId", requestId)
            result.fold(
                onSuccess = { credential ->
                    val exchange =
                        profileTokenExchange ?: ProfileTokenExchange(::exchangePasscodeForToken)
                    val exchangeBase =
                        serverBaseUrl()
                            ?.takeIf { originOf(it) == originOf(sourceOrigin) }
                            ?: sourceOrigin
                    exchange.exchange(exchangeBase, profileId, credential.passcode) { tokenResult ->
                        tokenResult.fold(
                            onSuccess = {
                                credential.registrationId?.let { registrationId ->
                                    authenticator.confirmRegistration(
                                        sourceOrigin,
                                        profileId,
                                        registrationId,
                                    )
                                }
                                response.put("token", it)
                            },
                            onFailure = {
                                credential.registrationId?.let { registrationId ->
                                    authenticator.rollbackRegistration(
                                        sourceOrigin,
                                        profileId,
                                        registrationId,
                                    )
                                }
                                response.put("error", it.message ?: "Profile unlock failed")
                            },
                        )
                        postReply(response.toString())
                    }
                },
                onFailure = {
                    response.put("error", it.message ?: "Biometric authentication failed")
                    postReply(response.toString())
                },
            )
        }
    }

    private fun exchangePasscodeForToken(
        serverUrl: String,
        profileId: String,
        passcode: String,
        callback: (Result<String>) -> Unit,
    ) {
        NETWORK_EXECUTOR.execute {
            callback(
                runCatching {
                    val encodedProfile = URLEncoder.encode(profileId, Charsets.UTF_8.name())
                    val connection =
                        URL("${serverUrl.trimEnd('/')}/v1/profiles/$encodedProfile/unlock")
                            .openConnection() as HttpURLConnection
                    connection.requestMethod = "POST"
                    connection.connectTimeout = 15_000
                    connection.readTimeout = 15_000
                    connection.doOutput = true
                    connection.setRequestProperty("Content-Type", "application/json")
                    connection.setRequestProperty("Origin", originOf(serverUrl))
                    connection.setRequestProperty("X-Omnigent-Client", "android")
                    CookieManager.getInstance().getCookie(serverUrl)?.let {
                        connection.setRequestProperty("Cookie", it)
                    }
                    connection.outputStream.use { output ->
                        output.write(
                            JSONObject().put("passcode", passcode).toString().toByteArray(),
                        )
                    }
                    val status = connection.responseCode
                    if (status !in 200..299) {
                        connection.disconnect()
                        error("Profile unlock failed ($status)")
                    }
                    val payload = connection.inputStream.bufferedReader().use { it.readText() }
                    connection.disconnect()
                    JSONObject(payload).optString("token").ifEmpty {
                        error("Profile unlock returned no token")
                    }
                },
            )
        }
    }

    /** Parse and dispatch one bridge message; malformed input is dropped. */
    internal fun handle(data: String) {
        val json = parse(data) ?: return
        handle(json)
    }

    private fun parse(data: String): JSONObject? =
        try {
            JSONObject(data)
        } catch (_: Throwable) {
            null
        }

    private fun handle(json: JSONObject) {
        when (json.optString("method")) {
            "setColorScheme" -> {
                when (json.optString("scheme")) {
                    "light" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_NO,
                        )
                    }

                    "dark" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_YES,
                        )
                    }

                    "system" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM,
                        )
                    }
                }
            }

            "setBadgeCount" -> {
                notifications.setBadgeCount(
                    count = json.optInt("count", 0),
                    navigatePath = json.optString("navigatePath").ifEmpty { null },
                    title = json.optString("title").ifEmpty { null },
                    body = json.optString("body").ifEmpty { null },
                )
            }

            "notify" -> {
                val params = json.optJSONObject("params") ?: return
                val title = params.optString("title").ifEmpty { return }
                notifications.notify(
                    title = title,
                    body = params.optString("body").ifEmpty { null },
                    navigatePath = params.optString("navigatePath").ifEmpty { null },
                )
            }

            "dismissSessionNotifications" -> {
                val sessionId = json.optString("sessionId").ifEmpty { return }
                notifications.dismissSessionNotification(sessionId, null)
            }

            "openServerSettings" -> {
                onOpenServerSettings()
            }

            "blobBase64" -> {
                blobSaver.save(
                    base64 = json.optString("base64").ifEmpty { return },
                    mimeType = json.optString("mimeType").ifEmpty { "application/octet-stream" },
                    suggestedName = json.optString("name"),
                )
            }
        }
    }

    companion object {
        /** Name of the injected transport object as seen from page JS. */
        const val JS_OBJECT_NAME = "omnigentNativeBridge"
        private val NETWORK_EXECUTOR = Executors.newCachedThreadPool()
    }
}

internal fun isTrustedBridgeMessage(
    pageUrl: String?,
    sourceOrigin: String?,
    serverBaseUrl: String?,
): Boolean =
    originOf(sourceOrigin) == originOf(serverBaseUrl) &&
        isWithinServerBase(pageUrl, serverBaseUrl)

internal fun allowedDownloadHeaders(
    rawHeaders: JSONObject?,
    cookie: String?,
): MutableMap<String, String> {
    val headers = mutableMapOf<String, String>()
    listOf(
        "X-Omnigent-Profile-Unlock",
        "X-Forwarded-Email",
        "X-Databricks-Omnigent-Slice-Key",
    ).forEach { name ->
        rawHeaders?.optString(name)?.takeIf(String::isNotEmpty)?.let { headers[name] = it }
    }
    cookie?.let { headers["Cookie"] = it }
    return headers
}

fun interface ProfileTokenExchange {
    fun exchange(
        sourceOrigin: String,
        profileId: String,
        passcode: String,
        callback: (Result<String>) -> Unit,
    )
}
