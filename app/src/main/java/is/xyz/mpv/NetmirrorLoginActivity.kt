package `is`.xyz.mpv

import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers
import org.json.JSONObject
import java.io.File

class NetmirrorLoginActivity : ComponentActivity() {

    companion object {
        private val TARGET_DOMAINS = listOf(
            "https://net77.cc",
            "https://net52.cc",
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        setContentView(ComposeView(this).apply {
            setContent {
                val ctx = LocalContext.current
                val webViewRef = remember { mutableListOf<WebView>() }

                LaunchedEffect(webViewRef) {
                    // Wait for the WebView reference then poll cookies
                    var waited = 0
                    while (waited < 120) {
                        delay(1500)
                        waited += 1

                        val wv = webViewRef.firstOrNull() ?: continue
                        val cm = CookieManager.getInstance()
                        val cookies = mutableMapOf<String, String>()

                        for (domain in TARGET_DOMAINS) {
                            val raw = cm.getCookie(domain) ?: continue
                            for (pair in raw.split(";")) {
                                val parts = pair.trim().split("=", limit = 2)
                                if (parts.size == 2) {
                                    cookies[parts[0]] = parts[1]
                                }
                            }
                        }

                        val token = cookies["user_token"]
                        if (!token.isNullOrEmpty()) {
                            val json = JSONObject(cookies.toMap()).toString(2)
                            withContext(Dispatchers.IO) {
                                val configDir = File(ctx.filesDir, "home/.config/anime-watch")
                                configDir.mkdirs()
                                File(configDir, "net77_cookies.json").writeText(json)
                            }
                            Toast.makeText(ctx, "Net77 login successful!", Toast.LENGTH_LONG).show()
                            finish()
                            return@LaunchedEffect
                        }
                    }

                    Toast.makeText(ctx, "Login timed out after 2 min.", Toast.LENGTH_LONG).show()
                    finish()
                }

                NetmirrorLoginContent(
                    onClose = { finish() },
                    onWebViewCreated = { wv ->
                        if (webViewRef.isEmpty()) {
                            webViewRef.add(wv)
                        }
                    }
                )
            }
        })
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun NetmirrorLoginContent(
    onClose: () -> Unit,
    onWebViewCreated: (WebView) -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("NetMirror Login") },
                navigationIcon = {
                    IconButton(onClick = onClose) {
                        Icon(Icons.Default.Close, contentDescription = "Close")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface
                )
            )
        }
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.loadWithOverviewMode = true
                        settings.useWideViewPort = true
                        settings.builtInZoomControls = true
                        settings.displayZoomControls = false
                        settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                        settings.allowContentAccess = true
                        settings.allowFileAccess = false

                        CookieManager.getInstance().setAcceptThirdPartyCookies(this, true)

                        webViewClient = object : WebViewClient() {}
                        webChromeClient = WebChromeClient()

                        loadUrl("https://net77.cc/")
                        onWebViewCreated(this)
                    }
                },
                modifier = Modifier.fillMaxSize()
            )
        }
    }
}
