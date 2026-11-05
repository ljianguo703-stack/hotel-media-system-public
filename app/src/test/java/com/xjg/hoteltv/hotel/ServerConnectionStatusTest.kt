package com.xjg.hoteltv.hotel

import org.junit.Assert.*
import org.junit.Test

class ServerConnectionStatusTest {
    @Test fun initialStateDoesNotClaimTheCachedServerIsConnected() {
        assertEquals(ServerLinkState.CHECKING, ServerConnectionStatus().state)
    }
    @Test fun firstFailedCheckIsOfflineUntilThereHasBeenARealSuccess() {
        assertEquals(ServerLinkState.OFFLINE, ServerConnectionStatus().checked(false, 1).state)
    }
    @Test fun anOnlineServerNeedsTwoConsecutiveFailuresToAvoidFlickering() {
        val online = ServerConnectionStatus().checked(true, 1)
        val first = online.checked(false, 5_001)
        assertEquals(ServerLinkState.ONLINE, first.state)
        assertEquals(ServerLinkState.OFFLINE, first.checked(false, 10_001).state)
    }
    @Test fun recoveryImmediatelyRemovesTheWarningAndResetsFailures() {
        val recovered = ServerConnectionStatus().checked(false, 1).checked(true, 2)
        assertEquals(ServerLinkState.ONLINE, recovered.state)
        assertEquals(0, recovered.failures)
        assertEquals(ServerLinkState.ONLINE, recovered.checked(false, 3).state)
    }
    @Test fun staleOrRebasedMonotonicClockRequiresFreshProof() {
        val online = ServerConnectionStatus().checked(true, 1_000)
        assertEquals(ServerLinkState.ONLINE, online.fresh(16_000).state)
        assertEquals(ServerLinkState.CHECKING, online.fresh(16_001).state)
        assertEquals(ServerLinkState.CHECKING, online.fresh(999).state)
    }
    @Test fun repeatedFailuresRemainOfflineWithoutAnUnboundedCounter() {
        var value = ServerConnectionStatus()
        repeat(100) { value = value.checked(false, it.toLong()) }
        assertEquals(ServerLinkState.OFFLINE, value.state)
        assertEquals(2, value.failures)
    }
    @Test fun anOldInflightReplyCannotTurnTheLightGreenAfterStandby() {
        val online = ServerConnectionStatus().checked(true, 1)
        assertEquals(ServerLinkState.CHECKING, online.checked(true, 20_000, 2).state)
        assertEquals(ServerLinkState.CHECKING, online.checked(false, 1, 2).state)
    }
}
