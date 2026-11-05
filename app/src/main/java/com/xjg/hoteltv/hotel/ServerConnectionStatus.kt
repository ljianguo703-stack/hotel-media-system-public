package com.xjg.hoteltv.hotel

internal enum class ServerLinkState { CHECKING, ONLINE, OFFLINE }

/** A cached catalog is never proof of a current, authenticated connection. */
internal data class ServerConnectionStatus(
    val state: ServerLinkState = ServerLinkState.CHECKING,
    val failures: Int = 0,
    val checkedAt: Long? = null,
) {
    fun checked(healthy: Boolean, at: Long, startedAt: Long = at): ServerConnectionStatus {
        // A reply queued before a long standby/background freeze is not fresh proof on resume.
        if (at < startedAt || at - startedAt > STALE_MS) return ServerConnectionStatus()
        return if (healthy) ServerConnectionStatus(ServerLinkState.ONLINE, 0, at)
        else copy(state = if (state == ServerLinkState.ONLINE && failures == 0) ServerLinkState.ONLINE else ServerLinkState.OFFLINE,
            failures = (failures + 1).coerceAtMost(2), checkedAt = at)
    }

    fun fresh(at: Long): ServerConnectionStatus = if (checkedAt?.let { at - it > STALE_MS || at < it } == true)
        ServerConnectionStatus() else this

    companion object {
        const val POLL_MS = 5_000L
        const val STALE_MS = 15_000L
    }
}
