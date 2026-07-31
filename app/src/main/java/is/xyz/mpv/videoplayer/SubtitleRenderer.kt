package `is`.xyz.mpv.videoplayer

import `is`.xyz.mpv.MPVLib
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.File

data class SubtitleEntry(
    val startTime: Long,
    val endTime: Long,
    val text: String
)

class SubtitleParser {
    private fun vttToSrt(vttContent: String): String {
        val lines = vttContent.lines()
        val srtBuilder = StringBuilder()
        var counter = 1
        var i = 0

        while (i < lines.size && !lines[i].contains("-->")) {
            i++
        }

        while (i < lines.size) {
            val line = lines[i].trim()
            if (line.contains("-->")) {
                val parts = line.split("-->").map { it.trim().split(" ")[0] }
                val startTime = convertVttTimestamp(parts[0])
                val endTime = convertVttTimestamp(parts[1])

                srtBuilder.appendLine(counter++)
                srtBuilder.appendLine("$startTime --> $endTime")

                i++
                while (i < lines.size && lines[i].isNotBlank()) {
                    val cleaned = lines[i].trim()
                        .replace(Regex("<[^>]+>"), "")
                        .replace(Regex("\\{[^}]+\\}"), "")
                    if (cleaned.isNotEmpty()) {
                        srtBuilder.appendLine(cleaned)
                    }
                    i++
                }
                srtBuilder.appendLine()
            }
            i++
        }
        return srtBuilder.toString().trim()
    }

    private fun convertVttTimestamp(vttTime: String): String {
        val parts = vttTime.split(":")
        return when (parts.size) {
            2 -> "00:${parts[0]}:${parts[1].replace(".", ",")}"
            3 -> vttTime.replace(".", ",")
            else -> vttTime
        }
    }

    fun parseVTT(file: File): List<SubtitleEntry> {
        return parseSRT(File.createTempFile("sub", ".srt").apply {
            writeText(vttToSrt(file.readText()))
        })
    }

    fun parseSRT(file: File): List<SubtitleEntry> {
        val entries = mutableListOf<SubtitleEntry>()
        val lines = file.readLines()
        var i = 0

        while (i < lines.size) {
            val line = lines[i].trim()
            if (line.contains("-->")) {
                val times = line.split("-->").map { it.trim() }
                if (times.size == 2) {
                    val startTime = parseTime(times[0])
                    val endTime = parseTime(times[1])

                    val textLines = mutableListOf<String>()
                    i++
                    while (i < lines.size && lines[i].trim().isNotEmpty()) {
                        textLines.add(lines[i].trim())
                        i++
                    }

                    if (textLines.isNotEmpty()) {
                        entries.add(SubtitleEntry(startTime, endTime, textLines.map { it.trim() }.joinToString(" ")))
                    }
                    continue
                }
            }
            i++
        }

        return entries
    }

    private fun parseTime(timeStr: String): Long {
        val cleaned = timeStr.replace(",", ".")
        val parts = cleaned.split(":")

        if (parts.size < 2) return 0L

        val hours = parts.getOrNull(0)?.toLongOrNull() ?: 0L
        val minutes = parts.getOrNull(1)?.toLongOrNull() ?: 0L
        val secondsParts = parts.getOrNull(2)?.split(".") ?: return 0L
        val seconds = secondsParts.getOrNull(0)?.toLongOrNull() ?: 0L
        val millis = secondsParts.getOrNull(1)?.take(3)?.padEnd(3, '0')?.toLongOrNull() ?: 0L

        return (hours * 3600000) + (minutes * 60000) + (seconds * 1000) + millis
    }
}

@Composable
fun SubtitleOverlay(
    subtitleFile: File?,
    currentPosition: Double,
    subtitlePosition: Float = 100f,
    subtitleMode: String = "none",
    modifier: Modifier = Modifier
) {
    var subtitles by remember { mutableStateOf<List<SubtitleEntry>>(emptyList()) }
    var currentText by remember { mutableStateOf("") }

    // Load file-based subtitles
    LaunchedEffect(subtitleFile) {
        subtitleFile?.let { file ->
            try {
                android.util.Log.d("SubtitleOverlay", "Loading subtitle file: ${file.absolutePath}")
                withContext(Dispatchers.IO) {
                    val parser = SubtitleParser()
                    val isVtt = file.useLines { it.firstOrNull()?.trim()?.startsWith("WEBVTT") == true }
                    subtitles = if (isVtt || file.extension.lowercase() == "vtt") {
                        parser.parseVTT(file)
                    } else {
                        parser.parseSRT(file)
                    }
                }
            } catch (e: Exception) {
                android.util.Log.e("SubtitleOverlay", "Error loading subtitles", e)
                subtitles = emptyList()
            }
        }
    }

    // Update current subtitle from file or mpv sub-text
    LaunchedEffect(currentPosition, subtitles, subtitleMode) {
        while (true) {
            val text = if (subtitleMode == "mpv") {
                MPVLib.getPropertyString("sub-text") ?: ""
            } else {
                val positionMs = (currentPosition * 1000).toLong()
                subtitles.firstOrNull {
                    positionMs >= it.startTime && positionMs <= it.endTime
                }?.text ?: ""
            }
            if (text != currentText) {
                currentText = text
            }
            delay(100)
        }
    }

    val subOffset = ((100f - subtitlePosition) / 100f * 80f + 10f).dp

    Box(
        modifier = modifier.fillMaxSize(),
        contentAlignment = Alignment.BottomCenter
    ) {
        if (currentText.isNotEmpty()) {
            Text(
                text = currentText,
                modifier = Modifier.padding(horizontal = 32.dp, vertical = subOffset),
                color = Color.White,
                fontSize = 24.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center,
                style = androidx.compose.ui.text.TextStyle(
                    shadow = androidx.compose.ui.graphics.Shadow(
                        color = Color.Black,
                        offset = androidx.compose.ui.geometry.Offset(2f, 2f),
                        blurRadius = 8f
                    )
                )
            )
        }
    }
}
