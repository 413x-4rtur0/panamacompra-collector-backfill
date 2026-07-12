package com.panamacompra.monitor.ads

import androidx.compose.runtime.Composable

/**
 * Paid flavor: no ads, no AdMob dependency at all. Same call signature as the
 * free flavor's BannerAd so ClientMainActivity (clientCommon) doesn't need to
 * know which flavor it's compiled into.
 */
@Composable
fun BannerAd() {
    // Intentionally empty.
}
