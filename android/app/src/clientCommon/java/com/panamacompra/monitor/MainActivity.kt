package com.panamacompra.monitor

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.google.android.gms.auth.api.signin.GoogleSignIn
import com.google.android.gms.auth.api.signin.GoogleSignInOptions
import com.google.android.gms.common.api.ApiException
import com.panamacompra.monitor.ads.BannerAd
import com.panamacompra.monitor.ads.initAds
import com.panamacompra.monitor.network.NotificationDto
import com.panamacompra.monitor.ui.ClientUiState
import com.panamacompra.monitor.ui.ClientViewModel
import com.panamacompra.monitor.ui.theme.MonitorTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        initAds(applicationContext)
        setContent {
            MonitorTheme {
                ClientApp()
            }
        }
    }
}

@Composable
fun ClientApp(viewModel: ClientViewModel = viewModel()) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("PanamaCompra Alerts") },
                actions = {
                    if (uiState.baseUrl.isNotBlank()) {
                        TextButton(onClick = { viewModel.resetServer() }) { Text("Server") }
                    }
                    if (uiState.user != null) {
                        TextButton(onClick = { viewModel.signOut() }) { Text("Sign out") }
                    }
                },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.padding(padding).fillMaxSize()) {
            when {
                uiState.baseUrl.isBlank() -> ServerSetupScreen(error = uiState.error, onSave = viewModel::saveBaseUrl)
                uiState.user == null -> AuthScreen(uiState = uiState, viewModel = viewModel)
                else -> ClientTabs(uiState = uiState, viewModel = viewModel)
            }
            BannerAd()
        }
    }
}

@Composable
private fun ServerSetupScreen(error: String?, onSave: (String) -> Unit) {
    var baseUrl by remember { mutableStateOf("") }
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text(
            "Ask whoever runs the PanamaCompra monitor for the server address.",
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(16.dp))
        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text("Server address") },
            placeholder = { Text("http://192.168.1.42:8766/") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
        Spacer(Modifier.height(16.dp))
        Button(onClick = { onSave(baseUrl.trim()) }, enabled = baseUrl.isNotBlank()) { Text("Continue") }
    }
}

@Composable
private fun AuthScreen(uiState: ClientUiState, viewModel: ClientViewModel) {
    val context = LocalContext.current
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var isSignUp by remember { mutableStateOf(false) }

    val googleLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val task = GoogleSignIn.getSignedInAccountFromIntent(result.data)
        try {
            val account = task.getResult(ApiException::class.java)
            account.idToken?.let(viewModel::signInGoogle)
                ?: viewModel.reportAuthError("Google sign-in returned no ID token")
        } catch (e: ApiException) {
            viewModel.reportAuthError("Google sign-in failed (${e.statusCode})")
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text(if (isSignUp) "Create account" else "Sign in", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = email,
            onValueChange = { email = it },
            label = { Text("Email") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        uiState.authError?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = {
                if (isSignUp) viewModel.signUpEmail(email.trim(), password) else viewModel.signInEmail(email.trim(), password)
            },
            enabled = !uiState.authBusy && email.isNotBlank() && password.isNotBlank(),
        ) { Text(if (uiState.authBusy) "Please wait…" else if (isSignUp) "Sign up" else "Sign in") }
        TextButton(onClick = { isSignUp = !isSignUp }) {
            Text(if (isSignUp) "Already have an account? Sign in" else "New here? Create an account")
        }
        Spacer(Modifier.height(16.dp))
        Button(onClick = {
            // R.string.default_web_client_id is generated by the
            // google-services Gradle plugin from app/google-services.json —
            // see android/README.md's Firebase setup step.
            val options = GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
                .requestIdToken(context.getString(R.string.default_web_client_id))
                .requestEmail()
                .build()
            googleLauncher.launch(GoogleSignIn.getClient(context, options).signInIntent)
        }) { Text("Sign in with Google") }
    }
}

@Composable
private fun ClientTabs(uiState: ClientUiState, viewModel: ClientViewModel) {
    var tab by remember { mutableStateOf(0) }
    val calendarVisible = uiState.profile?.calendarVisible ?: true
    val profileTab = if (calendarVisible) 2 else 1

    LaunchedEffect(calendarVisible) {
        if (!calendarVisible && tab == 1) tab = 0
    }
    TabRow(selectedTabIndex = tab) {
        Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Alerts") })
        if (calendarVisible) {
            Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Calendar") })
        }
        Tab(selected = tab == profileTab, onClick = { tab = profileTab }, text = { Text("Profile") })
    }
    when (tab) {
        0 -> AlertsScreen(uiState, viewModel)
        1 -> if (calendarVisible) {
            CalendarScreen(baseUrl = uiState.baseUrl, uid = uiState.user?.uid ?: "")
        } else {
            ProfileScreen(uiState, viewModel)
        }
        else -> ProfileScreen(uiState, viewModel)
    }
}

@Composable
private fun AlertsScreen(uiState: ClientUiState, viewModel: ClientViewModel) {
    val context = LocalContext.current

    fun startMonitoring() {
        viewModel.startMonitoring()
    }

    val notificationPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startMonitoring()
        } else {
            viewModel.reportMonitoringError("Notification permission is required for background monitoring")
        }
    }

    fun requestMonitoring() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            startMonitoring()
        }
    }

    LaunchedEffect(uiState.monitoringEnabled, uiState.baseUrl, uiState.user) {
        if (uiState.monitoringEnabled && uiState.baseUrl.isNotBlank() && uiState.user != null) {
            val permissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            if (permissionGranted) {
                startMonitoring()
            } else {
                viewModel.reportMonitoringError("Notification permission was removed; monitoring is off")
            }
        }
    }

    Column(modifier = Modifier.fillMaxWidth().padding(16.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Column {
                Text("Monitoring", style = MaterialTheme.typography.titleSmall)
                Text(
                    if (uiState.monitoringEnabled) "Active — checking every 30s" else "Off",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Switch(
                checked = uiState.monitoringEnabled,
                onCheckedChange = { checked ->
                    if (checked) {
                        requestMonitoring()
                    } else {
                        viewModel.stopMonitoring()
                    }
                },
            )
        }
    }
    uiState.error?.let {
        Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(horizontal = 16.dp))
    }
    NotificationList(uiState.notifications, loading = uiState.loading, onRefresh = viewModel::refreshNotifications)
}

@Composable
private fun NotificationList(notifications: List<NotificationDto>, loading: Boolean, onRefresh: () -> Unit) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Recent alerts", style = MaterialTheme.typography.titleSmall)
            Button(onClick = onRefresh) { Text(if (loading) "Refreshing…" else "Refresh") }
        }
        Spacer(Modifier.height(8.dp))
        if (notifications.isEmpty()) {
            Text(if (loading) "Loading…" else "No alerts yet.")
            return
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(notifications) { item ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text(item.createdAt, style = MaterialTheme.typography.bodySmall)
                        Spacer(Modifier.height(4.dp))
                        Text(item.text, style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
        }
    }
}

@Composable
private fun CalendarScreen(baseUrl: String, uid: String) {
    if (baseUrl.isBlank() || uid.isBlank()) {
        Text("Not ready yet.", modifier = Modifier.padding(16.dp))
        return
    }
    val url = remember(baseUrl, uid) { baseUrl.trimEnd('/') + "/client-calendar?uid=" + Uri.encode(uid) }
    AndroidView(
        modifier = Modifier.fillMaxSize(),
        factory = { context ->
            WebView(context).apply {
                settings.javaScriptEnabled = true
                settings.allowFileAccess = false
                settings.allowContentAccess = false
                webViewClient = WebViewClient()
                loadUrl(url)
            }
        },
        update = { webView ->
            if (webView.url != url) webView.loadUrl(url)
        },
        onRelease = { webView ->
            webView.stopLoading()
            webView.destroy()
        },
    )
}

@Composable
private fun ProfileScreen(uiState: ClientUiState, viewModel: ClientViewModel) {
    var phone by remember { mutableStateOf("") }
    var profession by remember { mutableStateOf("") }
    var location by remember { mutableStateOf("") }
    var institution by remember { mutableStateOf("") }
    var filters by remember { mutableStateOf("") }
    var wantIndex by remember { mutableStateOf(true) }
    var wantDetails by remember { mutableStateOf(true) }
    var wantStatus by remember { mutableStateOf(true) }
    var calendarVisible by remember { mutableStateOf(true) }

    LaunchedEffect(uiState.profile) {
        val profile = uiState.profile ?: return@LaunchedEffect
        phone = profile.phone
        profession = profile.profession
        location = profile.location
        institution = profile.institution
        filters = profile.filters
        wantIndex = "index" in profile.purposes
        wantDetails = "details" in profile.purposes
        wantStatus = "status" in profile.purposes
        calendarVisible = profile.calendarVisible
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.weight(1f)) {
            item {
                Text(uiState.user?.email ?: "", style = MaterialTheme.typography.bodySmall)
            }
            item {
                OutlinedTextField(
                    value = phone,
                    onValueChange = { phone = it },
                    label = { Text("Phone number") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                SuggestField("Profession", profession, uiState.suggestions.professions) { profession = it }
            }
            item {
                SuggestField("Location", location, uiState.suggestions.locations) { location = it }
            }
            item {
                SuggestField("Institution", institution, uiState.suggestions.institutions) { institution = it }
            }
            item {
                Text("Notify me about", style = MaterialTheme.typography.titleSmall)
            }
            item { ToggleRow("New opportunities", wantIndex) { wantIndex = it } }
            item { ToggleRow("Item details", wantDetails) { wantDetails = it } }
            item { ToggleRow("Status changes", wantStatus) { wantStatus = it } }
            item { ToggleRow("Show calendar matching my filters", calendarVisible) { calendarVisible = it } }
            item {
                OutlinedTextField(
                    value = filters,
                    onValueChange = { filters = it },
                    label = { Text("Advanced filter keywords (optional)") },
                    placeholder = { Text("salud + insumos, -construccion") },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            uiState.error?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
            if (uiState.profileJustSaved) {
                item { Text("Saved.", color = MaterialTheme.colorScheme.primary) }
            }
        }
        Button(
            onClick = {
                val purposes = buildList {
                    if (wantIndex) add("index")
                    if (wantDetails) add("details")
                    if (wantStatus) add("status")
                }
                viewModel.saveProfile(phone, purposes, filters, profession, location, institution, calendarVisible)
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (uiState.loading) "Saving…" else "Save") }
    }
}

@Composable
private fun SuggestField(label: String, value: String, suggestions: List<String>, onChange: (String) -> Unit) {
    Column {
        OutlinedTextField(
            value = value,
            onValueChange = onChange,
            label = { Text(label) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        if (suggestions.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            LazyRow {
                items(suggestions.take(15)) { suggestion ->
                    AssistChip(
                        onClick = { onChange(suggestion) },
                        label = { Text(suggestion, maxLines = 1) },
                        modifier = Modifier.padding(end = 4.dp),
                    )
                }
            }
        }
    }
}

@Composable
private fun ToggleRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label)
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
