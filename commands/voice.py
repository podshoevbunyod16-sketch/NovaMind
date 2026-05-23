import subprocess
import os

def run(args):
    if not args:
        return "Использование: /voice [on|off|говори|текст]"

    cmd = args[0]
    if cmd == "on":
        os.environ["ASSISTANT_VOICE_REPLY"] = "1"
        return "Голосовые ответы включены."
    elif cmd == "off":
        os.environ["ASSISTANT_VOICE_REPLY"] = "0"
        return "Голосовые ответы отключены."
    elif cmd == "говори":
        try:
            # Запись речи и преобразование в текст
            result = subprocess.run(
                ["termux-dialog", "speech", "-t", "Говорите..."],
                capture_output=True, text=True
            )
            # termux-dialog возвращает JSON в stdout
            import json
            data = json.loads(result.stdout)
            return data.get("text", "Не удалось распознать речь.")
        except Exception as e:
            return f"Ошибка голосового ввода: {e}"
    else:
        return "Неизвестная подкоманда. /voice on/off/говори"