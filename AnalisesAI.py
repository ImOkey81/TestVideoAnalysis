import os
import cv2
import base64
import requests
import tempfile
import time
import random
import threading
import re

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

# ================== INIT ==================

load_dotenv()

app = Flask(__name__)
CORS(app, supports_credentials=True)

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not MISTRAL_API_KEY:
    raise RuntimeError("MISTRAL_API_KEY not found in .env")

HEADERS = {
    "Authorization": f"Bearer {MISTRAL_API_KEY}",
    "Content-Type": "application/json",
}

# Один запрос к Mistral за раз (часто решает 429 по concurrency)
MISTRAL_LOCK = threading.Lock()

# ================== CORE ==================

class VideoTestGenerator:
    def __init__(self):
        self.generated_test_cases: str | None = None

        # Один Session на процесс: keep-alive, меньше накладных расходов
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def extract_frames(self, video_path: str, max_frames: int = 3):
        cap = cv2.VideoCapture(video_path)
        frames = []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        step = max(total_frames // max_frames, 1)

        for i in range(0, total_frames, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                continue

            # Полегче для API (можешь вернуть 800x600 если нужно)
            frame = cv2.resize(frame, (640, 360))
            _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            frames.append(base64.b64encode(buffer).decode())

            if len(frames) >= max_frames:
                break

        cap.release()
        return frames

    def call_mistral_with_retry(self, payload: dict, timeout: int = 90, max_attempts: int = 5):
        """
        Ретрай для 429/временных проблем.
        Возвращает (response_json, None) или (None, error_dict)
        """
        last_status = None
        last_text = None

        for attempt in range(1, max_attempts + 1):
            try:
                # Важно: сериализованный доступ (concurrency limit)
                with MISTRAL_LOCK:
                    resp = self.session.post(MISTRAL_API_URL, json=payload, timeout=timeout)

                if resp.status_code == 429:
                    last_status = 429
                    last_text = resp.text

                    # Если сервер говорит сколько ждать — ждём
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_s = int(retry_after)
                    else:
                        # Экспоненциальный backoff + jitter
                        base = min(2 ** (attempt - 1), 16)
                        sleep_s = base + random.uniform(0.2, 0.8)

                    time.sleep(sleep_s)
                    continue

                # Иногда 5xx тоже временные
                if 500 <= resp.status_code < 600:
                    last_status = resp.status_code
                    last_text = resp.text
                    base = min(2 ** (attempt - 1), 16)
                    time.sleep(base + random.uniform(0.2, 0.8))
                    continue

                resp.raise_for_status()
                return resp.json(), None

            except requests.RequestException as e:
                # сетевые ошибки тоже ретраим
                last_status = last_status or "network_error"
                last_text = str(e)
                base = min(2 ** (attempt - 1), 16)
                time.sleep(base + random.uniform(0.2, 0.8))
                continue

        return None, {"status": last_status, "body": last_text}

    def normalize_snapshot_gherkin(self, text: str) -> str:
        # Заменяем любые числа на <value>, но не трогаем уже существующие <value>/<hostname> и т.п.
        # Примеры: 41, 45.5, 1 000, 12/34, 15%, 100Р
        t = text

        # 1) числа с возможными пробелами-разделителями тысяч
        t = re.sub(r'(?<!<)\b\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d+)?\b(?!>)', '<value>', t)

        # 2) проценты и дроби вида <value>/<value> упрощаем до <value> (чтобы не плодить)
        t = re.sub(r'\b<value>\s*/\s*<value>\b', '<value>', t)
        t = re.sub(r'\b<value>\s*%\b', '<value>%', t)

        return t


    def generate_gherkin(self, frames: list[str]) -> str | None:
        if not frames:
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты QA-инженер. Генерируешь тест-кейсы ТОЛЬКО по фактам, видимым на предоставленных кадрах UI (несколько изображений).\n"
                    "ЖЁСТКИЕ ПРАВИЛА:\n"
                    "1) Запрещены любые предположения о поведении и действиях пользователя. НЕ используй слова/идеи: click, tap, hover, scroll, open, opens, navigate, redirect, play, playback, search, login, modal.\n"
                    "2) Разрешены только проверки snapshot текущего состояния экранов: видимость/наличие элементов, читаемые тексты, названия секций, структура областей (левая панель/центральная область/верхняя часть/нижняя панель/правая часть), очевидные состояния (выделение/активный пункт), если это видно.\n"
                    "3) Если элемент не читается или ты не уверен — НЕ упоминай его.\n"
                    "4) Все числа/цены/проценты/счётчики/время/имена/прочие динамические значения заменяй на <value>.\n"
                    "5) НЕ используй точные координаты и сравнительные формулировки: top/right/left/bottom corner, 'справа от', 'слева от', 'ниже/выше', 'слева направо', 'в вертикальном порядке'.\n"
                    "6) НЕ интерпретируй иконки без текста и не перечисляй 'additional icons/controls'. Если у элемента нет читаемого текста — обычно не упоминай его.\n"
                    "7) Для карточек/плиток используй нейтральные формулировки: 'отображается минимум одна карточка' и 'карточка содержит текст <value>'. Не утверждай 'артист/страна/трек', если это не написано явно читаемым текстом на кадре.\n"
                    "8) Ответ строго в Gherkin. Без markdown. Без таблиц. Без комментариев.\n"
                    "9) Каждый Scenario должен содержать только шаги Then/And (без Given/When).\n"
                    "10) Если кадры показывают разные экраны/состояния, создавай отдельные Scenario по каждому видимому состоянию, но всё равно без действий пользователя.\n"
                ),
            },
            {
                "role": "user",
                "content": (
                        [
                            {
                                "type": "text",
                                "text": (
                                    "Сгенерируй 10–18 Scenario для проверки UI по этим кадрам.\n"
                                    "Только проверяемые по кадрам утверждения (snapshot validation).\n"
                                    "Покрой: левую панель (если видна), центральную область, верхнюю часть (если видна), нижнюю панель (если видна), заголовки/лейблы, основные секции, списки/карточки (минимум по одной, если видны).\n"
                                    "Сценарии должны быть устойчивыми к адаптивной верстке: избегай проверок точного порядка и точных координат.\n"
                                    "Не используй действия пользователя и не описывай то, чего не видно на кадрах."
                                ),
                            }
                        ]
                        + [
                            {
                                "type": "image_url",
                                "image_url": f"data:image/jpeg;base64,{frame}",
                            }
                            for frame in frames
                        ]
                ),
            },
        ]

        payload = {
            "model": "ministral-14b-2512",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1500,
        }

        data, err = self.call_mistral_with_retry(payload, timeout=90, max_attempts=5)
        if err:
            print("Mistral final error:", err)
            return None

        content = data["choices"][0]["message"]["content"].strip()

        if content.lower().startswith("gherkin"):
            content = content[len("gherkin"):].strip()
        if "```" in content:
            content = content.replace("```", "").strip()

        # ЖЁСТКО приводим к snapshot-стилю: числа → <value>
        content = self.normalize_snapshot_gherkin(content).strip()

        self.generated_test_cases = content

        return self.generated_test_cases


generator = VideoTestGenerator()

# ================== API ==================

@app.route("/upload-video", methods=["POST"])
def upload_video():
    if "video" not in request.files:
        return jsonify({"error": "File field must be named 'video'"}), 400

    file = request.files["video"]

    if not file.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only mp4 supported"}), 400

    try:
        video_bytes = file.read()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(video_bytes)
            video_path = tmp.name

        frames = generator.extract_frames(video_path)
        os.unlink(video_path)

        if not frames:
            return jsonify({"error": "Failed to extract frames"}), 400

        gherkin = generator.generate_gherkin(frames)

        # Если не получилось — вероятно лимит/временная недоступность
        if not gherkin:
            return jsonify({
                "error": "Mistral rate limit / temporary unavailable. Try again in a few seconds."
            }), 429

        return jsonify({
            "status": "success",
            "frames_extracted": len(frames),
            "gherkin": gherkin,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-test-cases", methods=["GET"])
def get_test_cases():
    if generator.generated_test_cases:
        return jsonify({"status": "success", "gherkin": generator.generated_test_cases})
    return jsonify({"status": "error", "message": "Test cases have not been generated yet"}), 400


# ================== RUN ==================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
