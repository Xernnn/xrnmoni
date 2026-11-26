package com.xern.xrnmoni.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.xern.xrnmoni.BuildConfig
import java.security.KeyStore
import java.util.Base64
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** What the setup screen shows. Times in ms, 0 for never; [lastPost] is an HTTP status, 0 unreachable, -1 none yet. */
data class Status(
    val hub: String,
    val hasToken: Boolean,
    val tokenRefused: Boolean,
    val lastPostAt: Long,
    val lastPost: Int,
    val queued: Int,
    val lastReadAt: Long,
    val usageAccess: Boolean,
)

/**
 * The hub's address, the device token (sealed with a key in the Android Keystore, never stored as
 * it is) and the figures for the setup screen, in this app's preferences.
 */
class Prefs(context: Context, private val queued: () -> Int) {
    private val prefs = context.getSharedPreferences("xrnmoni", Context.MODE_PRIVATE)
    private val flow = MutableStateFlow(read())

    val status: StateFlow<Status> get() = flow

    /** This install's own random id: every event id is made from it, so two phones never share one. */
    val installId: String
        get() = prefs.getString("install", null) ?: UUID.randomUUID().toString().replace("-", "").also {
            prefs.edit().putString("install", it).commit()
        }

    val hub: String get() = prefs.getString("hub", null) ?: BuildConfig.DEFAULT_HUB

    val token: String?
        get() = prefs.getString("token", null)?.let { runCatching { Sealed.open(it) }.getOrNull() }

    var tokenRefused: Boolean
        get() = prefs.getBoolean("tokenRefused", false)
        set(value) = prefs.edit().putBoolean("tokenRefused", value).apply()

    var bootKey: String?
        get() = prefs.getString("bootKey", null)
        set(value) = prefs.edit().putString("bootKey", value).apply()

    var usageAccess: Boolean
        get() = prefs.getBoolean("usageAccess", false)
        set(value) = prefs.edit().putBoolean("usageAccess", value).apply()

    var autostartOpened: Boolean
        get() = prefs.getBoolean("autostartOpened", false)
        set(value) = prefs.edit().putBoolean("autostartOpened", value).apply()

    /** Saves the hub and, when given, a new token; a new token is tried again even after a 401. */
    fun save(hub: String, token: String?) {
        val edit = prefs.edit().putString("hub", hub)
        if (token != null) edit.putString("token", Sealed.seal(token)).putBoolean("tokenRefused", false)
        edit.apply()
        publish()
    }

    fun posted(at: Long, status: Int) {
        prefs.edit().putLong("lastPostAt", at).putInt("lastPost", status).apply()
    }

    fun noteRead(at: Long) {
        prefs.edit().putLong("lastReadAt", at).apply()
    }

    /** Shows the figures as they are now. */
    fun publish() {
        flow.value = read()
    }

    private fun read() = Status(
        hub = hub,
        hasToken = prefs.contains("token"),
        tokenRefused = tokenRefused,
        lastPostAt = prefs.getLong("lastPostAt", 0),
        lastPost = prefs.getInt("lastPost", -1),
        queued = queued(),
        lastReadAt = prefs.getLong("lastReadAt", 0),
        usageAccess = usageAccess,
    )
}

/** The token at rest: AES-GCM under a key that never leaves the Android Keystore (xrnwal's Sealed). */
private object Sealed {
    private const val KEYSTORE = "AndroidKeyStore"
    private const val ALIAS = "xrnmoni-token"
    private const val CIPHER = "AES/GCM/NoPadding"
    private const val IV_BYTES = 12
    private const val TAG_BITS = 128

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    fun seal(text: String): String {
        val cipher = Cipher.getInstance(CIPHER).apply { init(Cipher.ENCRYPT_MODE, key()) }
        return Base64.getEncoder().encodeToString(cipher.iv + cipher.doFinal(text.toByteArray()))
    }

    fun open(sealed: String): String {
        val bytes = Base64.getDecoder().decode(sealed)
        val spec = GCMParameterSpec(TAG_BITS, bytes, 0, IV_BYTES)
        val cipher = Cipher.getInstance(CIPHER).apply { init(Cipher.DECRYPT_MODE, key(), spec) }
        return String(cipher.doFinal(bytes, IV_BYTES, bytes.size - IV_BYTES))
    }
}
