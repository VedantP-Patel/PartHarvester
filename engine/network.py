"""Multi-Network Adapter Manager for Combinational Net.

Features:
- Dynamic discovery of physical & tethered network interfaces (Wi-Fi, Ethernet, USB 5G Tethering).
- Exponentially Weighted Moving Average (EWMA) throughput tracking per adapter.
- Proportional adaptive slot allocation: shifts download concurrency dynamically to faster interfaces.
- Anti-429 per-interface connection ceiling (e.g. max 3 streams per unique public IP).
"""

import socket
import time
from typing import Dict, List, Optional
import psutil
from pydantic import BaseModel, Field


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

    def __init__(self, ewma_alpha: float = 0.35, default_per_adapter_cap: int = 3):
        self._enabled_adapters: Dict[str, bool] = {}
        self._adapter_bytes_history: Dict[str, List[tuple]] = {}
        self._ewma_speeds: Dict[str, float] = {}
        self._active_connections: Dict[str, int] = {}
        self._ewma_alpha = ewma_alpha
        self.default_per_adapter_cap = default_per_adapter_cap

    def get_available_adapters(self) -> List[NetworkAdapter]:
        """Scans the system for active IPv4 network interfaces suitable for bonding.

        Filters out:
        - Loopback (127.0.0.1)
        - Disconnected cables / down interfaces
        - Hyper-V / WSL internal virtual bridges (172.17.x.x)
        """
        adapters = []
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()

        for iface_name, addr_list in addrs.items():
            iface_stat = stats.get(iface_name)
            is_up = iface_stat.isup if iface_stat else True
            speed_mbps = iface_stat.speed if iface_stat else 0

            # Ignore virtual WSL / Hyper-V internal bridges
            if "wsl" in iface_name.lower() or "vethernet" in iface_name.lower():
                continue

            for addr in addr_list:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    if ip.startswith("172.17.") or ip.startswith("172.18."):
                        continue

                    lower_name = iface_name.lower()
                    if "wi-fi" in lower_name or "wireless" in lower_name or "wlan" in lower_name:
                        friendly = "Wi-Fi"
                    elif "tether" in lower_name or "rndis" in lower_name or "remote ndis" in lower_name:
                        friendly = "USB Tethering"
                    else:
                        friendly = "Ethernet / LAN"

                    is_enabled = self._enabled_adapters.get(ip, is_up)
                    curr_speed = self.get_adapter_speed(ip)
                    ewma = self._ewma_speeds.get(ip, 0.0)
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

        # Calculate relative bandwidth share percentages
        total_ewma = sum(a.ewma_speed_bytes for a in adapters if a.is_enabled and a.is_up)
        for a in adapters:
            if total_ewma > 0 and a.is_enabled and a.is_up:
                a.bandwidth_share_pct = round((a.ewma_speed_bytes / total_ewma) * 100.0, 1)
            else:
                a.bandwidth_share_pct = 0.0

        return adapters

    def set_adapter_enabled(self, ip_address: str, enabled: bool) -> None:
        """Enables or disables an adapter for downloading."""
        self._enabled_adapters[ip_address] = enabled

    def get_adapter_by_ip(self, ip_address: str) -> Optional[NetworkAdapter]:
        """Finds an adapter by its IP address."""
        for a in self.get_available_adapters():
            if a.ip_address == ip_address:
                return a
        return None

    def record_chunk_transfer(self, ip_address: str, bytes_count: int) -> None:
        """Records transferred bytes and updates the EWMA speed model."""
        now = time.time()
        if ip_address not in self._adapter_bytes_history:
            self._adapter_bytes_history[ip_address] = []
        self._adapter_bytes_history[ip_address].append((now, bytes_count))

        # Keep rolling 8-second window for stable throughput tracking
        self._adapter_bytes_history[ip_address] = [
            (t, b) for t, b in self._adapter_bytes_history[ip_address] if now - t <= 8.0
        ]

        # Update instant and EWMA speed
        instant_speed = self.get_adapter_speed(ip_address)
        prev_ewma = self._ewma_speeds.get(ip_address, instant_speed)
        new_ewma = self._ewma_alpha * instant_speed + (1.0 - self._ewma_alpha) * prev_ewma
        self._ewma_speeds[ip_address] = new_ewma

    def get_adapter_speed(self, ip_address: str) -> float:
        """Calculates current rolling throughput (bytes/sec) for a given interface IP."""
        history = self._adapter_bytes_history.get(ip_address, [])
        if not history:
            return 0.0

        total_bytes = sum(b for _, b in history)
        now = time.time()
        earliest = min(t for t, _ in history)
        duration = max(0.5, now - earliest)
        return total_bytes / duration

    def increment_connections(self, ip_address: Optional[str]) -> None:
        """Registers a new active stream connection on this adapter."""
        if ip_address:
            self._active_connections[ip_address] = self._active_connections.get(ip_address, 0) + 1

    def decrement_connections(self, ip_address: Optional[str]) -> None:
        """Decrements active stream count when a chunk or file completes."""
        if ip_address and ip_address in self._active_connections:
            self._active_connections[ip_address] = max(0, self._active_connections[ip_address] - 1)

    def calculate_adaptive_quotas(self, total_slots: int) -> Dict[str, int]:
        """Calculates proportional concurrency quotas for each active adapter.

        Fast adapters are awarded more slots; slow adapters receive fewer slots.
        No adapter exceeds max_concurrency_cap to avoid 429 rate limits.
        """
        active_adapters = [a for a in self.get_available_adapters() if a.is_enabled and a.is_up]
        if not active_adapters:
            return {}

        total_ewma = sum(max(1024, a.ewma_speed_bytes) for a in active_adapters)
        quotas = {}

        for a in active_adapters:
            speed = max(1024, a.ewma_speed_bytes)
            # Proportional share
            raw_quota = int(round(total_slots * (speed / total_ewma)))
            # Clamp between 1 and max_concurrency_cap
            clamped = max(1, min(a.max_concurrency_cap, raw_quota))
            quotas[a.ip_address] = clamped

        return quotas

    def get_best_available_adapter(self, total_target_concurrency: int = 6) -> Optional[NetworkAdapter]:
        """Selects the best adapter with available capacity under its adaptive quota.

        Sorts by highest EWMA speed first to keep fast pipes 100% saturated.
        """
        active = [a for a in self.get_available_adapters() if a.is_enabled and a.is_up]
        if not active:
            return None

        quotas = self.calculate_adaptive_quotas(total_target_concurrency)

        # Sort adapters by speed descending (fastest first)
        active.sort(key=lambda a: self._ewma_speeds.get(a.ip_address, 0.0), reverse=True)

        for a in active:
            quota = quotas.get(a.ip_address, a.max_concurrency_cap)
            active_conns = self._active_connections.get(a.ip_address, 0)
            if active_conns < quota:
                return a

        # Fallback: if all quotas are reached, return adapter with lowest connection count under cap
        under_cap = [a for a in active if self._active_connections.get(a.ip_address, 0) < a.max_concurrency_cap]
        if under_cap:
            under_cap.sort(key=lambda a: self._active_connections.get(a.ip_address, 0))
            return under_cap[0]

        return None

    def allocate_segments_to_adapters(
        self,
        segment_count: int,
        strategy: str = "ewma",
    ) -> List[Optional[NetworkAdapter]]:
        """Maps a sequence of segments to active adapters using EWMA throughput weighting or round-robin."""
        active = [a for a in self.get_available_adapters() if a.is_enabled and a.is_up]
        if not active:
            return [None] * segment_count
        if len(active) == 1 or strategy == "primary":
            return [active[0]] * segment_count
        if strategy == "equal":
            return [active[i % len(active)] for i in range(segment_count)]

        # EWMA proportional allocation
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
            result.extend([adapter_map[ip]] * count)

        while len(result) < segment_count:
            result.append(active[0])
        return result[:segment_count]

    def get_next_adapter(self) -> Optional[NetworkAdapter]:
        """Backward-compatible alias for get_best_available_adapter."""
        return self.get_best_available_adapter()
