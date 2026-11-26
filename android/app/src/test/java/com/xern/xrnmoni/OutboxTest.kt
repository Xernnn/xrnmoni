package com.xern.xrnmoni

import com.xern.xrnmoni.data.Outbox
import com.xern.xrnmoni.data.TrackFile
import com.xern.xrnmoni.domain.Event
import com.xern.xrnmoni.domain.Open
import com.xern.xrnmoni.domain.TrackState
import java.io.File
import java.nio.file.Files
import kotlin.concurrent.thread
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class OutboxTest {
    private lateinit var dir: File

    @Before fun makeDir() {
        dir = Files.createTempDirectory("outbox").toFile()
    }

    @After fun removeDir() {
        dir.deleteRecursively()
    }

    private fun event(n: Int) = Event(
        id = "%032x".format(n), type = "app", app = "Chat", title = "com.example.chat",
        start = 1_795_100_000.0 + n, end = 1_795_100_001.0 + n, duration = 1.0,
    )

    private fun ids(box: Outbox, n: Int = 100_000) = box.peek(n).map { it.event.id.toInt(16) }

    @Test fun eventsComeOutOldestFirstAndGoOnlyWhenRemoved() {
        val box = Outbox(dir)
        box.add(listOf(event(1), event(2)))
        box.add(listOf(event(3)))
        assertEquals(listOf(1, 2), ids(box, 2))
        assertEquals(listOf(1, 2), ids(box, 2)) // peeking takes nothing out
        box.remove(2)
        assertEquals(listOf(3), ids(box))
        assertEquals(1, box.size)
        assertEquals(event(3), box.peek(1).single().event)
    }

    @Test fun whatIsLeftSurvivesReopening() {
        Outbox(dir).apply {
            add((1..5).map(::event))
            remove(2)
        }
        val again = Outbox(dir)
        assertEquals(listOf(3, 4, 5), ids(again))
        again.add(listOf(event(6)))
        assertEquals(listOf(3, 4, 5, 6), ids(Outbox(dir)))
    }

    @Test fun aLineCutShortByACrashIsDropped() {
        Outbox(dir).add(listOf(event(1), event(2)))
        val data = dir.listFiles()!!.single { it.name.endsWith(".jsonl") }
        data.appendText("""{"q":1,"e":{"id":"00""")
        val box = Outbox(dir)
        assertEquals(listOf(1, 2), ids(box))
        box.add(listOf(event(3)))
        assertEquals(listOf(1, 2, 3), ids(Outbox(dir)))
    }

    @Test fun unreadableLinesAreSkippedAndTakenOutWithTheirNeighbours() {
        val box = Outbox(dir)
        box.add(listOf(event(1)))
        dir.listFiles()!!.single { it.name.endsWith(".jsonl") }.appendText("not json\n")
        val reopened = Outbox(dir)
        reopened.add(listOf(event(2)))
        val head = reopened.peek(10)
        assertEquals(listOf(1, 2), head.map { it.event.id.toInt(16) })
        assertEquals(listOf(1, 2), head.map { it.lines }) // event 2 carries the bad line before it
        reopened.remove(head.sumOf { it.lines })
        assertEquals(0, reopened.size)
    }

    @Test fun overTheRowCapTheOldestGo() {
        val box = Outbox(dir, maxRows = 3)
        box.add((1..5).map(::event))
        assertEquals(2, box.trim())
        assertEquals(listOf(3, 4, 5), ids(box))
    }

    @Test fun eventsWaitingLongerThanTheAgeCapGo() {
        val day = 86_400_000L
        val box = Outbox(dir, maxAgeMs = 30 * day)
        box.add(listOf(event(1), event(2)), now = 0)
        box.add(listOf(event(3)), now = 10 * day)
        assertEquals(2, box.trim(now = 31 * day))
        assertEquals(listOf(3), ids(box))
        assertEquals(0, box.trim(now = 31 * day))
    }

    @Test fun sentLinesAreDroppedFromDiskOnceHalfIsSent() {
        val box = Outbox(dir)
        box.add((1..2500).map(::event))
        box.remove(1300)
        val files = dir.listFiles()!!.filter { it.name.endsWith(".jsonl") }
        assertEquals(1, files.size)
        assertEquals(1200, files.single().readLines().size)
        assertEquals(1301, ids(box, 1).single())
        val again = Outbox(dir)
        assertEquals(1200, again.size)
        assertEquals((1301..2500).toList(), ids(again))
    }

    @Test fun aNextFileLeftHalfWrittenByACrashIsIgnored() {
        Outbox(dir).add((1..3).map(::event))
        // A switch to the next file that died before the head named it.
        File(dir, "outbox-1.jsonl").writeText("partial")
        val box = Outbox(dir)
        assertEquals(listOf(1, 2, 3), ids(box))
        assertTrue(!File(dir, "outbox-1.jsonl").exists())
    }

    @Test fun emptyingItStartsAFreshFile() {
        val box = Outbox(dir)
        box.add((1..3).map(::event))
        box.remove(3)
        assertEquals(0, box.size)
        assertTrue(box.peek(10).isEmpty())
        box.add(listOf(event(4)))
        assertEquals(listOf(4), ids(Outbox(dir)))
    }

    @Test fun addsFromSeveralThreadsAllLand() {
        val box = Outbox(dir)
        (0 until 8).map { t -> thread { repeat(50) { box.add(listOf(event(t * 1000 + it))) } } }.forEach { it.join() }
        assertEquals(400, box.size)
        assertEquals(400, Outbox(dir).peek(1000).size)
    }

    @Test fun theTrackersStateIsKeptWholeAndAnUnreadableOneIsNone() {
        val file = TrackFile(File(dir, "track.json"))
        assertNull(file.read())
        val state = TrackState(cursor = 5, alive = 9, locked = true, open = Open("locked", start = 6, sent = 6))
        file.write(state)
        assertEquals(state, file.read())
        File(dir, "track.json").writeText("{broken")
        assertNull(file.read())
    }
}
