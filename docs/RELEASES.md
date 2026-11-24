# Сборки RoboLab

GitHub Actions проверяет Python 3.11 и 3.12 на Linux и Python 3.12 на Windows. Ручной запуск workflow **RoboLab CI and builds** после тестов собирает клиент для всех трёх платформ и сохраняет файлы в артефактах запуска.

| Платформа | Файл | Запуск |
| --- | --- | --- |
| Ubuntu 24.04, x86_64 | `RoboLab-<версия>-linux-x64.tar.gz` | Извлечь весь архив и выполнить `./robolab` |
| Windows, x86_64 | `RoboLab-<версия>-windows-x64.zip` | Извлечь весь архив и открыть `robolab.exe` |
| Android 7.0 и новее, ARM64 и x86_64 | `RoboLab-<версия>-android.apk` | Установить через `adb install путь-к.apk` или открыть APK на устройстве |

Чтобы подключить Android Emulator к агенту на компьютере, выберите **Напрямую к агенту** и укажите `http://10.0.2.2:8765`. В десктопные сборки входит Python 3.12, в APK — Python 3.14. Для сохранения пароля на Linux требуется системное хранилище Secret Service, например GNOME Keyring. Агент запускается отдельно по [инструкции из README](../README.md).

APK по умолчанию подписывается отладочным ключом Flutter. Сборки с разными ключами требуют удалить предыдущий APK перед установкой. При удалении стираются локальные настройки клиента. Для сборок, которые должны обновляться поверх установленных, используйте один Android-ключ через переменные окружения Flet. [Документация Android-сборок Flet](https://flet.dev/docs/publish/android/)

## Локальная сборка

В Python 3.12 установите инструменты сборки и выберите цель для текущей системы.

```sh
python -m pip install ".[release]"
python scripts/build_client.py linux
python scripts/build_client.py windows
python scripts/build_client.py apk
```

Linux-сборка требует системных пакетов из `flet --version --json` и `libsecret-1-dev`, Windows-сборка требует Visual Studio с компонентами C++ Desktop, а Android-сборка требует JDK 17 и Android SDK. Результат сохраняется в `dist`. [Требования Flet к сборке](https://flet.dev/docs/publish/)

## Выпуск на GitHub

Версию задаёт `project.version` в `pyproject.toml`. После проверки артефактов создайте и отправьте тег формата `v<версия>` на коммите с этой версией. Workflow проверит совпадение тега и версии проекта, повторит тесты и соберёт все три платформы. GitHub Release будет опубликован с этими файлами и их контрольными суммами в `SHA256SUMS.txt`.
