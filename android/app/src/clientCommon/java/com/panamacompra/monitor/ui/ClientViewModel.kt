package com.panamacompra.monitor.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.google.firebase.auth.FirebaseUser
import com.panamacompra.monitor.auth.AuthRepository
import com.panamacompra.monitor.data.ClientSettingsStore
import com.panamacompra.monitor.network.ApiClient
import com.panamacompra.monitor.network.ClientProfileDto
import com.panamacompra.monitor.network.ClientProfileUpdateDto
import com.panamacompra.monitor.network.FilterSuggestionsDto
import com.panamacompra.monitor.network.MonitorApi
import com.panamacompra.monitor.network.NotificationDto
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
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
        return if (url.isBlank()) null else ApiClient.create(url)
    }

    init {
        viewModelScope.launch {
            val savedUrl = settingsStore.baseUrl.first()
            val monitoring = settingsStore.monitoringEnabled.first()
            _uiState.value = _uiState.value.copy(baseUrl = savedUrl, monitoringEnabled = monitoring, user = auth.currentUser)
            viewModelScope.launch {
                auth.authState.collect { user ->
                    _uiState.value = _uiState.value.copy(user = user)
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
        viewModelScope.launch {
            settingsStore.setBaseUrl(url)
            _uiState.value = _uiState.value.copy(baseUrl = url)
        }
    }

    // --- Auth ----------------------------------------------------------------

    fun signUpEmail(email: String, password: String) = runAuth { auth.signUpWithEmail(email, password) }

    fun signInEmail(email: String, password: String) = runAuth { auth.signInWithEmail(email, password) }

    fun signInGoogle(idToken: String) = runAuth { auth.signInWithGoogleIdToken(idToken) }

    private fun runAuth(block: suspend () -> Result<FirebaseUser>) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(authBusy = true, authError = null)
            block()
                .onSuccess { _uiState.value = _uiState.value.copy(authBusy = false, user = it) }
                .onFailure { _uiState.value = _uiState.value.copy(authBusy = false, authError = it.message ?: "Sign-in failed") }
        }
    }

    fun signOut() {
        auth.signOut()
        _uiState.value = _uiState.value.copy(user = null, profile = null, notifications = emptyList())
    }

    // --- Profile / filters -----------------------------------------------------

    fun loadProfile() {
        val client = api() ?: return
        val uid = _uiState.value.user?.uid ?: return
        viewModelScope.launch {
            runCatching { client.getClientProfile(uid) }
                .onSuccess { response ->
                    if (response.isSuccessful) {
                        _uiState.value = _uiState.value.copy(profile = response.body())
                    }
                    // 404 = brand new signup, no profile saved yet — leave
                    // profile null so the Profile screen shows blank fields.
                }
                .onFailure { _uiState.value = _uiState.value.copy(error = "Couldn't load profile: ${it.message}") }
        }
    }

    fun loadSuggestions() {
        val client = api() ?: return
        viewModelScope.launch {
            runCatching { client.getFilterSuggestions() }
                .onSuccess { _uiState.value = _uiState.value.copy(suggestions = it) }
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
            _uiState.value = _uiState.value.copy(loading = true, profileJustSaved = false)
            runCatching { client.saveClientProfile(jsonCodec.encodeToString(ClientProfileUpdateDto.serializer(), update)) }
                .onSuccess { saved ->
                    _uiState.value = _uiState.value.copy(loading = false, profile = saved, profileJustSaved = true)
                }
                .onFailure {
                    _uiState.value = _uiState.value.copy(loading = false, error = "Couldn't save profile: ${it.message}")
                }
        }
    }

    // --- Monitoring toggle / notification history ------------------------------

    fun setMonitoringEnabled(enabled: Boolean) {
        viewModelScope.launch {
            settingsStore.setMonitoringEnabled(enabled)
            _uiState.value = _uiState.value.copy(monitoringEnabled = enabled)
        }
    }

    fun refreshNotifications() {
        val client = api() ?: return
        val uid = _uiState.value.user?.uid ?: return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(loading = true)
            runCatching { client.getClientNotifications(uid, 0L) }
                .onSuccess { list ->
                    _uiState.value = _uiState.value.copy(
                        notifications = list.sortedByDescending { it.id },
                        loading = false,
                        error = null,
                    )
                }
                .onFailure {
                    _uiState.value = _uiState.value.copy(loading = false, error = "Couldn't load notifications: ${it.message}")
                }
        }
    }
}
