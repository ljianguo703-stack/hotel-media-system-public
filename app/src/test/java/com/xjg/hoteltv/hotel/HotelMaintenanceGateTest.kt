package com.xjg.hoteltv.hotel

import org.junit.Assert.*
import org.junit.Test

class HotelMaintenanceGateTest {
    private var time=1000L
    private fun gate()=HotelMaintenanceGate{time}
    @Test fun onlyQuietHomeCanPrepareAndUserInputRestartsQuietPeriod(){
        val g=gate();time+=59000;assertFalse(g.acquire("job","nonce",true))
        time+=1000;assertFalse(g.acquire("job","nonce",false))
        g.interacted();time+=59999;assertFalse(g.acquire("job","nonce",true))
        time++;assertTrue(g.acquire("job","nonce",true));assertEquals("job",g.current()?.job)
    }
    @Test fun sameAttemptCanRecoverButOtherJobOrNonceCannotTakeItsLock(){
        val g=gate();time+=60000;assertTrue(g.acquire("one","token",true))
        assertFalse(g.acquire("two","token",true));assertFalse(g.acquire("one","other",true))
        time+=5000;assertTrue(g.acquire("one","token",true))
        assertFalse(g.release("one","other"));assertNotNull(g.current())
        assertTrue(g.release("one","token"));assertNull(g.current());assertEquals(0L,g.idleMs())
    }
    @Test fun pausedMovieAdvertisementOrExternalAppCannotRenewAnIdleLease(){
        val g=gate();time+=60000;assertTrue(g.acquire("one","token",true))
        assertFalse(g.acquire("one","token",false))
    }
    @Test fun processRecreationRestoresOnlyBoundedLeaseAndTimeoutUnlocks(){
        val g=gate();time+=60000;g.acquire("one","token",true)
        val recreated=gate();recreated.restore(g.current());assertNotNull(recreated.current())
        time+=600000;assertNull(recreated.current())
    }
    @Test fun rebootClockResetAndCorruptLeaseCannotBlockGuestPermanently(){
        val g=gate();time+=60000;g.acquire("one","token",true);val saved=g.current()
        time=20;val recreated=gate();recreated.restore(saved);assertNull(recreated.current())
        recreated.restore(HotelMaintenanceGate.Lease("bad","bad",0,Long.MAX_VALUE));assertNull(recreated.current())
    }
}
