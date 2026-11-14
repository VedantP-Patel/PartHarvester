"""Multi-Network Adapter Manager for Combinational Net."""
from collections import deque
import socket
import time
from typing import Deque, Dict, List, Optional, Tuple
import psutil
from pydantic import BaseModel


class NetworkAdapter(BaseModel):
    """Represents a discovered network interface with adaptive telemetry."""
    name: str
    ip_address: str
    is_up: bool = True
    speed_mbps: int = 0
    friendly_type: str = "Ethernet"
    is_enabled: bool = True
    current_speed_bytes: float = 0.0
    ewma_speed_bytes: float = 0.0
    active_connections: int = 0
    max_concurrency_cap: int = 3
    allocated_slots: int = 1
    bandwidth_share_pct: float = 0.0


class NetworkManager:
    """Manages adapter enumeration, interface binding, EWMA tracking, and adaptive load balancing."""

    WINDOW_SECONDS = 8.0
    HW_CACHE_TTL = 2.0
    _VIRTUAL_KEYWORDS = (
        "wsl",
        "vethernet",
        "loopback",
        "vmware",
        "vmnet",
        "virtualbox",
        "vbox",
        "hyper-v",
        "docker",
        "npcap",
        "bluetooth",
        "tailscale",
        "zerotier",
        "wireguard",
        "wintun",
        "tap-windows",
    )

    def __init__(self, ewma_alpha: float = 0.35, default_per_adapter_cap: int = 3):
        self._enabled_adapters: Dict[str, bool] = {}
        self._per_adapter_caps: Dict[str, int] = {}
        self._adapter_bytes_history: Dict[str, Deque[Tuple[float, int]]] = {}
        self._adapter_rolling_bytes: Dict[str, int] = {}
        self._ewma_speeds: Dict[str, float] = {}
        self._last_ewma_update: Dict[str, float] = {}
        self._active_connections: Dict[str, int] = {}
        self._ewma_alpha = ewma_alpha
        self.default_per_adapter_cap = default_per_adapter_cap

        self._cached_hw_interfaces: List[Tuple[str, str, bool, int, str]] = []
        self._last_hw_scan: float = 0.0

    def set_adapter_cap(self, ip_address: str, cap: int) -> None:
        if ip_address:
            self._per_adapter_caps[ip_address] = max(1, int(cap))

    def _scan_hardware_interfaces(self) -> List[Tuple[str, str, bool, int, str]]:
        now = time.monotonic()
        if self._cached_hw_interfaces and (now - self._last_hw_scan) < self.HW_CACHE_TTL:
            return self._cached_hw_interfaces

        discovered: List[Tuple[str, str, bool, int, str]] = []
        active_ips = set()
        try:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
        except Exception:
            return self._cached_hw_interfaces

        for iface_name, addr_list in addrs.items():
            lower_name = iface_name.lower()
            if any(k in lower_name for k in self._VIRTUAL_KEYWORDS):
                continue

            iface_stat = stats.get(iface_name)
            is_up = iface_stat.isup if iface_stat else True
            speed_mbps = iface_stat.speed if iface_stat else 0

            for addr in addr_list:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith(("127.", "169.254.", "172.17.", "172.18.", "192.168.56.")):
                        continue

                    if any(k in lower_name for k in ("wi-fi", "wireless", "wlan")):
                        friendly = "Wi-Fi"
                    elif (
                        any(k in lower_name for k in ("tether", "rndis", "remote ndis", "ncm", "usb", "apple", "android", "mobile"))
                        or ip.startswith(("172.20.10.", "192.168.42."))
                    ):
                        friendly = "USB Tethering"
                    else:
                        friendly = "Ethernet / LAN"

                    active_ips.add(ip)
                    discovered.append((iface_name, ip, is_up, speed_mbps, friendly))

        # Evict stale disconnected DHCP IPs that have zero active connections
        if active_ips:
            for ip_key in list(self._ewma_speeds.keys()):
                if ip_key not in active_ips and self._active_connections.get(ip_key, 0) == 0:
                    self._ewma_speeds.pop(ip_key, None)
                    self._last_ewma_update.pop(ip_key, None)
                    self._adapter_bytes_history.pop(ip_key, None)
                    self._adapter_rolling_bytes.pop(ip_key, None)
                    self._per_adapter_caps.pop(ip_key, None)

        self._cached_hw_interfaces = discovered
        self._last_hw_scan = now
        return discovered

    def _prune_old_samples(self, ip_address: str, now: float) -> None:
        history = self._adapter_bytes_history.get(ip_address)
        if not history:
            return
        cutoff = now - self.WINDOW_SECONDS
        rolling = self._adapter_rolling_bytes.get(ip_address, 0)
        while history and history[0][0] < cutoff:
            _, old_bytes = history.popleft()
            rolling -= old_bytes
        self._adapter_rolling_bytes[ip_address] = max(0, rolling)

    def get_available_adapters(self) -> List[NetworkAdapter]:
        """Returns active IPv4 network interfaces with O(1) live speed telemetry and persisted caps."""
        hw_list = self._scan_hardware_interfaces()
        adapters: List[NetworkAdapter] = []

        for iface_name, ip, is_up, speed_mbps, friendly in hw_list:
            is_enabled = self._enabled_adapters.get(ip, is_up)
            now = time.monotonic()
            curr_speed = self.get_adapter_speed(ip, now=now)
            ewma = self._ewma_speeds.get(ip, 0.0)
            last_upd = self._last_ewma_update.get(ip)
            if curr_speed == 0.0 and ewma > 0.0 and last_upd is not None:
                idle_sec = now - last_upd
                if idle_sec > 2.0:
                    ewma = ewma * (0.5 ** ((idle_sec - 2.0) / 5.0))
                    if ewma <= 1024.0:
                        ewma = 0.0
                    self._ewma_speeds[ip] = ewma
            conns = self._active_connections.get(ip, 0)
            cap = self._per_adapter_caps.get(ip, self.default_per_adapter_cap)

            adapters.append(
                NetworkAdapter(
                    name=iface_name,
                    ip_address=ip,
                    is_up=is_up,
                    speed_mbps=speed_mbps,
                    friendly_type=friendly,
                    is_enabled=is_enabled,
                    current_speed_bytes=curr_speed,
                    ewma_speed_bytes=ewma,
                    active_connections=conns,
                    max_concurrency_cap=cap,
                    allocated_slots=cap if (is_enabled and is_up) else 0,
                )
            )

        total_ewma = sum(a.ewma_speed_bytes for a in adapters if a.is_enabled and a.is_up)
        for a in adapters:
            if total_ewma > 0 and a.is_enabled and a.is_up:
                a.bandwidth_share_pct = round((a.ewma_speed_bytes / total_ewma) * 100.0, 1)
            else:
                a.bandwidth_share_pct = 0.0

        return adapters

    def set_adapter_enabled(self, ip_address: str, enabled: bool) -> None:
        self._enabled_adapters[ip_address] = enabled

    def get_adapter_by_ip(self, ip_address: str) -> Optional[NetworkAdapter]:
        for a in self.get_available_adapters():
            if a.ip_address == ip_address:
                return a
        return None

    def record_chunk_transfer(self, ip_address: str, bytes_count: int) -> None:
        now = time.monotonic()
        if ip_address not in self._adapter_bytes_history:
            self._adapter_bytes_history[ip_address] = deque()
            self._adapter_rolling_bytes[ip_address] = 0

        self._adapter_bytes_history[ip_address].append((now, bytes_count))
        self._adapter_rolling_bytes[ip_address] += bytes_count
        self._prune_old_samples(ip_address, now)

        instant_speed = self.get_adapter_speed(ip_address, now=now)
        prev_ewma = self._ewma_speeds.get(ip_address, instant_speed)
        self._ewma_speeds[ip_address] = (
            self._ewma_alpha * instant_speed + (1.0 - self._ewma_alpha) * prev_ewma
        )
        self._last_ewma_update[ip_address] = now

    def get_adapter_speed(self, ip_address: str, now: Optional[float] = None) -> float:
        if now is None:
            now = time.monotonic()
        self._prune_old_samples(ip_address, now)
        history = self._adapter_bytes_history.get(ip_address)
        if not history:
            return 0.0
        total_bytes = self._adapter_rolling_bytes.get(ip_address, 0)
        earliest = history[0][0]
        duration = max(0.5, now - earliest)
        return total_bytes / duration

    def increment_connections(self, ip_address: Optional[str]) -> None:
        if ip_address:
            self._active_connections[ip_address] = self._active_connections.get(ip_address, 0) + 1

    def decrement_connections(self, ip_address: Optional[str]) -> None:
        if ip_address and ip_address in self._active_connections:
            self._active_connections[ip_address] = max(0, self._active_connections[ip_address] - 1)

    def calculate_adaptive_quotas(
        self, total_slots: int, adapters: Optional[List[NetworkAdapter]] = None
    ) -> Dict[str, int]:
        if adapters is None:
            adapters = self.get_available_adapters()
        active_adapters = [a for a in adapters if a.is_enabled and a.is_up]
        if not active_adapters:
            return {}

        total_ewma = sum(max(1024.0, a.ewma_speed_bytes) for a in active_adapters)
        quotas: Dict[str, int] = {}
        for a in active_adapters:
            speed = max(1024.0, a.ewma_speed_bytes)
            raw_quota = int(round(total_slots * (speed / total_ewma)))
            quotas[a.ip_address] = max(1, min(a.max_concurrency_cap, raw_quota))
        return quotas

    def get_best_available_adapter(self, total_target_concurrency: int = 6) -> Optional[NetworkAdapter]:
        all_adapters = self.get_available_adapters()
        active = [a for a in all_adapters if a.is_enabled and a.is_up]
        if not active:
            return None

        quotas = self.calculate_adaptive_quotas(total_target_concurrency, adapters=all_adapters)
        active.sort(
            key=lambda a: (
                a.max_concurrency_cap - self._active_connections.get(a.ip_address, 0) > 0,
                self._ewma_speeds.get(a.ip_address, 0.0),
            ),
            reverse=True,
        )

        for a in active:
            quota = quotas.get(a.ip_address, a.max_concurrency_cap)
            if self._active_connections.get(a.ip_address, 0) < quota:
                return a

        under_cap = [
            a for a in active if self._active_connections.get(a.ip_address, 0) < a.max_concurrency_cap
        ]
        if under_cap:
            under_cap.sort(
                key=lambda a: (
                    a.max_concurrency_cap - self._active_connections.get(a.ip_address, 0),
                    a.ewma_speed_bytes,
                ),
                reverse=True,
            )
            return under_cap[0]
        return None

    def allocate_segments_to_adapters(
        self, segment_count: int, strategy: str = "ewma"
    ) -> List[Optional[NetworkAdapter]]:
        """Assigns segments respecting each adapter's remaining socket headroom (cap - active)."""
        active = [a for a in self.get_available_adapters() if a.is_enabled and a.is_up]
        if not active:
            return [None] * segment_count

        if len(active) == 1 or strategy == "primary":
            return [active[0]] * segment_count

        if strategy == "equal":
            return [active[i % len(active)] for i in range(segment_count)]

        headroom: Dict[str, int] = {
            a.ip_address: max(0, a.max_concurrency_cap - a.active_connections)
            for a in active
        }
        total_ewma = sum(max(1024.0, a.ewma_speed_bytes) for a in active)
        quotas: Dict[str, int] = {}
        assigned_total = 0

        for a in active:
            share = max(1024.0, a.ewma_speed_bytes) / total_ewma
            desired = int(round(segment_count * share))
            capped = min(headroom[a.ip_address], desired) if sum(headroom.values()) > 0 else desired
            quotas[a.ip_address] = max(0, capped)
            assigned_total += quotas[a.ip_address]

        while assigned_total < segment_count:
            candidates = [
                a for a in active if quotas.get(a.ip_address, 0) < headroom[a.ip_address]
            ]
            if not candidates:
                candidates = active
            best = max(
                candidates,
                key=lambda a: (headroom[a.ip_address] - quotas.get(a.ip_address, 0), a.ewma_speed_bytes),
            )
            quotas[best.ip_address] = quotas.get(best.ip_address, 0) + 1
            assigned_total += 1

        adapter_map = {a.ip_address: a for a in active}
        result: List[Optional[NetworkAdapter]] = []
        for ip, count in quotas.items():
            if count > 0:
                result.extend([adapter_map[ip]] * count)

        while len(result) < segment_count:
            result.append(active[0])
        return result[:segment_count]

    def get_next_adapter(self) -> Optional[NetworkAdapter]:
        return self.get_best_available_adapter()