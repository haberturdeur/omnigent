package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SessionTransitionTrackerTest {
    private fun session(
        id: String = "a",
        status: String = "running",
        pending: Int = 0,
    ) = MonitoredSession(id, "Session $id", status, pending)

    @Test
    fun `initial snapshot never notifies`() {
        val tracker = SessionTransitionTracker()

        assertTrue(tracker.observe(listOf(session(status = "idle", pending = 1))).isEmpty())
    }

    @Test
    fun `new elicitation notifies immediately`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session()))

        val events = tracker.observe(listOf(session(status = "waiting", pending = 1)))

        assertEquals(listOf(SessionAttentionEvent.Kind.NEEDS_INPUT), events.map { it.kind })
    }

    @Test
    fun `completion requires a second terminal snapshot`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session(status = "running")))

        assertTrue(tracker.observe(listOf(session(status = "idle"))).isEmpty())
        val events = tracker.observe(listOf(session(status = "idle")))

        assertEquals(listOf(SessionAttentionEvent.Kind.COMPLETED), events.map { it.kind })
    }

    @Test
    fun `resuming work cancels pending completion`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session(status = "running")))
        tracker.observe(listOf(session(status = "idle")))

        assertTrue(tracker.observe(listOf(session(status = "running"))).isEmpty())
        assertTrue(tracker.observe(listOf(session(status = "running"))).isEmpty())
    }

    @Test
    fun `new elicitation supersedes pending completion`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session(status = "running")))
        tracker.observe(listOf(session(status = "idle")))

        val events = tracker.observe(listOf(session(status = "idle", pending = 1)))

        assertEquals(listOf(SessionAttentionEvent.Kind.NEEDS_INPUT), events.map { it.kind })
    }

    @Test
    fun `failed completion is distinguished`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session(status = "running")))
        tracker.observe(listOf(session(status = "failed")))

        val events = tracker.observe(listOf(session(status = "failed")))

        assertEquals(listOf(SessionAttentionEvent.Kind.FAILED), events.map { it.kind })
    }

    @Test
    fun `reset seeds a fresh baseline`() {
        val tracker = SessionTransitionTracker()
        tracker.observe(listOf(session(status = "running")))
        tracker.observe(listOf(session(status = "idle")))
        tracker.reset()

        assertTrue(tracker.observe(listOf(session(status = "idle"))).isEmpty())
    }
}
