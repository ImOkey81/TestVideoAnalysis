# TestVideoAnalysis

Flask-сервис для загрузки `mp4`-видео, извлечения кадров, генерации Gherkin через Mistral и сохранения jobs/results/artifacts/logs в PostgreSQL. Видео и preview frames сохраняются локально в `storage/`.

По умолчанию backend слушает `5000`, потому что PostgreSQL в этом задании использует `localhost:8080`.

## Запуск

Локально:

```bash
pip install -r requirements.txt
python AnalisesAI.py
```

Docker:

```bash
docker-compose up --build
```

## Конфиг

Обязательная переменная:

- `MISTRAL_API_KEY`

PostgreSQL defaults:

- `POSTGRES_HOST=localhost`
- `POSTGRES_PORT=8080`
- `POSTGRES_DB=test_platform`
- `POSTGRES_USER=postgres`
- `POSTGRES_PASSWORD=postgres`

Опционально:

- `DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:8080/test_platform`
- `APP_PORT=5000`
- `MAX_FRAMES=3`
- `FRAME_WIDTH=640`
- `JPEG_QUALITY=80`
- `MAX_VIDEO_SIZE_MB=100`
- `MISTRAL_TIMEOUT=90`

Готовая SQL-схема лежит в [schema.sql](/C:/Users/artem/OneDrive/Desktop/TestVideoAnalysis/schema.sql).

## API

### `GET /health`

```json
{
  "status": "UP",
  "service": "video-analysis"
}
```

### `POST /upload-video`

`multipart/form-data`, поле строго `video`.

Ограничения:

- только `video/mp4`
- размер ограничен через `MAX_VIDEO_SIZE_MB`

Ответ сохранён совместимым:

```json
{
  "status": "success",
  "frames_extracted": 3,
  "gherkin": "Feature: ..."
}
```

Во время обработки сервис:

- создаёт `Job`, `JobInput`, `JobResult`
- сохраняет исходное видео в `storage/videos/`
- сохраняет extracted frames в `storage/frames/`
- пишет `Artifact` и `JobLog`

### `GET /jobs`

Поддерживает фильтры `service_type`, `status`, `limit`, `offset`.

### `GET /jobs/<job_id>`

Возвращает metadata job из PostgreSQL.

### `GET /jobs/<job_id>/result`

Возвращает `gherkin_text`, `result_json` и статус.

### `GET /jobs/<job_id>/artifacts`

Возвращает список сохранённых artifacts.

### `GET /jobs/<job_id>/status`

Оставлен для совместимости со старым клиентом.

### `GET /jobs/<job_id>/feature`

Скачивает Gherkin-результат как `.feature`.

### `GET /get-test-cases`

Оставлен для совместимости и берёт последний успешный результат из PostgreSQL.
