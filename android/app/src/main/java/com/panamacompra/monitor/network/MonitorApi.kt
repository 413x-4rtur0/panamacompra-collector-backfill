package com.panamacompra.monitor.network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Field
import retrofit2.http.FormUrlEncoded
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Query

/**
 * Mirrors the subset of src/40_monitor/001b-monitor-web.py's status_payload()
 * this app currently reads. `progress`/`processes` are left as raw maps
 * instead of being fully typed, since the server's key set is large and
 * evolves independently of this client (see PROGRESS keys / process_snapshot
 * in the web monitor).
 */
@Serializable
data class StatusResponse(
    val progress: Map<String, String> = emptyMap(),
    val percent: Int = 0,
    val processes: Map<String, Boolean> = emptyMap(),
    val done: Boolean = false,
    @SerialName("server_time") val serverTime: String = "",
)

/** One row from MANUAL_ACTIONS, served by GET /api/actions. */
@Serializable
data class ManualActionDto(
    val zone: String,
    val label: String,
    val comment: String = "",
)

/** One row from GET /api/client-notifications — a message that already went to this client's WhatsApp group. */
@Serializable
data class NotificationDto(
    val id: Long,
    @SerialName("created_at") val createdAt: String,
    val purpose: String? = null,
    val text: String,
)

/** Full profile as GET /api/client-profile returns it (includes admin-only fields the app never edits). */
@Serializable
data class ClientProfileDto(
    val name: String = "",
    @SerialName("chat_id") val chatId: String = "",
    val purposes: List<String> = listOf("index", "details", "status"),
    val filters: String = "",
    val enabled: Boolean = true,
    @SerialName("app_code") val appCode: String = "",
    @SerialName("firebase_uid") val firebaseUid: String = "",
    val email: String = "",
    val phone: String = "",
    val profession: String = "",
    val location: String = "",
    val institution: String = "",
    @SerialName("calendar_visible") val calendarVisible: Boolean = true,
)

/**
 * Body for POST /api/client-profile. Deliberately narrower than
 * ClientProfileDto: only fields the client is allowed to self-edit. The
 * server merges this onto the existing profile by key, so admin-only fields
 * (chat_id, app_code, name, enabled) must never appear here — including them
 * with blank/default values would silently clobber whatever an operator set
 * manually in the web/Tk monitor.
 */
@Serializable
data class ClientProfileUpdateDto(
    @SerialName("firebase_uid") val firebaseUid: String,
    val email: String,
    val phone: String,
    val purposes: List<String>,
    val filters: String,
    val profession: String,
    val location: String,
    val institution: String,
    @SerialName("calendar_visible") val calendarVisible: Boolean,
)

/** GET /api/filter-suggestions — distinct values already in the archive, for the Profile screen's suggestion chips. */
@Serializable
data class FilterSuggestionsDto(
    val institutions: List<String> = emptyList(),
    val locations: List<String> = emptyList(),
    val professions: List<String> = emptyList(),
)

interface MonitorApi {
    @GET("api/status")
    suspend fun getStatus(): StatusResponse

    @GET("api/actions")
    suspend fun getActions(): List<ManualActionDto>

    /** label must match a ManualActionDto.label exactly (server matches by label string). */
    @FormUrlEncoded
    @POST("api/manual-action")
    suspend fun runManualAction(@Field("label") label: String): Response<ResponseBody>

    /** uid = the signed-in Firebase user's uid. since = last-seen notification id, for incremental polling. */
    @GET("api/client-notifications")
    suspend fun getClientNotifications(@Query("uid") uid: String, @Query("since") since: Long): List<NotificationDto>

    @GET("api/client-profile")
    suspend fun getClientProfile(@Query("uid") uid: String): Response<ClientProfileDto>

    @FormUrlEncoded
    @POST("api/client-profile")
    suspend fun saveClientProfile(@Field("profile") profileJson: String): ClientProfileDto

    @GET("api/filter-suggestions")
    suspend fun getFilterSuggestions(): FilterSuggestionsDto
}
