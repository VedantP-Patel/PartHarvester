"""Unit Tests for Multi-Network Adapter Manager."""

import time
from unittest.mock import patch
from engine.network import NetworkAdapter, NetworkManager


def test_network_manager_enumeration():
    manager = NetworkManager()
    adapters = manager.get_available_adapters()
    assert isinstance(adapters, list)
    # Ensure all returned adapters have an IPv4 address
    for a in adapters:
        assert "." in a.ip_address
        assert not a.ip_address.startswith("127.")


def test_network_manager_round_robin_and_transfer_speed():
    manager = NetworkManager()
    next_adapter = manager.get_next_adapter()
    # If there are adapters available, test transfer recording
    if next_adapter:
        ip = next_adapter.ip_address
        manager.record_chunk_transfer(ip, 1024 * 1024)  # 1 MB
        speed = manager.get_adapter_speed(ip)
        assert speed > 0


def test_network_manager_ewma_and_proportional_quotas():
    manager = NetworkManager(ewma_alpha=0.5)

    fake_adapters = [
        NetworkAdapter(name="Wi-Fi", ip_address="192.168.1.50", friendly_type="Wi-Fi", is_up=True, is_enabled=True),
        NetworkAdapter(name="USB Phone 5G", ip_address="192.168.42.10", friendly_type="USB Tethering", is_up=True, is_enabled=True),
    ]

    with patch.object(manager, "get_available_adapters", return_value=fake_adapters):
        # Simulate 10 MB/s for USB Phone and 1 MB/s for Wi-Fi
        manager._ewma_speeds["192.168.42.10"] = 10 * 1024 * 1024
        manager._ewma_speeds["192.168.1.50"] = 1 * 1024 * 1024

        quotas = manager.calculate_adaptive_quotas(total_slots=4)
        assert "192.168.42.10" in quotas
        assert "192.168.1.50" in quotas
        # Fast adapter should get max cap (3) or higher quota than slow adapter
        assert quotas["192.168.42.10"] >= quotas["192.168.1.50"]
        assert quotas["192.168.42.10"] <= 3  # clamped to default max_concurrency_cap


def test_network_manager_connection_saturation_and_fallback():
    manager = NetworkManager()

    fake_adapters = [
        NetworkAdapter(name="Adapter Fast", ip_address="10.0.0.1", friendly_type="Ethernet", is_up=True, is_enabled=True, max_concurrency_cap=2),
        NetworkAdapter(name="Adapter Slow", ip_address="10.0.0.2", friendly_type="Wi-Fi", is_up=True, is_enabled=True, max_concurrency_cap=2),
    ]

    with patch.object(manager, "get_available_adapters", return_value=fake_adapters):
        manager._ewma_speeds["10.0.0.1"] = 20 * 1024 * 1024
        manager._ewma_speeds["10.0.0.2"] = 2 * 1024 * 1024

        # Initially, fast adapter should be selected
        selected = manager.get_best_available_adapter(total_target_concurrency=4)
        assert selected is not None
        assert selected.ip_address == "10.0.0.1"

        # Saturate fast adapter
        manager.increment_connections("10.0.0.1")
        manager.increment_connections("10.0.0.1")

        # Now fast adapter is at capacity, best available should fall back to slow adapter
        selected2 = manager.get_best_available_adapter(total_target_concurrency=4)
        assert selected2 is not None
        assert selected2.ip_address == "10.0.0.2"

        # Decrement connection on fast adapter
        manager.decrement_connections("10.0.0.1")
        selected3 = manager.get_best_available_adapter(total_target_concurrency=4)
        assert selected3.ip_address == "10.0.0.1"


