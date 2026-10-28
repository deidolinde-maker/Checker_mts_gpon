"""MTS GPON availability checker.

The checker deliberately keeps variant selection and URL validation independent
from navigation. This makes invalid configuration fail before a browser run.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

MARKER = "landing-yandex-events:v1"
ALLOWED_HOST = "mts-home-gpon.ru"
DETECTION_URL = "https://mts-home-gpon.ru/"
ERROR_HTTP_STATUSES = {400, 401, 403, 404, 408, 429, 500, 502, 503, 504}
logger = logging.getLogger("mts_gpon_checker")


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
    warnings: list[str] = field(default_factory=list)


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
    skipped: int = 0


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
    """Detect A/B by localStorage key presence, matching Everyday_test."""
    try:
        logger.info("[DETECTION 1/3] Открытие detection URL: %s", detection_url)
        response = page.goto(detection_url, wait_until="load")
        if response is None:
            raise VersionDetectionError("detection navigation returned no HTTP response")
        if response.status in ERROR_HTTP_STATUSES or response.status >= 400:
            raise VersionDetectionError(f"detection URL returned HTTP {response.status}")
        logger.info("[DETECTION 2/3] Страница загружена: HTTP %s, final_url=%s",
                    response.status, getattr(page, "url", detection_url))
        # The landing sets the marker asynchronously; match Everyday_test timing.
        page.wait_for_timeout(500)
        logger.info("[DETECTION 3/3] Чтение localStorage key=%s", MARKER)
        marker_present = page.evaluate(
            "key => window.localStorage.getItem(key) !== null",
            MARKER,
        )
    except VersionDetectionError:
        raise
    except Exception as exc:
        raise VersionDetectionError(f"localStorage/navigation error: {exc}") from exc
    found = bool(marker_present)
    logger.info("[DETECTION] localStorage marker_present=%s", found)
    logger.info("[DETECTION] marker=%s; выбран вариант %s", "найдена" if found else "не найдена", "B" if found else "A")
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
    except Exception as exc:
        logger.warning("proxy preflight failed: %s", type(exc).__name__)
        return False


def direct_preflight(timeout: float = 10.0) -> bool:
    """Check the primary direct route without inheriting system proxy settings."""
    try:
        opener = build_opener(ProxyHandler({}))
        request = Request(DETECTION_URL, method="GET")
        with opener.open(request, timeout=timeout) as response:
            response.read(1)
            return True
    except Exception as exc:
        logger.warning("direct preflight failed: %s", type(exc).__name__)
        return False


def choose_proxy() -> tuple[ProxyConfig | None, str | None]:
    mode = os.getenv("NETWORK_MODE", "").strip().lower()
    if not mode:
        mode = "proxy" if os.getenv("PROXIES_ENABLED", "true").lower() in {"1", "true", "yes"} else "direct"
    if mode == "direct":
        logger.info("[NETWORK] Режим direct: proxy не используется")
        return None, "direct"
    if mode == "proxy":
        logger.warning("NETWORK_MODE=proxy устарел; используется auto: direct → proxy")
        mode = "auto"
    if mode not in {"auto"}:
        raise ValueError("NETWORK_MODE must be 'auto', 'direct' or 'proxy'")
    if mode == "auto":
        logger.info("[NETWORK 1/3] Основной маршрут: direct")
        if direct_preflight():
            logger.info("[NETWORK] Direct доступен; proxy не используется")
            return None, "direct"
        logger.warning("[NETWORK] Direct недоступен; fallback на proxy №1")
    configured = proxy_specs()
    if not configured:
        raise RuntimeError("direct недоступен, proxy credentials не заданы")
    for name, value in configured:
        assert value is not None
        proxy_number = name.replace("PROXY_", "").replace("_URL", "")
        logger.info("[NETWORK] Проверка proxy №%s", proxy_number)
        proxy = normalize_proxy(value)
        if proxy_preflight(proxy):
            logger.info("[NETWORK] Proxy №%s доступен; он будет использован для browser context", proxy_number)
            return proxy, name.replace("PROXY_", "").replace("_URL", "").lower()
        logger.warning("[NETWORK] Proxy №%s недоступен; переход к следующему proxy", proxy_number)
    raise RuntimeError("оба прокси не сработали")


def configured_proxy_candidates() -> list[tuple[ProxyConfig, str]]:
    """Return proxy candidates without selecting one for the whole run."""
    mode = os.getenv("NETWORK_MODE", "auto").strip().lower()
    if mode == "direct":
        return []
    candidates: list[tuple[ProxyConfig, str]] = []
    for name, value in proxy_specs():
        assert value is not None
        number = name.replace("PROXY_", "").replace("_URL", "").lower()
        candidates.append((normalize_proxy(value), number))
    return candidates


def is_retryable_network_failure(result: UrlResult) -> bool:
    return result.status == "failed" and result.error_type in {
        "network_error", "navigation_error", "timeout", "dns_error",
        "connection_refused", "connection_reset", "proxy_error", "ssl_tls_error",
    }


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
    logger.info("[URL] Проверка начата: %s", url)
    failed_requests: list[str] = []
    def on_page_error(exc):
        # JavaScript errors are outside the scope of this availability checker.
        logger.debug("[URL] JS error ignored for availability: %s", exc)
    def on_request_failed(req):
        # Aborted subresources are common during normal page loading.
        if req.is_navigation_request():
            failed_requests.append(req.failure or "request failed")

    def finish(result: UrlResult) -> UrlResult:
        if result.status == "passed":
            logger.info("[URL] PASS HTTP=%s final_url=%s redirects=%s duration=%.2fs",
                        result.http_status, result.final_url, max(0, len(result.redirect_chain) - 1),
                        result.duration_seconds or 0)
        else:
            logger.error("[URL] FAIL type=%s http=%s final_url=%s message=%s duration=%.2fs",
                         result.error_type, result.http_status or "-", result.final_url or "-",
                         result.message or "-", result.duration_seconds or 0)
        if result.warnings:
            logger.warning("[URL] WARN count=%s details=%s", len(result.warnings), "; ".join(result.warnings))
        return result

    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    try:
        response = page.goto(url, wait_until="load")
        duration = round(time.monotonic() - started, 2)
        if response is None:
            return finish(UrlResult(url, "failed", proxy=proxy_name, duration_seconds=duration,
                                    error_type="navigation_error", message="navigation returned no response"))
        final_url = page.url
        final_host = urlparse(final_url).hostname
        chain = redirect_chain(response)
        if final_host != ALLOWED_HOST:
            return finish(UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                                    "redirect_other_domain", f"final host is {final_host}", chain))
        if response.status in ERROR_HTTP_STATUSES or response.status >= 400:
            return finish(UrlResult(url, "failed", response.status, final_url, proxy_name, duration,
                                    f"http_{response.status}", response.status_text, chain))
        warnings = failed_requests
        return finish(UrlResult(url, "passed", response.status, final_url, proxy_name, duration,
                                redirect_chain=chain, warnings=warnings))
    except Exception as exc:
        return finish(UrlResult(url, "failed", proxy=proxy_name,
                                duration_seconds=round(time.monotonic() - started, 2),
                                error_type=classify_navigation_error(str(exc)), message=str(exc)))
    finally:
        page.remove_listener("pageerror", on_page_error)
        page.remove_listener("requestfailed", on_request_failed)


def run(checker_root: Path) -> RunReport:
    run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_file: str | None = None
    detected_variant: str | None = None
    marker_found: bool | None = None
    results: list[UrlResult] = []
    skipped = 0
    context: BrowserContext | None = None
    browser = None
    try:
        logger.info("[RUN] Старт проверки; detection_url=%s", DETECTION_URL)
        mode = os.getenv("NETWORK_MODE", "auto").strip().lower() or "auto"
        proxy_candidates = configured_proxy_candidates()
        logger.info("[NETWORK] Per-URL режим: direct first; proxy fallback candidates=%s", len(proxy_candidates))
        logger.info("[BROWSER 1/4] Запуск Chromium")
        with sync_playwright() as playwright:
            launch_args: dict[str, Any] = {"headless": True}
            browser = playwright.chromium.launch(**launch_args)
            logger.info("[BROWSER 2/4] Browser запущен")
            context = browser.new_context()
            logger.info("[BROWSER 3/4] Создан один browser context; cookies/localStorage сохраняются")
            page = context.new_page()
            try:
                detection_used_proxy = False
                try:
                    detected_variant, marker_found = detect_variant(page)
                except VersionDetectionError:
                    if mode == "direct":
                        raise
                    logger.warning("[DETECTION] Direct detection не выполнен; пробуем proxy context")
                    direct_state = context.storage_state()
                    context.close()
                    context = None
                    detection_succeeded = False
                    for proxy, proxy_name in proxy_candidates:
                        logger.info("[DETECTION] Preflight proxy %s", proxy_name)
                        if not proxy_preflight(proxy):
                            continue
                        proxy_context = browser.new_context(proxy=proxy.playwright_args(), storage_state=direct_state)
                        proxy_page = proxy_context.new_page()
                        try:
                            detected_variant, marker_found = detect_variant(proxy_page)
                            detection_succeeded = True
                            detection_used_proxy = True
                            context = proxy_context
                            page = proxy_page
                            logger.info("[DETECTION] Успешно через proxy %s; вариант %s", proxy_name, detected_variant)
                            break
                        except VersionDetectionError:
                            logger.warning("[DETECTION] Proxy %s не прошёл detection", proxy_name)
                            proxy_context.close()
                    if not detection_succeeded:
                        raise VersionDetectionError("direct и proxy detection не сработали")
                source_file = f"urls_variant_{detected_variant.lower()}.txt"
                urls = load_urls(checker_root / "config" / source_file)
                logger.info("[RUN] Выбран %s, файл %s, URL к проверке: %s", detected_variant, source_file, len(urls))
                if detection_used_proxy:
                    logger.info("[DETECTION] Возврат на direct context после proxy detection")
                    direct_state = context.storage_state()
                    context.close()
                    context = browser.new_context(storage_state=direct_state)
                    page = context.new_page()
                for url in urls:
                    direct_result = check_url(page, url, "direct")
                    result = direct_result
                    if is_retryable_network_failure(direct_result) and mode != "direct" and proxy_candidates:
                        logger.warning("[URL] Direct не прошёл для %s; начинаем per-URL proxy fallback", url)
                        direct_state = context.storage_state()
                        context.close()
                        context = None
                        for proxy, proxy_name in proxy_candidates:
                            logger.info("[URL] Retry через proxy %s: %s", proxy_name, url)
                            if not proxy_preflight(proxy):
                                logger.warning("[URL] Proxy %s недоступен; следующий proxy", proxy_name)
                                continue
                            proxy_context = browser.new_context(proxy=proxy.playwright_args(), storage_state=direct_state)
                            proxy_page = proxy_context.new_page()
                            try:
                                proxy_variant, _ = detect_variant(proxy_page)
                                if proxy_variant != detected_variant:
                                    skipped += 1
                                    result = UrlResult(
                                        url, "skipped", proxy=proxy_name,
                                        duration_seconds=direct_result.duration_seconds,
                                        error_type="variant_mismatch",
                                        message=f"proxy variant {proxy_variant} does not match selected variant {detected_variant}",
                                    )
                                    logger.warning("[URL] SKIP variant mismatch: selected=%s proxy=%s url=%s",
                                                    detected_variant, proxy_variant, url)
                                    break
                                result = check_url(proxy_page, url, proxy_name)
                                if result.status == "passed":
                                    break
                            finally:
                                proxy_context.close()
                        context = browser.new_context(storage_state=direct_state)
                        page = context.new_page()
                    results.append(result)
            finally:
                logger.info("[BROWSER 4/4] Закрытие context и browser")
                if context is not None:
                    context.close()
                browser.close()
    except VersionDetectionError as exc:
        return RunReport(run_id, detected_variant, DETECTION_URL, marker_found, source_file, len(results),
                         sum(r.status == "passed" for r in results), sum(r.status == "failed" for r in results),
                         0, [asdict(r) for r in results], "FAILURE", str(exc), "VERSION_DETECTION_ERROR", skipped)
    except Exception as exc:
        error_type = "PROXY_ERROR" if "прокси" in str(exc).lower() else "CHECKER_ERROR"
        return RunReport(run_id, detected_variant, DETECTION_URL, marker_found, source_file, len(results),
                         sum(r.status == "passed" for r in results), sum(r.status == "failed" for r in results),
                         0, [asdict(r) for r in results], "FAILURE", str(exc), error_type, skipped)
    passed = sum(r.status == "passed" for r in results)
    failed = sum(r.status == "failed" for r in results)
    warnings = sum(len(r.warnings) for r in results) + skipped
    status = "FAILURE" if failed else "UNSTABLE" if warnings else "SUCCESS"
    logger.info("[RUN] Завершено: status=%s total=%s passed=%s failed=%s warnings=%s skipped=%s",
                status, len(results), passed, failed, warnings, skipped)
    return RunReport(run_id, detected_variant, DETECTION_URL, marker_found, source_file, len(results), passed,
                     failed, warnings, [asdict(r) for r in results], status, skipped=skipped)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = Path(__file__).resolve().parent
    report = run(root)
    output = Path(os.getenv("REPORT_FILE", "mts_gpon_report.json"))
    output.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    if report.status != "SUCCESS":
        print(format_russian_notification(report))
    return 0 if report.status == "SUCCESS" else 2 if report.status == "UNSTABLE" else 1


TELEGRAM_TEXT_LIMIT = 3500


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
    failed_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_urls: list[str] = []
    for item in report.results:
        if item.get("status") == "failed":
            failed_groups[item.get("error_type") or "unknown"].append(item)
        elif item.get("status") == "skipped":
            skipped_urls.append(item.get("url") or "неизвестный URL")

    def render(include_urls: bool) -> str:
        rendered = list(lines)
        for error_type, items in failed_groups.items():
            rendered.extend(["", f"❌ Ошибка проверки MTS GPON: {error_type} ({len(items)})"])
            if include_urls:
                rendered.extend([
                    f"Страницы ({len(items)}):",
                    *[f"- {item.get('url')}" for item in items],
                ])
        if skipped_urls:
            rendered.extend([
                "", f"⚠️ URL пропущены из-за несовпадения варианта ({len(skipped_urls)})",
            ])
            if include_urls:
                rendered.extend([
                    f"Страницы ({len(skipped_urls)}):",
                    *[f"- {url}" for url in skipped_urls],
                ])
        return "\n".join(rendered)

    message = render(include_urls=True)
    return message if len(message) <= TELEGRAM_TEXT_LIMIT else render(include_urls=False)


if __name__ == "__main__":
    sys.exit(main())
