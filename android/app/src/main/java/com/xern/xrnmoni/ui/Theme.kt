package com.xern.xrnmoni.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.xern.xrnmoni.R

/** The tile's signal teal, and the xrn family's surfaces. */
object Ink {
    val Teal = Color(0xFF039B7D)
    val TealLight = Color(0xFF3FD1AE)
    val Black = Color(0xFF000000)
    val Surface = Color(0xFF18181B)
    val Raised = Color(0xFF232327)
    val Line = Color(0xFF3A3A40)
    val White = Color(0xFFFFFFFF)
    val Dim = Color(0xFFA1A1AA)
    val Paper = Color(0xFFF6F6F7)
    val Ink = Color(0xFF18181B)
    val Grey = Color(0xFF5F5F68)
    val Rule = Color(0xFFDDDDE1)
    val Red = Color(0xFFE5484D)
}

/** MiSans Latin by Xiaomi, unmodified files. It runs heavier than its names, so Bold draws Semibold. */
val MiSans = FontFamily(
    Font(R.font.misans_regular, FontWeight.Normal),
    Font(R.font.misans_medium, FontWeight.Medium),
    Font(R.font.misans_demibold, FontWeight.SemiBold),
    Font(R.font.misans_semibold, FontWeight.Bold),
)

private fun type(size: Int, weight: FontWeight = FontWeight.Normal) = TextStyle(fontFamily = MiSans, fontSize = size.sp, fontWeight = weight)

private val typography = Typography(
    titleMedium = type(18, FontWeight.SemiBold),
    bodyLarge = type(16),
    bodyMedium = type(15),
    labelLarge = type(15, FontWeight.Medium),
    labelMedium = type(13, FontWeight.Medium),
)

@Composable
fun MoniTheme(content: @Composable () -> Unit) {
    val colors = if (isSystemInDarkTheme()) {
        darkColorScheme(
            primary = Ink.TealLight, onPrimary = Ink.Black, background = Ink.Black, onBackground = Ink.White,
            surface = Ink.Surface, onSurface = Ink.White, surfaceVariant = Ink.Raised, onSurfaceVariant = Ink.Dim,
            outline = Ink.Line, outlineVariant = Ink.Line, error = Ink.Red,
        )
    } else {
        lightColorScheme(
            primary = Ink.Teal, onPrimary = Ink.White, background = Ink.Paper, onBackground = Ink.Ink,
            surface = Ink.White, onSurface = Ink.Ink, surfaceVariant = Ink.Paper, onSurfaceVariant = Ink.Grey,
            outline = Ink.Rule, outlineVariant = Ink.Rule, error = Ink.Red,
        )
    }
    MaterialTheme(colorScheme = colors, typography = typography, content = content)
}
