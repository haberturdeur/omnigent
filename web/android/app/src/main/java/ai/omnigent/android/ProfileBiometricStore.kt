package ai.omnigent.android

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import java.security.KeyStore
import java.security.MessageDigest
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class ProfileCredential(
    val passcode: String,
    val registrationId: String? = null,
)

interface ProfileCredentialStore {
    fun authenticate(
        serverOrigin: String,
        profileId: String,
        passcode: String?,
        callback: (Result<ProfileCredential>) -> Unit,
    )

    fun confirmRegistration(serverOrigin: String, profileId: String, registrationId: String)

    fun rollbackRegistration(serverOrigin: String, profileId: String, registrationId: String)

    fun remove(serverOrigin: String, profileId: String)
}

/** Stores profile passcodes behind an Android Keystore biometric key. */
class ProfileBiometricStore(
    private val activity: MainActivity,
) : ProfileCredentialStore {
    private val preferences =
        activity.getSharedPreferences("private-profile-credentials", Context.MODE_PRIVATE)

    override fun authenticate(
        serverOrigin: String,
        profileId: String,
        passcode: String?,
        callback: (Result<ProfileCredential>) -> Unit,
    ) {
        val storageKey = digest("$serverOrigin\u0000$profileId")
        try {
            if (passcode != null) register(storageKey, passcode, callback)
            else unlock(storageKey, callback)
        } catch (error: Throwable) {
            callback(Result.failure(error))
        }
    }

    override fun remove(serverOrigin: String, profileId: String) {
        clearCredential(digest("$serverOrigin\u0000$profileId"))
    }

    override fun confirmRegistration(
        serverOrigin: String,
        profileId: String,
        registrationId: String,
    ) {
        val storageKey = digest("$serverOrigin\u0000$profileId")
        if (preferences.getString("$storageKey.pending.id", null) != registrationId) return
        val iv = preferences.getString("$storageKey.pending.iv", null) ?: return
        val value = preferences.getString("$storageKey.pending.value", null) ?: return
        preferences
            .edit()
            .putString("$storageKey.iv", iv)
            .putString("$storageKey.value", value)
            .remove("$storageKey.pending.id")
            .remove("$storageKey.pending.iv")
            .remove("$storageKey.pending.value")
            .apply()
    }

    override fun rollbackRegistration(
        serverOrigin: String,
        profileId: String,
        registrationId: String,
    ) {
        val storageKey = digest("$serverOrigin\u0000$profileId")
        if (preferences.getString("$storageKey.pending.id", null) == registrationId) {
            clearPendingRegistration(storageKey)
        }
    }

    private fun register(
        storageKey: String,
        passcode: String,
        callback: (Result<ProfileCredential>) -> Unit,
    ) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        val alias = aliasFor(storageKey)
        try {
            cipher.init(Cipher.ENCRYPT_MODE, secretKey(alias))
        } catch (_: KeyPermanentlyInvalidatedException) {
            clearCredential(storageKey)
            cipher.init(Cipher.ENCRYPT_MODE, secretKey(alias))
        }
        prompt(
            cipher,
            "Protect private profile",
            onError = { callback(Result.failure(it)) },
        ) { authenticated ->
            try {
                val encrypted = authenticated.doFinal(passcode.toByteArray(Charsets.UTF_8))
                val registrationId = UUID.randomUUID().toString()
                preferences
                    .edit()
                    .putString("$storageKey.pending.id", registrationId)
                    .putString(
                        "$storageKey.pending.iv",
                        android.util.Base64.encodeToString(cipher.iv, 0),
                    ).putString(
                        "$storageKey.pending.value",
                        android.util.Base64.encodeToString(encrypted, 0),
                    )
                    .apply()
                callback(Result.success(ProfileCredential(passcode, registrationId)))
            } catch (error: Throwable) {
                callback(Result.failure(error))
            }
        }
    }

    private fun unlock(
        storageKey: String,
        callback: (Result<ProfileCredential>) -> Unit,
    ) {
        val iv = preferences.getString("$storageKey.iv", null)
        val value = preferences.getString("$storageKey.value", null)
        if (iv == null || value == null) {
            callback(Result.failure(IllegalStateException("No biometric credential registered")))
            return
        }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        val alias = aliasFor(storageKey)
        try {
            cipher.init(
                Cipher.DECRYPT_MODE,
                secretKey(alias),
                GCMParameterSpec(128, android.util.Base64.decode(iv, 0)),
            )
        } catch (error: KeyPermanentlyInvalidatedException) {
            clearCredential(storageKey)
            callback(
                Result.failure(
                    IllegalStateException(
                        "Biometrics changed; enter the passcode once to register again.",
                        error,
                    ),
                ),
            )
            return
        }
        prompt(cipher, "Unlock private profile", onError = { callback(Result.failure(it)) }) {
            authenticated ->
            try {
                val plaintext = authenticated.doFinal(android.util.Base64.decode(value, 0))
                callback(Result.success(ProfileCredential(plaintext.toString(Charsets.UTF_8))))
            } catch (error: Throwable) {
                clearCredential(storageKey)
                callback(Result.failure(error))
            }
        }
    }

    private fun prompt(
        cipher: Cipher,
        title: String,
        onError: (Throwable) -> Unit,
        onSuccess: (Cipher) -> Unit,
    ) {
        val prompt =
            BiometricPrompt(
                activity,
                ContextCompat.getMainExecutor(activity),
                object : BiometricPrompt.AuthenticationCallback() {
                    override fun onAuthenticationError(
                        errorCode: Int,
                        errString: CharSequence,
                    ) {
                        onError(IllegalStateException(errString.toString()))
                    }

                    override fun onAuthenticationSucceeded(
                        result: BiometricPrompt.AuthenticationResult,
                    ) {
                        val authenticated = result.cryptoObject?.cipher ?: return
                        onSuccess(authenticated)
                    }
                },
            )
        val info =
            BiometricPrompt.PromptInfo
                .Builder()
                .setTitle(title)
                .setSubtitle("Omnigent uses your device biometrics; the passcode stays encrypted on this device.")
                .setNegativeButtonText("Use passcode")
                .build()
        prompt.authenticate(info, BiometricPrompt.CryptoObject(cipher))
    }

    private fun secretKey(alias: String): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        val builder =
            KeyGenParameterSpec
                .Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setUserAuthenticationRequired(true)
                .setInvalidatedByBiometricEnrollment(true)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            builder.setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
        } else {
            @Suppress("DEPRECATION")
            builder.setUserAuthenticationValidityDurationSeconds(-1)
        }
        return KeyGenerator
            .getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            .apply { init(builder.build()) }
            .generateKey()
    }

    private fun clearCredential(storageKey: String) {
        preferences
            .edit()
            .remove("$storageKey.iv")
            .remove("$storageKey.value")
            .remove("$storageKey.pending.id")
            .remove("$storageKey.pending.iv")
            .remove("$storageKey.pending.value")
            .apply()
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val alias = aliasFor(storageKey)
        if (store.containsAlias(alias)) store.deleteEntry(alias)
    }

    private fun clearPendingRegistration(storageKey: String) {
        preferences
            .edit()
            .remove("$storageKey.pending.id")
            .remove("$storageKey.pending.iv")
            .remove("$storageKey.pending.value")
            .apply()
    }

    private fun aliasFor(storageKey: String): String = "$KEY_ALIAS_PREFIX$storageKey"

    private fun digest(value: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    companion object {
        private const val KEY_ALIAS_PREFIX = "omnigent-private-profile-"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
