package com.panamacompra.monitor.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.panamacompra.monitor.data.SettingsStore
import com.panamacompra.monitor.network.ApiClient
import com.panamacompra.monitor.network.ManualActionDto
import com.panamacompra.monitor.network.MonitorApi
import com.panamacompra.monitor.network.StatusResponse
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

data class MonitorUiState(
    val baseUrl: String = "",
    val status: StatusResponse? = null,
    val actions: List<ManualActionDto> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
    val lastActionResult: String? = null,
)

private const val POLL_INTERVAL_MS = 5000L

class MonitorViewModel(application: Application) : AndroidViewModel(application) {
    private val settingsStore = SettingsStore(application)
    private val _uiState = MutableStateFlow(MonitorUiState())
    val uiState: StateFlow<MonitorUiState> = _uiState.asStateFlow()

    private var api: MonitorApi? = null
    private var pollingJob: Job? = null

    init {
        viewModelScope.launch {
            val savedUrl = settingsStore.baseUrl.first()
            connect(savedUrl)
        }
    }

    /** Point the app at a new monitor address and (re)start status polling. */
    fun connect(url: String) {
        if (url.isBlank()) return
        pollingJob?.cancel()
        api = ApiClient.create(url)
        _uiState.value = _uiState.value.copy(baseUrl = url, error = null)
        viewModelScope.launch { settingsStore.setBaseUrl(url) }
        loadActions()
        startPolling()
    }

    private fun loadActions() {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.getActions() }
                .onSuccess { _uiState.value = _uiState.value.copy(actions = it) }
                .onFailure { _uiState.value = _uiState.value.copy(error = "Actions load failed: ${it.message}") }
        }
    }

    private fun startPolling() {
        pollingJob = viewModelScope.launch {
            while (true) {
                refreshStatus()
                delay(POLL_INTERVAL_MS)
            }
        }
    }

    private suspend fun refreshStatus() {
        val client = api ?: return
        runCatching { client.getStatus() }
            .onSuccess { _uiState.value = _uiState.value.copy(status = it, loading = false, error = null) }
            .onFailure { _uiState.value = _uiState.value.copy(loading = false, error = "Connection failed: ${it.message}") }
    }

    fun runAction(label: String) {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.runManualAction(label) }
                .onSuccess { response ->
                    val result = if (response.isSuccessful) "Started: $label" else "Failed ($label): HTTP ${response.code()}"
                    _uiState.value = _uiState.value.copy(lastActionResult = result)
                }
                .onFailure { _uiState.value = _uiState.value.copy(lastActionResult = "Failed ($label): ${it.message}") }
        }
    }

    override fun onCleared() {
        pollingJob?.cancel()
        super.onCleared()
    }
}
