package ai.omnigent.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NativeBridgeScriptTest {
    @Test
    fun `bridge exposes the configured server base path`() {
        val source = NativeBridgeScript.sourceFor("https://example.test/omnigent")

        assertTrue(source.contains("serverBaseUrl: \"https://example.test/omnigent\""))
        assertTrue(source.contains("window.location.pathname"))
    }

    @Test
    fun `fallback settings button is removed when the spa control appears`() {
        val source = NativeBridgeScript.source

        assertTrue(source.contains("new MutationObserver"))
        assertTrue(source.contains("omnigent-native-server-recovery\")?.remove()"))
    }

    @Test
    fun `server base path is safely encoded for javascript`() {
        val source = NativeBridgeScript.sourceFor("https://example.test/a\";alert(1)//")

        assertFalse(source.contains("serverBaseUrl: \"https://example.test/a\";alert"))
        assertTrue(source.contains("serverBaseUrl: \"https://example.test/a\\\";alert(1)//\""))
    }
}
