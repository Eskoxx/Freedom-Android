package `is`.xyz.mpv.videoplayer

import android.content.Context
import android.media.AudioManager
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

@Composable
fun BoxScope.VideoPlayerGestureLayer(
    onDoubleTapSeek: (Boolean) -> Unit,
    videoPlayerViewModel: VideoPlayerViewModel,
) {
    val uiState by videoPlayerViewModel.uiState.collectAsState()
    val context = LocalContext.current
    val audioManager = remember { context.getSystemService(Context.AUDIO_SERVICE) as AudioManager }
    val maxVolume = remember { audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC) }

    var currentVolume by remember { mutableStateOf(audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)) }
    var brightness by remember { mutableStateOf(getBrightnessVideo(context)) }
    var overlayType by remember { mutableStateOf<String?>(null) }
    var overlayValue by remember { mutableStateOf(0f) }
    var overlayAlpha by remember { mutableStateOf(0f) }
    val overlayAnim by animateFloatAsState(targetValue = overlayAlpha, animationSpec = tween(200), label = "fade")

    LaunchedEffect(overlayType) {
        if (overlayType != null) { delay(1500); overlayAlpha = 0f; delay(300); overlayType = null }
    }

    Box(
        Modifier.fillMaxSize()
            .pointerInput(Unit) {
                detectTapGestures(
                    onTap = {
                        videoPlayerViewModel.setShowControls(!uiState.showControls)
                        videoPlayerViewModel.setControlsTimer(uiState.controlsTimer + 1)
                    },
                    onDoubleTap = { offset ->
                        onDoubleTapSeek(offset.x < size.width / 2f)
                    }
                )
            }
            .pointerInput(Unit) {
                detectDragGestures(
                    onDragStart = { offset ->
                        overlayType = if (offset.x < size.width * 0.4f) "brightness" else "volume"
                        overlayValue = if (overlayType == "brightness") brightness else currentVolume.toFloat() / maxVolume.toFloat()
                        overlayAlpha = 1f
                    },
                    onDrag = { change, dragAmount ->
                        if (kotlin.math.abs(dragAmount.y) > kotlin.math.abs(dragAmount.x)) {
                            change.consume()
                            val delta = -dragAmount.y / size.height.toFloat() * 1.5f
                            when (overlayType) {
                                "brightness" -> {
                                    brightness = (brightness + delta).coerceIn(0f, 1f)
                                    setBrightnessVideo(context, brightness)
                                    overlayValue = brightness
                                }
                                "volume" -> {
                                    val vf = (currentVolume.toFloat() / maxVolume.toFloat() + delta).coerceIn(0f, 1f)
                                    val nv = (vf * maxVolume).toInt().coerceIn(0, maxVolume)
                                    audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, nv, 0)
                                    currentVolume = nv
                                    overlayValue = vf
                                }
                            }
                        }
                    },
                    onDragEnd = { overlayType = null },
                    onDragCancel = { overlayType = null }
                )
            }
    )

    if (overlayType == "brightness" && overlayAnim > 0.01f) {
        Box(Modifier.align(Alignment.CenterStart).padding(start = 16.dp)) {
            BrightnessOverlay(overlayValue = overlayValue, modifier = Modifier.graphicsLayer { alpha = overlayAnim })
        }
    }
    if (overlayType == "volume" && overlayAnim > 0.01f) {
        Box(Modifier.align(Alignment.CenterEnd).padding(end = 16.dp)) {
            VolumeOverlay(overlayValue = overlayValue, modifier = Modifier.graphicsLayer { alpha = overlayAnim })
        }
    }

}
