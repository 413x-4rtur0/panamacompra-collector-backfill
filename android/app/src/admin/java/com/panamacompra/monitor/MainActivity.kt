package com.panamacompra.monitor

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.panamacompra.monitor.network.ManualActionDto
import com.panamacompra.monitor.network.StatusResponse
import com.panamacompra.monitor.ui.MonitorViewModel
import com.panamacompra.monitor.ui.theme.MonitorTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MonitorTheme {
                MonitorApp()
            }
        }
    }
}

@Composable
fun MonitorApp(viewModel: MonitorViewModel = viewModel()) {
    val uiState by viewModel.uiState.collectAsState()
    var tab by remember { mutableStateOf(0) }
    var showConnectDialog by remember { mutableStateOf(false) }
    var urlDraft by remember(uiState.baseUrl) { mutableStateOf(uiState.baseUrl) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("PanamaCompra Monitor") },
                actions = {
                    TextButton(onClick = {
                        urlDraft = uiState.baseUrl
                        showConnectDialog = true
                    }) { Text("Connect") }
                },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.padding(padding).fillMaxSize()) {
            Text(
                text = uiState.baseUrl.ifBlank { "Not connected — tap Connect" },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                style = MaterialTheme.typography.bodySmall,
            )
            uiState.error?.let {
                Text(
                    it,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(horizontal = 16.dp),
                )
            }
            TabRow(selectedTabIndex = tab) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Dashboard") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Actions") })
            }
            when (tab) {
                0 -> DashboardScreen(status = uiState.status, loading = uiState.loading)
                else -> ActionsScreen(
                    actions = uiState.actions,
                    lastResult = uiState.lastActionResult,
                    onRun = viewModel::runAction,
                )
            }
        }
    }

    if (showConnectDialog) {
        AlertDialog(
            onDismissRequest = { showConnectDialog = false },
            title = { Text("Monitor address") },
            text = {
                Column {
                    Text(
                        "Phone must be on the same WiFi as the machine running the web " +
                            "monitor, and PC_MONITOR_HOST there must be 0.0.0.0 (not 127.0.0.1). " +
                            "Example: http://192.168.1.42:8766/",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = urlDraft,
                        onValueChange = { urlDraft = it },
                        label = { Text("Base URL") },
                        singleLine = true,
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.connect(urlDraft.trim())
                    showConnectDialog = false
                }) { Text("Connect") }
            },
            dismissButton = {
                TextButton(onClick = { showConnectDialog = false }) { Text("Cancel") }
            },
        )
    }
}

@Composable
fun DashboardScreen(status: StatusResponse?, loading: Boolean) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        if (status == null) {
            Text(if (loading) "Loading…" else "No data yet. Tap Connect and enter the monitor's address.")
            return
        }
        Text(status.progress["MESSAGE"] ?: "-", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(4.dp))
        Text(
            "Status: ${status.progress["STATUS"] ?: "-"} · ${status.serverTime}",
            style = MaterialTheme.typography.bodySmall,
        )
        Spacer(Modifier.height(12.dp))
        LinearProgressIndicator(progress = status.percent / 100f, modifier = Modifier.fillMaxWidth())
        Text("${status.percent}%", style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(16.dp))
        Text("Processes", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(6.dp))
        ProcessChips(status.processes)
    }
}

@Composable
private fun ProcessChips(processes: Map<String, Boolean>) {
    // Two-per-row chip grid; avoids pulling in the experimental FlowRow API
    // for this first cut of the dashboard.
    Column {
        processes.entries.toList().chunked(2).forEach { rowEntries ->
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                rowEntries.forEach { (key, on) ->
                    AssistChip(
                        onClick = {},
                        label = { Text(key) },
                        colors = AssistChipDefaults.assistChipColors(
                            containerColor = if (on) {
                                MaterialTheme.colorScheme.primaryContainer
                            } else {
                                MaterialTheme.colorScheme.surfaceVariant
                            },
                        ),
                    )
                }
            }
            Spacer(Modifier.height(6.dp))
        }
    }
}

@Composable
fun ActionsScreen(actions: List<ManualActionDto>, lastResult: String?, onRun: (String) -> Unit) {
    var pendingConfirm by remember { mutableStateOf<ManualActionDto?>(null) }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        lastResult?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(bottom = 8.dp))
        }
        if (actions.isEmpty()) {
            Text("No actions loaded yet.")
            return
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            actions.groupBy { it.zone }.forEach { (zone, zoneActions) ->
                item {
                    Text(zone, style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(top = 8.dp))
                }
                items(zoneActions) { action ->
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(action.label, style = MaterialTheme.typography.bodyLarge)
                                if (action.comment.isNotBlank()) {
                                    Text(action.comment, style = MaterialTheme.typography.bodySmall)
                                }
                            }
                            Button(onClick = {
                                // Mirror the web monitor's confirm() gate on DANGER-labelled
                                // actions (e.g. "STOP all runners") — a mis-tap is cheaper to
                                // recover from than an accidentally-stopped collection run.
                                if (action.comment.contains("DANGER", ignoreCase = true)) {
                                    pendingConfirm = action
                                } else {
                                    onRun(action.label)
                                }
                            }) { Text("Run") }
                        }
                    }
                }
            }
        }
    }

    pendingConfirm?.let { action ->
        AlertDialog(
            onDismissRequest = { pendingConfirm = null },
            title = { Text("Confirm: ${action.label}") },
            text = { Text(action.comment) },
            confirmButton = {
                TextButton(onClick = {
                    onRun(action.label)
                    pendingConfirm = null
                }) { Text("Run anyway") }
            },
            dismissButton = {
                TextButton(onClick = { pendingConfirm = null }) { Text("Cancel") }
            },
        )
    }
}
