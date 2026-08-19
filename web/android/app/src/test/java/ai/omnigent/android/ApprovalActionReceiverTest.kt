package ai.omnigent.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ApprovalActionReceiverTest {
    @Test
    fun `approval actions produce the expected resolve bodies`() {
        assertEquals("accept", body(ApprovalActionReceiver.ACTION_APPROVE).getString("action"))
        assertEquals(
            true,
            body(ApprovalActionReceiver.ACTION_ALLOW_ALL_EDITS)
                .getJSONObject("content")
                .getBoolean("allow_all_edits"),
        )
        assertEquals(
            true,
            body(ApprovalActionReceiver.ACTION_REMEMBER)
                .getJSONObject("content")
                .getBoolean("remember"),
        )
        assertEquals("decline", body(ApprovalActionReceiver.ACTION_REJECT).getString("action"))
        assertNull(ApprovalActionReceiver.requestBody("unknown"))
    }

    private fun body(action: String) =
        JSONObject(requireNotNull(ApprovalActionReceiver.requestBody(action)))
}
