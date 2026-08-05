package `is`.xyz.mpv

import android.os.Bundle
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

object MpvIpcServer {
    private const val TAG = "MpvIpcServer"
    private const val IPC_PORT = 41987
    private var serverSocket: ServerSocket? = null
    private val running = AtomicBoolean(false)
    private var lastPosition: Double = 0.0
    private var lastDuration: Double = 0.0
    private var lastPaused: Boolean = false
    private var appContext: android.content.Context? = null
    private val clientSockets = mutableListOf<Socket>()

    data class SubtitleTrackInfo(val id: String, val label: String, val lang: String)

    private val _subtitleFiles = mutableMapOf<String, String>()
    private val _subtitleLabels = mutableMapOf<String, String>()
    private val _availableSubs = MutableStateFlow<List<SubtitleTrackInfo>>(emptyList())
    val availableSubs: StateFlow<List<SubtitleTrackInfo>> = _availableSubs.asStateFlow()

    private val _subtitlePath = MutableStateFlow<String?>(null)
    val subtitlePath: StateFlow<String?> = _subtitlePath.asStateFlow()

    fun selectSubtitle(id: String) {
        Log.d(TAG, "selectSubtitle called id=$id files=${_subtitleFiles.keys}")
        val path = _subtitleFiles[id]
        if (path != null) {
            Log.d(TAG, "selectSubtitle: setting path=$path")
            _subtitlePath.value = path
        } else {
            Log.w(TAG, "selectSubtitle: no path for id=$id")
        }
    }

    fun disableSubtitle() {
        Log.d(TAG, "disableSubtitle")
        _subtitlePath.value = null
    }

    fun resetSubs() {
        _subtitleFiles.clear()
        _subtitleLabels.clear()
        _availableSubs.value = emptyList()
        _subtitlePath.value = null
        resetAudio()
    }

    // ── Audio track management (IPC-based, controls MPV's aid) ──

    data class AudioTrackInfo(val id: Int, val label: String, val lang: String)

    private val _audioTrackList = mutableListOf<AudioTrackInfo>()
    private val _availableAudio = MutableStateFlow<List<AudioTrackInfo>>(emptyList())
    val availableAudio: StateFlow<List<AudioTrackInfo>> = _availableAudio.asStateFlow()

    private val _selectedAudioId = MutableStateFlow(-1)
    val selectedAudioId: StateFlow<Int> = _selectedAudioId.asStateFlow()

    fun selectAudio(id: Int) {
        Log.d(TAG, "selectAudio id=$id")
        _selectedAudioId.value = id
        MPVLib.setPropertyInt("aid", id)
    }

    fun disableAudio() {
        Log.d(TAG, "disableAudio")
        _selectedAudioId.value = -1
        MPVLib.setPropertyInt("aid", 1)
    }

    fun resetAudio() {
        _audioTrackList.clear()
        _availableAudio.value = emptyList()
        _selectedAudioId.value = -1
    }

    fun setAudioTracks(tracks: List<AudioTrackInfo>) {
        _audioTrackList.clear()
        _audioTrackList.addAll(tracks)
        _availableAudio.value = _audioTrackList.toList()
        Log.d(TAG, "setAudioTracks: ${tracks.size} tracks")
    }

    fun start(context: android.content.Context) {
        appContext = context
        if (running.get()) return
        running.set(true)
        Thread {
            try {
                serverSocket = ServerSocket(IPC_PORT) // hardcoded port on localhost
                Log.d(TAG, "IPC server on port $IPC_PORT")

                while (running.get()) {
                    try {
                        val client = serverSocket!!.accept()
                        synchronized(clientSockets) { clientSockets.add(client) }
                        Thread { handleClient(client); synchronized(clientSockets) { clientSockets.remove(client) } }.start()
                    } catch (_: Exception) {
                        if (!running.get()) break
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server error", e)
            }
        }.apply { isDaemon = true }.start()
    }

    fun stop() {
        running.set(false)
        try { serverSocket?.close() } catch (_: Exception) {}
        serverSocket = null
        synchronized(clientSockets) {
            for (s in clientSockets) {
                try { s.close() } catch (_: Exception) {}
            }
            clientSockets.clear()
        }
        resetSubs()
    }

    fun setPaused(paused: Boolean) { lastPaused = paused }

    fun setPosition(pos: Double, dur: Double) {
        lastPosition = pos
        lastDuration = dur
    }

    private fun handleClient(socket: Socket) {
        try {
            val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
            val writer = OutputStreamWriter(socket.getOutputStream())
            while (true) {
                val line = reader.readLine() ?: break
                val req = JSONObject(line)
                val cmd = req.optString("cmd")
                val resp = JSONObject()

                try {
                    when (cmd) {
                        "get_position" -> {
                            resp.put("position", lastPosition)
                            resp.put("duration", lastDuration)
                            resp.put("paused", lastPaused)
                        }
                        "get_property" -> {
                            val name = req.optString("name")
                            val format = req.optInt("format", MPVLib.Format.DOUBLE)
                            val value: Any? = when (format) {
                                MPVLib.Format.DOUBLE -> MPVLib.getPropertyDouble(name)
                                MPVLib.Format.INT64 -> MPVLib.getPropertyInt(name)
                                MPVLib.Format.FLAG -> MPVLib.getPropertyBoolean(name)
                                MPVLib.Format.STRING -> MPVLib.getPropertyString(name)
                                else -> null
                            }
                            if (value != null) resp.put("data", value)
                            else resp.put("error", "property not found")
                        }
                        "set_property" -> {
                            val name = req.optString("name")
                            when (val v = req.get("value")) {
                                is Int -> MPVLib.setPropertyInt(name, v)
                                is Double -> MPVLib.setPropertyDouble(name, v)
                                is Boolean -> MPVLib.setPropertyBoolean(name, v)
                                is String -> MPVLib.setPropertyString(name, v)
                            }
                        }
                        "set_option" -> {
                            val name = req.optString("name")
                            val value = req.optString("value")
                            MPVLib.setOptionString(name, value)
                        }
                        "command" -> {
                            val args = req.optJSONArray("args")
                            val cmdArray = if (args != null) {
                                Array(args.length()) { i -> args.optString(i) }
                            } else {
                                arrayOf(req.optString("cmd"))
                            }
                            MPVLib.command(cmdArray)
                        }
                        "play" -> {
                            val url = req.optString("url")
                            if (url.isNotEmpty()) {
                                MPVLib.command(arrayOf("loadfile", url))
                            }
                        }
                        "resume" -> MPVLib.setPropertyBoolean("pause", false)
                        "pause" -> MPVLib.setPropertyBoolean("pause", true)
                        "stop" -> MPVLib.command(arrayOf("stop"))
                        "seek" -> {
                            val seconds = req.optDouble("seconds", 0.0)
                            MPVLib.command(arrayOf("seek", seconds.toString(), "relative"))
                        }
                        "seek_to" -> {
                            val seconds = req.optDouble("seconds", 0.0)
                            MPVLib.command(arrayOf("seek", seconds.toString(), "absolute"))
                        }
                        "observe_property" -> {
                            val name = req.optString("name")
                            val format = req.optInt("format", MPVLib.Format.FLAG)
                            MPVLib.observeProperty(name, format)
                        }
                        "set_subtitle_content" -> {
                            val ctx = appContext ?: throw Exception("no context")
                            val content = req.optString("content", "")
                            val ext = req.optString("ext", ".srt")
                            val lang = req.optString("lang", "und")
                            val label = req.optString("label", lang)
                            Log.d(TAG, "set_subtitle_content: len=${content.length} ext=$ext lang=$lang label=$label")
                            if (content.isNotEmpty()) {
                                val subFile = File(ctx.cacheDir, "subtitle_${lang}${ext}")
                                subFile.writeText(content)
                                Log.d(TAG, "Wrote subtitle to ${subFile.absolutePath} (exists=${subFile.exists()})")
                                _subtitleFiles[lang] = subFile.absolutePath
                                _subtitleLabels[lang] = label
                                _availableSubs.value = _subtitleLabels.map { (id, lbl) ->
                                    SubtitleTrackInfo(
                                        id = id,
                                        label = lbl,
                                        lang = id.substringBeforeLast("-")
                                    )
                                }
                                if (_subtitlePath.value == null) {
                                    _subtitlePath.value = subFile.absolutePath
                                }
                            } else {
                                Log.w(TAG, "set_subtitle_content: empty content")
                            }
                        }
                        "select_subtitle" -> {
                            val lang = req.optString("lang", "")
                            val path = _subtitleFiles[lang]
                            if (path != null) {
                                _subtitlePath.value = path
                                resp.put("selected", lang)
                            } else {
                                resp.put("error", "subtitle not found: $lang")
                            }
                        }
                        "disable_subtitle" -> {
                            _subtitlePath.value = null
                        }
                        "set_audio_tracks" -> {
                            val tracks = req.optJSONArray("tracks")
                            if (tracks != null) {
                                val list = mutableListOf<AudioTrackInfo>()
                                for (i in 0 until tracks.length()) {
                                    val t = tracks.optJSONObject(i)
                                    if (t != null) {
                                        list.add(AudioTrackInfo(
                                            id = t.optInt("id", -1),
                                            label = t.optString("label", ""),
                                            lang = t.optString("lang", "")
                                        ))
                                    }
                                }
                                setAudioTracks(list)
                                resp.put("count", list.size)
                            }
                        }
                        "select_audio" -> {
                            val id = req.optInt("id", -1)
                            if (id > 0) {
                                selectAudio(id)
                                resp.put("selected", id)
                            } else {
                                resp.put("error", "invalid audio id: $id")
                            }
                        }
                        "disable_audio" -> {
                            disableAudio()
                        }
                        else -> resp.put("error", "unknown cmd: $cmd")
                    }
                } catch (e: Exception) {
                    resp.put("error", e.message)
                }

                resp.put("cmd", cmd)
                writer.write(resp.toString() + "\n")
                writer.flush()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Client error", e)
        } finally {
            try { socket.close() } catch (_: Exception) {}
        }
    }
}
