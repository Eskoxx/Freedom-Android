package `is`.xyz.mpv

import android.content.Context
import android.util.AttributeSet
import android.util.Log
import android.view.SurfaceHolder
import android.view.SurfaceView

class MPVView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : SurfaceView(context, attrs), SurfaceHolder.Callback {

    private var filePath: String? = null
    private var voInUse: String = "gpu"
    private var isInitialized = false

    fun initialize(configDir: String, cacheDir: String) {
        MPVLib.create(context)
        MPVLib.setOptionString("config", "yes")
        MPVLib.setOptionString("config-dir", configDir)
        MPVLib.setOptionString("gpu-shader-cache-dir", cacheDir)
        MPVLib.setOptionString("icc-cache-dir", cacheDir)

        MPVLib.setOptionString("vo", "gpu")
        MPVLib.setOptionString("ytdl", "no")
        MPVLib.setOptionString("profile", "fast")
        MPVLib.setOptionString("hwdec", "mediacodec,mediacodec-copy")
        MPVLib.setOptionString("hwdec-codecs", "h264,hevc,mpeg4,mpeg2video,vp8,vp9,av1")
        MPVLib.setOptionString("gpu-context", "android")
        MPVLib.setOptionString("opengl-es", "yes")
        MPVLib.setOptionString("audio-client-name", "NovaBeat")
        MPVLib.setOptionString("force-window", "no")
        MPVLib.setOptionString("idle", "once")
        MPVLib.setOptionString("keep-open", "yes")

        MPVLib.init()

        holder.addCallback(this)
        isInitialized = true
    }

    fun destroy() {
        isInitialized = false
        holder.removeCallback(this)
        MPVLib.destroy()
    }

    fun playFile(path: String) {
        if (isInitialized && holder.surface?.isValid == true) {
            MPVLib.command(arrayOf("loadfile", path))
        } else {
            filePath = path
        }
    }

    fun stop() {
        MPVLib.command(arrayOf("stop"))
    }

    fun pause() {
        MPVLib.setPropertyBoolean("pause", true)
    }

    fun resume() {
        MPVLib.setPropertyBoolean("pause", false)
    }

    fun seek(seconds: Double) {
        MPVLib.command(arrayOf("seek", seconds.toString(), "relative"))
    }

    fun seekTo(seconds: Double) {
        MPVLib.command(arrayOf("seek", seconds.toString(), "absolute"))
    }

    val isPaused: Boolean?
        get() = MPVLib.getPropertyBoolean("pause")

    val duration: Double?
        get() = MPVLib.getPropertyDouble("duration")

    val timePosition: Double?
        get() = MPVLib.getPropertyDouble("time-pos")

    val filename: String?
        get() = MPVLib.getPropertyString("filename")

    var vo: String
        get() = voInUse
        set(value) {
            voInUse = value
            MPVLib.setOptionString("vo", value)
        }

    override fun surfaceCreated(holder: SurfaceHolder) {
        Log.d(TAG, "Surface created")
        MPVLib.attachSurface(holder.surface)
        MPVLib.setOptionString("force-window", "yes")

        filePath?.let { path ->
            MPVLib.command(arrayOf("loadfile", path))
            filePath = null
        } ?: run {
            MPVLib.setPropertyString("vo", voInUse)
        }
    }

    override fun surfaceChanged(
        holder: SurfaceHolder,
        format: Int,
        width: Int,
        height: Int
    ) {
        MPVLib.setPropertyString("android-surface-size", "${width}x$height")
        // Force MPV to recalculate VO geometry and render a fresh frame
        try {
            MPVLib.setPropertyString("video-unscaled", "no")
            // Force video chain rebuild to recalculate letterbox borders
            MPVLib.command(arrayOf("vf", "prepend", "eq"))
            MPVLib.command(arrayOf("vf", "remove", "eq"))
            // Force a frame render on resized surface (no seek needed)
            val isPaused = MPVLib.getPropertyBoolean("pause")
            if (isPaused == true) {
                MPVLib.setPropertyBoolean("pause", false)
                MPVLib.setPropertyBoolean("pause", true)
            }
        } catch (_: Exception) {}
    }

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        Log.d(TAG, "Surface destroyed")
        MPVLib.setPropertyString("vo", "null")
        MPVLib.setPropertyString("force-window", "no")
        MPVLib.detachSurface()
    }

    companion object {
        private const val TAG = "MPVView"
    }
}
