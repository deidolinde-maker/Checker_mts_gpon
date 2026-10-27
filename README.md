# Checker MTS GPON

Отдельный браузерный checker доступности `mts-home-gpon.ru`. Вариант URL выбирается
по `localStorage["landing-yandex-events:v1"]`: непустое значение выбирает B, отсутствие
или пустое значение — A. За один запуск используется один Chromium, один context и одна
страница; cookies и localStorage не очищаются.

## Локальный запуск

```text
pip install -r requirements.txt
playwright install chromium
python checker.py
```

Прокси задаются только переменными окружения `PROXY_1_URL` и `PROXY_2_URL`; значения с
учётными данными не коммитятся. `PROXIES_ENABLED=false` отключает proxy preflight и
запускает checker напрямую. Отчёт создаётся в `REPORT_FILE` (по умолчанию
`mts_gpon_report.json`).

## Jenkins

Подключите этот файл через `Pipeline script from SCM`. Для pipeline нужны два Jenkins
Secret Text credentials для проверки страниц: `mts_gpon_proxy_1_url` и
`mts_gpon_proxy_2_url`. Для уведомлений pipeline переиспользует credentials из
`Everyday_test`: `telegram_proxy_url`, `telegram_proxy_auth_secret` и
`tg_proxy_creds_survarius`. Агент должен
иметь Python 3, доступ к PyPI и возможность установить Chromium. Pipeline поддерживает
Unix и Windows agents, отключает параллельные сборки и архивирует JSON-отчёт.

## QA coverage

Unit-проверки конфигурации URL находятся в `tests/test_checker.py`. Полный QA-контекст:
`../docs/qa-kb/workflows/analyze-task.md`, `../docs/qa-kb/core/test-design/techniques.md`,
`../docs/qa-kb/project-contexts/everyday-test/mts-new-version-implementation.md`.
