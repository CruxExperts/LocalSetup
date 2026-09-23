"""Focused fixtures for advisory, anonymous GitHub certificate discovery."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace

import pytest

from ls.core.openpgp import discovery
from ls.core.openpgp.keys import KeyInspectionError, KeyInspectionErrorCode


EXPECTED = "A" * 40
OTHER = "B" * 40


def _record(entry_id: int, certificate: str, *, public_key: str = "ignored") -> dict:
    return {"id": entry_id, "raw_key": certificate, "public_key": public_key}


def _reply(records: object, *, status: int = 200, headers: dict | None = None):
    return discovery._HTTPReply(
        status=status,
        headers={} if headers is None else headers,
        body=json.dumps(records).encode("utf-8"),
    )


def _stub_inspector(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> list[str]:
    inspected: list[str] = []

    def inspect(*, certificate: str):
        inspected.append(certificate)
        return SimpleNamespace(primary_fingerprint=mapping[certificate])

    monkeypatch.setattr(discovery, "inspect_key", inspect)
    return inspected


def test_match_parses_raw_key_and_returns_immutable_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected = _stub_inspector(monkeypatch, {"armored cert": EXPECTED})
    paths: list[str] = []
    monkeypatch.setattr(
        discovery,
        "_http_get",
        lambda path: (paths.append(path) or _reply([_record(7, "armored cert")])),
    )

    result = discovery.discover_github_certificate(
        username="Octo-Cat", expected_fingerprint=EXPECTED.lower()
    )

    assert paths == ["/users/Octo-Cat/gpg_keys?per_page=100"]
    assert inspected == ["armored cert"]
    assert result.status is discovery.CertificateDiscoveryStatus.MATCH
    assert result.expected_fingerprint == EXPECTED
    assert result.discovered_fingerprints == (EXPECTED,)
    with pytest.raises(FrozenInstanceError):
        result.status = discovery.CertificateDiscoveryStatus.MISMATCH


def test_full_pin_mismatch_and_entry_id_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_inspector(monkeypatch, {"first cert": EXPECTED, "second cert": OTHER})
    monkeypatch.setattr(
        discovery,
        "_http_get",
        lambda _path: _reply([_record(7, "first cert"), _record(8, "second cert")]),
    )

    result = discovery.discover_github_certificate(
        username="octocat", expected_fingerprint=EXPECTED, api_entry_id=8
    )

    assert result.status is discovery.CertificateDiscoveryStatus.MISMATCH
    assert result.discovered_fingerprints == (EXPECTED, OTHER)


@pytest.mark.parametrize(
    "reply",
    [
        _reply([{"id": 7, "public_key": "not an armored certificate"}]),
        _reply([_record(7, "armored cert")], headers={"link": '<https://api.github.com/next?page=2>; rel="next"'}),
        _reply([_record(7, "armored cert")], status=302),
        discovery._HTTPReply(200, {}, b"{"),
    ],
)
def test_incomplete_or_malformed_source_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, reply: discovery._HTTPReply
) -> None:
    _stub_inspector(monkeypatch, {"armored cert": EXPECTED})
    monkeypatch.setattr(discovery, "_http_get", lambda _path: reply)

    result = discovery.discover_github_certificate(
        username="octocat", expected_fingerprint=EXPECTED
    )

    assert result.status is discovery.CertificateDiscoveryStatus.UNAVAILABLE
    assert result.expected_fingerprint == EXPECTED
    assert result.discovered_fingerprints == ()


def test_bad_certificate_or_duplicate_entries_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_certificate(*, certificate: str):
        del certificate
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)

    monkeypatch.setattr(discovery, "inspect_key", reject_certificate)
    monkeypatch.setattr(
        discovery,
        "_http_get",
        lambda _path: _reply([_record(7, "bad cert")]),
    )
    invalid = discovery.discover_github_certificate(
        username="octocat", expected_fingerprint=EXPECTED
    )
    assert invalid.status is discovery.CertificateDiscoveryStatus.UNAVAILABLE

    monkeypatch.setattr(
        discovery,
        "_http_get",
        lambda _path: _reply([_record(7, "bad cert"), _record(7, "bad cert")]),
    )
    duplicate = discovery.discover_github_certificate(
        username="octocat", expected_fingerprint=EXPECTED
    )
    assert duplicate.status is discovery.CertificateDiscoveryStatus.UNAVAILABLE


def test_invalid_configuration_does_not_make_a_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        discovery,
        "_http_get",
        lambda _path: pytest.fail("invalid configuration must not contact GitHub"),
    )
    result = discovery.discover_github_certificate(
        username="../other", expected_fingerprint="short", api_entry_id=True
    )
    assert result.status is discovery.CertificateDiscoveryStatus.UNAVAILABLE


@pytest.mark.parametrize("declared_length", [2, 3])
def test_http_transport_uses_fixed_anonymous_https_endpoint(
    monkeypatch: pytest.MonkeyPatch, declared_length: int,
) -> None:
    calls: dict[str, object] = {}

    class FakeSocket:
        def settimeout(self, timeout: float) -> None:
            calls["read_timeout"] = timeout

    class FakeResponse:
        status = 200
        read_once = False

        def getheader(self, name: str):
            assert name == "Content-Length"
            return str(declared_length)

        def getheaders(self):
            return [("Content-Type", "application/json")]

        def read1(self, size: int) -> bytes:
            calls["read_size"] = size
            if self.read_once:
                return b""
            self.read_once = True
            return b"[]"

    class FakeConnection:
        def __init__(self, host: str, *, port: int, timeout: float, context: object):
            calls.update(host=host, port=port, timeout=timeout, context=context)
            self.sock = FakeSocket()

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            calls.update(method=method, path=path, headers=headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(discovery.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(discovery.ssl, "create_default_context", lambda: "verified-context")

    if declared_length != 2:
        with pytest.raises(ValueError, match="incomplete response"):
            discovery._http_get("/users/octocat/gpg_keys?per_page=100")
        assert calls["closed"] is True
        return
    reply = discovery._http_get("/users/octocat/gpg_keys?per_page=100")

    assert calls["host"] == "api.github.com"
    assert calls["port"] == 443
    assert calls["method"] == "GET"
    assert calls["path"] == "/users/octocat/gpg_keys?per_page=100"
    assert "Authorization" not in calls["headers"]
    assert calls["headers"]["Accept-Encoding"] == "identity"
    assert calls["closed"] is True
    assert reply.status == 200
    assert reply.body == b"[]"


def test_full_page_without_completion_metadata_is_unavailable(monkeypatch):
    monkeypatch.setattr(discovery, "_http_get", lambda path: _reply([
        _record(i + 1, "certificate") for i in range(100)
    ]))
    monkeypatch.setattr(discovery, "inspect_key", lambda **kwargs: pytest.fail("incomplete page"))
    assert discovery.discover_github_certificate(username="example", expected_fingerprint=EXPECTED).status is discovery.CertificateDiscoveryStatus.UNAVAILABLE


def test_slow_inspection_exhausts_shared_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(discovery, "_http_get", lambda path: _reply([_record(1, "certificate")]))
    def inspect(**kwargs):
        clock[0] = 6.0
        return SimpleNamespace(primary_fingerprint=EXPECTED)
    monkeypatch.setattr(discovery, "inspect_key", inspect)
    result = discovery.discover_github_certificate(username="example", expected_fingerprint=EXPECTED)
    assert result.status is discovery.CertificateDiscoveryStatus.UNAVAILABLE
    assert result.discovered_fingerprints == ()
