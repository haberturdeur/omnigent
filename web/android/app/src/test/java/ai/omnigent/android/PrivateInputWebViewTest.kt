package ai.omnigent.android

import android.view.inputmethod.EditorInfo
import org.junit.Assert.assertEquals
import org.junit.Test

class PrivateInputWebViewTest {
    @Test
    fun `private input flag is added without dropping existing IME options`() {
        val existing = EditorInfo.IME_ACTION_SEND
        val editorInfo = EditorInfo().apply { imeOptions = existing }

        PrivateInputWebView.applyPrivateImeOptions(editorInfo)

        assertEquals(
            existing or EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING,
            editorInfo.imeOptions,
        )
    }
}
