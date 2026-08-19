package ai.omnigent.android

import android.content.Context
import android.util.AttributeSet
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection
import android.webkit.WebView

/** WebView that asks the active IME not to retain personalized input history. */
class PrivateInputWebView
    @JvmOverloads
    constructor(
        context: Context,
        attrs: AttributeSet? = null,
        defStyleAttr: Int = 0,
    ) : WebView(context, attrs, defStyleAttr) {
        override fun onCreateInputConnection(outAttrs: EditorInfo): InputConnection? {
            val connection = super.onCreateInputConnection(outAttrs)
            applyPrivateImeOptions(outAttrs)
            return connection
        }

        internal companion object {
            fun applyPrivateImeOptions(editorInfo: EditorInfo) {
                editorInfo.imeOptions =
                    editorInfo.imeOptions or EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
            }
        }
    }
