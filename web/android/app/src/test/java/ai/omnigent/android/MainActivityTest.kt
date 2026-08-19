package ai.omnigent.android

import android.content.Context
import android.content.Intent
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.os.Looper
import android.webkit.WebView
import android.widget.FrameLayout
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowAlertDialog
import org.robolectric.shadows.ShadowRestrictionsManager

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityTest {
    @Before
    fun clearPrivateKeyboardPreference() {
        ApplicationProvider
            .getApplicationContext<Context>()
            .getSharedPreferences("private_input", Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
    }

    @Test
    fun `exported launcher ignores a supplied notification server`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect("https://two.example")
        val intent =
            Intent(context, MainActivity::class.java).apply {
                putExtra(NativeNotificationManager.EXTRA_SERVER_URL, "https://one.example")
                putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, "/c/session-a")
            }

        Robolectric.buildActivity(MainActivity::class.java, intent).setup().get()

        assertEquals("https://two.example", ServerStore(context).currentServerUrl())
    }

    @Test
    fun `internal notification router selects its source server`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect("https://two.example")
        val intent =
            Intent(context, NotificationRouterActivity::class.java).apply {
                putExtra(NativeNotificationManager.EXTRA_SERVER_URL, "https://one.example")
                putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, "/c/session-a")
            }

        Robolectric.buildActivity(NotificationRouterActivity::class.java, intent).setup()

        assertEquals("https://one.example", ServerStore(context).currentServerUrl())
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    fun `standard webview input is the default so the software keyboard works`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        PrivateInputWebView.setEnabled(context, false)
        ServerStore(context).connect("https://example.com")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals(WebView::class.java, activity.webView().javaClass)
    }

    @Test
    fun `server controls do not overlay the webview`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val content = activity.findViewById<android.view.ViewGroup>(android.R.id.content)
        val container = content.getChildAt(0) as FrameLayout

        assertEquals(1, container.childCount)
        assertTrue(container.getChildAt(0) is WebView)
    }

    @Test
    fun `server settings open as a visible dialog independent of the webview bounds`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val method =
            MainActivity::class.java
                .getDeclaredMethod("showServerSwitcherMenu", android.view.View::class.java)
                .apply { isAccessible = true }

        method.invoke(activity, activity.webView())

        val dialog = ShadowAlertDialog.getLatestAlertDialog()
        assertNotNull(dialog)
        val message = dialog.findViewById<android.widget.TextView>(android.R.id.message)
        assertTrue(message == null || message.visibility != android.view.View.VISIBLE)
        assertEquals(
            activity.getString(R.string.server_settings_title),
            dialog
                .findViewById<android.widget.TextView>(
                    R.id.server_settings_title,
                ).text
                .toString(),
        )
        assertEquals(
            "example.com",
            dialog
                .findViewById<android.widget.TextView>(
                    R.id.connected_server_host,
                ).text
                .toString(),
        )
        val actionContainer =
            dialog.findViewById<android.widget.LinearLayout>(R.id.server_settings_actions)
        val actions =
            (0 until actionContainer.childCount).map { index ->
                actionContainer
                    .getChildAt(index)
                    .findViewById<android.widget.TextView>(R.id.server_action_label)
                    .text
                    .toString()
            }
        assertTrue(actions.contains(activity.getString(R.string.menu_reload)))
        assertTrue(actions.contains(activity.getString(R.string.menu_connect_new)))
        assertTrue(actions.contains(activity.getString(R.string.menu_enable_push)))
        assertTrue(actions.contains(activity.getString(R.string.menu_enable_monitor)))
    }

    @Test
    fun `private keyboard is enabled by default`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals(PrivateInputWebView::class.java, activity.webView().javaClass)
    }

    @Test
    fun `switching server paths on the same origin reloads the webview`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com/omnigent-a")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()

        ServerStore(context).connect("https://example.com/omnigent-b")
        controller.newIntent(Intent(context, MainActivity::class.java))

        assertEquals(
            "https://example.com/omnigent-b",
            shadowOf(activity.webView()).lastLoadedUrl,
        )
    }

    @Test
    fun `post-auth reload preserves the configured server path`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com/omnigent")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val method =
            MainActivity::class.java
                .getDeclaredMethod("onSessionToken", String::class.java)
                .apply { isAccessible = true }

        method.invoke(activity, "header.payload.signature")
        shadowOf(Looper.getMainLooper()).idle()

        assertEquals(
            "https://example.com/omnigent",
            shadowOf(activity.webView()).lastLoadedUrl,
        )
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `a managed preset never overrides the server the user picked`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")
        val manager = context.getSystemService(RestrictionsManager::class.java)
        Shadow
            .extract<ShadowRestrictionsManager>(manager)
            .setApplicationRestrictions(
                Bundle().apply {
                    putString(ManagedConfig.KEY_SERVER_URLS, "https://managed.example.com")
                },
            )

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView
}
