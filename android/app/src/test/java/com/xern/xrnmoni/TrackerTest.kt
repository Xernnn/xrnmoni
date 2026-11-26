package com.xern.xrnmoni

import com.xern.xrnmoni.domain.Event
import com.xern.xrnmoni.domain.Ids
import com.xern.xrnmoni.domain.Open
import com.xern.xrnmoni.domain.TrackState
import com.xern.xrnmoni.domain.Tracker
import com.xern.xrnmoni.domain.Usage
import com.xern.xrnmoni.domain.UsageType.ACTIVITY_PAUSED
import com.xern.xrnmoni.domain.UsageType.ACTIVITY_RESUMED
import com.xern.xrnmoni.domain.UsageType.ACTIVITY_STOPPED
import com.xern.xrnmoni.domain.UsageType.DEVICE_SHUTDOWN
import com.xern.xrnmoni.domain.UsageType.DEVICE_STARTUP
import com.xern.xrnmoni.domain.UsageType.KEYGUARD_HIDDEN
import com.xern.xrnmoni.domain.UsageType.KEYGUARD_SHOWN
import com.xern.xrnmoni.domain.UsageType.SCREEN_INTERACTIVE
import com.xern.xrnmoni.domain.UsageType.SCREEN_NON_INTERACTIVE
import java.time.LocalDateTime
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TrackerTest {
    // A zone far from UTC, so a midnight that only works in UTC would show.
    private val zone = ZoneId.of("Pacific/Auckland")
    private val labels = mapOf("com.example.chat" to "Chat", "com.example.maps" to "Maps")
    private val tracker = Tracker("install-a", zone, { labels[it] ?: it })

    private fun at(h: Int, m: Int, s: Int = 0, day: Int = 20): Long =
        LocalDateTime.of(2026, 11, day, h, m, s).atZone(zone).toInstant().toEpochMilli()

    private fun secs(ms: Long) = ms / 1000.0

    private fun state(from: Long) = TrackState(cursor = from)

    /** (type, app, start, end) of each event, for comparing the shape of a run's output. */
    private fun shape(events: List<Event>) = events.map { listOf(it.type, it.app, it.start, it.end) }

    @Test fun derivedIdsMatchTheDesktopAgents() {
        // agent.py: derived_id("boot", "laptop", 42)
        assertEquals("b2c69bef21c34f3aa638d48a6a6754e2", Ids.derived("boot", "laptop", 42))
    }

    @Test fun appSwitchesBecomeAppIntervalsCutIntoMinutePieces() {
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat", "Main"),
            Usage(at(10, 2, 30), ACTIVITY_PAUSED, "com.example.chat", "Main"),
            Usage(at(10, 2, 30), ACTIVITY_RESUMED, "com.example.maps", "Map"),
            Usage(at(10, 2, 31), ACTIVITY_STOPPED, "com.example.chat", "Main"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 3))
        assertEquals(
            listOf(
                listOf("app", "Chat", secs(at(10, 0)), secs(at(10, 1))),
                listOf("app", "Chat", secs(at(10, 1)), secs(at(10, 2))),
                listOf("app", "Chat", secs(at(10, 2)), secs(at(10, 2, 30))),
            ),
            shape(step.events),
        )
        val e = step.events.first()
        assertEquals("com.example.chat", e.title)
        assertEquals(60.0, e.duration, 0.0)
        assertTrue(step.events.all { it.id.matches(Regex("[0-9a-f]{32}")) })
        assertEquals(step.events.size, step.events.map { it.id }.toSet().size)
        // Maps is in front, its first minute not over yet.
        assertEquals(Open("app", "com.example.maps", "Maps", at(10, 2, 30), at(10, 2, 30)), step.state.open)
    }

    @Test fun anAppWithoutALabelGoesByItsPackage() {
        val usage = listOf(Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.unknown"), Usage(at(10, 0, 20), SCREEN_NON_INTERACTIVE, "android"))
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 1))
        assertEquals("com.example.unknown", step.events.first().app)
    }

    @Test fun theScreenOffIsSleepAndTheLockScreenIsLocked() {
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat", "Main"),
            Usage(at(10, 0, 30), SCREEN_NON_INTERACTIVE, "android"),
            Usage(at(10, 0, 30), ACTIVITY_PAUSED, "com.example.chat", "Main"),
            Usage(at(10, 0, 31), ACTIVITY_STOPPED, "com.example.chat", "Main"),
            Usage(at(10, 0, 31), KEYGUARD_SHOWN, "android"),
            Usage(at(10, 2), SCREEN_INTERACTIVE, "android"),
            Usage(at(10, 2, 5), KEYGUARD_HIDDEN, "android"),
            Usage(at(10, 2, 6), ACTIVITY_RESUMED, "com.example.chat", "Main"),
            Usage(at(10, 2, 20), SCREEN_NON_INTERACTIVE, "android"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 2, 40))
        assertEquals(
            listOf(
                listOf("app", "Chat", secs(at(10, 0)), secs(at(10, 0, 30))),
                listOf("sleep", null, secs(at(10, 0, 30)), secs(at(10, 1, 30))),
                listOf("sleep", null, secs(at(10, 1, 30)), secs(at(10, 2))),
                listOf("locked", null, secs(at(10, 2)), secs(at(10, 2, 5))),
                listOf("app", "Chat", secs(at(10, 2, 6)), secs(at(10, 2, 20))),
            ),
            shape(step.events),
        )
        assertNull(step.events[1].title)
        assertEquals("sleep", step.state.open?.type)
    }

    @Test fun sleepBeatsTheLockScreen() {
        val usage = listOf(
            Usage(at(10, 0), KEYGUARD_SHOWN, "android"),
            Usage(at(10, 0, 10), SCREEN_NON_INTERACTIVE, "android"),
            Usage(at(10, 0, 40), SCREEN_INTERACTIVE, "android"),
            Usage(at(10, 0, 50), KEYGUARD_HIDDEN, "android"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 1))
        assertEquals(
            listOf(
                listOf("locked", null, secs(at(10, 0)), secs(at(10, 0, 10))),
                listOf("sleep", null, secs(at(10, 0, 10)), secs(at(10, 0, 40))),
                listOf("locked", null, secs(at(10, 0, 40)), secs(at(10, 0, 50))),
            ),
            shape(step.events),
        )
        assertNull(step.state.open) // unlocked, no app in front
    }

    @Test fun aStoppedActivityEndsOnlyTheAppStillInFront() {
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat", "Main"),
            Usage(at(10, 0, 10), ACTIVITY_RESUMED, "com.example.maps", "Map"),
            Usage(at(10, 0, 11), ACTIVITY_STOPPED, "com.example.chat", "Main"),
            Usage(at(10, 0, 30), ACTIVITY_STOPPED, "com.example.maps", "Map"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 1))
        assertEquals(
            listOf(
                listOf("app", "Chat", secs(at(10, 0)), secs(at(10, 0, 10))),
                listOf("app", "Maps", secs(at(10, 0, 10)), secs(at(10, 0, 30))),
            ),
            shape(step.events),
        )
    }

    @Test fun anIntervalIsCutAtLocalMidnight() {
        val usage = listOf(
            Usage(at(23, 59, 30), ACTIVITY_RESUMED, "com.example.chat"),
            Usage(at(0, 0, 45, day = 21), SCREEN_NON_INTERACTIVE, "android"),
        )
        val step = tracker.run(state(at(23, 59)), usage, at(23, 59), at(0, 1, day = 21))
        assertEquals(
            listOf(
                listOf("app", "Chat", secs(at(23, 59, 30)), secs(at(0, 0, day = 21))),
                listOf("app", "Chat", secs(at(0, 0, day = 21)), secs(at(0, 0, 45, day = 21))),
            ),
            shape(step.events),
        )
    }

    @Test fun theSameHistoryReadAgainGivesTheSameIds() {
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat"),
            Usage(at(10, 3, 10), ACTIVITY_RESUMED, "com.example.maps"),
            Usage(at(10, 4), SCREEN_NON_INTERACTIVE, "android"),
        )
        val first = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 6))
        val again = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 6))
        assertEquals(first, again)
        // Another phone with the same history makes other ids: the hub's ids are unique across devices.
        val other = Tracker("install-b", zone, { labels[it] ?: it }).run(state(at(9, 59)), usage, at(9, 59), at(10, 6))
        assertTrue(first.events.map { it.id }.intersect(other.events.map { it.id }.toSet()).isEmpty())
    }

    @Test fun aRunSplitInTwoSendsWhatOneRunSends() {
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat"),
            Usage(at(10, 3, 10), ACTIVITY_RESUMED, "com.example.maps"),
            Usage(at(10, 4), SCREEN_NON_INTERACTIVE, "android"),
            Usage(at(10, 9), SCREEN_INTERACTIVE, "android"),
            Usage(at(10, 9, 1), ACTIVITY_RESUMED, "com.example.chat"),
        )
        val whole = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 12))
        for (cut in listOf(at(10, 1, 15), at(10, 3, 10), at(10, 5, 59), at(10, 9, 1))) {
            val one = tracker.run(state(at(9, 59)), usage, at(9, 59), cut)
            val two = tracker.run(one.state, usage, cut, at(10, 12))
            assertEquals("cut at $cut", whole.events, one.events + two.events)
            assertEquals(whole.state, two.state)
        }
    }

    @Test fun theOpenIntervalCarriesOverAndItsTailWaitsForItsEnd() {
        val start = listOf(Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat"))
        val one = tracker.run(state(at(9, 59)), start, at(9, 59), at(10, 2, 30))
        assertEquals(2, one.events.size) // 10:00-10:01, 10:01-10:02; 10:02-10:02:30 waits
        val two = tracker.run(one.state, emptyList(), at(10, 2, 30), at(10, 3, 30))
        assertEquals(listOf(listOf("app", "Chat", secs(at(10, 2)), secs(at(10, 3)))), shape(two.events))
        val end = listOf(Usage(at(10, 3, 40), SCREEN_NON_INTERACTIVE, "android"))
        val three = tracker.run(two.state, end, at(10, 3, 30), at(10, 4))
        assertEquals(listOf(listOf("app", "Chat", secs(at(10, 3)), secs(at(10, 3, 40)))), shape(three.events))
    }

    @Test fun aShutdownIsAPointAndThePhoneSleepsUntilItStartsAgain() {
        val tracker = Tracker("install-a", zone, { labels[it] ?: it }, currentBoot = at(12, 0))
        val usage = listOf(
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat"),
            Usage(at(10, 0, 20), DEVICE_SHUTDOWN, "android"),
            Usage(at(10, 1, 50), DEVICE_STARTUP, "android"),
            Usage(at(10, 2, 30), KEYGUARD_HIDDEN, "android"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 3))
        assertEquals(
            listOf(
                listOf("app", "Chat", secs(at(10, 0)), secs(at(10, 0, 20))),
                listOf("shutdown", null, secs(at(10, 0, 20)), secs(at(10, 0, 20))),
                listOf("sleep", null, secs(at(10, 0, 20)), secs(at(10, 1, 20))),
                listOf("sleep", null, secs(at(10, 1, 20)), secs(at(10, 1, 50))),
                // an earlier boot than this one, so the history reports it
                listOf("boot", null, secs(at(10, 1, 50)), secs(at(10, 1, 50))),
                listOf("locked", null, secs(at(10, 1, 50)), secs(at(10, 2, 30))),
            ),
            shape(step.events).sortedWith(compareBy({ it[2] as Double }, { it[0] as String })),
        )
    }

    @Test fun aStartupWithNoShutdownEndsTheAppWhereThePhoneWasLastSeen() {
        val one = tracker.run(state(at(9, 59)), listOf(Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat")), at(9, 59), at(10, 1, 30))
        assertEquals(at(10, 1, 30), one.state.alive)
        // The battery ran out; nothing until the phone starts again at noon.
        val two = tracker.run(one.state, listOf(Usage(at(12, 0), DEVICE_STARTUP, "android")), at(10, 1, 30), at(12, 0, 10))
        val kinds = shape(two.events)
        assertEquals(listOf("app", "Chat", secs(at(10, 1)), secs(at(10, 1, 30))), kinds.first())
        val sleep = two.events.filter { it.type == "sleep" }
        assertEquals(listOf("sleep", null, secs(at(10, 1, 30)), secs(at(10, 2, 30))), shape(sleep).first())
        assertEquals(listOf("boot", null, secs(at(12, 0)), secs(at(12, 0))), shape(two.events.filter { it.type == "boot" }).single())
        assertEquals(secs(at(12, 0)), sleep.last().end, 0.0)
        assertEquals(secs(at(12, 0) - at(10, 1, 30)), sleep.sumOf { it.duration }, 1e-6)
        assertEquals("locked", two.state.open?.type)
    }

    @Test fun aStartupOfThisBootIsLeftToTheBootEvent() {
        val tracker = Tracker("install-a", zone, currentBoot = at(10, 0))
        val step = tracker.run(state(at(9, 59)), listOf(Usage(at(10, 0, 30), DEVICE_STARTUP, "android")), at(9, 59), at(10, 1))
        assertTrue(step.events.none { it.type == "boot" })
        val boot = tracker.bootEvent(at(10, 0), 57)
        assertEquals(boot.id, tracker.bootEvent(at(10, 0) + 400, 57).id) // the boot time moves a little between starts
        assertNotEquals(boot.id, tracker.bootEvent(at(10, 0), 58).id)
        assertEquals("boot", boot.type)
        assertEquals(boot.start, boot.end, 0.0)
    }

    @Test fun historyTooOldToReadEndsWhatWasOpenWhereThePhoneWasLastSeen() {
        val one = tracker.run(state(at(9, 59)), listOf(Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat")), at(9, 59), at(10, 0, 30))
        // Read again only days later, past what the phone keeps.
        val later = at(10, 0, day = 28)
        val two = tracker.run(one.state, listOf(Usage(later + 5_000, ACTIVITY_RESUMED, "com.example.maps")), later, later + 10_000)
        assertEquals(listOf(listOf("app", "Chat", secs(at(10, 0)), secs(at(10, 0, 30)))), shape(two.events))
        assertEquals("com.example.maps", two.state.open?.pkg)
    }

    @Test fun eventsOutsideTheRunsWindowAreLeftForAnotherRun() {
        val usage = listOf(
            Usage(at(9, 58), ACTIVITY_RESUMED, "com.example.maps"),
            Usage(at(10, 0), ACTIVITY_RESUMED, "com.example.chat"),
            Usage(at(10, 2), SCREEN_NON_INTERACTIVE, "android"),
        )
        val step = tracker.run(state(at(9, 59)), usage, at(9, 59), at(10, 2))
        assertTrue(step.events.all { it.app == "Chat" })
        assertEquals("app", step.state.open?.type) // the screen-off at 10:02 is the next run's
    }
}
