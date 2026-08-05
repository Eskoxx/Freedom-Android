@file:Suppress("DEPRECATION")

package `is`.xyz.mpv.videoplayer

import `is`.xyz.mpv.MPVLib
import `is`.xyz.mpv.MPVView
import `is`.xyz.mpv.MpvIpcServer
import `is`.xyz.mpv.util.formatTimeMs
import android.app.Activity
import android.content.Context
import android.os.Bundle
import android.provider.Settings
import android.view.View
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.nestedscroll.NestedScrollConnection
import androidx.compose.ui.input.nestedscroll.NestedScrollSource
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Velocity
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collect
import java.io.File

private val ACCENT = Color(0xFF9C27B0)
private val VTXT1 = Color(0xFFE0E0E0)
private val VTXT2 = Color(0xFF9E9E9E)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VideoOverlay(
    videoPath: String,
    videoTitle: String,
    headersBundle: Bundle? = null,
    resume: Double = 0.0,
    onClose: () -> Unit
) {
    val context = LocalContext.current
    val view = LocalView.current
    val vm = remember { VideoPlayerViewModel() }
    val uiState by vm.uiState.collectAsState()

    DisposableEffect(Unit) {
        view.keepScreenOn = true
        val window = (context as? Activity)?.window
        window?.let {
            val decorView = it.decorView
            decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    or View.SYSTEM_UI_FLAG_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION)
        }
        onDispose {
            view.keepScreenOn = false
            window?.decorView?.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
        }
    }

    var mpvView by remember { mutableStateOf<MPVView?>(null) }
    var isPaused by remember { mutableStateOf(false) }
    var duration by remember { mutableStateOf(0.0) }
    var position by remember { mutableStateOf(0.0) }

    var playbackSpeed by remember { mutableStateOf(1.0f) }
    var aspectRatio by remember { mutableStateOf("Default") }
    var showSpeedDialog by remember { mutableStateOf(false) }
    var showAspectRatioDialog by remember { mutableStateOf(false) }
    var showAudioDialog by remember { mutableStateOf(false) }
    var showSubtitleDialog by remember { mutableStateOf(false) }
    var showInfoDialog by remember { mutableStateOf(false) }
    var repeatMode by remember { mutableStateOf(false) }

    var subtitleFile by remember { mutableStateOf<File?>(null) }
    var subtitleMode by remember { mutableStateOf("none") } // "none", "file", "mpv"
    var mpvSubtitleId by remember { mutableStateOf(-1) }
    var mpvSubtitleTracks by remember { mutableStateOf<List<String>>(emptyList()) }
    var mpvSubtitleIds by remember { mutableStateOf<List<Int>>(emptyList()) }
    var subtitlePosition by remember { mutableStateOf(100f) } // 0=top, 100=bottom
    var showSubtitlePosDialog by remember { mutableStateOf(false) }
    val subtitleVisible = subtitleFile != null || subtitleMode == "mpv"

    var availableSubs by remember { mutableStateOf<List<MpvIpcServer.SubtitleTrackInfo>>(emptyList()) }
    LaunchedEffect(Unit) {
        MpvIpcServer.availableSubs.collect { subs ->
            availableSubs = subs
            android.util.Log.d("VideoOverlay", "availableSubs updated: $subs")
        }
    }

    var availableAudio by remember { mutableStateOf<List<MpvIpcServer.AudioTrackInfo>>(emptyList()) }
    var selectedAudioId by remember { mutableStateOf(-1) }
    LaunchedEffect(Unit) {
        MpvIpcServer.availableAudio.collect { tracks ->
            availableAudio = tracks
        }
    }
    LaunchedEffect(Unit) {
        MpvIpcServer.selectedAudioId.collect { id ->
            selectedAudioId = id
        }
    }

    val controlsAlpha by animateFloatAsState(targetValue = if (uiState.showControls) 1f else 0f, animationSpec = tween(500), label = "controlsFade")
    LaunchedEffect(uiState.controlsTimer) { if (uiState.controlsTimer > 0) { delay(3000); vm.setShowControls(false) } }

    LaunchedEffect(Unit) {
        MpvIpcServer.subtitlePath.collect { path ->
            android.util.Log.d("VideoOverlay", "subtitlePath emitted: path=$path")
            if (path != null) {
                subtitleFile = File(path)
                subtitleMode = "file"
                mpvSubtitleId = -1
                MPVLib.setPropertyInt("sid", 0)
                MPVLib.setPropertyInt("sub-visibility", 0)
            }
            android.util.Log.d("VideoOverlay", "subtitleFile set: ${subtitleFile?.absolutePath} visible=$subtitleVisible")
        }
    }

    DisposableEffect(videoPath) {
        MpvIpcServer.start(context)
        onDispose {
            mpvView?.stop(); mpvView?.destroy(); mpvView = null
            MpvIpcServer.stop()
        }
    }

    LaunchedEffect(Unit) {
        while (true) {
            delay(250)
            val v = mpvView ?: continue
            position = v.timePosition ?: 0.0
            duration = v.duration ?: 0.0
            MpvIpcServer.setPosition(position, duration)
        }
    }

    if (showSpeedDialog) {
        PlaybackSpeedDialog(
            currentSpeed = playbackSpeed,
            onSpeedSelected = { speed ->
                playbackSpeed = speed
                MPVLib.setPropertyString("speed", speed.toString())
                showSpeedDialog = false
            },
            onBack = { showSpeedDialog = false }
        )
    }
    if (showAspectRatioDialog) {
        AspectRatioDialog(
            currentRatio = aspectRatio,
            onRatioSelected = { ratio ->
                aspectRatio = ratio
                val mpvRatio = when (ratio) {
                    "Default" -> "-1"
                    "1:1" -> "1/1"
                    "3:4" -> "3/4"
                    "9:16" -> "9/16"
                    else -> "-1"
                }
                MPVLib.setPropertyString("video-aspect-override", mpvRatio)
                showAspectRatioDialog = false
            },
            onBack = { showAspectRatioDialog = false }
        )
    }
    if (showAudioDialog) {
        AudioTrackDialog2(
            tracks = availableAudio,
            currentId = selectedAudioId,
            onSelect = { id ->
                MpvIpcServer.selectAudio(id)
                showAudioDialog = false
            },
            onDisable = {
                MpvIpcServer.disableAudio()
                showAudioDialog = false
            },
            onBack = { showAudioDialog = false }
        )
    }
    if (showSubtitleDialog) {
        SubtitleTrackDialog2(
            availableSubs = availableSubs,
            mpvTracks = mpvSubtitleTracks,
            mpvTrackIds = mpvSubtitleIds,
            subtitlePosition = subtitlePosition,
            onSelectCustom = { lang ->
                MpvIpcServer.selectSubtitle(lang)
                subtitleFile = MpvIpcServer.subtitlePath.value?.let { File(it) }
                subtitleMode = "file"
                mpvSubtitleId = -1
                MPVLib.setPropertyInt("sid", 0)
                MPVLib.setPropertyInt("sub-visibility", 0)
                showSubtitleDialog = false
            },
            onSelectMpv = { id ->
                subtitleFile = null
                subtitleMode = "mpv"
                mpvSubtitleId = id
                MpvIpcServer.disableSubtitle()
                MPVLib.setPropertyInt("sid", id)
                MPVLib.setPropertyString("sub-visibility", "no")
                showSubtitleDialog = false
            },
            onDisable = {
                subtitleFile = null
                subtitleMode = "none"
                mpvSubtitleId = -1
                MpvIpcServer.disableSubtitle()
                MPVLib.setPropertyInt("sid", 0)
                MPVLib.setPropertyInt("sub-visibility", 0)
                showSubtitleDialog = false
            },
            onPositionChange = { pos ->
                subtitlePosition = pos
                if (subtitleMode == "mpv") {
                    MPVLib.setPropertyString("sub-pos", pos.toInt().toString())
                }
            },
            onBack = { showSubtitleDialog = false }
        )
    }
    if (showInfoDialog) {
        InformationDialog(
            videoTitle = videoTitle,
            videoPath = videoPath,
            duration = duration,
            onBack = { showInfoDialog = false }
        )
    }

    Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
        // ── MPV view ──
        key(videoPath) {
            AndroidView(
                factory = { ctx ->
                    MPVView(ctx).apply {
                        val cacheDir = ctx.cacheDir.absolutePath
                        val configDir = File(ctx.filesDir, "mpv-config").also { it.mkdirs() }.absolutePath
                        initialize(configDir, cacheDir)

                        MPVLib.setPropertyInt("sid", 0)
                        MPVLib.setPropertyInt("sub-visibility", 0)
                        MPVLib.setPropertyString("sub-auto", "no")
                        MPVLib.setPropertyString("subs-with-matching-audio", "no")
                        MPVLib.setPropertyString("sub-file-paths", "")
                        MPVLib.setPropertyString("sub-files", "")
                        MPVLib.setPropertyString("audio-file-auto", "no")

                        if (headersBundle != null) {
                            for (key in headersBundle.keySet()) {
                                headersBundle.getString(key)?.let { value ->
                                    MPVLib.setOptionString("http-header-fields", "$key: $value")
                                }
                            }
                        }
                        if (videoTitle.isNotEmpty()) {
                            MPVLib.setOptionString("force-media-title", videoTitle)
                        }

                        mpvView = this

                        fun refreshTracks() {
                            val count = MPVLib.getPropertyInt("track-list/count") ?: 0
                            val audioTracks = mutableListOf<MpvIpcServer.AudioTrackInfo>()
                            val subs = mutableListOf<String>()
                            val subIds = mutableListOf<Int>()
                            for (i in 0 until count) {
                                val t = MPVLib.getPropertyString("track-list/$i/type") ?: continue
                                val id = MPVLib.getPropertyInt("track-list/$i/id") ?: continue
                                val lang = MPVLib.getPropertyString("track-list/$i/lang") ?: ""
                                val title = MPVLib.getPropertyString("track-list/$i/title") ?: ""
                                val label = if (title.isNotEmpty()) title else if (lang.isNotEmpty()) lang else "Track $id"
                                when (t) {
                                    "audio" -> audioTracks.add(MpvIpcServer.AudioTrackInfo(id, label, lang))
                                    "sub" -> { subs.add(label); subIds.add(id) }
                                }
                            }
                            if (audioTracks.isNotEmpty()) MpvIpcServer.setAudioTracks(audioTracks)
                            mpvSubtitleTracks = subs
                            mpvSubtitleIds = subIds
                        }

                        MPVLib.addObserver(object : MPVLib.EventObserver {
                            override fun eventProperty(p: String) {}
                            override fun eventProperty(p: String, v: Long) {}
                            override fun eventProperty(p: String, v: Boolean) { if (p == "pause") isPaused = v }
                            override fun eventProperty(p: String, v: String) {}
                            override fun eventProperty(p: String, v: Double) {}
                            override fun event(e: Int) {
                                when (e) {
                                    MPVLib.Event.FILE_LOADED -> {
                                        isPaused = false
                                        MPVLib.setPropertyInt("sid", 0)
                                        MPVLib.setPropertyInt("sub-visibility", 0)
                                        refreshTracks()
                                        if (resume > 0.0) seekTo(resume)
                                    }
                                    MPVLib.Event.SHUTDOWN -> {
                                        android.os.Handler(android.os.Looper.getMainLooper()).post { (ctx as? Activity)?.finish() }
                                    }
                                    MPVLib.Event.END_FILE -> {
                                        val p = MPVLib.getPropertyBoolean("pause")
                                        if (p != true) {
                                            android.os.Handler(android.os.Looper.getMainLooper()).post { (ctx as? Activity)?.finish() }
                                        }
                                    }
                                }
                            }
                        })
                        MPVLib.observeProperty("pause", MPVLib.Format.FLAG)
                        playFile(videoPath)
                    }
                },
                modifier = Modifier.fillMaxSize()
            )
        }

        // ── Subtitle overlay ──
        if (subtitleVisible) {
            SubtitleOverlay(
                subtitleFile = subtitleFile,
                currentPosition = position,
                subtitlePosition = subtitlePosition,
                subtitleMode = subtitleMode,
                modifier = Modifier.fillMaxSize()
            )
        }

        // ── Gesture layer (tap, double-tap, brightness/volume drag, overlays, lock) ──
        VideoPlayerGestureLayer(
            onDoubleTapSeek = { backward ->
                mpvView?.seek(if (backward) -10.0 else 10.0)
            },
            videoPlayerViewModel = vm
        )

        // ── Swipe down to dismiss ──
        var dragOffsetY by remember { mutableStateOf(0f) }
        var hasDismissed by remember { mutableStateOf(false) }
        val dismissThreshold = with(LocalDensity.current) { 250.dp.toPx() }
        LaunchedEffect(hasDismissed) { if (hasDismissed) onClose() }
        Box(
            Modifier.fillMaxSize().nestedScroll(object : NestedScrollConnection {
                override fun onPreScroll(available: Offset, source: NestedScrollSource): Offset {
                    if (available.y > 0f) { dragOffsetY += available.y; if (dragOffsetY > dismissThreshold && !hasDismissed) hasDismissed = true; return Offset(0f, available.y) }
                    return Offset.Zero
                }
                override fun onPostScroll(consumed: Offset, available: Offset, source: NestedScrollSource): Offset {
                    if (available.y > 0f) { dragOffsetY += available.y; if (dragOffsetY > dismissThreshold && !hasDismissed) hasDismissed = true; return Offset(0f, available.y) }
                    return Offset.Zero
                }
                override suspend fun onPreFling(available: Velocity): Velocity { dragOffsetY = 0f; return available }
            })
        )

        // ── Controls overlay ──
        Box(
            modifier = Modifier.fillMaxSize().graphicsLayer { alpha = controlsAlpha }
        ) {
            Column(Modifier.fillMaxSize()) {
                TopToolbar(
                    videoTitle = videoTitle,
                    playbackSpeed = playbackSpeed,
                    onBack = onClose,
                    onSpeedClick = { vm.setShowControls(true); showSpeedDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onAspectRatioClick = { vm.setShowControls(true); showAspectRatioDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onAudioClick = { vm.setShowControls(true); showAudioDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onSubtitleClick = { vm.setShowControls(true); showSubtitleDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onInfoClick = { vm.setShowControls(true); showInfoDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1) }
                )

                Spacer(Modifier.weight(1f))

                BottomControls(
                    position = position,
                    duration = duration,
                    isPaused = isPaused,
                    repeatMode = repeatMode,
                    onSeekTarget = { target ->
                        mpvView?.seek(target - position)
                    },
                    onPlayPause = {
                        vm.setShowControls(true)
                        if (isPaused) { mpvView?.resume(); isPaused = false } else { mpvView?.pause(); isPaused = true }
                        vm.setControlsTimer(uiState.controlsTimer + 1)
                    },
                    onSkipPrevious = { mpvView?.seek(-30.0); vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onSkipNext = { mpvView?.seek(30.0); vm.setControlsTimer(uiState.controlsTimer + 1) },
                    onAspectRatio = {
                        vm.setShowControls(true); showAspectRatioDialog = true; vm.setControlsTimer(uiState.controlsTimer + 1)
                    },
                    onRepeat = {
                        repeatMode = !repeatMode
                        MPVLib.setPropertyString("loop-file", if (repeatMode) "inf" else "no")
                        vm.setControlsTimer(uiState.controlsTimer + 1)
                    }
                )
            }
        }
    }
}

// ══════════════════════════════════════════════
//  TopToolbar (kiro VideoPlayerControls.kt port)
// ══════════════════════════════════════════════

@Composable
private fun TopToolbar(
    videoTitle: String,
    playbackSpeed: Float,
    onBack: () -> Unit,
    onSpeedClick: () -> Unit,
    onAspectRatioClick: () -> Unit,
    onAudioClick: () -> Unit = {},
    onSubtitleClick: () -> Unit,
    onInfoClick: () -> Unit
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth()
                .background(Color.Black.copy(alpha = 0.3f))
                .padding(horizontal = 4.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            IconButton(onClick = onBack) {
                Icon(Icons.Default.ArrowBack, "Back", tint = Color.White)
            }
            Spacer(Modifier.width(8.dp))
            Text(
                text = videoTitle.removeSuffix(".mp4").removeSuffix(".mkv").removeSuffix(".avi")
                    .removeSuffix(".mov").removeSuffix(".webm").removeSuffix(".3gp").removeSuffix(".m4v")
                    .removeSuffix(".mpeg4").removeSuffix(".mpeg").removeSuffix(".mpg"),
                color = Color.White, fontWeight = FontWeight.Medium, fontSize = 16.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f)
            )
        }
        Row(
            modifier = Modifier.fillMaxWidth()
                .background(Color.Black.copy(alpha = 0.3f))
                .padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            ToolbarButton(text = "${playbackSpeed}x", onClick = onSpeedClick)
            ToolbarButton(icon = Icons.Default.AspectRatio, onClick = onAspectRatioClick)
            ToolbarButton(icon = Icons.Default.VolumeUp, onClick = onAudioClick)
            ToolbarButton(icon = Icons.Default.Subtitles, onClick = onSubtitleClick)
            ToolbarButton(icon = Icons.Default.Info, onClick = onInfoClick)
        }
    }
}

@Composable
private fun ToolbarButton(
    text: String? = null,
    icon: ImageVector? = null,
    tint: Color = Color.White,
    onClick: () -> Unit
) {
    Box(
        modifier = Modifier
            .size(48.dp)
            .clip(CircleShape)
            .background(if (tint != Color.White) tint.copy(alpha = 0.15f) else Color.White.copy(alpha = 0.1f))
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
                onClick = onClick
            ),
        contentAlignment = Alignment.Center
    ) {
        if (text != null) {
            Text(text, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
        } else if (icon != null) {
            Icon(icon, null, tint = tint, modifier = Modifier.size(24.dp))
        }
    }
}



// ══════════════════════════════════════════════
//  BottomControls (Canvas seekbar + no lock)
// ══════════════════════════════════════════════

@Composable
private fun BottomControls(
    modifier: Modifier = Modifier,
    position: Double,
    duration: Double,
    isPaused: Boolean,
    repeatMode: Boolean,
    onSeekTarget: (Double) -> Unit,
    onPlayPause: () -> Unit,
    onSkipPrevious: () -> Unit,
    onSkipNext: () -> Unit,
    onAspectRatio: () -> Unit,
    onRepeat: () -> Unit
) {
    var isSeeking by remember { mutableStateOf(false) }
    var seekProgress by remember { mutableStateOf(0f) }

    Column(modifier = modifier.fillMaxWidth().padding(16.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(formatTimeMs((position.toLong() * 1000)), color = Color.White, fontSize = 12.sp)
            Box(
                modifier = Modifier.weight(1f).padding(horizontal = 8.dp).height(20.dp)
                    .drawBehind {
                        val lineY = size.height / 2f
                        val progressVal = if (isSeeking) seekProgress else if (duration > 0) (position / duration).toFloat() else 0f
                        val p = progressVal.coerceIn(0f, 1f)
                        drawLine(Color.White.copy(alpha = 0.15f), Offset(0f, lineY), Offset(size.width, lineY), strokeWidth = 2.dp.toPx())
                        drawLine(ACCENT, Offset(0f, lineY), Offset(size.width * p, lineY), strokeWidth = 2.dp.toPx())
                        drawCircle(ACCENT, 4.dp.toPx(), Offset(size.width * p, lineY))
                    }
                    .pointerInput(Unit) {
                        detectTapGestures { offset ->
                            val p = (offset.x / size.width).coerceIn(0f, 1f)
                            onSeekTarget(p * duration)
                        }
                    }
                    .pointerInput(Unit) {
                        detectDragGestures(
                            onDragStart = { offset ->
                                isSeeking = true
                                seekProgress = (offset.x / size.width).coerceIn(0f, 1f)
                            },
                            onDrag = { change, _ ->
                                change.consume()
                                seekProgress = (change.position.x / size.width).coerceIn(0f, 1f)
                            },
                            onDragEnd = {
                                isSeeking = false
                                onSeekTarget(seekProgress * duration)
                            },
                            onDragCancel = { isSeeking = false }
                        )
                    }
            )
            Text(formatTimeMs((duration.toLong() * 1000)), color = Color.White, fontSize = 12.sp)
        }
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceEvenly,
            verticalAlignment = Alignment.CenterVertically
        ) {
            IconButton(onClick = onSkipPrevious) {
                Icon(Icons.Default.SkipPrevious, null, tint = Color.White, modifier = Modifier.size(32.dp))
            }
            IconButton(onClick = onPlayPause) {
                Icon(
                    if (isPaused) Icons.Default.PlayArrow else Icons.Default.Pause,
                    null, tint = Color.White, modifier = Modifier.size(40.dp)
                )
            }
            IconButton(onClick = onSkipNext) {
                Icon(Icons.Default.SkipNext, null, tint = Color.White, modifier = Modifier.size(32.dp))
            }
            IconButton(onClick = onRepeat) {
                Icon(
                    if (repeatMode) Icons.Default.RepeatOne else Icons.Default.Repeat,
                    null, tint = if (repeatMode) ACCENT else Color.White
                )
            }
            IconButton(onClick = onAspectRatio) {
                Icon(Icons.Default.AspectRatio, null, tint = Color.White)
            }
        }
    }
}

// ══════════════════════════════════════════════
//  BrightnessOverlay / VolumeOverlay (kiro VideoPlayerOverlay.kt port)
// ══════════════════════════════════════════════

@Composable
internal fun BrightnessOverlay(overlayValue: Float, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(8.dp))
            .padding(12.dp)
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(Icons.Default.BrightnessHigh, null, tint = Color.White, modifier = Modifier.size(24.dp))
            Spacer(Modifier.height(8.dp))
            Box(
                modifier = Modifier.width(4.dp).height(100.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(Color.White.copy(alpha = 0.3f))
            ) {
                Box(
                    modifier = Modifier.width(4.dp).height((overlayValue * 100).dp)
                        .align(Alignment.BottomCenter)
                        .clip(RoundedCornerShape(2.dp))
                        .background(Color.White)
                )
            }
            Spacer(Modifier.height(8.dp))
            Text("${(overlayValue * 100).toInt()}", color = Color.White, fontSize = 14.sp)
        }
    }
}

@Composable
internal fun VolumeOverlay(overlayValue: Float, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(8.dp))
            .padding(12.dp)
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(Icons.Default.VolumeUp, null, tint = Color.White, modifier = Modifier.size(24.dp))
            Spacer(Modifier.height(8.dp))
            Box(
                modifier = Modifier.width(4.dp).height(100.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(Color.White.copy(alpha = 0.3f))
            ) {
                Box(
                    modifier = Modifier.width(4.dp).height((overlayValue * 100).dp)
                        .align(Alignment.BottomCenter)
                        .clip(RoundedCornerShape(2.dp))
                        .background(Color.White)
                )
            }
            Spacer(Modifier.height(8.dp))
            Text("${(overlayValue * 100).toInt()}", color = Color.White, fontSize = 14.sp)
        }
    }
}

// ══════════════════════════════════════════════
//  Dialog composables (kiro VideoPlayerDialogs.kt style)
// ══════════════════════════════════════════════

@Composable
fun PlaybackSpeedDialog(currentSpeed: Float, onSpeedSelected: (Float) -> Unit, onBack: () -> Unit) {
    androidx.compose.ui.window.Dialog(onDismissRequest = onBack) {
        VideoMenuCard {
            Text("Playback Speed", color = VTXT1, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(8.dp))
            HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(4.dp))
            listOf(0.25f, 0.5f, 0.75f, 1.0f, 1.25f, 1.5f, 1.75f, 2.0f).forEach { speed ->
                RadioRow(icon = Icons.Default.FastForward, label = "${speed}x", selected = currentSpeed == speed, onClick = { onSpeedSelected(speed) })
            }
            Spacer(Modifier.height(4.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onBack) { Text("Cancel", color = VTXT2) }
            }
        }
    }
}

@Composable
fun AspectRatioDialog(currentRatio: String, onRatioSelected: (String) -> Unit, onBack: () -> Unit) {
    androidx.compose.ui.window.Dialog(onDismissRequest = onBack) {
        VideoMenuCard {
            Text("Aspect Ratio", color = VTXT1, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(8.dp))
            HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(4.dp))
            RadioRow(icon = Icons.Default.AspectRatio, label = "Default", selected = currentRatio == "Default", onClick = { onRatioSelected("Default") })
            RadioRow(icon = Icons.Default.AspectRatio, label = "1:1", selected = currentRatio == "1:1", onClick = { onRatioSelected("1:1") })
            RadioRow(icon = Icons.Default.AspectRatio, label = "3:4", selected = currentRatio == "3:4", onClick = { onRatioSelected("3:4") })
            RadioRow(icon = Icons.Default.AspectRatio, label = "9:16", selected = currentRatio == "9:16", onClick = { onRatioSelected("9:16") })
            Spacer(Modifier.height(4.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onBack) { Text("Cancel", color = VTXT2) }
            }
        }
    }
}

@Composable
fun AudioTrackDialog2(
    tracks: List<MpvIpcServer.AudioTrackInfo>,
    currentId: Int,
    onSelect: (Int) -> Unit,
    onDisable: () -> Unit,
    onBack: () -> Unit
) {
    androidx.compose.ui.window.Dialog(onDismissRequest = onBack) {
        VideoMenuCard {
            Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                Text("Audio Track", color = VTXT1, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp))
                Spacer(Modifier.height(8.dp))
                HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                Spacer(Modifier.height(4.dp))
                Row(Modifier.fillMaxWidth().clickable(onClick = onDisable).padding(horizontal = 4.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(44.dp).clip(RoundedCornerShape(10.dp)).background(Color(0x33000000)), contentAlignment = Alignment.Center) {
                        Icon(Icons.Default.VolumeOff, null, tint = VTXT2, modifier = Modifier.size(20.dp))
                    }
                    Spacer(Modifier.width(12.dp))
                    Text("Default", color = VTXT1, fontSize = 15.sp)
                }
                if (tracks.isNotEmpty()) {
                    HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                    tracks.forEach { track ->
                        val display = if (track.label.isNotEmpty()) track.label else if (track.lang.isNotEmpty()) track.lang else "Track ${track.id}"
                        RadioRow(icon = Icons.Default.VolumeUp, label = display, subtitle = track.lang, selected = currentId == track.id, onClick = { onSelect(track.id) })
                    }
                }
                Spacer(Modifier.height(4.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = onBack) { Text("Cancel", color = VTXT2) }
                }
            }
        }
    }
}

@Composable
fun SubtitleTrackDialog2(
    availableSubs: List<MpvIpcServer.SubtitleTrackInfo> = emptyList(),
    mpvTracks: List<String> = emptyList(),
    mpvTrackIds: List<Int> = emptyList(),
    subtitlePosition: Float = 100f,
    onSelectCustom: (String) -> Unit = {},
    onSelectMpv: (Int) -> Unit = {},
    onDisable: () -> Unit,
    onPositionChange: (Float) -> Unit = {},
    onBack: () -> Unit
) {
    androidx.compose.ui.window.Dialog(onDismissRequest = onBack) {
        VideoMenuCard {
            Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                Text("Subtitle", color = VTXT1, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp))
                Spacer(Modifier.height(8.dp))
                HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                Spacer(Modifier.height(4.dp))
                Row(Modifier.fillMaxWidth().clickable(onClick = onDisable).padding(horizontal = 4.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(44.dp).clip(RoundedCornerShape(10.dp)).background(Color(0x33000000)), contentAlignment = Alignment.Center) {
                        Icon(Icons.Default.VisibilityOff, null, tint = VTXT2, modifier = Modifier.size(20.dp))
                    }
                    Spacer(Modifier.width(12.dp))
                    Text("Default", color = VTXT1, fontSize = 15.sp)
                }
                if (availableSubs.isNotEmpty()) {
                    HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                    Text("External", color = VTXT2, fontSize = 12.sp, modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp))
                    availableSubs.forEach { track ->
                        RadioRow(icon = Icons.Default.Subtitles, label = track.label, selected = false, onClick = { onSelectCustom(track.id) })
                    }
                }
                if (mpvTracks.isNotEmpty()) {
                    HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                    Text("Embedded", color = VTXT2, fontSize = 12.sp, modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp))
                    mpvTracks.zip(mpvTrackIds).forEach { (label, id) ->
                        RadioRow(icon = Icons.Default.Subtitles, label = label, selected = false, onClick = { onSelectMpv(id) })
                    }
                }
                Spacer(Modifier.height(8.dp))
                HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
                Spacer(Modifier.height(4.dp))
                Text("Position", color = VTXT2, fontSize = 12.sp, modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp))
                Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("Top", color = VTXT2, fontSize = 11.sp)
                    Slider(
                        value = subtitlePosition,
                        onValueChange = onPositionChange,
                        valueRange = 0f..100f,
                        modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
                        colors = SliderDefaults.colors(thumbColor = ACCENT, activeTrackColor = ACCENT)
                    )
                    Text("Bottom", color = VTXT2, fontSize = 11.sp)
                }
                Text("${subtitlePosition.toInt()}%", color = VTXT1, fontSize = 12.sp, modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp).wrapContentWidth(Alignment.CenterHorizontally))
                Spacer(Modifier.height(4.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = onBack) { Text("Done", color = VTXT2) }
                }
            }
        }
    }
}

@Composable
fun InformationDialog(videoTitle: String, videoPath: String, duration: Double, onBack: () -> Unit) {
    androidx.compose.ui.window.Dialog(onDismissRequest = onBack) {
        VideoMenuCard {
            Text("Information", color = VTXT1, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(8.dp))
            HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(4.dp))
            val displayName = videoTitle.ifEmpty { videoPath.substringAfterLast("/").substringBefore("?") }
            InfoRow("Title", displayName)
            InfoRow("URL", videoPath.take(80))
            Spacer(Modifier.height(8.dp))
            HorizontalDivider(color = VTXT2.copy(alpha = 0.2f), modifier = Modifier.padding(horizontal = 8.dp))
            Spacer(Modifier.height(4.dp))
            InfoRow("Duration", formatTimeMs((duration.toLong() * 1000)))
            Spacer(Modifier.height(4.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onBack) { Text("Close", color = VTXT2) }
            }
        }
    }
}

// ══════════════════════════════════════════════
//  Dialog helpers
// ══════════════════════════════════════════════

@Composable
private fun VideoMenuCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = modifier.widthIn(max = 360.dp),
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xFF1a1a1a))
    ) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 16.dp), content = content)
    }
}

@Composable
private fun RadioRow(icon: ImageVector, label: String, subtitle: String = "", selected: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 4.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier.size(44.dp).clip(RoundedCornerShape(10.dp))
                .background(if (selected) Color(0x33FFFFFF) else Color(0x33000000)),
            contentAlignment = Alignment.Center
        ) {
            Icon(icon, null, tint = if (selected) Color.White else VTXT1, modifier = Modifier.size(20.dp))
        }
        Spacer(Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(label, color = VTXT1, fontSize = 15.sp, maxLines = 1)
            if (subtitle.isNotEmpty()) {
                Spacer(Modifier.height(1.dp))
                Text(subtitle, color = VTXT2, fontSize = 12.sp, maxLines = 1)
            }
        }
        Spacer(Modifier.width(8.dp))
        RadioButton(selected = selected, onClick = onClick, colors = RadioButtonDefaults.colors(selectedColor = ACCENT, unselectedColor = VTXT2))
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, color = VTXT2, fontSize = 13.sp)
        Text(value, color = VTXT1, fontSize = 13.sp, modifier = Modifier.widthIn(max = 200.dp), maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
}

// ══════════════════════════════════════════════
//  Utility
// ══════════════════════════════════════════════

internal fun getBrightnessVideo(context: Context): Float {
    return try {
        val lp = (context as? Activity)?.window?.attributes?.screenBrightness
        if (lp != null && lp >= 0f) lp
        else Settings.System.getFloat(context.contentResolver, Settings.System.SCREEN_BRIGHTNESS) / 255f
    } catch (_: Exception) { 0.5f }
}

internal fun setBrightnessVideo(context: Context, value: Float) {
    try {
        val activity = context as? Activity ?: return
        val lp = activity.window.attributes ?: return
        lp.screenBrightness = value.coerceIn(0.01f, 1f)
        activity.window.attributes = lp
    } catch (_: Exception) {}
}
