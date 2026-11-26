package com.xern.xrnmoni.data

import com.xern.xrnmoni.domain.Event
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** One queued event and when it was queued (ms): the age cap counts from then. */
@Serializable
private data class Line(val q: Long, val e: Event)

/** An event at the head of the outbox and how many lines it takes there (unreadable ones before it included). */
data class Queued(val event: Event, val lines: Int)

/**
 * The events the hub hasn't taken yet, in a folder of their own: `outbox-<n>.jsonl` (one event a
 * line, oldest first) and `outbox.head` ("<n> <lines sent>"). An event goes in, synced to disk, the
 * moment it is made, and comes out only after the hub answered for it, so a reboot, a crash or a
 * week without the hub loses nothing.
 *
 * Sending moves the head on (a small file swapped in whole); the sent lines are dropped by writing
 * what's left to the next `outbox-<n+1>.jsonl` once at least half the file is sent, and then the
 * head names that file. A crash at any point leaves one consistent pair: a half-written next file
 * isn't named by the head and is deleted on the next start, and a half-written last line is cut off.
 *
 * Capped at [maxRows] events and [maxAgeMs] unsent, the oldest dropped first, so a hub that is gone
 * for good can't fill the phone.
 */
class Outbox(
    private val dir: File,
    private val maxRows: Int = 200_000,
    private val maxAgeMs: Long = 30L * 86_400_000,
) {
    private val lock = Any()
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val headFile = File(dir, "outbox.head")
    private var gen = 0L
    private var head = 0
    private var total = 0

    init {
        synchronized(lock) { load() }
    }

    private fun file(n: Long) = File(dir, "outbox-$n.jsonl")

    private fun load() {
        dir.mkdirs()
        val saved = runCatching { headFile.readText().trim().split(" ") }.getOrNull()
        gen = saved?.getOrNull(0)?.toLongOrNull() ?: 0L
        head = saved?.getOrNull(1)?.toIntOrNull() ?: 0
        val current = file(gen)
        dir.listFiles()?.forEach {
            val stray = (it.name.startsWith("outbox-") && it.name != current.name) || it.name.endsWith(".tmp")
            if (stray) it.delete()
        }
        if (!current.exists()) current.createNewFile()
        cutTornLine(current)
        total = current.useLines { lines -> lines.count() }
        head = head.coerceIn(0, total)
    }

    /** A line cut short by a crash mid-write is dropped: everything up to the last newline stays. */
    private fun cutTornLine(f: File) {
        RandomAccessFile(f, "rw").use { raf ->
            var end = raf.length()
            while (end > 0) {
                raf.seek(end - 1)
                if (raf.read() == '\n'.code) break
                end--
            }
            if (end != raf.length()) raf.setLength(end)
        }
    }

    /** Puts [events] at the end, synced to disk before it returns. */
    fun add(events: List<Event>, now: Long = System.currentTimeMillis()) {
        if (events.isEmpty()) return
        val text = buildString {
            for (e in events) append(json.encodeToString(Line.serializer(), Line(now, e))).append('\n')
        }
        synchronized(lock) {
            FileOutputStream(file(gen), true).use {
                it.write(text.toByteArray())
                it.fd.sync()
            }
            total += events.size
        }
    }

    /** Up to [n] of the oldest events, oldest first. Unreadable lines at the head are dropped here. */
    fun peek(n: Int): List<Queued> = synchronized(lock) {
        val out = mutableListOf<Queued>()
        var skipped = 0
        var leading = 0
        file(gen).useLines { lines ->
            for (line in lines.drop(head)) {
                if (out.size >= n) break
                val e = runCatching { json.decodeFromString(Line.serializer(), line).e }.getOrNull()
                if (e == null) {
                    if (out.isEmpty()) leading++ else skipped++
                    continue
                }
                out += Queued(e, 1 + skipped)
                skipped = 0
            }
        }
        if (leading > 0) moveHead(leading)
        out
    }

    /** Takes the oldest [lines] out: the ones the hub has answered for. */
    fun remove(lines: Int) {
        if (lines <= 0) return
        synchronized(lock) { moveHead(lines) }
    }

    /** Applies the caps; how many events were dropped. */
    fun trim(now: Long = System.currentTimeMillis()): Int = synchronized(lock) {
        var dropped = 0
        val over = (total - head) - maxRows
        if (over > 0) {
            moveHead(over)
            dropped += over
        }
        var old = 0
        file(gen).useLines { lines ->
            for (line in lines.drop(head)) {
                val q = runCatching { json.decodeFromString(Line.serializer(), line).q }.getOrNull()
                if (q != null && q >= now - maxAgeMs) break
                old++
            }
        }
        if (old > 0) moveHead(old)
        dropped + old
    }

    val size: Int get() = synchronized(lock) { total - head }

    private fun moveHead(lines: Int) {
        head = (head + lines).coerceAtMost(total)
        if (head >= total) {
            switchTo(emptyList())
        } else if (head >= COMPACT_AT && head * 2 >= total) {
            switchTo(file(gen).useLines { it.drop(head).toList() })
        } else {
            writeHead(gen, head)
        }
    }

    /** Starts the next file with [rest] in it, then points the head at it, then deletes the old one. */
    private fun switchTo(rest: List<String>) {
        val next = file(gen + 1)
        FileOutputStream(next).use { out ->
            for (line in rest) out.write((line + "\n").toByteArray())
            out.fd.sync()
        }
        writeHead(gen + 1, 0)
        file(gen).delete()
        gen += 1
        head = 0
        total = rest.size
    }

    private fun writeHead(n: Long, lines: Int) {
        val tmp = File(dir, "outbox.head.tmp")
        FileOutputStream(tmp).use {
            it.write("$n $lines\n".toByteArray())
            it.fd.sync()
        }
        if (!tmp.renameTo(headFile)) throw java.io.IOException("can't replace $headFile")
    }

    companion object {
        const val COMPACT_AT = 1000
    }
}
