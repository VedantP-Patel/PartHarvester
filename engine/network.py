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
    HW_CACHE_TTL = 3.0

    def __init__(self, ewma_alpha: float = 0.35, default_per_adapter_cap: int = 3):
        self._enabled_adapters: Dict[str, bool] = {}
        self._adapter_bytes_history: Dict[str, Deque[Tuple[float, int]]] = {}
        self._adapter_rolling_bytes: Dict[str, int] = {}
        self._ewma_speeds: Dict[str, float] = {}
        self._active_connections: Dict[str, int] = {}
        self._ewma_alpha = ewma_alpha
        self.default_per_adapter_cap = default_per_adapter_cap

        # Cached OS hardware interface discovery to avoid 20Hz psutil syscalls
        self._cached_hw_interfaces: List[Tuple[str, str, bool, int, str]] = []
        self._last_hw_scan: float = 0.0

    def _scan_hardware_interfaces(self) -> List[Tuple[str, str, bool, int, str]]:
        now = time.monotonic()
        if self._cached_hw_interfaces and (now - self._last_hw_scan) < self.HW_CACHE_TTL:
            return self._cached_hw_interfaces

        discovered: List[Tuple[str, str, bool, int, str]] = []
        try:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
        except Exception:
            return self._cached_hw_interfaces

        for iface_name, addr_list in addrs.items():
            lower_name = iface_name.lower()
            if "wsl" in lower_name or "vethernet" in lower_name or "loopback" in lower_name:
                continue

            iface_stat = stats.get(iface_name)
            is_up = iface_stat.isup if iface_stat else True
            speed_mbps = iface_stat.speed if iface_stat else 0

            for addr in addr_list:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith(("127.", "169.254.", "172.17.", "172.18.")):
                        continue

                    if any(k in lower_name for k in ("wi-fi", "wireless", "wlan")):
                        friendly = "Wi-Fi"
                    elif any(k in lower_name for k in ("tether", "rndis", "remote ndis")):
                        friendly = "USB Tethering"
                    else:
                        friendly = "Ethernet / LAN"

                    discovered.append((iface_name, ip, is_up, speed_mbps, friendly))

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
        """Returns active IPv4 network interfaces with O(1) live speed telemetry."""
        hw_list = self._scan_hardware_interfaces()
        adapters: List[NetworkAdapter] = []

        for iface_name, ip, is_up, speed_mbps, friendly in hw_list:
            is_enabled = self._enabled_adapters.get(ip, is_up)
            curr_speed = self.get_adapter_speed(ip)
            ewma = self._ewma_speeds.get(ip, 0.0)
            # Decay EWMA smoothly if adapter has gone completely idle
            if curr_speed == 0.0 and ewma > 0.0:
                ewma = ewma * 0.85 if ewma > 1024 else 0.0
                self._ewma_speeds[ip] = ewma
            conns = self._active_connections.get(ip, 0)

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
                    max_concurrency_cap=self.default_per_adapter_cap,
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
        """O(1) amortized sliding window throughput tracker and EWMA updater."""
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

    def get_adapter_speed(self, ip_address: str, now: Optional[float] = None) -> float:
        """Calculates current rolling throughput (bytes/sec) in O(1) time."""
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
        active.sort(key=lambda a: self._ewma_speeds.get(a.ip_address, 0.0), reverse=True)

        for a in active:
            quota = quotas.get(a.ip_address, a.max_concurrency_cap)
            if self._active_connections.get(a.ip_address, 0) < quota:
                return a

        under_cap = [a for a in active if self._active_connections.get(a.ip_address, 0) < a.max_concurrency_cap]
        if under_cap:
            under_cap.sort(key=lambda a: self._active_connections.get(a.ip_address, 0))
            return under_cap[0]
        return None

    def allocate_segments_to_adapters(
        self, segment_count: int, strategy: str = "ewma"
    ) -> List[Optional[NetworkAdapter]]:
        active = [a for a in self.get_available_adapters() if a.is_enabled and a.is_up]
        if not active:
            return [None] * segment_count

        if len(active) == 1 or strategy == "primary":
            return [active[0]] * segment_count

        if strategy == "equal":
            return [active[i % len(active)] for i in range(segment_count)]

        total_ewma = sum(max(1024.0, a.ewma_speed_bytes) for a in active)
        quotas: Dict[str, int] = {}
        assigned_total = 0
        for a in active:
            share = max(1024.0, a.ewma_speed_bytes) / total_ewma
            count = int(round(segment_count * share))
            quotas[a.ip_address] = count
            assigned_total += count

        diff = segment_count - assigned_total
        if diff != 0:
            fastest = max(active, key=lambda a: a.ewma_speed_bytes)
            quotas[fastest.ip_address] = max(1, quotas.get(fastest.ip_address, 0) + diff)

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