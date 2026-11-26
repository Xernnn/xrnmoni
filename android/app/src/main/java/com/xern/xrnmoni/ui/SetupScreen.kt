package com.xern.xrnmoni.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.xern.xrnmoni.R
import com.xern.xrnmoni.data.Status
import java.text.DateFormat
import java.util.Date

/** What the phone allows, read again whenever the app comes back to the front. null: not asked on this phone. */
data class Access(val usage: Boolean, val notifications: Boolean?, val battery: Boolean, val autostart: Boolean?, val autostartOpened: Boolean)

/** What the setup screen can do. */
interface SetupActions {
    /** Saves the hub and a pasted token (blank: keep the saved one); the label to show, or null when saved. */
    fun save(hub: String, token: String): String?
    fun openUsageAccess()
    fun askNotifications()
    fun askBattery()
    fun openAutostart()
    fun sendNow()
}

/** The one screen: the hub and the token, each step of access, and how recording is going. Labels and figures only. */
@Composable
fun SetupScreen(status: Status, access: Access, recording: Boolean, actions: SetupActions) {
    var hub by remember(status.hub) { mutableStateOf(status.hub) }
    var token by remember { mutableStateOf("") }
    var note by remember { mutableStateOf<String?>(null) }
    val c = MaterialTheme.colorScheme
    Column(
        Modifier.fillMaxSize().background(c.background).safeDrawingPadding().verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Image(painterResource(R.drawable.tile), contentDescription = "xrnmoni", modifier = Modifier.size(40.dp))
        Panel {
            OutlinedTextField(
                value = hub, onValueChange = { hub = it; note = null }, label = { Text("Hub") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = token, onValueChange = { token = it.trim(); note = null }, label = { Text("Token") }, singleLine = true,
                placeholder = { if (status.hasToken) Text("Saved") }, visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = {
                    note = actions.save(hub, token) ?: "Saved"
                    if (note == "Saved") token = ""
                }) { Text("Save") }
                note?.let { Text(it, style = MaterialTheme.typography.labelMedium, color = if (it == "Saved") c.onSurfaceVariant else c.error, modifier = Modifier.padding(start = 12.dp)) }
            }
        }
        Panel {
            Step("Usage access", if (access.usage) "Allowed" else "Allow", access.usage, actions::openUsageAccess)
            access.notifications?.let {
                HorizontalDivider(color = c.outlineVariant)
                Step("Notifications", if (it) "Allowed" else "Allow", it, actions::askNotifications)
            }
            HorizontalDivider(color = c.outlineVariant)
            Step("Battery", if (access.battery) "Unrestricted" else "Allow", access.battery, actions::askBattery)
            if (access.autostart != null || access.autostartOpened) {
                HorizontalDivider(color = c.outlineVariant)
                val on = access.autostart
                Step("Autostart", when (on) { true -> "On"; false -> "Off"; null -> "Open" }, on == true, actions::openAutostart, always = true)
            }
        }
        Panel {
            Figure("Recording", if (recording) "On" else "Off")
            HorizontalDivider(color = c.outlineVariant)
            Figure("Last post", postLabel(status), warn = status.tokenRefused || status.lastPost == 0 || status.lastPost >= 400)
            HorizontalDivider(color = c.outlineVariant)
            Figure("Queued", status.queued.toString())
            HorizontalDivider(color = c.outlineVariant)
            Figure("Last read", if (!status.usageAccess) "No usage access" else time(status.lastReadAt), warn = !status.usageAccess)
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = actions::sendNow, enabled = status.hasToken) { Text("Send now") }
        }
    }
}

/** The last post as a short label: its time and status, or what went wrong. */
fun postLabel(s: Status): String = when {
    s.tokenRefused -> "Token refused"
    !s.hasToken -> "No token"
    s.lastPost < 0 -> "—"
    s.lastPost == 0 -> "Hub unreachable"
    s.lastPost == 403 -> "Refused"
    else -> "${time(s.lastPostAt)} · ${s.lastPost}"
}

private fun time(ms: Long): String = if (ms <= 0) "—" else DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ms))

@Composable
private fun Panel(content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface, RoundedCornerShape(16.dp)).padding(16.dp),
    ) { content() }
}

@Composable
private fun Step(label: String, value: String, ok: Boolean, onClick: () -> Unit, always: Boolean = false) {
    val c = MaterialTheme.colorScheme
    Row(
        Modifier.fillMaxWidth().clickable(enabled = !ok || always, onClick = onClick).padding(vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = MaterialTheme.typography.bodyLarge, color = c.onSurface, modifier = Modifier.weight(1f))
        Text(value, style = MaterialTheme.typography.labelLarge, color = if (ok) c.onSurfaceVariant else c.primary)
    }
}

@Composable
private fun Figure(label: String, value: String, warn: Boolean = false) {
    val c = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth().padding(vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyLarge, color = c.onSurface, modifier = Modifier.weight(1f))
        Text(value, style = MaterialTheme.typography.labelLarge, color = if (warn) c.error else c.onSurfaceVariant)
    }
}
