package com.panamacompra.monitor.ui

import android.app.Application
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.core.content.ContextCompat
import com.google.firebase.auth.FirebaseUser
import com.panamacompra.monitor.auth.AuthRepository
import com.panamacompra.monitor.data.ClientSettingsStore
import com.panamacompra.monitor.network.ApiClient
import com.panamacompra.monitor.network.ClientProfileDto
import com.panamacompra.monitor.network.ClientProfileUpdateDto
import com.panamacompra.monitor.network.FilterSuggestionsDto
import com.panamacompra.monitor.network.MonitorApi
import com.panamacompra.monitor.network.NotificationDto
import com.panamacompra.monitor.service.NotificationPollingService
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json

data class ClientUiState(
    val baseUrl: String = "",
    val user: FirebaseUser? = null,
    val monitoringEnabled: Boolean = false,
    val notifications: List<NotificationDto> = emptyList(),
    val profile: ClientProfileDto? = null,
    val suggestions: FilterSuggestionsDto = FilterSuggestionsDto(),
    val loading: Boolean = false,
    val error: String? = null,
    val authBusy: Boolean = false,
    val authError: String? = null,
    val profileJustSaved: Boolean = false,
)

private val jsonCodec = Json { ignoreUnknownKeys = true }

class ClientViewModel(application: Application) : AndroidViewModel(application) {
    private val settingsStore = ClientSettingsStore(application)
    private val auth = AuthRepository()
    private val _uiState = MutableStateFlow(ClientUiState())
    val uiState: StateFlow<ClientUiState> = _uiState.asStateFlow()

    private fun api(): MonitorApi? {
        val url = _uiState.value.baseUrl
        if (url.isBlank()) return null
        return runCatching { ApiClient.create(url) }.getOrElse { failure ->
            _uiState.update { it.copy(error = failure.message ?: "Invalid server address") }
            null
        }
    }

    init {
        viewModelScope.launch {
            val savedUrl = settingsStore.baseUrl.first()
            val monitoring = settingsStore.monitoringEnabled.first()
            _uiState.update { it.copy(baseUrl = savedUrl, monitoringEnabled = monitoring, user = auth.currentUser) }
            viewModelScope.launch {
                auth.authState.collect { user ->
                    _uiState.update { it.copy(user = user) }
                    if (user != null) {
                        loadProfile()
                        loadSuggestions()
                        refreshNotifications()
                    }
                }
            }
        }
    }

    // --- Server address -----------------------------------------------------

    fun saveBaseUrl(url: String) {
        val normalized = runCatching { ApiClient.normalizeBaseUrl(url) }.getOrElse { failure ->
            _uiState.update { it.copy(error = failure.message ?: "Invalid server address") }
            return
        }
        viewModelScope.launch {
            settingsStore.setBaseUrl(normalized)
            _uiState.update { it.copy(baseUrl = normalized, error = null) }
        }
    }

    fun resetServer() {
        getApplication<Application>().stopService(Intent(getApplication(), NotificationPollingService::class.java))
        viewModelScope.launch {
            settingsStore.setMonitoringEnabled(false)
            settingsStore.setBaseUrl("")
            _uiState.update { it.copy(baseUrl = "", monitoringEnabled = false, error = null) }
        }
    }

    // --- Auth ----------------------------------------------------------------

    fun signUpEmail(email: String, password: String) = runAuth { auth.signUpWithEmail(email, password) }

    fun signInEmail(email: String, password: String) = runAuth { auth.signInWithEmail(email, password) }

    fun signInGoogle(idToken: String) = runAuth { auth.signInWithGoogleIdToken(idToken) }

    private fun runAuth(block: suspend () -> Result<FirebaseUser>) {
        viewModelScope.launch {
            _uiState.update { it.copy(authBusy = true, authError = null) }
            block()
                .onSuccess { user -> _uiState.update { it.copy(authBusy = false, user = user) } }
                .onFailure { failure -> _uiState.update { it.copy(authBusy = false, authError = failure.message ?: "Sign-in failed") } }
        }
    }

    fun reportAuthError(message: String) {
        _uiState.update { it.copy(authBusy = false, authError = message) }
    }

    fun reportMonitoringError(message: String) {
        _uiState.update { it.copy(monitoringEnabled = false, error = message) }
        viewModelScope.launch { settingsStore.setMonitoringEnabled(false) }
    }

    fun signOut() {
        getApplication<Application>().stopService(Intent(getApplication(), NotificationPollingService::class.java))
        auth.signOut()
        viewModelScope.launch { settingsStore.setMonitoringEnabled(false) }
        _uiState.update {
            it.copy(user = null, profile = null, notifications = emptyList(), monitoringEnabled = false)
        }
    }

    // --- Profile / filters -----------------------------------------------------

    fun loadProfile() {
        val client = api() ?: return
        val uid = _uiState.value.user?.uid ?: return
        viewModelScope.launch {
            runCatching { client.getClientProfile(uid) }
                .onSuccess { response ->
                    if (response.isSuccessful) {
                        _uiState.update { it.copy(profile = response.body()) }
                    }
                    // 404 = brand new signup, no profile saved yet — leave
                    // profile null so the Profile screen shows blank fields.
                }
                .onFailure { failure -> _uiState.update { it.copy(error = "Couldn't load profile: ${failure.message}") } }
        }
    }

    fun loadSuggestions() {
        val client = api() ?: return
        viewModelScope.launch {
            runCatching { client.getFilterSuggestions() }
                .onSuccess { suggestions -> _uiState.update { it.copy(suggestions = suggestions) } }
                .onFailure { /* suggestions are a nice-to-have; ignore failures silently */ }
        }
    }

    fun saveProfile(
        phone: String,
        purposes: List<String>,
        filters: String,
        profession: String,
        location: String,
        institution: String,
        calendarVisible: Boolean,
    ) {
        val client = api() ?: return
        val user = _uiState.value.user ?: return
        val update = ClientProfileUpdateDto(
            firebaseUid = user.uid,
            email = user.email ?: "",
            phone = phone,
            purposes = purposes,
            filters = filters,
            profession = profession,
            location = location,
            institution = institution,
            calendarVisible = calendarVisible,
        )
        viewModelScope.launch {
            _uiState.update { it.copy(loading = true, profileJustSaved = false, error = null) }
            runCatching { client.saveClientProfile(jsonCodec.encodeToString(ClientProfileUpdateDto.serializer(), update)) }
                .onSuccess { saved ->
                    _uiState.update { it.copy(loading = false, profile = saved, profileJustSaved = true) }
                }
                .onFailure {
                    _uiState.update { state -> state.copy(loading = false, error = "Couldn't save profile: ${it.message}") }
                }
        }
    }

    // --- Monitoring toggle / notification history ------------------------------

    fun startMonitoring() {
        viewModelScope.launch {
            // Persist first: the service checks this flag as soon as it starts.
            settingsStore.setMonitoringEnabled(true)
            runCatching {
                ContextCompat.startForegroundService(
                    getApplication(),
                    Intent(getApplication(), NotificationPollingService::class.java),
                )
            }.onSuccess {
                _uiState.update { it.copy(monitoringEnabled = true, error = null) }
            }.onFailure { failure ->
                settingsStore.setMonitoringEnabled(false)
                _uiState.update {
                    it.copy(monitoringEnabled = false, error = "Monitoring could not start: ${failure.message}")
                }
            }
        }
    }

    fun stopMonitoring() {
        getApplication<Application>().stopService(Intent(getApplication(), NotificationPollingService::class.java))
        viewModelScope.launch {
            settingsStore.setMonitoringEnabled(false)
            _uiState.update { it.copy(monitoringEnabled = false) }
        }
    }

    fun refreshNotifications() {
        val client = api() ?: return
        val uid = _uiState.value.user?.uid ?: return
        viewModelScope.launch {
            _uiState.update { it.copy(loading = true, error = null) }
            runCatching { client.getClientNotifications(uid, 0L, latest = true) }
                .onSuccess { list ->
                    _uiState.update { state -> state.copy(
                        notifications = list.sortedByDescending { it.id },
                        loading = false,
                        error = null,
                    ) }
                }
                .onFailure {
                    _uiState.update { state -> state.copy(loading = false, error = "Couldn't load notifications: ${it.message}") }
                }
        }
    }
}
