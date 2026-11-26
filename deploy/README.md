# Центральный плоттер на Ubuntu

Nginx передаёт запросы одному экземпляру Plotter через loopback. Старые workers Nginx завершают начатые запросы при перезагрузке конфигурации. Сервисы Plotter запускаются отдельными экземплярами systemd с собственными портами и виртуальными окружениями.

Приложение хранит реестр лекал, наряды и рабочие копии раскроев в `production.sqlite3` рядом с `workspace.json`. Последний файл по-прежнему хранит самостоятельный раскрой. Макеты, настройки и очередь бирок хранятся рядом в `labels.sqlite3`. Оба слота используют базы из `shared`, а кандидат на изолированном порту использует временные копии. Снимки обеих баз создаются через механизм резервного копирования SQLite. Если прежний выпуск ещё не создал `labels.sqlite3`, новый создаст её при запуске. При откате на выпуск без бирок база сохранится для следующего обновления. При откате на 1.x доступен самостоятельный раскрой из `workspace.json`. Реестр станет доступен при возврате к Plotter 2.0. Команды станка выполняет локальная панель у станка.

## Первоначальная настройка

На целевой машине нужны Bash, nginx, systemd, Python 3.10+ с `venv`, curl и util-linux. Для импорта DWG в общем реестре установите `dwg2dxf` и проверьте доступность команды для службы Plotter. Проверена версия LibreDWG 0.14. Проверьте владельца HTTPS-адреса, имеющиеся виртуальные хосты Nginx и выбранные loopback-порты перед установкой конфигурации.

1. Создайте отдельную системную учётную запись `plotter` и каталоги приложения.

   ```bash
   sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin plotter
   sudo install -d -m 0755 /srv/plotter/{releases,slots} /etc/plotter /etc/nginx/plotter
   sudo install -d -o plotter -g plotter -m 0750 /srv/plotter/shared
   sudo install -d -m 0700 /srv/plotter/shared/backups /etc/plotter/tls
   sudo -u plotter touch /srv/plotter/shared/workspace.json.lock
   ```

2. Установите конфигурацию деплоя и шаблон systemd. Для профиля используйте пример репозитория с локальным адресом контроллера. Размеры рабочего поля можно настроить в этом профиле.

   ```bash
   sudo install -m 0600 deploy/deploy.conf.example /etc/plotter/deploy.conf
   sudo install -m 0640 -o root -g plotter research/machine-profile.example.json /etc/plotter/machine-profile.json
   sudo install -m 0644 deploy/plotter@.service /etc/systemd/system/plotter@.service
   sudo systemctl daemon-reload
   ```

   Укажите `PUBLIC_HOST` и `PUBLIC_PORT` в `deploy.conf`. Создайте `/etc/plotter/blue.env` и `/etc/plotter/green.env` от имени root с правами `0600`. Значения `PORT` должны соответствовать `BLUE_PORT` и `GREEN_PORT`. В `PUBLIC_ORIGIN` укажите полный адрес HTTPS и добавьте порт, если он отличается от 443.

3. Установите сертификат и ключ в `/etc/plotter/tls/` по путям из шаблона Nginx. В сертификате должны быть имя согласованного адреса или IP SAN. Если сертификат выпущен внутренним УЦ, добавьте доверенный корневой сертификат на устройства операторов и укажите его в `TLS_CA_FILE` для локальной проверки.

4. Создайте начальный include один раз, перед первой публикацией.

   ```bash
   printf '# plotter-slot: none\nreturn 503;\n' | sudo tee /etc/nginx/plotter/upstream.conf >/dev/null
   sudo install -m 0644 deploy/plotter.nginx.conf.example /etc/nginx/sites-available/plotter.conf
   ```

   Замените `PLOTTER_HOST` и `PLOTTER_PORT` значениями из конфигурации, а `PLOTTER_ORIGIN` полным HTTPS-адресом. Ограничьте Nginx интерфейсом локальной сети и loopback для проверки релиза. Включите отдельный виртуальный хост, выполните `nginx -t` и перезагрузите Nginx. Журналирование приложения доступно через `journalctl -u 'plotter@*.service'`.

## HTTP-адрес для браузеров в локальной сети

Для рабочих мест в доверенной сети можно включить отдельный HTTP-виртуальный хост из `deploy/plotter.http.nginx.conf.example`. В примере браузеры открывают `http://plotter.example.invalid:8080`, а локальный помощник использует существующий центральный API `https://plotter.example.invalid:8443`. Адреса и порты замените согласованными значениями. Для HTTPS помощнику нужен проверяемый сертификат или явно настроенный доверенный сертификат УЦ. HTTP передаёт данные без шифрования. Ограничьте доступ к нему согласованной сетью и интерфейсом, включая правила брандмауэра.

Задайте IPv4 интерфейса в `http_bind` и разрешённую подсеть CIDR в `lan_cidr`. Для HTTP на стандартном порту 80 и HTTPS на стандартном порту 443 оставьте порт только в отдельной переменной порта, опустив его из соответствующего значения `*_authority`. Используйте одно каноническое имя для сотрудников. HTTP-порт должен быть свободен на выбранном интерфейсе.

```bash
http_host=plotter.example.invalid
http_port=8080
http_authority=plotter.example.invalid:8080
https_authority=plotter.example.invalid:8443
http_bind=192.0.2.10
lan_cidr=192.0.2.0/24

sed -e "s|PLOTTER_HTTP_BIND|$http_bind|g" \
    -e "s|PLOTTER_HTTP_PORT|$http_port|g" \
    -e "s|PLOTTER_HTTP_HOST|$http_host|g" \
    -e "s|PLOTTER_HTTP_AUTHORITY|$http_authority|g" \
    -e "s|PLOTTER_HTTP_ORIGIN|http://$http_authority|g" \
    -e "s|PLOTTER_HTTPS_AUTHORITY|$https_authority|g" \
    -e "s|PLOTTER_HTTPS_ORIGIN|https://$https_authority|g" \
    -e "s|PLOTTER_LAN_CIDR|$lan_cidr|g" \
    deploy/plotter.http.nginx.conf.example |
    sudo tee /etc/nginx/sites-available/plotter-http.conf >/dev/null
sudo ln -s /etc/nginx/sites-available/plotter-http.conf /etc/nginx/sites-enabled/plotter-http.conf
sudo nginx -t && sudo systemctl reload nginx
```

`https_authority` должен совпадать с именем и портом в существующем `PUBLIC_ORIGIN`. Оба хоста используют один upstream и одну рабочую область. HTTP-хост передаёт центральному приложению фиксированный HTTPS Host. Он заменяет Origin только при точном совпадении с настроенным HTTP-адресом. Запросы GET и HEAD проходят без Origin, а запросы с посторонним Origin, `null` либо без Origin при записи получают 403. Правила доступа к существующему HTTPS-адресу остаются в его конфигурации.

## Публикация и возврат версии

Используйте чистый исходный каталог из проверенного коммита. После изменения версии этот выпуск должен быть совместим с публичным центральным API.

Перед `stage` сверьте коммит тега с успешным прогоном CI и SHA-256 опубликованных архивов. Подключайтесь по SSH с закреплённым ключом хоста. Держите реквизиты доступа в защищённом хранилище рабочего компьютера. Для Linux используйте скрипт деплоя с окончаниями строк LF.

```bash
sudo bash deploy/release.sh stage VERSION /path/to/release-source
sudo bash deploy/release.sh activate VERSION
sudo bash deploy/release.sh rollback
```

Чтобы в окне «Релиз» появилась кнопка «Скачать помощник», передайте в `stage` после метки выпуска и каталога исходников путь к `Plotter-Companion-Setup-VERSION-x64.exe`, собранному для этой сети. Следующим параметром можно передать архив `plotter-companion-vVERSION-windows-x64.zip` из той же сборки. Помощник проверяет его номер сборки и контрольную сумму перед установкой.

Для повторной выкладки с тем же номером из `VERSION` используйте новую метку каталога, например `2.5.0-companion-r1`. Сборку помощника при сохранении номера версии различает файл `COMPANION_REVISION`.

Команда `stage` создаёт отдельный venv, запускает тесты и поднимает кандидата на preview-порту, используя временную копию рабочей области и журнала. `activate` запускает кандидата во втором слоте, проверяет API на loopback, обновляет upstream Plotter и делает graceful reload Nginx. Проверка HTTPS через loopback использует согласованный Host и подтверждает заголовком, что запрос дошёл до нового слота. Предыдущий экземпляр останавливается после завершения старых workers. Если они ещё работают, скрипт оставляет прежний процесс живым, выключает его автозапуск и сообщает имя слота. Дождитесь завершения старых workers перед ручной остановкой этого экземпляра. До её завершения следующий переход будет отклонён.

Скрипт сохраняет одну ссылку на предыдущий выпуск и снимки данных перед переключениями в `shared/backups`. Снимки предназначены для восстановления по отдельному решению. Выберите `rollback`, чтобы вернуть предыдущий код на том же адресе и продолжить с текущими данными. Перед переключением скрипт отклоняет версию, которая не может прочитать или повторно сохранить рабочую область без потери полей или прочитать все записи журнала. Проверка выполняется на копии. Для релизов, меняющих формат, заранее проверьте оба направления на данных, созданных новой версией. Версия Windows без `--central` не подходит в качестве предыдущего центрального выпуска.

Перед `activate` сохраните отдельные свежие копии `production.sqlite3` и `labels.sqlite3` (если она уже есть), рабочей области, её резервного файла и журнала. Проверьте их через `check-state.py` с окружением текущего выпуска. После переключения проверьте версию и активный слот через HTTPS, целостность реестра, скачивание карты пошива и контрольные суммы выдаваемых файлов помощника. Обновите помощника после проверки центрального API и освобождения станка.
