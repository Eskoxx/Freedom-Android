package `is`.xyz.mpv

import android.content.Context
import android.graphics.Bitmap
import android.view.Surface

object MPVLib {
    init {
        System.loadLibrary("mpv")
        System.loadLibrary("player")
    }

    external fun create(appctx: Context)
    external fun init()
    external fun destroy()
    external fun attachSurface(surface: Surface)
    external fun detachSurface()
    external fun command(cmd: Array<out String>)
    external fun setOptionString(name: String, value: String): Int
    external fun getPropertyInt(property: String): Int?
    external fun setPropertyInt(property: String, value: Int)
    external fun getPropertyDouble(property: String): Double?
    external fun setPropertyDouble(property: String, value: Double)
    external fun getPropertyBoolean(property: String): Boolean?
    external fun setPropertyBoolean(property: String, value: Boolean)
    external fun getPropertyString(property: String): String?
    external fun setPropertyString(property: String, value: String)
    external fun observeProperty(property: String, format: Int)

    private val observers = mutableListOf<EventObserver>()
    private val logObservers = mutableListOf<LogObserver>()

    @JvmStatic
    fun addObserver(o: EventObserver) {
        synchronized(observers) { observers.add(o) }
    }

    @JvmStatic
    fun removeObserver(o: EventObserver) {
        synchronized(observers) { observers.remove(o) }
    }

    @JvmStatic
    fun eventProperty(property: String, value: Long) {
        synchronized(observers) { for (o in observers) o.eventProperty(property, value) }
    }

    @JvmStatic
    fun eventProperty(property: String, value: Boolean) {
        synchronized(observers) { for (o in observers) o.eventProperty(property, value) }
    }

    @JvmStatic
    fun eventProperty(property: String, value: Double) {
        synchronized(observers) { for (o in observers) o.eventProperty(property, value) }
    }

    @JvmStatic
    fun eventProperty(property: String, value: String) {
        synchronized(observers) { for (o in observers) o.eventProperty(property, value) }
    }

    @JvmStatic
    fun eventProperty(property: String) {
        synchronized(observers) { for (o in observers) o.eventProperty(property) }
    }

    @JvmStatic
    fun event(eventId: Int) {
        synchronized(observers) { for (o in observers) o.event(eventId) }
    }

    @JvmStatic
    fun addLogObserver(o: LogObserver) {
        synchronized(logObservers) { logObservers.add(o) }
    }

    @JvmStatic
    fun removeLogObserver(o: LogObserver) {
        synchronized(logObservers) { logObservers.remove(o) }
    }

    @JvmStatic
    fun logMessage(prefix: String, level: Int, text: String) {
        synchronized(logObservers) { for (o in logObservers) o.logMessage(prefix, level, text) }
    }

    interface EventObserver {
        fun eventProperty(property: String)
        fun eventProperty(property: String, value: Long)
        fun eventProperty(property: String, value: Boolean)
        fun eventProperty(property: String, value: String)
        fun eventProperty(property: String, value: Double)
        fun event(eventId: Int)
    }

    interface LogObserver {
        fun logMessage(prefix: String, level: Int, text: String)
    }

    object Format {
        const val NONE = 0
        const val STRING = 1
        const val OSD_STRING = 2
        const val FLAG = 3
        const val INT64 = 4
        const val DOUBLE = 5
        const val NODE = 6
        const val NODE_ARRAY = 7
        const val NODE_MAP = 8
        const val BYTE_ARRAY = 9
    }

    object Event {
        const val NONE = 0
        const val SHUTDOWN = 1
        const val LOG_MESSAGE = 2
        const val GET_PROPERTY_REPLY = 3
        const val SET_PROPERTY_REPLY = 4
        const val COMMAND_REPLY = 5
        const val START_FILE = 6
        const val END_FILE = 7
        const val FILE_LOADED = 8
        const val CLIENT_MESSAGE = 16
        const val VIDEO_RECONFIG = 17
        const val AUDIO_RECONFIG = 18
        const val SEEK = 20
        const val PLAYBACK_RESTART = 21
        const val PROPERTY_CHANGE = 22
    }
}
