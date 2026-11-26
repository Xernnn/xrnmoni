package com.xern.xrnmoni.data

import com.xern.xrnmoni.domain.Batch
import com.xern.xrnmoni.domain.Event
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonPrimitive

/** What one post got. [status] is the HTTP status, 0 when the hub couldn't be reached. */
sealed interface Post {
    val status: Int

    data class Ok(override val status: Int) : Post

    /** Not the events' fault (the network, the hub, a proxy, too many at once): keep them, try later. */
    data class Keep(override val status: Int, val retryAfterS: Long? = null) : Post

    /** The token: nothing is posted until a new one is saved. */
    data object Unauthorized : Post {
        override val status = 401
    }

    /** The hub refused these events. */
    data class Refused(override val status: Int) : Post
}

/** What one round of sending did. */
sealed interface Sent {
    data object Empty : Sent

    data object Some : Sent

    data class Retry(val post: Post) : Sent

    data object Stopped : Sent
}

/**
 * The hub's address as kept: https only, without a trailing slash, or null. Plain http only to this
 * phone itself or to an emulator's host, and only where [local] allows it (debug builds, tests).
 */
fun hubAddress(text: String, local: Boolean): String? {
    val t = text.trim().trimEnd('/')
    val uri = runCatching { URI(t) }.getOrNull() ?: return null
    val host = uri.host ?: return null
    if (uri.userInfo != null || uri.query != null || uri.fragment != null) return null
    return when (uri.scheme?.lowercase()) {
        "https" -> t
        "http" -> t.takeIf { local && host in LOCAL_HOSTS }
        else -> null
    }
}

private val LOCAL_HOSTS = setOf("127.0.0.1", "localhost", "10.0.2.2")

/** Whether [text] is shaped like a device token the hub makes (`xrnmoni_<8 hex>_<43 characters>`). */
fun isDeviceToken(text: String): Boolean = DEVICE_TOKEN.matches(text)

private val DEVICE_TOKEN = Regex("xrnmoni_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")

/**
 * Sends the outbox to the hub, oldest first, in batches of [BATCH], as the desktop agent's Sender
 * does (agent/agent.py): a 2xx with `{"ok": true}` takes the batch out (events the hub lists as
 * rejected are dropped with it); a 2xx without it (a captive portal, a proxy page), 5xx, network
 * errors and the statuses in [KEEP] keep everything for later; 401 stops; any other 4xx sends each
 * event alone once, and drops the ones still refused, so one bad event can't hold up the rest.
 */
class Sender(
    hub: String,
    private val token: String,
    private val device: String,
    private val outbox: Outbox,
    private val log: (String) -> Unit = {},
) {
    private val url = URL("$hub/api/events")
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    /** The last post's outcome, for the status figures; null before any. */
    var last: Post? = null
        private set

    fun post(events: List<Event>): Post {
        val body = json.encodeToString(Batch.serializer(), Batch(device, "android", events)).toByteArray()
        val result = try {
            val c = url.openConnection() as HttpURLConnection
            try {
                c.instanceFollowRedirects = false
                c.useCaches = false
                c.connectTimeout = 15_000
                c.readTimeout = 30_000
                c.requestMethod = "POST"
                c.doOutput = true
                c.setRequestProperty("Authorization", "Bearer $token")
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                c.setRequestProperty("User-Agent", "xrnmoni-android")
                c.setFixedLengthStreamingMode(body.size)
                c.outputStream.use { it.write(body) }
                val status = c.responseCode
                val text = runCatching {
                    (if (status < 400) c.inputStream else c.errorStream)?.use { it.readBytes().decodeToString() }
                }.getOrNull().orEmpty()
                answer(status, text, c.getHeaderField("Retry-After"))
            } finally {
                c.disconnect()
            }
        } catch (e: IOException) {
            log("hub unreachable: ${e.javaClass.simpleName}")
            Post.Keep(0)
        }
        last = result
        return result
    }

    private fun answer(status: Int, text: String, retryAfter: String?): Post = when {
        status in 200..299 -> {
            val reply = runCatching { json.parseToJsonElement(text) as? JsonObject }.getOrNull()
            if (reply?.get("ok")?.jsonPrimitive?.booleanOrNull == true) {
                Post.Ok(status)
            } else {
                log("unexpected answer from the hub, events kept")
                Post.Keep(status)
            }
        }
        status == 401 -> Post.Unauthorized
        status >= 500 || status in KEEP || status in 300..399 -> Post.Keep(status, retryAfter?.trim()?.toLongOrNull())
        else -> Post.Refused(status)
    }

    /** Sends one batch. */
    fun sendOnce(): Sent {
        val batch = outbox.peek(BATCH)
        if (batch.isEmpty()) return Sent.Empty
        when (val result = post(batch.map { it.event })) {
            is Post.Ok -> {
                outbox.remove(batch.sumOf { it.lines })
                return Sent.Some
            }
            is Post.Keep -> return Sent.Retry(result)
            Post.Unauthorized -> return Sent.Stopped
            is Post.Refused -> {
                for (q in batch) {
                    when (val one = if (batch.size > 1) post(listOf(q.event)) else result) {
                        is Post.Keep -> return Sent.Retry(one)
                        Post.Unauthorized -> return Sent.Stopped
                        is Post.Refused -> {
                            log("hub refused an event, dropped: ${q.event.type} ${q.event.start}")
                            outbox.remove(q.lines)
                        }
                        is Post.Ok -> outbox.remove(q.lines)
                    }
                }
                return Sent.Some
            }
        }
    }

    companion object {
        const val BATCH = 250

        /** Answers that say the request, not the events, is wrong: keep everything (agent.py's KEEP_STATUSES). */
        val KEEP = setOf(403, 404, 405, 407, 408, 409, 425, 429)
    }
}
