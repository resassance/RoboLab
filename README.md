# RoboLab

RoboLab подключается к агенту робота, запускает разрешённый алгоритм и показывает состояние процесса и журнал. В версии 0.1.0 доступен **Mock Robot**, работающий на ПК без Raspberry Pi. Учебные скрипты, полевые журналы и конфигурация Yahboom хранятся в отдельном проекте.

- [Аудит Yahboom и точки интеграции](docs/INTEGRATION_AUDIT.md)
- [Архитектура и контракт агента](docs/ARCHITECTURE.md)

## Запуск на ПК

Нужен Python 3.11 или новее. Выполняйте команды из корня RoboLab в двух терминалах.

**Linux, агент** (bash или sh)

```sh
python3 -m venv .venv-agent
.venv-agent/bin/python -m pip install ".[agent]"
export ROBOLAB_TOKEN="$(.venv-agent/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$ROBOLAB_TOKEN"
.venv-agent/bin/robolab-agent
```

**Linux, клиент**

```sh
python3 -m venv .venv-client
.venv-client/bin/python -m pip install ".[client,desktop]"
.venv-client/bin/robolab-client
```

**Windows, агент** (PowerShell)

```powershell
py -3 -m venv .venv-agent
.\.venv-agent\Scripts\python.exe -m pip install ".[agent]"
$env:ROBOLAB_TOKEN = & .\.venv-agent\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:ROBOLAB_TOKEN
.\.venv-agent\Scripts\robolab-agent.exe
```

**Windows, клиент** (PowerShell)

```powershell
py -3 -m venv .venv-client
.\.venv-client\Scripts\python.exe -m pip install ".[client,desktop]"
.\.venv-client\Scripts\robolab-client.exe
```

1. Оставьте адрес `http://127.0.0.1:8765`, вставьте токен из первого терминала и нажмите **Подключиться**.
2. Выберите **Mock Robot**, нажмите **Запустить** и дождитесь строки «Шаг симуляции». Окно покажет PID и длительность процесса.
3. Нажмите **Остановить** и дождитесь статуса «Остановлен». На Linux симулятор обрабатывает сигнал и завершает работу с кодом 0 после штатной остановки. Если его не остановить, он завершится примерно через 40 с.

Первый запуск десктопного Flet может скачать соответствующий движок. Для проверки интерфейса в браузере установите `.[client,web]` и выполните `robolab-client --web`. Сервер интерфейса также слушает только loopback.

Агент по умолчанию слушает только `127.0.0.1:8765`. Для подключения с другого устройства укажите интерфейс через `--host` и адрес этого интерфейса в клиенте. Для передачи управляющих запросов за пределами доверенной сети используйте TLS или защищённый туннель. Токен нужен для HTTP и WebSocket. Подключение к Raspberry Pi с реальным роботом потребует аппаратного адаптера.

## Проверки разработки

```sh
python -m pip install -e ".[agent,client,dev]"
python -m pytest -q
ruff check .
ruff format --check .
```

В наборе тестов запуск процесса, передача stdout/stderr, остановка, восстановление журнала по номеру сообщения, отказ запуска и блокировка одновременного старта. Сценарий клиента можно пройти через Flet по шагам выше. Windows, Android и Raspberry Pi требуют отдельных проверок.
