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
            private const val PREFERENCES = "private_input"
            private const val ENABLED = "enabled"

            fun isEnabled(context: Context): Boolean =
                context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
                    .getBoolean(ENABLED, true)

            fun setEnabled(
                context: Context,
                enabled: Boolean,
            ) {
                context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
                    .edit()
                    .putBoolean(ENABLED, enabled)
                    .apply()
            }

            fun applyPrivateImeOptions(editorInfo: EditorInfo) {
                editorInfo.imeOptions =
                    editorInfo.imeOptions or EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
            }
        }
    }
