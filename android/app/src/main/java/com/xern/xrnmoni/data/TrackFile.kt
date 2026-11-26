package com.xern.xrnmoni.data

import com.xern.xrnmoni.domain.TrackState
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import kotlinx.serialization.json.Json

/**
 * The tracker's state between runs, in one JSON file written whole (tmp, sync, rename), so a crash
 * leaves the old state or the new one. It is saved after the run's events are in the outbox: a crash
 * between the two reads that stretch again, and the hub skips the ids it already has.
 */
class TrackFile(private val file: File) {
    private val json = Json { ignoreUnknownKeys = true }

    /** The saved state, or null for none (or one that can't be read: the tracker starts afresh). */
    fun read(): TrackState? = runCatching { json.decodeFromString(TrackState.serializer(), file.readText()) }.getOrNull()

    fun write(state: TrackState) {
        file.parentFile?.mkdirs()
        val tmp = File(file.path + ".tmp")
        FileOutputStream(tmp).use {
            it.write(json.encodeToString(TrackState.serializer(), state).toByteArray())
            it.fd.sync()
        }
        if (!tmp.renameTo(file)) throw IOException("can't replace $file")
    }
}
