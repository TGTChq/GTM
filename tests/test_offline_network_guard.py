"""No provider calls: exercise the offline gate before DNS or socket transport."""
import socket
import sys

import pytest
import ci_no_network


def test_external_dns_is_refused_before_resolution():
    assert ci_no_network._AUDIT_ACTIVE
    with pytest.raises(ci_no_network.NetworkUseInTests, match="socket.getaddrinfo"):
        socket.getaddrinfo("offline-test.invalid", 443)


def test_a_socket_function_replacement_cannot_remove_the_audit_guard():
    with pytest.raises(ci_no_network.NetworkUseInTests, match="socket.connect"):
        sys.audit("socket.connect", None, ("192.0.2.1", 443))
