Остановить	
sudo systemctl stop tgbot

Запустить (если остановлен)	
sudo systemctl start tgbot

Перезапустить	
sudo systemctl restart tgbot

Перечитать конфиг systemd (после правки .service-файла)	
sudo systemctl daemon-reload && sudo systemctl restart tgbot

Отключить автозапуск	
sudo systemctl disable tgbot

Включить автозапуск обратно	
sudo systemctl enable tgbot
