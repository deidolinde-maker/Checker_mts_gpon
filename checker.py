"""MTS GPON availability checker.

The checker deliberately keeps variant selection and URL validation independent
from navigation. This makes invalid configuration fail before a browser run.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from playwright.sync_api import Page, Response, sync_playwright

MARKER = "landing-yandex-events:v1"
ALLOWED_HOST = "mts-home-gpon.ru"
DETECTION_URL = "https://mts-home-gpon.ru/"
ERROR_HTTP_STATUSES = {400, 401, 403, 404, 408, 429, 500, 502, 503, 504}


class VersionDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxyConfig:
    server: str
    username: str | None = None
    password: str | None = None

    def playwright_args(self) -> dict[str, str]:
        args = {"server": self.server}
        if self.username is not None:
            args["username"] = self.username
        if self.password is not None:
            args["password"] = self.password
        return args

    def urllib_url(self) -> str:
        if self.username is None:
            return self.server
        parsed = urlsplit(self.server)
        auth = f"{quote(self.username, safe='')}:{quote(self.password or '', safe='')}@"
        return f"{parsed.scheme}://{auth}{parsed.hostname}:{parsed.port}"


@dataclass
class UrlResult:
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    proxy: str | None = None
    duration_seconds: float | None = None
    error_type: str | None = None
    message: str | None = None
    redirect_chain: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    run_id: str
    detected_variant: str | None
    detection_url: str
    marker_found: bool | None
    source_file: str | None
    total: int
    passed: int
    failed: int
    warnings: int
    results: list[dict[str, Any]]
    status: str
    detection_error: str | None = None
    error_type: str | None = None


def is_allowed_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == ALLOWED_HOST


def load_urls(path: Path) -> list[str]:
    """Load, validate, de-duplicate URLs while preserving source order."""
    urls: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if not is_allowed_url(value):
            raise ValueError(f"{path}:{line_no}: URL must be HTTPS on {ALLOWED_HOST}: {value}")
        if value not in seen:
            seen.add(value)
            urls.append(value)
    if not urls:
        raise ValueError(f"{path}: no URLs configured")
    return urls


def detect_variant(page: Page, detection_url: str = DETECTION_URL) -> tuple[str, bool]:
    """Detect A/B without treating localStorage errors as variant A."""
    try:
        response = page.goto(detection_url, wait_until="load")
        if response is None:
            raise VersionDetectionError("detection navigation returned no HTTP response")
        if response.status in ERROR_HTTP_STATUSES or response.status >= 400:
            raise VersionDetectionError(f"detection URL returned HTTP {response.status}")
        value = page.evaluate("key => window.localStorage.getItem(key)", MARKER)
    except VersionDetectionError:
        raise
    except Exception as exc:
        raise VersionDetectionError(f"localStorage/navigation error: {exc}") from exc
    found = value is not None and str(value) != ""
    return ("B" if found else "A"), found


def proxy_specs() -> list[tuple[str, str | None]]:
    return [(name, os.getenv(name)) for name in ("PROXY_1_URL", "PROXY_2_URL") if os.getenv(name)]


def normalize_proxy(value: str) -> ProxyConfig:
    """Accept URL form or HOST:PORT:USERNAME:PASSWORD from Jenkins Secret Text."""
    raw = value.strip()
    if "://" not in raw:
        parts = raw.split(":", 3)
        if len(parts) != 4 or not parts[0] or not parts[1].isdigit():
            raise ValueError("proxy must be URL or HOST:PORT:USERNAME:PASSWORD")
        host, port, username, password = parts
        return ProxyConfig(f"http://{host}:{port}", username, password)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("proxy URL must contain http/https scheme, host and port")
    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None
    return ProxyConfig(f"{parsed.scheme}://{parsed.hostname}:{parsed.port}", username, password)


def proxy_preflight(proxy: ProxyConfig, timeout: float = 10.0) -> bool:
    """Check proxy connectivity before creating the single browser context."""
    try:
        proxy_url = proxy.urllib_url()
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        request = Request(DETECTION_URL, method="GET")
        with opener.open(request, timeout=timeout) as response:
            response.read(1)
            return True
    except Exception:
        return False


def choose_proxy() -> tuple[ProxyConfig | None, str | None]:
    if os.getenv("PROXIES_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None, None
    configured = proxy_specs()
    if not configured:
        return None, None
    for name, value in configured:
        assert value is not None
        proxy = normalize_proxy(value)
        if proxy_preflight(proxy):
            return proxy, name.replace("PROXY_", "").replace("_URL", "").lower()
    raise RuntimeError("оба прокси не сработали")


def redirect_chain(response: Response) -> list[str]:
    chain: list[str] = []
    request = response.request
    while request is not None:
        chain.append(request.url)
        request = request.redirected_from
    return list(reversed(chain))


def classify_navigation_error(message: str) -> str:
    text = message.lower()
    if "timeout" in text or "timed_out" in text:
        return "timeout"
    if "certificate" in text or "cert_" in text or "ssl" in text or "tls" in text:
        return "ssl_tls_error"
    if "proxy" in text:
        return "proxy_error"
    if "dns" in text or "enotfound" in text or "name_not_resolved" in text:
        return "dns_error"
    if "refused" in text:
        return "connection_refused"
    if "reset" in text:
        return "connection_reset"
    return "navigation_error"


def check_url(page: Page, url: str, proxy_name: str | None) -> UrlResult:
    started = time.monotonic()
    page_errors: list[str] = []
    failed_requests: list[str] = []
    on_page_error = lambda exc: page_errors.append(str(exc))
    on_request_failed = lambda req: failed_requests.append(req.failure or "request failed")
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    try:
        response = page.goto(url, wait_until="load")
        duration = round(time.monotonic() - started, 2)
        if response is None:
            return UrlResult(url, "failed", proxy=proxy_name, duration_seconds=duration,
                             error_type="navigation_error", message="navigation returned no response")
        final_url = page.url
        final_host = urlparse(final_url).hostname
        chain = redirect_chain(response)
        if final_host != ALLOWED_HOST:
            return UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                             "redirect_other_domain", f"final host is {final_host}", chain)
        if response.status in ERROR_HTTP_STATUSES or response.status >= 400:
            return UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                             f"http_{response.status}", response.status_text, chain)
        if failed_requests:
            return UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                             "network_error", "; ".join(failed_requests), chain)
        if page_errors:
            return UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                             "page_error", "; ".join(page_errors), chain)
        return UrlResult(url, "passed", response.status, final_url, proxy_name, duration, redirect_chain=chain)
    except Exception as exc:
        return UrlResult(url, "failed", proxy=proxy_name,
                         duration_seconds=round(time.monotonic() - started, 2),
                         error_type=classify_navigation_error(str(exc)), message=str(exc))
    finally:
        page.remove_listener("pageerror", on_page_error)
        page.remove_listener("requestfailed", on_request_failed)


def run(checker_root: Path) -> RunReport:
    run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    proxy: ProxyConfig | None = None
    proxy_name: str | None = None
    source_file: str | None = None
    detected_variant: str | None = None
    marker_found: bool | None = None
    results: list[UrlResult] = []
    try:
        proxy, proxy_name = choose_proxy()
        with sync_playwright() as playwright:
            launch_args: dict[str, Any] = {"headless": True}
            if proxy:
                # Proxy is selected before this single browser/context is created.
                launch_args["proxy"] = proxy.playwright_args()
            browser = playwright.chromium.launch(**launch_args)
            context = browser.new_context()
            page = context.new_page()
            try:
                detected_variant, marker_found = detect_variant(page)
                source_file = f"urls_variant_{detected_variant.lower()}.txt"
                urls = load_urls(checker_root / "config" / source_file)
                for url in urls:
                    results.append(check_url(page, url, proxy_name))
            finally:
                context.close()
                browser.close()
    except VersionDetectionError as exc:
        return RunReport(run_id, detected_variant, DETECTION_URL, marker_found, source_file, len(results),
                         sum(r.status == "passed" for r in results), sum(r.status == "failed" for r in results),
                         0, [asdict(r) for r in results], "FAILURE", str(exc), "VERSION_DETECTION_ERROR")
    except Exception as exc:
        error_type = "PROXY_ERROR" if "прокси" in str(exc).lower() else "CHECKER_ERROR"
        return RunReport(run_id, detected_variant, DETECTION_URL, marker_found, source_file, len(results),
                         sum(r.status == "passed" for r in results), sum(r.status == "failed" for r in results),
                         0, [asdict(r) for r in results], "FAILURE", str(exc), error_type)
    passed = sum(r.status == "passed" for r in results)
    failed = sum(r.status == "failed" for r in results)
    status = "SUCCESS" if failed == 0 else "FAILURE"
    return RunReport(run_id, variant, DETECTION_URL, marker_found, source_file, len(results), passed,
                     failed, 0, [asdict(r) for r in results], status)


def main() -> int:
    root = Path(__file__).resolve().parent
    report = run(root)
    output = Path(os.getenv("REPORT_FILE", "mts_gpon_report.json"))
    output.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    if report.status != "SUCCESS":
        print(format_russian_notification(report))
    return 0 if report.status == "SUCCESS" else 1


def format_russian_notification(report: RunReport) -> str:
    variant = report.detected_variant or "не определён"
    marker = "найдена" if report.marker_found is True else "не найдена" if report.marker_found is False else "не определена"
    lines = [
        "🌐 Проверка доступности MTS GPON", "", f"📌 Вариант: {variant}",
        f"🔑 Метка localStorage: {marker}", f"📄 Файл URL: {report.source_file or 'не выбран'}", "",
        f"🔗 Всего URL: {report.total}", f"✅ Успешно: {report.passed}",
        f"❌ Ошибки: {report.failed}", f"⚠️ Предупреждения: {report.warnings}",
    ]
    if report.detection_error:
        label = "Ошибка определения версии" if report.error_type == "VERSION_DETECTION_ERROR" else "Ошибка запуска"
        lines.extend(["", f"🧩 Тип ошибки: {report.error_type or 'CHECKER_ERROR'}",
                      f"💬 {label}: {report.detection_error}"])
    for item in report.results:
        if item.get("status") == "failed":
            lines.extend([
                "", "❌ Ошибка проверки MTS GPON", f"🔗 URL: {item.get('url')}",
                f"📡 Ответ: HTTP {item.get('http_status') or 'нет ответа'}",
                f"🧩 Тип: {item.get('error_type') or 'неизвестен'}",
                f"🛡️ Прокси: {item.get('proxy') or 'direct'}",
                f"⏱️ Время: {item.get('duration_seconds') or 0} сек.",
                f"🔄 Финальный URL: {item.get('final_url') or 'нет'}",
                f"💬 {item.get('message') or 'без сообщения'}",
            ])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
