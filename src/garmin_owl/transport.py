"""Connect to Garmin over IPv4 first.

Garmin publishes several IPv6 addresses and the system lists them first. urllib3 tries addresses
one after another, each with the full timeout, so on a network whose IPv6 silently drops packets
every new connection stalls for over a minute before reaching IPv4. IPv6 stays the fallback.
"""

from __future__ import annotations

import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util.connection import create_connection


class _IPv4FirstConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        # Only the socket's address order changes: TLS still verifies the host name.
        host = self._dns_host.strip("[]")
        try:
            found = socket.getaddrinfo(host, self.port, 0, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        error: OSError = OSError("getaddrinfo returned no addresses")
        for item in sorted(found, key=lambda item: item[0] != socket.AF_INET):
            try:
                return create_connection(
                    (str(item[4][0]), self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
            except OSError as exc:
                error = exc
        if isinstance(error, TimeoutError):
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out.") from error
        raise NewConnectionError(self, f"Failed to establish a new connection: {error}") from error


class _IPv4FirstPool(HTTPSConnectionPool):
    ConnectionCls = _IPv4FirstConnection


class IPv4FirstAdapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        # A per-manager copy, so no other code in the process is affected.
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "https": _IPv4FirstPool,
        }


def prefer_ipv4(session: requests.Session) -> None:
    session.mount("https://", IPv4FirstAdapter(pool_connections=20, pool_maxsize=20))
