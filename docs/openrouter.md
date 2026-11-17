# Один ключ OpenRouter

Все роли могут обращаться через `OPENROUTER_API_KEY`. У каждой остаётся свой `model_id` и реальное семейство модели. Общий транспорт не делает Claude и GPT одной моделью; независимость ошибок при этом всё равно проверяется предметным eval.

## Запуск

Создайте ключ в [OpenRouter Keys](https://openrouter.ai/keys), обеспечьте доступ к выбранным моделям и установите лимит расходов ключа. В PowerShell из папки проекта выполните:

```powershell
.\connect-openrouter.ps1
```

Скрипт запросит **один ключ скрытым вводом**, получит актуальный каталог/тарифы выбранных endpoints, выполнит до четырёх платных проверок контрактов и запустит сервер на `http://127.0.0.1:8089/docs`. Ключ передаётся через окружение текущего окна; в конфиг и отчёт он не записывается. Для Swagger используется отдельный локальный bearer token, который сервер выводит в терминал.

Резервный бюджет проверки по умолчанию — 2 USD, без автоматических повторов. Это предел резервирования приложения, а не обещание точной суммы счёта. Можно задать `-MaxSetupCostUsd 1`, но меньший резерв может остановить проверку до следующего вызова. При провале рабочий config не создаётся; `config/openrouter.qualification.json` и отдельная SQLite-база сохраняют известные и неопределённые расходы.

Настройки по умолчанию продолжают роли из ТЗ: `openai/gpt-6-astra` для planner/reviewer и `anthropic/claude-sonnet-5.5` для executor/reporter. Это кандидаты для проверки совместимости, не доказанное оптимальное распределение. Их наличие, published canonical_slug, поддержка structured outputs и цены проверяются через каталог при setup. Доступность именно для вашего ключа выясняется реальным запросом.

При необходимости запустить setup вручную:

```powershell
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'OpenRouter key' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m application openrouter-setup --max-cost-usd 2
.\.venv\Scripts\python.exe -m application --root var/openrouter --config config/openrouter.json serve --port 8089
```

Модели и endpoints задаются параметрами `--planner-model`, `--executor-model`, `--planner-upstream`, `--executor-upstream`. Требуются разные vendor namespaces для reviewer и автора. Уже существующий config setup не перезаписывает: для повторной квалификации укажите новый `--output config/openrouter-v2.json`.

## Что проверяет setup

Фактические ответы четырёх ролей проходят полные локальные схемы, неизменяемый DAG, совпадение цитат, покрытие метрик, контракт ревью и выбор элементов отчёта. В `scope: contract` фиксируется доля прохождения этих контрольных проверок. Это **не оценка предметного качества**, не измерение recall reviewer и не подтверждение AC-11.

Создаётся режим `qualification_scope: contract_pilot`. Он допускает только `public` данные, `low` риск и обязательную приёмку человеком. `internal`, `confidential`, high/medium risk и автоматический выпуск блокируются кодом. Итоговый пакет содержит явное ограничение о контрактном пилоте. Для обычного режима `domain` нужна отдельная предметная квалификация: контрактный результат её не подменяет.

Пример разрешённой публичной синтетической задачи: `examples/task.openrouter.json`. После запуска сервера его можно отправить через Swagger `POST /v1/runs` или из того же PowerShell:

```powershell
.\.venv\Scripts\python.exe -m application --root var/openrouter --config config/openrouter.json run examples/task.openrouter.json
```

## Маршрут и расходы

- Используется `POST https://openrouter.ai/api/v1/chat/completions`, Bearer key и `response_format: json_schema`.
- `provider.only` задаёт разрешённого upstream; `allow_fallbacks: false` и отсутствие списка запасных моделей сохраняют контроль маршрута у оркестратора.
- `require_parameters: true` требует поддержку параметров у endpoint. Полная валидация всё равно выполняется в приложении.
- `data_collection: deny` запрашивается по умолчанию. Это не гарантия отсутствия всех форм хранения у посредника; для чувствительных данных нужна отдельная проверка договорных условий, региона и ZDR.
- `provider.max_price` ограничен настроенными тарифами; setup берёт максимум опубликованных ценовых tiers выбранного endpoint. Это может быть консервативнее базового тарифа.
- В receipt сохраняются фактически возвращённые model ID, upstream provider, generation ID, usage и `usage.cost`. Допустимы запрошенный model ID либо явно зафиксированный canonical snapshot; другая модель блокирует выполнение с сохранением уже известных расходов.
- Если стоимость отсутствует, сохраняется полный reservation с неопределённой стоимостью. HTTP 200 с error-объектом не считается успешным ответом. Reasoning-блоки не сохраняются.
- Canonical slug фиксируется для аудита. Если провайдер не предоставляет вызываемый неизменяемый snapshot, запись ревизии не является технической гарантией неизменности модели по alias.

Официальные источники: [API](https://openrouter.ai/docs/api/reference/overview), [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), [маршрутизация](https://openrouter.ai/docs/guides/routing/provider-selection), [usage и стоимость](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
