package com.dasinong.backend.net

import android.net.ConnectivityManager
import android.net.LinkAddress
import android.net.Network
import android.net.NetworkCapabilities
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import java.net.NetworkInterface
import java.net.Socket

/**
 * 「无互联网热点」的关键：不依赖系统默认网络，而是显式枚举并选中 WLAN/以太网的
 * [Network]，再对每个 socket 调用 [Network.bindSocket]。
 *
 * 判据用 hasTransport(WIFI|ETHERNET) 而非 NET_CAPABILITY_INTERNET —— 后者会误伤
 * 「无互联网但仍是局域网」的热点 WiFi。
 */
class LanNetworkBinder(private val cm: ConnectivityManager) {

    fun candidateLans(): List<Network> =
        cm.allNetworks.filter { n ->
            val caps = cm.getNetworkCapabilities(n) ?: return@filter false
            (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) &&
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
        }

    fun pickPrimary(): Network? = candidateLans().firstOrNull()

    fun sourceIp(network: Network): Inet4Address? {
        val lp = cm.getLinkProperties(network) ?: return null
        for (la in lp.linkAddresses) {
            val addr = la.address
            if (addr is Inet4Address && !addr.isLoopbackAddress) return addr
        }
        return null
    }

    fun allSourceIps(): List<Inet4Address> =
        candidateLans().mapNotNull { sourceIp(it) }

    /** 定向广播地址 ip | ~mask。 */
    fun directedBroadcast(network: Network): Inet4Address? {
        val lp = cm.getLinkProperties(network) ?: return null
        val la: LinkAddress = lp.linkAddresses.firstOrNull { it.address is Inet4Address } ?: return null
        val ip = (la.address as Inet4Address).address
        val prefix = la.prefixLength
        val mask = if (prefix <= 0) 0 else (-1 shl (32 - prefix))
        val bc = ByteArray(4) { i ->
            (ip[i].toInt() or (mask ushr (24 - i * 8)).inv()).toByte()
        }
        return InetAddress.getByAddress(bc) as Inet4Address
    }

    fun interfaceName(network: Network): String? =
        cm.getLinkProperties(network)?.interfaceName

    fun openUdp(network: Network?, port: Int): DatagramSocket {
        val s = DatagramSocket(null)
        s.reuseAddress = true
        s.broadcast = true
        if (network != null) network.bindSocket(s)
        s.bind(InetSocketAddress(port))
        return s
    }

    fun openMulticast(network: Network, port: Int): MulticastSocket {
        val mc = MulticastSocket(null)
        mc.reuseAddress = true
        network.bindSocket(mc)
        mc.bind(InetSocketAddress(port))
        return mc
    }

    fun joinMulticast(mc: MulticastSocket, group: InetAddress, network: Network) {
        val name = interfaceName(network) ?: return
        val iface: NetworkInterface = NetworkInterface.getByName(name) ?: return
        try {
            mc.joinGroup(InetSocketAddress(group, mc.localPort), iface)
        } catch (_: Exception) {
        }
    }

    /** 逐 socket 绑定到指定网络后再连接。 */
    fun connectTcp(network: Network?, ip: String, port: Int, timeoutMs: Int): Socket {
        val s = Socket()
        if (network != null) network.bindSocket(s)
        s.tcpNoDelay = true
        s.connect(InetSocketAddress(ip, port), timeoutMs)
        return s
    }

    /**
     * 依次尝试所有候选局域网（WiFi/以太网）建连，最后再试系统默认网络。
     * 多网络/无互联网热点下，只绑一个网络很容易连不上。
     */
    fun connectTcpAny(ip: String, port: Int, timeoutMs: Int): Socket {
        var lastError: Exception? = null
        for (net in candidateLans()) {
            try {
                return connectTcp(net, ip, port, timeoutMs)
            } catch (e: Exception) {
                lastError = e
            }
        }
        return try {
            connectTcp(null, ip, port, timeoutMs)
        } catch (e: Exception) {
            throw (lastError ?: e)
        }
    }
}
