package com.panamacompra.monitor.ads

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.panamacompra.monitor.network.MonitorApi
import com.panamacompra.monitor.network.SponsorAdDto

/**
 * Private/sponsor ad banner loaded from the local monitor's GET /api/sponsor-ad.
 * Shows an image from the sponsor, tappable to open click_url in the browser.
 * Renders nothing when no active ad is returned.
 */
@Composable
fun PrivateAdBanner(api: MonitorApi?) {
    var ad by remember { mutableStateOf<SponsorAdDto?>(null) }

    LaunchedEffect(api) {
        if (api == null) return@LaunchedEffect
        try {
            ad = api.getSponsorAd()
        } catch (_: Exception) {
            ad = null
        }
    }

    ad?.let { sponsor ->
        val context = LocalContext.current
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .clickable {
                    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(sponsor.clickUrl))
                    context.startActivity(intent)
                },
        ) {
            AsyncImage(
                model = sponsor.imageUrl,
                contentDescription = sponsor.altText,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 80.dp),
                contentScale = ContentScale.Fit,
            )
        }
    }
}
