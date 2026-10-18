# План демонстрации ассистента Outline

Цель: показать работающий `/help`, затем полный цикл **PR → MCP → RAG/Ollama → ревью → один обновляемый комментарий**. Основной показ — 12–20 минут при заранее подготовленном кеше; это ориентир, не гарантированное время Actions. На подготовку оставить ещё 10–15 минут.

Этот документ — сценарий будущего показа. Его создание не запускает workflow, не создаёт PR и не публикует комментарии. Команды ниже выполняются ведущим последовательно. Учебный PR не сливать. По разрешению пользователя оба демонстрационных коммита выполняются в существующей папке `/Users/sergey/ai/aichallenge/outline`, в отдельной ветке. Новые клоны не создаются. `origin` направлен на `metalice80/outline_t`, исходный `outline/outline` сохранён как `upstream`.

## 1. Подготовка до демонстрации

Открыть три окна: терминал ассистента, терминал `/Users/sergey/ai/aichallenge/outline`, браузер с fork. В браузере использовать аккаунт `metalice80`.

В терминале ассистента:

```bash
cd /Users/sergey/ai/aichallenge/aichallenge31
.venv/bin/outline-assistant doctor
.venv/bin/outline-assistant index
/Users/sergey/ai/outline-review-runtime/venv/bin/outline-assistant \
  --config /Users/sergey/ai/outline-review-runtime/config.toml doctor --review
```

Проверить доступность Ollama, моделей `bge-m3` и `qwen3:8b`, MCP и GitHub. Команда `index` готовит документацию для `/help`, а не индекс кода для ревью. Лимит ревью — `review_max_index_files = 1000` в конфигурации runner. Модели перед показом не менять.

В [Settings → Actions → Runners](https://github.com/metalice80/outline_t/settings/actions/runners) проверить, что `outline-ai-review-sergey` подключён и свободен. Локальный `doctor` без токена может не видеть список runners: это не подтверждает его отсутствие. Если runner остановлен, в отдельном терминале запустить:

```bash
cd /Users/sergey/ai/outline-review-runtime/runner
./run.sh
```

Не запускать второй экземпляр работающего runner. Системный автозапуск не настроен; терминал runner должен оставаться открытым.

Заранее подготовить RAG именно в кеше установленного ассистента. Если учебный [PR #1](https://github.com/metalice80/outline_t/pull/1) по-прежнему открыт, выполнить при отсутствии активного workflow:

```bash
/Users/sergey/ai/outline-review-runtime/venv/bin/outline-assistant \
  --config /Users/sergey/ai/outline-review-runtime/config.toml \
  review-pr --repo metalice80/outline_t --number 1 \
  --output-dir /Users/sergey/ai/aichallenge/aichallenge31/review-output/demo-warmup
```

Это настоящий анализ без публикации: `review-pr` не вызывает `publish-review`. Если PR #1 закрыт, выбрать другой открытый доверенный PR и заменить номер. Не запускать локальное ревью одновременно с workflow: они используют общую блокировку кеша и одну Ollama.

Первичное построение 1000 файлов с пустым кешем заняло в измерении 6 мин 42 с. Кеш из эксперимента изолирован от кеша runner, поэтому сам факт завершённого эксперимента не означает готовый кеш runner. Общие embeddings переиспользуются между PR, но новый SHA требует нового снимка. Детали: [замер индексации](../evaluation/day32/index-benchmark/REPORT.md).

Открыть вкладки [PR fork](https://github.com/metalice80/outline_t/pulls) и [Actions](https://github.com/metalice80/outline_t/actions). Проверить variables `AI_REVIEW_BIN`, `AI_REVIEW_CONFIG`, `AI_REVIEW_HOME` по [руководству](AI_REVIEW.md). Подготовить доступ к SSH push и локальную Git identity для коммитов в основной папке Outline. До начала ветвления проверить, что нет незакоммиченных или неотслеживаемых пользовательских файлов; при наличии сначала сохранить свою работу. Сценарий не использует `reset`, `clean` или автоматический stash.

## 2. Вступление — 1 минута

Показать схему:

```text
Документация → BGE-M3/RAG → Qwen3 → /help с источниками
PR → GitHub Actions → доверенный ассистент → MCP → снимок Git
   → diff + RAG по коду → Qwen3 → проверенный формат → комментарий
```

Ключевые тезисы: модели работают локально; GitHub нужен для PR и публикации. Ассистент и Outline — разные проекты. Workflow запускает установленную версию ассистента; код PR используется как данные и не исполняется.

## 3. Работающий /help — 2 минуты

Из корня проекта ассистента:

```bash
.venv/bin/outline-assistant chat
```

Внутри чата последовательно ввести:

```text
/help
/help Как включить HTTP-логи?
/exit
```

Показать описание проекта, примеры вопросов, ответ, путь документа и строки источника, ветку и commit. Объяснить, что ссылки позволяют сверить ответ с документацией. Ожидается содержательный ответ с источником; точная формулировка модели может меняться.

## 4. Учебный PR с ошибкой — 3–5 минут плюс ожидание Actions

В терминале Outline проверить рабочее дерево и адреса remotes:

```bash
cd /Users/sergey/ai/aichallenge/outline
git status --short
git remote -v
git branch --show-current
```

Ожидается пустой `git status --short`, обычная именованная ветка и `origin` с fetch/push URL `git@github.com:metalice80/outline_t.git`. Затем в том же терминале сохранить имя исходной ветки, получить свежую `main` fork и создать демонстрационную ветку:

```bash
DEMO_RETURN_BRANCH="$(git branch --show-current)"
test -n "$DEMO_RETURN_BRANCH" &&
test -z "$(git status --porcelain)" &&
git fetch origin &&
git switch --no-track -c "demo-ai-review-$(date +%Y%m%d-%H%M%S)" origin/main
```

Продолжать только после успешного создания ветки. Она начинается от актуальной `origin/main`, поэтому содержит workflow fork. Локальная `main` при этом не перемещается. `--no-track` исключает привязку демонстрационной ветки к удалённой `main`; первый push ниже установит отсэкралеживание её собственной ветки. Не закрывать этот терминал до возврата на исходную ветку: в нём сохранена переменная `DEMO_RETURN_BRANCH`.

Внести одну демонстрационную ошибку. Скрипт проверяет исходный фрагмент и остановится, если код уже изменился:

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('shared/utils/shallowEqual.ts')
text = p.read_text()
old = 'if (a.length !== b.length) {\n    return false;'
assert text.count(old) == 1, 'Исходный код изменился: сначала проверьте функцию'
p.write_text(text.replace(old, 'if (a.length !== b.length) {\n    return true;', 1))
PY
git diff -- shared/utils/shallowEqual.ts
git -c core.hooksPath=/dev/null add shared/utils/shallowEqual.ts
git -c core.hooksPath=/dev/null commit -m "test: introduce array comparison regression for AI review demo"
git -c core.hooksPath=/dev/null push -u origin HEAD
```

Объяснить ошибку зрителям: массивы `[1]` и `[1, 2]` будут ошибочно считаться равными из-за разной длины.

Через браузер открыть **обычный, не Draft** PR из этой ветки в `main` репозитория `metalice80/outline_t`, автор — `metalice80`. Заголовок: `Demo: AI review — do not merge`. В описании указать, что это учебная намеренная регрессия и PR нельзя сливать. Записать номер PR; не считать, что он обязательно равен 1.

Показать вкладку Actions: workflow `Local AI review`, сначала `gate`, затем `review` на self-hosted runner. Пока идёт обработка, кратко объяснить роли MCP, BGE-M3, Qwen3 и ограничение 1000 файлов. Не запускать второе ревью вручную во время этого job.

## 5. Результат ревью — 2 минуты

Дождаться завершения первого workflow **до исправляющего push**, иначе concurrency может отменить первый запуск.

В PR показать комментарий `github-actions[bot]`:

- Ожидаемое замечание: разные длины массивов дают `true`, конкретный путь и строка, последствия и исправление. Такой баг был обнаружен в предыдущем настоящем внешнем тесте; повторный результат модели не гарантирован.
- SHA анализируемого commit совпадает с head PR.
- Источники и ограничения, включая статус `partial`, если часть корпуса не охвачена.
- Ссылка на Actions run, Summary и артефакты `review.md`, `review.json`, логи.

Скопировать ссылку на комментарий с `#issuecomment-...`: этот ID нужен для следующего шага. В JSON показать `rag.files`, `chunks`, `embedded`, `reused`. Количество вычисленных embeddings зависит от кеша; не обещать заранее ноль.

## 6. Исправление и обновление комментария — 3–5 минут

В той же папке `/Users/sergey/ai/aichallenge/outline` и той же демонстрационной ветке исправить функцию, оставив поясняющий комментарий, чтобы итоговый diff PR не стал пустым:

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('shared/utils/shallowEqual.ts')
text = p.read_text()
old = 'if (a.length !== b.length) {\n    return true;'
assert text.count(old) == 1, 'Демонстрационная ошибка не найдена'
new = 'if (a.length !== b.length) {\n    // Arrays of different lengths cannot be equal.\n    return false;'
p.write_text(text.replace(old, new, 1))
PY
git diff -- shared/utils/shallowEqual.ts
git -c core.hooksPath=/dev/null add shared/utils/shallowEqual.ts
git -c core.hooksPath=/dev/null commit -m "fix: restore array length comparison"
git -c core.hooksPath=/dev/null push
```

Дождаться нового запуска по событию `synchronize`. Ожидается исчезновение замечания об ошибке сравнения. Открыть ранее сохранённую ссылку на комментарий и показать: ID прежний, SHA новый, текст обновлён, второго комментария-отчёта не появилось. Сопоставить оба run и статистику переиспользования embeddings.

Если модель оставила ошибочное замечание, показать его как ограничение качества; не редактировать результат вручную ради демонстрации.

## 7. Завершение — 1–2 минуты

Подвести наблюдаемые результаты: `/help` отвечает с источниками; PR запустил реальный MCP/Ollama-анализ; ошибка получила ревью; исправление вызвало обновление одного комментария. Различать успешный workflow и полноту семантического анализа: статус `partial` допустим, `failed` не означает «ошибок нет».

Показать числа индексации 80/1000: 47,54/402,38 с с пустым кешем. Повторная загрузка индекса того же SHA — 0,033/0,297 с, но это не время всего workflow. Охват 1000/2336 относится к измеренному снимку; лимиты поиска и контекста остаются. Замер скорости не доказывает рост точности.

После показа закрыть учебный PR без merge через браузер. Сохранить номер PR, два run URL, comment ID и отчёты. В том же терминале Outline вернуться на исходную ветку после проверки чистоты:

```bash
cd /Users/sergey/ai/aichallenge/outline
git status --short
test -n "$DEMO_RETURN_BRANCH" &&
test -z "$(git status --porcelain)" &&
git switch "$DEMO_RETURN_BRANCH"
```

Если терминал был закрыт, вместо переменной указать записанное имя исходной ветки (при подготовке это была `main`). При незакоммиченных изменениях сначала разобраться с ними; не сбрасывать их. Демонстрационная ветка остаётся доступной для разбора. `/help` читает эту же рабочую копию, поэтому во время показа отражает её текущую ветку; PR-ревью использует отдельный снимок GitHub по SHA.

## Резервный сценарий

| Ситуация | Действие ведущего |
|---|---|
| Долгое построение индекса | Показать реальный progress в Actions, объяснить пустой кеш. Пока job идёт, разобрать архитектуру или прошлый отчёт. Явно назвать его прошлым результатом. |
| Runner offline/queued | Проверить runner и его терминал; не создавать повторные PR и не запускать второй runner. Если восстановление затягивается, перейти к сохранённым артефактам. |
| GitHub недоступен | Показать живой `/help`, затем сохранённые реальные отчёты и историю прошлого внедрения. Новый цикл публикации считать непроверенным. |
| Ollama недоступна | Показать диагностику и сохранённые отчёты; не выдавать запись за живой ответ модели. |
| Баг не обнаружен или есть ложное замечание | Разобрать фактический ответ и ограничения модели; не заявлять успешное обнаружение. |
| Нужен короткий показ без нового изменения кода | Если PR #1 открыт, Actions → Local AI review → Run workflow, ветка `main`, поле `number=1`. Это повтор ревью исправленного PR и обновление существующего комментария; обнаружение нового бага такой показ не проверяет. |

Сохранённые доказательства предыдущего цикла: [DEPLOYMENT.md](../evaluation/day32/DEPLOYMENT.md), [первый отчёт](../evaluation/day32/actions-first/review.md), [после исправления](../evaluation/day32/actions-second/review.md). Эти внешние runs выполнялись с лимитом **80**. Отдельные реальные локальные проверки **1000** без публикации: [отчёт замера](../evaluation/day32/index-benchmark/REPORT.md).

## Критерии успешной демонстрации

- `/help` и вопрос о HTTP-логах дают ответ с проверяемыми источниками.
- Оба run относятся к нужному учебному PR и правильным head SHA.
- В первом отчёте обнаружена внесённая ошибка, во втором она отсутствует.
- После исправления сохранён ID единственного комментария бота, обновлены SHA и содержимое.
- Показаны реальные ограничения и результаты; сохранённые примеры явно отделены от текущего запуска.
