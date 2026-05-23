import subprocess
import os

def run(args):
    if not args:
        return "Использование: /download <URL> [качество, например: best, 720, audio]"

    url = args[0]
    quality = args[1] if len(args) > 1 else "best"

    download_path = os.path.expanduser("~/storage/downloads/")
    os.makedirs(download_path, exist_ok=True)

    # Стандартные опции yt-dlp
    command = [
        "yt-dlp",
        "-f", f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]" if quality.isdigit() else quality,
        "-o", os.path.join(download_path, "%(title)s.%(ext)s"),
        url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return f"✅ Загрузка завершена! Файлы сохранены в {download_path}"
        else:
            return f"Ошибка загрузки:\n{result.stderr[-500:]}"
    except subprocess.TimeoutExpired:
        return "Скачивание заняло слишком много времени и было остановлено."
