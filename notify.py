"""Send a failed MTS GPON report through the existing Telegram proxy endpoint."""
from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from checker import RunReport, format_russian_notification


def send_report(report_path: Path) -> bool:
    report = RunReport(**json.loads(report_path.read_text(encoding="utf-8")))
    if report.status == "SUCCESS":
        print("Telegram notification skipped for successful run.")
        return True

    endpoint = os.getenv("TELEGRAM_PROXY_URL", "").strip()
    auth_secret = os.getenv("TELEGRAM_PROXY_AUTH_SECRET", "").strip()
    creds = os.getenv("TELEGRAM_PROXY_CREDS", "").strip()
    missing = [name for name, value in {
        "TELEGRAM_PROXY_URL": endpoint,
        "TELEGRAM_PROXY_AUTH_SECRET": auth_secret,
        "TELEGRAM_PROXY_CREDS": creds,
    }.items() if not value]
    if missing:
        print(f"Telegram notification skipped; missing: {', '.join(missing)}")
        return False

    message = format_russian_notification(report)
    payload = json.dumps({
        "title": "Проверка доступности MTS GPON",
        "text": html.escape(message),
        "creds": creds,
        "parse_mode": "HTML",
        "disable_notification": False,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, data=payload, headers={
        "Content-Type": "application/json",
        "X-Authentication": auth_secret,
    }, method="POST")
    try:
        with urlopen(request, timeout=float(os.getenv("TELEGRAM_PROXY_TIMEOUT_SEC", "15"))) as response:
            print(f"Telegram notification sent through proxy (status={response.status}).")
        return True
    except Exception as exc:
        print(f"Telegram notification failed: {exc}")
        return False


if __name__ == "__main__":
    report_file = Path(os.getenv("REPORT_FILE", "mts_gpon_report.json"))
    sys.exit(0 if send_report(report_file) else 0)
