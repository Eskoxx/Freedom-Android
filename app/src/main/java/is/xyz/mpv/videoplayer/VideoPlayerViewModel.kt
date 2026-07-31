package `is`.xyz.mpv.videoplayer

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

data class VideoPlayerUiState(
    val showControls: Boolean = true,
    val controlsTimer: Int = 1,
)

class VideoPlayerViewModel {
    private val _uiState = MutableStateFlow(VideoPlayerUiState())
    val uiState: StateFlow<VideoPlayerUiState> = _uiState.asStateFlow()

    fun setShowControls(show: Boolean) {
        _uiState.value = _uiState.value.copy(showControls = show)
    }

    fun setControlsTimer(timer: Int) {
        _uiState.value = _uiState.value.copy(controlsTimer = timer)
    }
}
