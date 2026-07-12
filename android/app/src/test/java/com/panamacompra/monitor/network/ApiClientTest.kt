package com.panamacompra.monitor.network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ApiClientTest {
    @Test
    fun normalizeBaseUrl_addsSchemeAndTrailingSlash() {
        assertEquals("http://192.168.1.42:8766/", ApiClient.normalizeBaseUrl("192.168.1.42:8766"))
    }

    @Test
    fun normalizeBaseUrl_preservesPathAndRemovesQueryData() {
        assertEquals(
            "https://monitor.example.test/base/",
            ApiClient.normalizeBaseUrl(" https://monitor.example.test/base?token=secret#fragment "),
        )
    }

    @Test
    fun normalizeBaseUrl_rejectsUnsupportedOrMalformedAddresses() {
        assertThrows(IllegalArgumentException::class.java) { ApiClient.normalizeBaseUrl("ftp://server.test") }
        assertThrows(IllegalArgumentException::class.java) { ApiClient.normalizeBaseUrl("http://") }
    }
}
