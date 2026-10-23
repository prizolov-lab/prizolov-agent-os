<!-- Prizolov Agent OS 0.3.0 | Author: Dm.Andreyanov | Brand: Prizolov Lab | © 2026
SPDX-FileCopyrightText: 2026 Dm.Andreyanov / Prizolov Lab
SPDX-License-Identifier: Apache-2.0 -->

# Как помочь проекту

Спасибо, что хотите улучшить Prizolov Agent OS! Помогают сообщения об ошибках,
идеи, исправления, новые источники данных и инструменты агентов.

## Сообщить об ошибке или предложить идею

Создайте [issue](https://github.com/prizolov-lab/prizolov-agent-os/issues/new/choose)
по шаблону. Для ошибки укажите:

- что делали и что ожидали;
- что получилось;
- вывод `prizolov doctor --offline`.

Ключи и личные данные не публикуйте. Об уязвимостях пишите не в issue, а по правилам
из [SECURITY.md](SECURITY.md).

## Подготовка к разработке

```bash
git clone https://github.com/prizolov-lab/prizolov-agent-os.git
cd prizolov-agent-os
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
python -m pytest                  # тесты: сеть и ключ Claude не нужны
ruff check .                      # стиль кода
python -m mypy prizolov_os cli    # типы
pre-commit install                # те же проверки перед каждым коммитом
```

## Правила для изменений

- **Тесты.** Каждое изменение поведения сопровождается тестом. Модель (`FakeLLMClient`),
  котировки и Telegram в тестах имитируются; тесты не ходят в сеть.
- **Стиль.** Код проходит `ruff check .` и `python -m mypy prizolov_os cli`, длина
  строки — 100 символов. Тексты для пользователя — на русском, понятные человеку без
  технической подготовки.
- **Шапка авторства.** Новый файл начинается с шапки проекта. Проще всего запустить
  `python scripts/stamp_headers.py`, а `--check` проверит все файлы (это делает и CI).
  Шапки и [NOTICE](NOTICE) не удаляйте.
- **Безопасность.**
  - Данные из интернета и файлов пользователя — недоверенные: они идут через
    `wrap_untrusted`.
  - Запись файлов и другие необратимые действия — только с подтверждения человека
    (`requires_approval=True`).
- **Один pull request — одна задача.** Описывайте, что изменилось и зачем. CI
  (ruff, шапки, тесты на Python 3.10–3.13, Docker-образ) должен быть зелёным.

## Лицензия вклада

Отправляя pull request, вы соглашаетесь, что вклад распространяется по лицензии
[Apache 2.0](LICENSE) на тех же условиях, что и проект (раздел 5 лицензии).
Правообладатель проекта — Dm.Andreyanov / Prizolov Lab. Авторы заметного вклада
указываются в [AUTHORS](AUTHORS).
