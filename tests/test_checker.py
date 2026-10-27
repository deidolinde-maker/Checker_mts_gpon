from pathlib import Path

import pytest

from checker import RunReport, VersionDetectionError, choose_proxy, classify_navigation_error, detect_variant, format_russian_notification, is_allowed_url, load_urls, normalize_proxy
from notify import split_message


def test_url_policy_and_deduplication(tmp_path: Path):
    source = tmp_path / "urls.txt"
    source.write_text("# comment\n\nhttps://mts-home-gpon.ru/a\nhttps://mts-home-gpon.ru/a\nhttps://mts-home-gpon.ru/b\n", encoding="utf-8")
    assert load_urls(source) == ["https://mts-home-gpon.ru/a", "https://mts-home-gpon.ru/b"]


@pytest.mark.parametrize("url", [
    "http://mts-home-gpon.ru/",
    "https://other.example/",
    "https://mts-home-gpon.ru.evil.example/",
])
def test_rejects_invalid_url(url: str):
    assert not is_allowed_url(url)


def test_rejects_invalid_file(tmp_path: Path):
    source = tmp_path / "urls.txt"
    source.write_text("https://example.com/\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be HTTPS"):
        load_urls(source)


class FakeResponse:
    def __init__(self, status: int):
        self.status = status


class FakePage:
    def __init__(self, value, status=200, error=None):
        self.value = value
        self.status = status
        self.error = error

    def goto(self, url, wait_until):
        if self.error:
            raise self.error
        return FakeResponse(self.status)

    def evaluate(self, script, key):
        return self.value


@pytest.mark.parametrize("value, expected", [(True, ("B", True)), (False, ("A", False))])
def test_variant_selection(value, expected):
    assert detect_variant(FakePage(value)) == expected


def test_localstorage_error_is_not_variant_a():
    with pytest.raises(VersionDetectionError, match="localStorage/navigation error"):
        detect_variant(FakePage(None, error=RuntimeError("security restriction")))


def test_detection_http_error_is_not_variant_a():
    with pytest.raises(VersionDetectionError, match="HTTP 404"):
        detect_variant(FakePage(None, status=404))


@pytest.mark.parametrize("message, expected", [
    ("net::ERR_NAME_NOT_RESOLVED", "dns_error"),
    ("net::ERR_CONNECTION_REFUSED", "connection_refused"),
    ("net::ERR_CONNECTION_RESET", "connection_reset"),
    ("net::ERR_TIMED_OUT", "timeout"),
    ("net::ERR_CERT_AUTHORITY_INVALID", "ssl_tls_error"),
    ("proxy connection failed", "proxy_error"),
])
def test_classifies_navigation_errors(message, expected):
    assert classify_navigation_error(message) == expected


def test_proxy_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PROXIES_ENABLED", "false")
    monkeypatch.setenv("PROXY_1_URL", "http://invalid-proxy-1")
    monkeypatch.setenv("PROXY_2_URL", "http://invalid-proxy-2")
    assert choose_proxy() == (None, "direct")


def test_direct_network_mode_ignores_proxy_values(monkeypatch):
    monkeypatch.setenv("NETWORK_MODE", "direct")
    monkeypatch.setenv("PROXY_1_URL", "invalid-proxy-value")
    assert choose_proxy() == (None, "direct")


def test_auto_prefers_direct_before_proxy(monkeypatch):
    monkeypatch.setenv("NETWORK_MODE", "auto")
    monkeypatch.setenv("PROXY_1_URL", "invalid-proxy-value")
    monkeypatch.setattr("checker.direct_preflight", lambda: True)
    assert choose_proxy() == (None, "direct")


def test_normalizes_host_port_user_password_proxy():
    proxy = normalize_proxy("185.183.160.117:8000:username:password")
    assert proxy.playwright_args() == {
        "server": "http://185.183.160.117:8000",
        "username": "username",
        "password": "password",
    }


def test_normalizes_url_proxy():
    proxy = normalize_proxy("http://username:password@185.183.160.117:8000")
    assert proxy.playwright_args()["server"] == "http://185.183.160.117:8000"
    assert proxy.urllib_url() == "http://username:password@185.183.160.117:8000"


def test_notification_message_is_split_into_safe_chunks():
    chunks = split_message("line\n" * 100, limit=40)
    assert chunks
    assert all(len(chunk) <= 40 for chunk in chunks)


def test_notification_groups_failed_urls_by_error_type():
    report = RunReport(
        run_id="2026-10-27T13:36:34+00:00",
        detected_variant="A",
        detection_url="https://mts-home-gpon.ru/",
        marker_found=False,
        source_file="urls_variant_a.txt",
        total=2,
        passed=0,
        failed=2,
        warnings=0,
        results=[
            {"url": "https://mts-home-gpon.ru/a", "status": "failed", "error_type": "http_404"},
            {"url": "https://mts-home-gpon.ru/b", "status": "failed", "error_type": "http_404"},
        ],
        status="FAILURE",
    )

    message = format_russian_notification(report)

    assert "❌ Ошибка проверки MTS GPON: http_404 (2)" in message
    assert "Страницы (2):" in message
    assert "- https://mts-home-gpon.ru/a" in message
    assert "- https://mts-home-gpon.ru/b" in message
    assert "📡 Ответ:" not in message


def test_notification_uses_counts_when_url_list_exceeds_telegram_limit():
    report = RunReport(
        run_id="2026-10-27T13:36:34+00:00",
        detected_variant="A",
        detection_url="https://mts-home-gpon.ru/",
        marker_found=False,
        source_file="urls_variant_a.txt",
        total=100,
        passed=0,
        failed=100,
        warnings=0,
        results=[
            {"url": f"https://mts-home-gpon.ru/{'x' * 80}{index}", "status": "failed", "error_type": "http_404"}
            for index in range(100)
        ],
        status="FAILURE",
    )

    message = format_russian_notification(report)

    assert len(message) <= 3500
    assert "❌ Ошибка проверки MTS GPON: http_404 (100)" in message
    assert "Страницы (100):" not in message
    assert "https://mts-home-gpon.ru/" not in message
