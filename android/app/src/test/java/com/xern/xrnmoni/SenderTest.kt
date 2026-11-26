package com.xern.xrnmoni

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import com.xern.xrnmoni.data.Outbox
import com.xern.xrnmoni.data.Post
import com.xern.xrnmoni.data.Sender
import com.xern.xrnmoni.data.Sent
import com.xern.xrnmoni.data.hubAddress
import com.xern.xrnmoni.data.isDeviceToken
import com.xern.xrnmoni.domain.Event
import java.io.File
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.nio.file.Files
import java.util.Collections
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class SenderTest {
    private lateinit var server: HttpServer
    private lateinit var hub: String
    private lateinit var dir: File
    private lateinit var outbox: Outbox
    private val requests: MutableList<Pair<HttpExchange, JsonObject>> = Collections.synchronizedList(mutableListOf())
    private var reply: (HttpExchange, JsonObject) -> Unit = { it, _ -> it.answer(200, """{"ok":true,"inserted":1,"duplicates":0,"rejected":[]}""") }
    private val token = "xrnmoni_0123abcd_" + "A".repeat(43)

    private fun HttpExchange.answer(code: Int, body: String, vararg headers: Pair<String, String>) {
        headers.forEach { (k, v) -> responseHeaders.add(k, v) }
        val bytes = body.toByteArray()
        sendResponseHeaders(code, if (bytes.isEmpty()) -1 else bytes.size.toLong())
        if (bytes.isNotEmpty()) responseBody.use { it.write(bytes) } else close()
    }

    @Before fun start() {
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") {
            val body = Json.parseToJsonElement(it.requestBody.readBytes().decodeToString()).jsonObject
            requests += it to body
            reply(it, body)
        }
        server.start()
        hub = "http://127.0.0.1:${server.address.port}/xrnmoni"
        dir = Files.createTempDirectory("sender").toFile()
        outbox = Outbox(dir)
    }

    @After fun stop() {
        server.stop(0)
        dir.deleteRecursively()
    }

    private fun event(n: Int, title: String = "com.example.chat") = Event(
        id = "%032x".format(n), type = "app", app = "Chat", title = title,
        start = 1_795_100_000.0 + n, end = 1_795_100_030.0 + n, duration = 30.0,
    )

    private fun sender() = Sender(hub, token, "Phone 7", outbox)

    @Test fun anOkTakesTheBatchOutAndPostsTheDesktopAgentsShape() {
        outbox.add(listOf(event(1), event(2)))
        assertEquals(Sent.Some, sender().sendOnce())
        assertEquals(0, outbox.size)
        val (exchange, body) = requests.single()
        assertEquals("POST", exchange.requestMethod)
        assertEquals("/xrnmoni/api/events", exchange.requestURI.path)
        assertEquals("Bearer $token", exchange.requestHeaders.getFirst("Authorization"))
        assertEquals("Phone 7", body["device"]?.jsonPrimitive?.content)
        assertEquals("android", body["os"]?.jsonPrimitive?.content)
        val first = body["events"]!!.jsonArray[0].jsonObject
        assertEquals("%032x".format(1), first["id"]?.jsonPrimitive?.content)
        assertEquals(setOf("id", "type", "app", "title", "ts_start", "ts_end", "duration"), first.keys)
        assertEquals(1_795_100_001.0, first["ts_start"]!!.jsonPrimitive.content.toDouble(), 0.0)
        assertEquals(Sent.Empty, sender().sendOnce())
    }

    @Test fun eventsTheHubRejectsGoWithTheBatch() {
        reply = { it, _ -> it.answer(200, """{"ok":true,"inserted":1,"duplicates":0,"rejected":[{"index":0,"id":null,"error":"bad type"}]}""") }
        outbox.add(listOf(event(1), event(2)))
        assertEquals(Sent.Some, sender().sendOnce())
        assertEquals(0, outbox.size)
    }

    @Test fun aCaptivePortalsPageKeepsTheEvents() {
        reply = { it, _ -> it.answer(200, "<html>Sign in to the Wi-Fi</html>") }
        outbox.add(listOf(event(1)))
        assertEquals(Sent.Retry(Post.Keep(200)), sender().sendOnce())
        assertEquals(1, outbox.size)
    }

    @Test fun aRefusedTokenStopsAndKeepsEverything() {
        reply = { it, _ -> it.answer(401, "bad or missing token") }
        outbox.add(listOf(event(1), event(2)))
        val sender = sender()
        assertEquals(Sent.Stopped, sender.sendOnce())
        assertEquals(2, outbox.size)
        assertEquals(1, requests.size)
        assertEquals(Post.Unauthorized, sender.last)
    }

    @Test fun aServerErrorKeepsTheEvents() {
        reply = { it, _ -> it.answer(503, "the database can't be written") }
        outbox.add(listOf(event(1)))
        assertEquals(Sent.Retry(Post.Keep(503)), sender().sendOnce())
        assertEquals(1, outbox.size)
    }

    @Test fun tooManyAtOnceKeepsTheEventsAndSaysWhenToTryAgain() {
        reply = { it, _ -> it.answer(429, "too many wrong tokens; try again later", "Retry-After" to "900") }
        outbox.add(listOf(event(1)))
        assertEquals(Sent.Retry(Post.Keep(429, 900)), sender().sendOnce())
        assertEquals(1, outbox.size)
    }

    @Test fun aRedirectKeepsTheEvents() {
        reply = { it, _ -> it.answer(302, "", "Location" to "https://example.org/") }
        outbox.add(listOf(event(1)))
        assertEquals(Sent.Retry(Post.Keep(302)), sender().sendOnce())
        assertEquals(1, outbox.size)
    }

    @Test fun aBatchTheHubRefusesIsSentOneByOneAndOnlyTheBadEventIsDropped() {
        reply = { it, body ->
            val events = body["events"]!!.jsonArray
            if (events.any { e -> e.jsonObject["title"]?.jsonPrimitive?.content == "bad" }) {
                it.answer(400, "body must be JSON")
            } else {
                it.answer(200, """{"ok":true,"inserted":${events.size},"duplicates":0,"rejected":[]}""")
            }
        }
        outbox.add(listOf(event(1), event(2, title = "bad"), event(3)))
        assertEquals(Sent.Some, sender().sendOnce())
        assertEquals(0, outbox.size)
        assertEquals(listOf(3, 1, 1, 1), requests.map { it.second["events"]!!.jsonArray.size })
    }

    @Test fun oneByOneStopsWhereTheHubGoesAwayAndKeepsTheRest() {
        var calls = 0
        reply = { it, _ ->
            calls++
            when (calls) {
                1 -> it.answer(400, "malformed")
                2 -> it.answer(200, """{"ok":true,"inserted":1,"duplicates":0,"rejected":[]}""")
                else -> it.answer(502, "bad gateway")
            }
        }
        outbox.add(listOf(event(1), event(2), event(3)))
        assertEquals(Sent.Retry(Post.Keep(502)), sender().sendOnce())
        assertEquals(listOf(2, 3), outbox.peek(10).map { it.event.id.toInt(16) })
    }

    @Test fun anUnreachableHubKeepsTheEvents() {
        val closed = ServerSocket(0).use { it.localPort }
        outbox.add(listOf(event(1)))
        val sender = Sender("http://127.0.0.1:$closed/xrnmoni", token, "Phone 7", outbox)
        assertEquals(Sent.Retry(Post.Keep(0)), sender.sendOnce())
        assertEquals(1, outbox.size)
    }

    @Test fun theHubsAddressIsHttpsOnlyBarThisPhoneInDebugBuilds() {
        assertEquals("https://hub.example.org/xrnmoni", hubAddress(" https://hub.example.org/xrnmoni/ ", local = false))
        assertNull(hubAddress("http://hub.example.org/xrnmoni", local = true))
        assertNull(hubAddress("http://127.0.0.1:5245/xrnmoni", local = false))
        assertEquals("http://127.0.0.1:5245/xrnmoni", hubAddress("http://127.0.0.1:5245/xrnmoni", local = true))
        assertEquals("http://10.0.2.2:5245", hubAddress("http://10.0.2.2:5245", local = true))
        assertNull(hubAddress("https://alex:secret@hub.example.org/xrnmoni", local = false))
        assertNull(hubAddress("hub.example.org/xrnmoni", local = false))
        assertNull(hubAddress("", local = false))
    }

    @Test fun onlyATokenShapedLikeTheHubsIsTaken() {
        assertTrue(isDeviceToken(token))
        assertFalse(isDeviceToken("xrnmoni_0123ABCD_" + "A".repeat(43)))
        assertFalse(isDeviceToken("xrnmoni_0123abcd_" + "A".repeat(42)))
        assertFalse(isDeviceToken("xrndev_0123abcd_" + "A".repeat(43)))
        assertFalse(isDeviceToken(" $token"))
    }
}
