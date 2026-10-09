package io.github.hexanomicon.lychd.companion.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val CompanionColors = darkColorScheme(
    primary = Color(0xFFB9C8B7),
    onPrimary = Color(0xFF1C2C1F),
    primaryContainer = Color(0xFF303F33),
    onPrimaryContainer = Color(0xFFD9E5D6),
    secondary = Color(0xFFC0C7C5),
    background = Color(0xFF101512),
    onBackground = Color(0xFFE3E8E1),
    surface = Color(0xFF171E19),
    onSurface = Color(0xFFE3E8E1),
    surfaceVariant = Color(0xFF303A32),
    onSurfaceVariant = Color(0xFFC3CDC1),
    outline = Color(0xFF899487),
    error = Color(0xFFFFB4AB),
)

@Composable
fun CompanionTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = CompanionColors, content = content)
}
