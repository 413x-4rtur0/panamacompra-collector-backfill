package com.panamacompra.monitor.ads

import androidx.compose.runtime.Composable
import com.panamacompra.monitor.network.MonitorApi

/**
 * Paid flavor: no private sponsor ads. Same call signature as the free flavor's
 * PrivateAdBanner so ClientMainActivity doesn't know which flavor it's in.
 */
@Composable
fun PrivateAdBanner(api: MonitorApi?) {
}
