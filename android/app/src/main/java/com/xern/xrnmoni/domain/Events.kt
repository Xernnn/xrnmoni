package com.xern.xrnmoni.domain

import java.security.MessageDigest
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One event as the hub takes it, the same shape the desktop agent posts (agent/agent.py): an
 * interval of at most [CHUNK_MS] inside one local day, or a point (boot, shutdown) whose end is
 * its start. Times are epoch seconds.
 */
@Serializable
data class Event(
    val id: String,
    val type: String,
    val app: String? = null,
    val title: String? = null,
    @SerialName("ts_start") val start: Double,
    @SerialName("ts_end") val end: Double,
    val duration: Double,
)

/** What a batch is posted as: `{"device", "os", "events"}`. The hub files it under the token's device. */
@Serializable
data class Batch(val device: String, val os: String, val events: List<Event>)

/** The longest interval sent, as the desktop agent's CHUNK. */
const val CHUNK_MS = 60_000L

object Ids {
    /**
     * The same id for the same parts, shaped like a UUID4's hex: SHA-256 of the parts joined by
     * U+001F, its first 16 bytes with the version and variant bits set (agent.py's derived_id).
     * The hub keeps the first event with an id and skips any other, so an interval read twice from
     * the phone's usage history is counted once.
     */
    fun derived(vararg parts: Any?): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(parts.joinToString("\u001f").toByteArray())
        val b = digest.copyOf(16)
        b[6] = ((b[6].toInt() and 0x0f) or 0x40).toByte()
        b[8] = ((b[8].toInt() and 0x3f) or 0x80).toByte()
        return b.joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }
}
