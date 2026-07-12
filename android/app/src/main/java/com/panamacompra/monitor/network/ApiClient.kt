package com.panamacompra.monitor.network

import com.panamacompra.monitor.BuildConfig
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

object ApiClient {
    private val json = Json { ignoreUnknownKeys = true }
    private val apiCache = mutableMapOf<String, MonitorApi>()
    private val httpClient: OkHttpClient by lazy {
        val builder = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)

        if (BuildConfig.DEBUG) {
            builder.addInterceptor(
                HttpLoggingInterceptor().apply {
                    level = HttpLoggingInterceptor.Level.BASIC
                    redactQueryParams("uid", "code")
                },
            )
        }
        builder.build()
    }

    /** Validate and canonicalize a user-entered HTTP(S) monitor address. */
    fun normalizeBaseUrl(baseUrl: String): String {
        val trimmed = baseUrl.trim()
        require(trimmed.isNotEmpty()) { "Server address is required" }
        val candidate = if ("://" in trimmed) trimmed else "http://$trimmed"
        val parsed = candidate.toHttpUrlOrNull()
            ?: throw IllegalArgumentException("Enter a valid HTTP or HTTPS server address")
        require(parsed.host.isNotBlank()) { "Server address must include a host" }
        val normalized = parsed.newBuilder().query(null).fragment(null).build().toString()
        return if (normalized.endsWith('/')) normalized else "$normalized/"
    }

    fun create(baseUrl: String): MonitorApi {
        val normalized = normalizeBaseUrl(baseUrl)
        synchronized(apiCache) {
            return apiCache.getOrPut(normalized) {
                Retrofit.Builder()
                    .baseUrl(normalized)
                    .client(httpClient)
                    .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
                    .build()
                    .create(MonitorApi::class.java)
            }
        }
    }
}
