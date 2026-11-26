package com.xern.xrnmoni.domain

import java.time.Instant
import java.time.ZoneId
import kotlinx.serialization.Serializable

/** One record of the phone's usage history (UsageEvents.Event), with no android.* in it. */
data class Usage(val time: Long, val type: Int, val pkg: String, val cls: String? = null)

/** The UsageEvents.Event types the tracker reads. */
object UsageType {
    const val ACTIVITY_RESUMED = 1
    const val ACTIVITY_PAUSED = 2
    const val SCREEN_INTERACTIVE = 15
    const val SCREEN_NON_INTERACTIVE = 16
    const val KEYGUARD_SHOWN = 17
    const val KEYGUARD_HIDDEN = 18
    const val ACTIVITY_STOPPED = 23
    const val DEVICE_SHUTDOWN = 26
    const val DEVICE_STARTUP = 27

    val read = setOf(
        ACTIVITY_RESUMED, ACTIVITY_PAUSED, SCREEN_INTERACTIVE, SCREEN_NON_INTERACTIVE, KEYGUARD_SHOWN,
        KEYGUARD_HIDDEN, ACTIVITY_STOPPED, DEVICE_SHUTDOWN, DEVICE_STARTUP,
    )
}

/** The interval the phone is in: what, since when, and where its unsent part begins ([sent]). Times in ms. */
@Serializable
data class Open(val type: String, val pkg: String? = null, val label: String? = null, val start: Long, val sent: Long)

/**
 * Everything the tracker carries from one run to the next. [cursor]: the usage history before it
 * has been read. [alive]: the last moment the phone is known to have been on (an event, or a run).
 */
@Serializable
data class TrackState(
    val cursor: Long,
    val alive: Long = cursor,
    val screenOn: Boolean = true,
    val locked: Boolean = false,
    val front: String? = null,
    val frontClass: String? = null,
    val open: Open? = null,
)

/** A run's outcome: the state for the next run and the events to send. */
data class Step(val state: TrackState, val events: List<Event>)

/**
 * Turns the phone's usage history into the desktop agent's intervals: `app` while an app is in
 * front, `locked` while the lock screen shows with the screen on, `sleep` while the screen is off
 * (or the phone is off). Sleep beats locked beats the app, as on the desktop.
 *
 * Every interval is cut into pieces of at most [CHUNK_MS] and at each local midnight, counted from
 * its start, and each piece's id comes from the install, the interval and the piece's start: the
 * same history read again, or read in two runs instead of one, gives the same events. An interval
 * still open at the end of a run sends its whole pieces; the last, partial one waits for the next.
 *
 * [install]: this install's own random id, so two phones never make the same event id (the hub's
 * ids are unique across devices). [currentBoot]: when this boot began; a DEVICE_STARTUP of this
 * boot is left to [bootEvent], which reports it once per boot.
 */
class Tracker(
    private val install: String,
    private val zone: ZoneId,
    private val label: (String) -> String = { it },
    private val currentBoot: Long? = null,
) {
    /** Reads [usage] from [begin] up to [end] (ms, end excluded) on from [state]. */
    fun run(state: TrackState, usage: List<Usage>, begin: Long, end: Long): Step {
        val out = mutableListOf<Event>()
        var s = state
        if (begin > s.cursor) {
            // History was skipped (more than the phone keeps): what was open ends where the phone was
            // last seen, and nothing is known about what came between.
            s = close(s, maxOf(s.alive, s.open?.sent ?: 0), out)
            s = TrackState(cursor = begin)
        }
        for (u in usage.sortedBy { it.time }) {
            if (u.time < s.cursor || u.time >= end) continue
            s = apply(s, u, out)
            s = settle(s, u.time, out)
        }
        s = s.copy(cursor = maxOf(s.cursor, end), alive = maxOf(s.alive, end))
        return Step(flushWhole(s, end, out), out)
    }

    /** The boot this phone runs in, once per boot: its id comes from the boot count. */
    fun bootEvent(bootMs: Long, bootKey: Any): Event = point("boot", bootMs, bootKey)

    private fun apply(s: TrackState, u: Usage, out: MutableList<Event>): TrackState = when (u.type) {
        UsageType.SCREEN_INTERACTIVE -> s.copy(screenOn = true)
        UsageType.SCREEN_NON_INTERACTIVE -> s.copy(screenOn = false)
        UsageType.KEYGUARD_SHOWN -> s.copy(locked = true)
        UsageType.KEYGUARD_HIDDEN -> s.copy(locked = false)
        UsageType.ACTIVITY_RESUMED -> s.copy(front = u.pkg, frontClass = u.cls)
        // Android 10 and later stop the activity after the next one resumes, so this ends only an
        // app that nothing has replaced (back to a screen with no activity). A pause alone doesn't:
        // the next activity's resume follows it.
        UsageType.ACTIVITY_STOPPED ->
            if (s.front == u.pkg && (s.frontClass == null || u.cls == null || s.frontClass == u.cls)) {
                s.copy(front = null, frontClass = null)
            } else {
                s
            }
        UsageType.DEVICE_SHUTDOWN -> {
            out += point("shutdown", u.time, u.time)
            s.copy(screenOn = false, front = null, frontClass = null)
        }
        UsageType.DEVICE_STARTUP -> {
            // No shutdown before it (the battery ran out, a crash): the phone was off from when it
            // was last seen, not in the app that was in front then.
            val off = if (s.screenOn) settle(s.copy(screenOn = false), maxOf(s.alive, s.open?.sent ?: 0), out) else s
            if (currentBoot == null || u.time < currentBoot - BOOT_SLACK_MS) out += point("boot", u.time, u.time)
            off.copy(screenOn = true, locked = true, front = null, frontClass = null)
        }
        else -> s
    }

    private fun want(s: TrackState): Pair<String, String?>? = when {
        !s.screenOn -> "sleep" to null
        s.locked -> "locked" to null
        s.front != null -> "app" to s.front
        else -> null
    }

    /** At [t]: ends what is open if it's no longer what the phone is doing, and opens that. */
    private fun settle(s: TrackState, t: Long, out: MutableList<Event>): TrackState {
        val w = want(s)
        val open = s.open
        val alive = maxOf(s.alive, t)
        if (open != null && w != null && open.type == w.first && open.pkg == w.second) return s.copy(alive = alive)
        val closed = close(s, t, out)
        val next = w?.let { (type, pkg) ->
            Open(type, pkg, pkg?.let(label), start = t, sent = t)
        }
        return closed.copy(open = next, alive = alive)
    }

    private fun close(s: TrackState, t: Long, out: MutableList<Event>): TrackState {
        val open = s.open ?: return s
        for ((a, b) in pieces(open.start, maxOf(t, open.sent))) {
            if (a >= open.sent) out += piece(open, a, b)
        }
        return s.copy(open = null)
    }

    /** Sends the open interval's whole pieces up to [end]; the partial last one waits. */
    private fun flushWhole(s: TrackState, end: Long, out: MutableList<Event>): TrackState {
        val open = s.open ?: return s
        var sent = open.sent
        for ((a, b) in pieces(open.start, end)) {
            val whole = b - a == CHUNK_MS || b == nextMidnight(a)
            if (!whole) break
            if (a >= open.sent) {
                out += piece(open, a, b)
                sent = b
            }
        }
        return s.copy(open = open.copy(sent = sent))
    }

    /** [start, stop) cut into pieces of at most [CHUNK_MS] and at every local midnight (agent.py's pieces). */
    fun pieces(start: Long, stop: Long): List<Pair<Long, Long>> {
        val out = mutableListOf<Pair<Long, Long>>()
        var a = start
        while (a < stop) {
            val b = minOf(stop, a + CHUNK_MS, nextMidnight(a))
            out += a to b
            a = b
        }
        return out
    }

    private fun nextMidnight(ms: Long): Long =
        Instant.ofEpochMilli(ms).atZone(zone).toLocalDate().plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()

    private fun piece(open: Open, a: Long, b: Long) = Event(
        id = Ids.derived("android", install, open.type, open.pkg ?: "", open.start, a),
        type = open.type,
        app = if (open.type == "app") open.label ?: open.pkg else null,
        title = open.pkg,
        start = a / 1000.0,
        end = b / 1000.0,
        duration = (b - a) / 1000.0,
    )

    private fun point(type: String, ms: Long, key: Any) = Event(
        id = Ids.derived("android", install, type, key),
        type = type,
        start = ms / 1000.0,
        end = ms / 1000.0,
        duration = 0.0,
    )

    companion object {
        /** A DEVICE_STARTUP this close to the boot time is this boot's. */
        const val BOOT_SLACK_MS = 5 * 60_000L
    }
}
