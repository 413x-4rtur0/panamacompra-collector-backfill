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
 * notification feed has been read (so both the foreground service and the
 * in-app list can dedupe across process restarts). Client *identity* isn't
 * stored here — it comes from FirebaseAuth's own session persistence (see
 * AuthRepository), not a manually-typed code.
 */
class ClientSettingsStore(private val context: Context) {
    private val baseUrlKey = stringPreferencesKey("base_url")
    private val lastNotificationIdKey = longPreferencesKey("last_notification_id")
    private val monitoringEnabledKey = booleanPreferencesKey("monitoring_enabled")

    val baseUrl: Flow<String> = context.clientDataStore.data.map { it[baseUrlKey] ?: "" }
    val lastNotificationId: Flow<Long> = context.clientDataStore.data.map { it[lastNotificationIdKey] ?: 0L }
    val monitoringEnabled: Flow<Boolean> = context.clientDataStore.data.map { it[monitoringEnabledKey] ?: false }

    suspend fun setBaseUrl(url: String) {
        context.clientDataStore.edit { it[baseUrlKey] = url }
    }

    suspend fun setLastNotificationId(id: Long) {
        context.clientDataStore.edit { it[lastNotificationIdKey] = id }
    }

    suspend fun setMonitoringEnabled(enabled: Boolean) {
        context.clientDataStore.edit { it[monitoringEnabledKey] = enabled }
    }
}
