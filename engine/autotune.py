"""Closed-loop network and hoster auto-tuner with per-IP cooldowns for PartHarvester."""
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class AutoTuneRecommendation:
    max_concurrency: int
    segments_per_file: int
    buffer_size_kb: int
    per_adapter_max_conns: Dict[str, int]
    total_socket_budget: int
    profile_label: str
    reason: str


class SmartAutoTuner:
    BURST_MAX_SOCKETS_PER_IP = 8
    MIN_SOCKETS_PER_IP = 1

    def __init__(self):
        self.enabled: bool = True
        self._penalty_by_ip: Dict[str, int] = {}
        self._last_error_ts: Dict[str, float] = {}
        self._clean_completions: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self.last_recommendation: Optional[AutoTuneRecommendation] = None

    def register_cooldown(self, adapter_ip: Optional[str], seconds: float) -> None:
        if not adapter_ip or seconds <= 0:
            return
        now = time.monotonic()
        until = now + min(120.0, float(seconds))
        self._cooldown_until[adapter_ip] = max(self._cooldown_until.get(adapter_ip, 0.0), until)
        self._penalty_by_ip[adapter_ip] = 5
        self._last_error_ts[adapter_ip] = now

    def get_cooldown_remaining(self, adapter_ip: Optional[str]) -> float:
        if not adapter_ip:
            return 0.0
        return max(0.0, self._cooldown_until.get(adapter_ip, 0.0) - time.monotonic())

    def record_stream_error(self, adapter_ip: Optional[str], error_msg: str = "") -> None:
        if not adapter_ip:
            return
        msg = (error_msg or "").lower()
        is_socket_pressure = any(
            sig in msg
            for sig in ("56", "reset", "truncated", "429", "503", "timeout", "stalled", "closed", "200")
        )
        step = 2 if is_socket_pressure else 1
        cur_penalty = self._penalty_by_ip.get(adapter_ip, 0)
        self._penalty_by_ip[adapter_ip] = min(5, cur_penalty + step)
        self._last_error_ts[adapter_ip] = time.monotonic()
        self._clean_completions[adapter_ip] = 0

    def record_stream_success(self, adapter_ip: Optional[str]) -> None:
        if not adapter_ip:
            return
        self._clean_completions[adapter_ip] = self._clean_completions.get(adapter_ip, 0) + 1
        if self._clean_completions[adapter_ip] >= 3 and self._penalty_by_ip.get(adapter_ip, 0) > 0:
            self._penalty_by_ip[adapter_ip] = max(0, self._penalty_by_ip[adapter_ip] - 1)
            self._clean_completions[adapter_ip] = 0

    @staticmethod
    def _adapter_speed_mbps(adapter: dict) -> float:
        ewma = float(adapter.get("ewma_speed_bytes") or 0.0)
        cur = float(adapter.get("current_speed_bytes") or 0.0)
        best_bps = ewma if ewma > 64 * 1024 else max(ewma, cur)
        return best_bps / (1024.0 * 1024.0)

    def _safe_sockets_for_adapter(self, adapter: dict, num_active_adapters: int) -> int:
        ip = adapter.get("ip_address", "")
        if self.get_cooldown_remaining(ip) > 0:
            return self.MIN_SOCKETS_PER_IP

        speed_mbps = self._adapter_speed_mbps(adapter)
        if speed_mbps <= 0.1:
            target = 4 if num_active_adapters == 1 else 3
        elif speed_mbps < 2.0:
            target = 2
        elif speed_mbps < 8.0:
            target = 4 if num_active_adapters == 1 else 3
        elif speed_mbps < 20.0:
            target = 6 if num_active_adapters == 1 else 4
        elif speed_mbps < 45.0:
            target = 7 if num_active_adapters == 1 else 5
        else:
            target = self.BURST_MAX_SOCKETS_PER_IP

        now = time.monotonic()
        penalty = self._penalty_by_ip.get(ip, 0)
        last_err = self._last_error_ts.get(ip, 0.0)
        if penalty > 0 and (now - last_err) > 20.0:
            penalty = max(0, penalty - 1)
            self._penalty_by_ip[ip] = penalty
            self._last_error_ts[ip] = now

        return max(self.MIN_SOCKETS_PER_IP, target - penalty)

    def evaluate(self, adapters: List[dict]) -> AutoTuneRecommendation:
        active = [a for a in (adapters or []) if a.get("is_enabled") and a.get("is_up")]

        if not active:
            rec = AutoTuneRecommendation(
                max_concurrency=1,
                segments_per_file=1,
                buffer_size_kb=256,
                per_adapter_max_conns={},
                total_socket_budget=1,
                profile_label="Standby (No Active Adapter)",
                reason="Waiting for an active network interface.",
            )
            self.last_recommendation = rec
            return rec

        per_adapter_caps: Dict[str, int] = {}
        total_sockets = 0
        total_speed_mbps = 0.0

        for ad in active:
            ip = ad.get("ip_address", "")
            cap = self._safe_sockets_for_adapter(ad, len(active))
            per_adapter_caps[ip] = cap
            total_sockets += cap
            total_speed_mbps += self._adapter_speed_mbps(ad)

        if total_sockets <= 2:
            concurrency, segments = 1, max(1, total_sockets)
        elif total_sockets <= 4:
            concurrency, segments = 2, 2
        elif total_sockets <= 6:
            concurrency, segments = 2, 3
        elif total_sockets <= 9:
            concurrency, segments = 3, 3
        else:
            concurrency = min(6, max(3, total_sockets // 3))
            segments = min(6, max(2, total_sockets // concurrency))

        if total_speed_mbps >= 25.0:
            buffer_kb = 1024
        elif total_speed_mbps >= 8.0:
            buffer_kb = 512
        else:
            buffer_kb = 256

        has_penalties = any(v > 0 for v in self._penalty_by_ip.values())
        net_desc = f"{len(active)} net{'s' if len(active) > 1 else ''}"
        speed_desc = f"{total_speed_mbps:.1f} MB/s" if total_speed_mbps > 0.1 else "probing"
        guard_tag = " · Reset-Guard" if has_penalties else ""

        rec = AutoTuneRecommendation(
            max_concurrency=concurrency,
            segments_per_file=segments,
            buffer_size_kb=buffer_kb,
            per_adapter_max_conns=per_adapter_caps,
            total_socket_budget=total_sockets,
            profile_label=f"Auto: {concurrency} files × {segments} segs ({net_desc}, {speed_desc}){guard_tag}",
            reason=(
                f"Allocated {total_sockets} safe sockets across {net_desc} "
                f"to maximize speed without triggering host resets."
            ),
        )
        self.last_recommendation = rec
        return rec