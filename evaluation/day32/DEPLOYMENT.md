# Внешнее внедрение Day 32

18 октября 2026 пользователь разрешил публикацию workflow в fork, регистрацию/запуск runner, создание учебного PR и подтвердил GitHub Customer Agreement.

## Результат

Полный внешний цикл **PR → настоящий self-hosted runner → Ollama/MCP → комментарий → новый commit → обновление того же комментария** выполнен без подмены GitHub API или моделей.

- [PR #1](https://github.com/metalice80/outline_t/pull/1), автор metalice80, head/base в `metalice80/outline_t`. PR открыт, не слит; первоначальная намеренная ошибка уже исправлена.
- [Первый run 39133791220](https://github.com/metalice80/outline_t/actions/runs/39133791220): gate и review **success**, head `0d848a63e3c54b352e8f973d2756f7a5befd595b`. Найден один обоснованный high bug: shallowEqual возвращал true для массивов разной длины.
- [Второй run 39135121614](https://github.com/metalice80/outline_t/actions/runs/39135121614): gate и review **success**, head `5c4efcfde8b15eb9c616e05dd17483a4b166be63`. Ошибка исправлена, добавлен поясняющий комментарий, замечаний нет.
- [Комментарий 6250929201](https://github.com/metalice80/outline_t/pull/1#issuecomment-6250929201) от `github-actions[bot]`: сначала `action=created`, затем `action=updated`. API подтвердил тот же ID и ровно один комментарий бота. В нём новый head SHA. Это настоящая публикация с GITHUB_TOKEN workflow, без личного PAT.
- Первый и второй ZIP-артефакты скачаны из GitHub; их SHA-256 совпали с digest из API. Локальные копии: `actions-first/` и `actions-second/`, включая review.json/review.md, doctor/review/publication logs и снимки комментария. Summary и upload-artifact успешно выполнены в обоих jobs.

## Установка и настройки

Доверенная обычная установка, без editable и без выполнения PR-кода:

- Runtime: `/Users/sergey/ai/outline-review-runtime`.
- Бинарник: `/Users/sergey/ai/outline-review-runtime/venv/bin/outline-assistant`.
- Config: `/Users/sergey/ai/outline-review-runtime/config.toml`; `publish_enabled=true`, автор `github-actions[bot]`, абсолютные отдельные каталоги Git/RAG/help.
- Пакет 0.2.0, code SHA-256 `433370640b1a66fcc4b5373ac3a4c393ca5bbb97a43251feb8a21c2a3053148c`, совпадает с проверенной версией.
- Repository variables `AI_REVIEW_BIN`, `AI_REVIEW_CONFIG`, `AI_REVIEW_HOME` сохранены и проверены через GitHub UI.
- Runner: `outline-ai-review-sergey`, ID 2, labels `self-hosted`, `macOS`, `ARM64`, `ai-review`; зарегистрирован только для fork. Установка `/Users/sergey/ai/outline-review-runtime/runner`, версия 2.338.0. SHA-256 официального пакета `df4cebda25c86a886ed204e49fee63f5c2e7cec5f447b5c98440a826bbdf9df2` проверен перед распаковкой.
- Runner запущен командой `./run.sh`; фоновая системная служба и автозапуск не устанавливались. После завершения процесса/перезагрузки его нужно запустить снова. Не запускайте второй экземпляр при работающем первом.

Для запуска после остановки:

```bash
cd /Users/sergey/ai/outline-review-runtime/runner
./run.sh
```

Настоящие job logs подтверждают установленный бинарник, MCP, BGE-M3/Qwen3 и включённую публикацию. Отсутствие у GITHUB_TOKEN административного доступа к списку runners не помешало чтению PR и записи комментария с `contents: read`/`pull-requests: write`. Привилегии токена не повышались.

## Исправление, обнаруженное при внедрении

Первый workflow commit `112007301799a85e3f3224cb43721417f514fac2` был отклонён валидатором GitHub: `runner.temp` недоступен в job-level env. Исправление `74ad6720c476a76087dbf3205c495173d7a9b57b` опубликовано в main fork: каталог создаётся из RUNNER_TEMP в первом шаге и передаётся через GITHUB_ENV. Учебная ветка получила это же исправление до открытия PR; diff PR к main не меняет workflow.

Шаблон в проекте ассистента обновлён. **7 тестов workflow прошли**, включая регрессию недопустимого контекста. До изменения YAML полный набор дал 48 passed; после изменения только YAML полный набор не повторялся. Следующие настоящие PR-runs прошли все шаги.

## Дополнительные реальные проверки

До запуска runner установленный пакет выполнил локальное ревью опубликованного PR: `external-pr1-local/`, `external-pr1-local.log`. Это уже настоящий GitHub PR, Git snapshots, MCP stdio и Ollama, без API-фикстур. Намеренная ошибка найдена.

После второго commit проверен старый expected-head:

```bash
/Users/sergey/ai/outline-review-runtime/venv/bin/outline-assistant \
  --config /Users/sergey/ai/outline-review-runtime/config.toml \
  review-pr --repo metalice80/outline_t --number 1 \
  --expected-head 0d848a63e3c54b352e8f973d2756f7a5befd595b \
  --output-dir /Users/sergey/ai/aichallenge/aichallenge31/evaluation/day32/external-stale
```

Получен ожидаемый **exit 1 / stale**, без LLM-анализа и публикации. Артефакты — `external-stale/` и `external-stale.log`. Отдельная попытка реальной публикации старого отчёта не выполнялась; её guards проверены автоматическими API-фикстурами. Узкая гонка между последним GET head и POST/PATCH API остаётся описанным ограничением, а комментарий всегда указывает SHA.

Ручной workflow_dispatch с PR number 1 запущен из main: [run 39135463123](https://github.com/metalice80/outline_t/actions/runs/39135463123). Gate и review завершились **success**. Запуск произошёл из main (workflow SHA `74ad672`), но отчёт/комментарий проверяют настоящий head PR `5c4efcf`. После него API подтвердил ровно один комментарий с прежним ID и актуальным SHA; доказательства сохранены в `actions-manual/`.

## Покрытие и ограничения

Оба PR-отчёта имеют **partial**: на момент этих запусков RAG был ограничен 80 из 2336 разрешённых файлов, поиск символов также ограничен. Обязательный diff shallowEqual обработан. Это не полный семантический аудит Outline. В первом job переиспользованы все 3776 фрагментов предварительного локального индекса; во втором рассчитан 1 новый embedding и переиспользованы 3775.

После этих запусков лимит в действующей конфигурации runner увеличен до **1000 файлов**. Новый лимит проверен установленным пакетом и четырьмя локальными реальными прогонами без публикации; новый Actions run для этого изменения не запускался. Построение с пустым кешем: 47,54 → 402,38 с; подробные результаты и ограничения — [замер индексации](index-benchmark/REPORT.md). Старые GitHub-артефакты и комментарий относятся к лимиту 80.

BGE-M3 и Qwen3 8B настоящие, digest моделей сохранены в каждом отчёте. Ложное замечание на безопасный рефакторинг в предыдущей локальной оценке остаётся известным ограничением Qwen3; формальная валидация источников не доказывает причинность. Автоматического merge/approve нет.

Постоянный runner в публичном fork предназначен только для доверенного владельца. Guards YAML не делают безопасными произвольные внешние PR или изменение самого workflow. Условия доверия описаны в `docs/AI_REVIEW.md`.

GitHub показал предупреждение о Node.js 20 у закреплённого upload-artifact v4; платформа запустила action на Node.js 24, оба артефакта успешно загружены. Это предупреждение, а не ошибка запуска.

Исходная рабочая копия Outline не изменялась. Все учебные изменения сделаны в отдельном временном checkout fork; пользовательские AGENTS.md/day32.md и локальный config.toml ассистента сохранены.
