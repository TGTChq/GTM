"""pytest plugin: make an outbound network call impossible during the suite.

    python -m pytest tests -p ci_no_network

The suite is designed to be hermetic -- every service client routes HTTP through
one ``request_with_retry`` seam that offline modes replace -- but "designed to be"
and "cannot" are different guarantees, and only the second one is a gate. A test
that quietly reaches a provider passes locally on a developer's credentials and
then fails, or worse spends a credit, in CI.

So this refuses the connection at the socket layer. Loopback stays open, because a
test that binds a local port is not a network call in the sense that matters here.
"""

from __future__ import annotations

import socket

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


class NetworkUseInTests(RuntimeError):
    """A test tried to open a socket to something other than loopback."""


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def pytest_configure(config) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def _guard(address):
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise NetworkUseInTests(
                f"the offline suite attempted a network connection to {host!r}. "
                "Route the call through the request_with_retry seam and fake it, "
                "or mark the test as requiring network and run it outside CI."
            )

    def connect(self, address):
        _guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _guard(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        _guard(address)
        return real_create(address, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    config._tgtc_restore_socket = (real_connect, real_connect_ex, real_create)


def pytest_unconfigure(config) -> None:
    restore = getattr(config, "_tgtc_restore_socket", None)
    if restore:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = restore
