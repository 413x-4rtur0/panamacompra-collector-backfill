package com.panamacompra.monitor.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.google.firebase.auth.FirebaseAuth
import com.panamacompra.monitor.data.ClientSettingsStore
import com.panamacompra.monitor.network.ApiClient
import com.panamacompra.monitor.network.NotificationDto
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

private const val FOREGROUND_CHANNEL_ID = "monitoring_service"
private const val ALERTS_CHANNEL_ID = "opportunity_alerts"
private const val FOREGROUND_NOTIFICATION_ID = 1
private const val POLL_INTERVAL_MS = 30_000L

/**
 * Keeps polling GET /api/client-notifications while the app is backgrounded,
 * posting a local notification for each new row — this is the actual
 * "instead of WhatsApp" delivery mechanism. There's no cloud push (FCM) here
 * since the monitor is a local-LAN-only server with no internet-reachable
 * backend, so a foreground service is the standard way to keep polling
 * reliably when the phone screen is off.
 */
class NotificationPollingService : Service() {
    private var job: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        createChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(FOREGROUND_NOTIFICATION_ID, buildForegroundNotification())
        if (job == null) {
            job = scope.launch { pollLoop() }
        }
        return START_STICKY
    }

    private suspend fun pollLoop() {
        val settings = ClientSettingsStore(applicationContext)
        while (true) {
            val baseUrl = settings.baseUrl.first()
            val uid = FirebaseAuth.getInstance().currentUser?.uid
            if (baseUrl.isNotBlank() && !uid.isNullOrBlank()) {
                val lastId = settings.lastNotificationId.first()
                runCatching {
                    ApiClient.create(baseUrl).getClientNotifications(uid, lastId)
                }.onSuccess { notifications ->
                    if (notifications.isNotEmpty()) {
                        notifications.forEach { postAlert(it) }
                        settings.setLastNotificationId(notifications.maxOf { it.id })
                    }
                }
                // Failures (monitor offline, wrong network, etc.) are silently
                // retried next cycle — this is a best-effort background poll,
                // not a user-facing action that needs an error surface.
            }
            delay(POLL_INTERVAL_MS)
        }
    }

    private fun postAlert(notification: NotificationDto) {
        val manager = getSystemService(NotificationManager::class.java)
        val title = when (notification.purpose) {
            "index" -> "New opportunity"
            "details" -> "Opportunity details"
            "status" -> "Status change"
            else -> "PanamaCompra alert"
        }
        val built = NotificationCompat.Builder(this, ALERTS_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(notification.text.take(120))
            .setStyle(NotificationCompat.BigTextStyle().bigText(notification.text))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        // Notification id = server row id, so a re-delivered row (e.g. after
        // a since-cursor reset) replaces rather than duplicates.
        manager.notify(notification.id.toInt(), built)
    }

    private fun buildForegroundNotification(): Notification =
        NotificationCompat.Builder(this, FOREGROUND_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setContentTitle("Monitoring for new opportunities")
            .setContentText("Checking every 30s while this is on")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(FOREGROUND_CHANNEL_ID, "Monitoring status", NotificationManager.IMPORTANCE_LOW),
        )
        manager.createNotificationChannel(
            NotificationChannel(ALERTS_CHANNEL_ID, "Opportunity alerts", NotificationManager.IMPORTANCE_HIGH),
        )
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
