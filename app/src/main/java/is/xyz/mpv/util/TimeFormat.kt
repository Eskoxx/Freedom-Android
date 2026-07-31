package `is`.xyz.mpv.util

fun formatTime(seconds: Double): String {
    if (seconds.isNaN() || seconds <= 0) return "0:00"
    val totalSec = seconds.toInt()
    val min = totalSec / 60
    val sec = totalSec % 60
    val hrs = min / 60
    return if (hrs > 0) "%d:%02d:%02d".format(hrs, min % 60, sec) else "%d:%02d".format(min, sec)
}

fun formatTimeMs(ms: Long): String {
    if (ms <= 0) return "0:00"
    val totalSec = ms / 1000
    val min = totalSec / 60
    val sec = totalSec % 60
    val hrs = min / 60
    return if (hrs > 0) "%d:%02d:%02d".format(hrs, min % 60, sec) else "%d:%02d".format(min, sec)
}
