# Checker MTS GPON

Отдельный браузерный checker доступности `mts-home-gpon.ru`. Вариант URL выбирается
по наличию ключа `localStorage["landing-yandex-events:v1"]`: ключ есть — B, ключа нет — A.
За один запуск используется один Chromium, один context и одна
страница; cookies и localStorage не очищаются.

## Локальный запуск

```text
pip install -r requirements.txt
playwright install chromium
python checker.py
```

Прокси задаются только переменными окружения `PROXY_1_URL` и `PROXY_2_URL`; значения с
учётными данными не коммитятся. Поддерживаются значения Secret Text в форматах:

```text
185.183.160.117:8000:USERNAME:PASSWORD
http://USERNAME:PASSWORD@185.183.160.117:8000
```

Первый формат автоматически преобразуется checker-ом. `PROXIES_ENABLED=false` отключает
proxy preflight и запускает checker напрямую. Отчёт создаётся в `REPORT_FILE` (по умолчанию
`mts_gpon_report.json`).

В Jenkins сетевой режим выбирается параметром `NETWORK_MODE`:

- `auto` — основной direct, затем fallback на proxy №1 и proxy №2; это режим по умолчанию;
- `direct` — прямое подключение без proxy credentials.
- `proxy` — только proxy №1, затем proxy №2; если оба недоступны, build завершается с ошибкой.

Checker оценивает именно доступность: JS-ошибки страницы не влияют на `passed/failed`.
Критичны HTTP-статус, SSL/TLS, timeout, DNS/connection errors и redirect на другой домен.

## Jenkins

Подключите этот файл через `Pipeline script from SCM`. Для pipeline нужны два Jenkins
Secret Text credentials для проверки страниц: `mts_gpon_proxy_1_url` и
`mts_gpon_proxy_2_url`. Для уведомлений pipeline переиспользует credentials из
`Everyday_test`: `telegram_proxy_url`, `telegram_proxy_auth_secret` и
`telegram_proxy_global_test`. Агент должен
иметь Python 3, доступ к PyPI и возможность установить Chromium. Pipeline поддерживает
Unix и Windows agents, отключает параллельные сборки и архивирует JSON-отчёт.

На Unix Jenkins agent зависимости сохраняются в `/var/lib/jenkins/shared/Checker_mts_gpon/venv`,
pip cache — в `/var/lib/jenkins/.cache/pip`, а Playwright browsers — в
`/var/lib/jenkins/.cache/ms-playwright`. Поэтому повторные builds не создают новый venv и
не скачивают Chromium заново, если версия уже установлена.

## QA coverage

Unit-проверки конфигурации URL находятся в `tests/test_checker.py`. Полный QA-контекст:
`../docs/qa-kb/workflows/analyze-task.md`, `../docs/qa-kb/core/test-design/techniques.md`,
`../docs/qa-kb/project-contexts/everyday-test/mts-new-version-implementation.md`.
