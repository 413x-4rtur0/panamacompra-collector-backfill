package com.panamacompra.monitor.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.clientDataStore by preferencesDataStore(name = "client_settings")

/**
 * Persists the client app's setup: which monitor to poll, and how far the
 * notification feed has been read (so foreground delivery can dedupe across
 * process restarts). Client *identity* isn't
 * stored here — it comes from FirebaseAuth's own session persistence (see
 * AuthRepository), not a manually-typed code.
 */
class ClientSettingsStore(private val context: Context) {
    private val baseUrlKey = stringPreferencesKey("base_url")
    private val monitoringEnabledKey = booleanPreferencesKey("monitoring_enabled")

    val baseUrl: Flow<String> = context.clientDataStore.data.map { it[baseUrlKey] ?: "" }
    val monitoringEnabled: Flow<Boolean> = context.clientDataStore.data.map { it[monitoringEnabledKey] ?: false }

    /** Keep independent cursors so switching accounts never skips another user's alerts. */
    fun lastNotificationId(uid: String): Flow<Long> {
        val key = longPreferencesKey("last_notification_id_$uid")
        return context.clientDataStore.data.map { it[key] ?: 0L }
    }

    suspend fun setBaseUrl(url: String) {
        context.clientDataStore.edit { it[baseUrlKey] = url }
    }

    suspend fun setLastNotificationId(uid: String, id: Long) {
        val key = longPreferencesKey("last_notification_id_$uid")
        context.clientDataStore.edit { it[key] = id }
    }

    suspend fun setMonitoringEnabled(enabled: Boolean) {
        context.clientDataStore.edit { it[monitoringEnabledKey] = enabled }
    }
}
