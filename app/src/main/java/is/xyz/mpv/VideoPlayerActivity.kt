package `is`.xyz.mpv

import android.os.Bundle
import android.view.View
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.compose.ui.platform.ComposeView
import `is`.xyz.mpv.videoplayer.VideoOverlay

class VideoPlayerActivity : ComponentActivity() {

    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_TITLE = "title"
        const val EXTRA_RESUME = "resume"
        const val EXTRA_HEADERS = "headers"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_FULLSCREEN
        )
        window.decorView.systemUiVisibility =
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or
            View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION

        val url = intent.getStringExtra(EXTRA_URL) ?: return finish()
        val title = intent.getStringExtra(EXTRA_TITLE) ?: ""
        val resume = intent.getStringExtra(EXTRA_RESUME)?.toDoubleOrNull() ?: 0.0
        val headersBundle: android.os.Bundle? = intent.getStringExtra(EXTRA_HEADERS)?.let { jsonStr ->
            try {
                val json = org.json.JSONObject(jsonStr)
                android.os.Bundle().apply {
                    for (key in json.keys()) {
                        putString(key, json.optString(key))
                    }
                }
            } catch (_: Exception) { null }
        }

        val composeView = ComposeView(this)
        composeView.setContent {
            VideoOverlay(
                videoPath = url,
                videoTitle = title,
                headersBundle = headersBundle,
                resume = resume,
                onClose = { finish() },
            )
        }
        setContentView(composeView)
    }
}
