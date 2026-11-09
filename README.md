# Takeone — запись экрана Android / iPhone с паузой

Программа для Windows и macOS: трансляция экрана телефона на компьютер и запись с кнопками
**● Запись**, **⏸ Пауза / ▶ Продолжить**, **■ Стоп и сохранить**. На выходе — один MP4 без пауз.

| Режим | Windows | macOS |
|---|---|---|
| Android по USB | ✅ | ✅ |
| iPhone по USB | — | ✅ |
| iPhone по Wi-Fi (AirPlay) | ✅ | ✅ (нужен `brew install uxplay`) |

Всё нужное (scrcpy, adb, ffmpeg, на Windows — AirPlay-приёмник) уже внутри установщика,
ставить ничего дополнительно не нужно.

---

## Как получить установщики (один раз, ~15 минут)

Установщики собираются бесплатно на серверах GitHub — нужен только аккаунт на github.com.

1. Создайте на GitHub новый репозиторий (можно **Private**), например `Takeone`.
2. Загрузите в него содержимое этой папки целиком, **включая скрытую папку `.github`**.
   Проще всего через GitHub Desktop или командами:
   ```
   git init && git add . && git commit -m "Takeone"
   git branch -M main
   git remote add origin https://github.com/<вы>/Takeone.git
   git push -u origin main
   ```
3. Откройте вкладку **Actions** → слева **Build** → **Run workflow**.
4. Через 15–25 минут внизу страницы запуска появятся файлы (раздел **Artifacts**):
   - `Takeone-Windows` → `Takeone-Setup-1.0.0.exe`
   - `Takeone-macOS-arm64` → `.dmg` для Mac на Apple M1/M2/M3/M4
   - `Takeone-macOS-x86_64` → `.dmg` для Mac на Intel

**Чтобы раздавать коллегам постоянной ссылкой:** создайте тег версии
(`git tag v1.0.0 && git push --tags`) — установщики сами прикрепятся к странице
**Releases** репозитория, оттуда их можно скачивать и пересылать.

Новая версия: поменяйте `VERSION = "..."` в `app/takeone.py` и поставьте новый тег.

### Собрать на своём компьютере (без GitHub)

- **macOS:** нужен Python с python.org и Xcode Command Line Tools (`xcode-select --install`):
  `bash packaging/build_macos.sh` → `build/Takeone-<версия>-macOS-<arch>.dmg`
- **Windows:** нужны Python и Inno Setup 6 (`winget install JRSoftware.InnoSetup`):
  `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`
  Без MSYS2 сборка пройдёт без режима AirPlay. Чтобы он был, сначала в оболочке MSYS2 UCRT64:
  `bash packaging/bundle_uxplay.sh build-uxplay`, затем `build_windows.ps1 -UxplayDir build-uxplay`.

---

## Установка (для тех, кому вы отправляете)

**Windows:** запустить `Takeone-Setup-…exe` → Далее → Установить. Если появится синее окно
SmartScreen «Windows защитила ваш компьютер» — **Подробнее → Выполнить в любом случае**
(предупреждение появляется у любой программы без платной цифровой подписи).

**macOS:** открыть `.dmg`, перетащить Takeone в «Программы». При первом запуске macOS скажет,
что не может проверить разработчика: **Системные настройки → Конфиденциальность и безопасность →
внизу «Takeone заблокировано» → Всё равно открыть**. Если пишет, что приложение «повреждено»,
в Терминале: `xattr -dr com.apple.quarantine /Applications/Takeone.app`

## Подготовка телефона

**Android:** Настройки → О телефоне → 7 раз нажать «Номер сборки» → «Для разработчиков» →
включить **Отладку по USB**. При подключении кабелем подтвердить доступ на телефоне.

**iPhone по USB (Mac):** подключить кабелем, разблокировать, «Доверять этому компьютеру».
При первой записи macOS спросит доступ к камере — разрешить (так macOS называет захват видео).

**iPhone по AirPlay:** iPhone и компьютер в одной сети Wi-Fi (не гостевой). В программе режим
«iPhone (AirPlay, Wi-Fi)» → «Подключить», на iPhone: Пункт управления → Повтор экрана → Takeone.

## Если что-то не работает

- Логи: папка `.takeone/logs` в домашней папке пользователя.
- Android: «Запись не стартовала» — в настройках поставьте «Макс. сторона» 1280.
- AirPlay на Windows: iPhone не видит Takeone — проверьте, что сеть в Windows отмечена как
  «Частная», и что сторонний антивирус не блокирует `uxplay.exe`.
- AirPlay на Mac: «порт занят» — выключите «Приёмник AirPlay» в Системных настройках →
  Основные → AirDrop и Handoff.

## Состав проекта

```
app/takeone.py            программа
app/ios_capture.swift       запись iPhone по USB на macOS
packaging/build_windows.ps1 сборка Windows (PyInstaller + Inno Setup)
packaging/bundle_uxplay.sh  сборка AirPlay-приёмника UxPlay для Windows (MSYS2)
packaging/installer.iss     установщик Windows
packaging/build_macos.sh    сборка .app и .dmg
packaging/make_icon.py      иконка
.github/workflows/build.yml автосборка на GitHub
```

Используемые открытые компоненты: scrcpy (Apache-2.0), ffmpeg (GPL), UxPlay (GPL-3.0), GStreamer (LGPL).
Для внутреннего использования в команде ограничений нет; при публичном распространении
программы прикладывайте ссылки на исходники этих проектов.
