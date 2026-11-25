from __future__ import annotations

import socket
from typing import Any

import pytest
import requests
from garminconnect import Garmin

import garmin_owl.transport as transport

V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))
V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the addresses tried; none of them answers."""
    tried: list[str] = []

    def refuse(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        tried.append(address[0])
        raise OSError("unreachable")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [V6, V4])
    monkeypatch.setattr(transport, "create_connection", refuse)
    return tried


def test_garmin_session_tries_ipv4_first_and_keeps_ipv6_as_fallback(attempts: list[str]) -> None:
    api = Garmin(retry_attempts=0)
    transport.prefer_ipv4(api.client._api_session)
    with pytest.raises(requests.ConnectionError):
        api.client._api_session.get("https://connectapi.garmin.com/", timeout=1)
    assert attempts == ["192.0.2.1", "2001:db8::1"]


def test_a_working_ipv4_address_is_used_without_touching_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[str] = []
    left, right = socket.socketpair()

    def connect(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        tried.append(address[0])
        return left

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [V6, V4])
    monkeypatch.setattr(transport, "create_connection", connect)
    connection = transport._IPv4FirstConnection("connectapi.garmin.com")
    assert connection._new_conn() is left
    assert connection.host == "connectapi.garmin.com"  # the name TLS verifies
    assert tried == ["192.0.2.1"]
    left.close()
    right.close()


def test_timeouts_and_dns_failures_stay_requests_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    session = requests.Session()
    transport.prefer_ipv4(session)

    def time_out(*args: Any, **kwargs: Any) -> socket.socket:
        raise TimeoutError

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [V6, V4])
    monkeypatch.setattr(transport, "create_connection", time_out)
    with pytest.raises(requests.ConnectTimeout):
        session.get("https://connectapi.garmin.com/", timeout=1)

    def no_dns(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(requests.ConnectionError):
        session.get("https://connectapi.garmin.com/", timeout=1)


def test_other_sessions_are_unaffected() -> None:
    transport.prefer_ipv4(requests.Session())
    adapter = requests.Session().get_adapter("https://connectapi.garmin.com/")
    assert not isinstance(adapter, transport.IPv4FirstAdapter)
