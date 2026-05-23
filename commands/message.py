import os
import subprocess
import requests
import json

def run(args):
    if not args:
        return """Использование команд:
/wa <номер> <текст> — открыть WhatsApp и вставить сообщение
/tg <username> <текст> — открыть Telegram и вставить сообщение

Примеры:
/wa +79161234567 Привет, как дела?
/tg myfriend Проверь этот файл"""

    if len(args) < 2:
        return "Укажите получателя и текст сообщения"

    cmd = args[0].lower()
    
    # --- WhatsApp ---
    if cmd == "wa":
        number = args[1].strip("+").replace(" ", "").replace("-", "")
        message = " ".join(args[2:])
        
        # Используем официальную Click-to-Chat ссылку [citation:6]
        encoded_msg = requests.utils.quote(message)
        wa_url = f"https://wa.me/{number}?text={encoded_msg}"
        
        # Открываем ссылку через Android intent
        subprocess.run([
            "termux-open-url", wa_url
        ])
        
        return f"WhatsApp открыт для номера +{number} с сообщением:\n\n_{message}_\n\nЕсли сообщение не появилось, проверьте, что WhatsApp установлен."

    # --- Telegram ---
    elif cmd == "tg":
        username = args[1].strip("@")
        message = " ".join(args[2:])
        
        # Используем диплинк Telegram
        encoded_msg = requests.utils.quote(message)
        tg_url = f"https://t.me/{username}?text={encoded_msg}"
        
        # Открываем ссылку через Android intent
        subprocess.run([
            "termux-open-url", tg_url
        ])
        
        return f"Telegram открыт для @{username} с сообщением:\n\n_{message}_\n\nЕсли сообщение не появилось, проверьте, что Telegram установлен."
    
    else:
        return f"Неизвестная команда: /{cmd}"
