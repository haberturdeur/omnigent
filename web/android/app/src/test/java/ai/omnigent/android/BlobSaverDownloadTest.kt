package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

@RunWith(RobolectricTestRunner::class)
class BlobSaverDownloadTest {
    @Test
    fun `wrong replica retries once without slice key`() {
        val first =
            FakeConnection(
                400,
                errorBody = """{"error":{"code":"wrong_replica"}}""",
            )
        val second = FakeConnection(200, body = "file")
        val connections = ArrayDeque(listOf(first, second))

        val result =
            openNativeDownloadConnection(
                "https://example.test/file",
                mapOf(
                    "Cookie" to "session=value",
                    "X-Databricks-Omnigent-Slice-Key" to "host-a",
                ),
            ) { connections.removeFirst() }

        assertNotNull(result)
        assertEquals("host-a", first.getRequestProperty("X-Databricks-Omnigent-Slice-Key"))
        assertNull(second.getRequestProperty("X-Databricks-Omnigent-Slice-Key"))
        assertEquals("session=value", second.getRequestProperty("Cookie"))
        assertTrue(first.disconnected)
    }

    @Test
    fun `ordinary failure is not retried`() {
        val first = FakeConnection(403, errorBody = "forbidden")
        var opened = 0

        val result =
            openNativeDownloadConnection(
                "https://example.test/file",
                mapOf("X-Databricks-Omnigent-Slice-Key" to "host-a"),
            ) {
                opened++
                first
            }

        assertNull(result)
        assertEquals(1, opened)
        assertTrue(first.disconnected)
    }

    private class FakeConnection(
        private val status: Int,
        private val body: String = "",
        private val errorBody: String = "",
    ) : HttpURLConnection(URL("https://example.test")) {
        var disconnected = false

        override fun getResponseCode(): Int = status

        override fun getInputStream(): InputStream = ByteArrayInputStream(body.toByteArray())

        override fun getErrorStream(): InputStream = ByteArrayInputStream(errorBody.toByteArray())

        override fun disconnect() {
            disconnected = true
        }

        override fun usingProxy(): Boolean = false

        override fun connect() = Unit
    }
}
