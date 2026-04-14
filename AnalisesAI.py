import base64
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request
from flask_cors import CORS
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
VIDEOS_DIR = STORAGE_DIR / "videos"
FRAMES_DIR = STORAGE_DIR / "frames"

for directory in (STORAGE_DIR, VIDEOS_DIR, FRAMES_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job_input: Mapped[Optional["JobInput"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    job_result: Mapped[Optional["JobResult"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobInput(Base):
    __tablename__ = "job_inputs"

    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    job: Mapped[Job] = relationship(back_populates="job_input")


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    gherkin_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    job: Mapped[Job] = relationship(back_populates="job_result")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    job: Mapped[Job] = relationship(back_populates="artifacts")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    job: Mapped[Job] = relationship(back_populates="logs")


DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = int(os.getenv("POSTGRES_PORT", "8080"))
DB_NAME = os.getenv("POSTGRES_DB", "test_platform")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
SERVICE_TYPE = "video_analysis"
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_DONE = "done"
JOB_FAILED = "failed"
PREVIEW_FRAME_TYPE = "preview_frame"
UPLOADED_VIDEO_TYPE = "uploaded_video"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MAX_FRAMES = int(os.getenv("MAX_FRAMES", "3"))
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "640"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "80"))
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "100"))
MISTRAL_TIMEOUT = int(os.getenv("MISTRAL_TIMEOUT", "90"))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

HEADERS = {
    "Authorization": f"Bearer {MISTRAL_API_KEY}" if MISTRAL_API_KEY else "",
    "Content-Type": "application/json",
}

MISTRAL_LOCK = threading.Lock()
ALLOWED_MIME_TYPES = {"video/mp4"}
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024

app = Flask(__name__)
CORS(app, supports_credentials=True)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base.metadata.create_all(engine)


class VideoProcessingError(Exception):
    code = "processing_error"
    status_code = 500


class InvalidVideoFormatError(VideoProcessingError):
    code = "invalid_video_format"
    status_code = 400


class VideoTooLargeError(VideoProcessingError):
    code = "video_too_large"
    status_code = 413


class VideoCorruptedError(VideoProcessingError):
    code = "video_corrupted"
    status_code = 400


class FrameExtractionError(VideoProcessingError):
    code = "frame_extraction_failed"
    status_code = 422


class MistralRateLimitError(VideoProcessingError):
    code = "mistral_rate_limit"
    status_code = 429


class MistralTemporaryError(VideoProcessingError):
    code = "mistral_temporary_error"
    status_code = 503


class MissingApiKeyError(VideoProcessingError):
    code = "missing_api_key"
    status_code = 500


class DatabaseOperationError(VideoProcessingError):
    code = "database_error"
    status_code = 500


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(self, filename: str, content_type: str, size_bytes: int) -> str:
        now = utc_now()
        job_id = str(uuid.uuid4())
        job = Job(
            id=job_id,
            service_type=SERVICE_TYPE,
            status=JOB_PENDING,
            title=filename,
            created_at=now,
            updated_at=now,
        )
        job_input = JobInput(
            job_id=job_id,
            payload_json={
                "originalFilename": filename,
                "contentType": content_type,
                "sizeBytes": size_bytes,
            },
        )
        self.session.add(job)
        self.session.add(job_input)
        self.session.flush()
        return job_id

    def set_job_processing(self, job_id: str) -> None:
        job = self._require_job(job_id)
        now = utc_now()
        job.status = JOB_PROCESSING
        job.started_at = now
        job.updated_at = now
        job.error_message = None
        self.session.flush()

    def update_job_input(self, job_id: str, **payload_updates) -> None:
        job = self._require_job(job_id)
        payload = dict(job.job_input.payload_json if job.job_input else {})
        payload.update(payload_updates)
        if job.job_input is None:
            job.job_input = JobInput(job_id=job_id, payload_json=payload)
        else:
            job.job_input.payload_json = payload
        job.updated_at = utc_now()
        self.session.flush()

    def set_job_done(self, job_id: str, gherkin_text: str, frames_extracted: int) -> None:
        job = self._require_job(job_id)
        now = utc_now()
        job.status = JOB_DONE
        job.finished_at = now
        job.updated_at = now
        job.error_message = None

        result = job.job_result
        payload = {"framesExtracted": frames_extracted}
        if result is None:
            result = JobResult(job_id=job_id, gherkin_text=gherkin_text, result_json=payload)
            self.session.add(result)
        else:
            result.gherkin_text = gherkin_text
            result.result_json = payload
        self.session.flush()

    def set_job_failed(self, job_id: str, message: str) -> None:
        job = self._require_job(job_id)
        now = utc_now()
        job.status = JOB_FAILED
        job.finished_at = now
        job.updated_at = now
        job.error_message = message
        self.session.flush()

    def add_artifact(self, job_id: str, artifact_type: str, file_name: str, file_path: Path, mime_type: str) -> None:
        artifact = Artifact(
            id=str(uuid.uuid4()),
            job_id=job_id,
            artifact_type=artifact_type,
            file_name=file_name,
            file_path=str(file_path.resolve()),
            mime_type=mime_type,
            size_bytes=file_path.stat().st_size,
        )
        self.session.add(artifact)
        self.session.flush()

    def add_log(self, job_id: str, level: str, message: str) -> None:
        self.session.add(JobLog(id=str(uuid.uuid4()), job_id=job_id, level=level, message=message))
        self.session.flush()

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.session.get(Job, job_id)

    def list_jobs(
        self,
        service_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Job]:
        query = self.session.query(Job).order_by(Job.created_at.desc())
        if service_type:
            query = query.filter(Job.service_type == service_type)
        if status:
            query = query.filter(Job.status == status)
        return query.offset(offset).limit(limit).all()

    def get_latest_successful_result(self) -> Optional[JobResult]:
        return (
            self.session.query(JobResult)
            .join(Job, Job.id == JobResult.job_id)
            .filter(Job.service_type == SERVICE_TYPE, Job.status == JOB_DONE)
            .order_by(Job.finished_at.desc(), Job.created_at.desc())
            .first()
        )

    def _require_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job is None:
            raise DatabaseOperationError(f"Job {job_id} not found")
        return job


class VideoTestGenerator:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def extract_frames(self, video_path: Path, job_id: str, repository: JobRepository, max_frames: int = MAX_FRAMES) -> list[str]:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap.release()
            raise VideoCorruptedError("Video file is corrupted or unreadable")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            raise VideoCorruptedError("Video file is corrupted or contains no frames")

        repository.add_log(job_id, "INFO", "frame extraction started")
        frames: list[str] = []
        step = max(total_frames // max_frames, 1)
        frame_index = 0

        for i in range(0, total_frames, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            height, width = frame.shape[:2]
            if width <= 0 or height <= 0:
                continue

            resized_height = max(int(height * (FRAME_WIDTH / width)), 1)
            frame = cv2.resize(frame, (FRAME_WIDTH, resized_height))
            ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                continue

            frame_index += 1
            frame_name = f"{job_id}_frame_{frame_index}.jpg"
            frame_path = FRAMES_DIR / frame_name
            frame_path.write_bytes(buffer.tobytes())
            repository.add_artifact(job_id, PREVIEW_FRAME_TYPE, frame_name, frame_path, "image/jpeg")
            frames.append(base64.b64encode(buffer).decode())
            if len(frames) >= max_frames:
                break

        cap.release()

        if not frames:
            raise FrameExtractionError("Failed to extract frames from the video")

        repository.add_log(job_id, "INFO", f"frame extraction finished: {len(frames)} frames")
        return frames

    def call_mistral_with_retry(self, payload: dict, timeout: int = MISTRAL_TIMEOUT, max_attempts: int = 5):
        last_status = None
        last_text = None

        for attempt in range(1, max_attempts + 1):
            try:
                with MISTRAL_LOCK:
                    resp = self.session.post(MISTRAL_API_URL, json=payload, timeout=timeout)

                if resp.status_code == 429:
                    last_status = 429
                    last_text = resp.text
                    logger.warning("Mistral rate limit on attempt %s: %s", attempt, resp.text[:500])
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_s = int(retry_after)
                    else:
                        sleep_s = min(2 ** (attempt - 1), 16) + random.uniform(0.2, 0.8)
                    time.sleep(sleep_s)
                    continue

                if 500 <= resp.status_code < 600:
                    last_status = resp.status_code
                    last_text = resp.text
                    logger.warning("Mistral server error on attempt %s: status=%s body=%s", attempt, resp.status_code, resp.text[:500])
                    time.sleep(min(2 ** (attempt - 1), 16) + random.uniform(0.2, 0.8))
                    continue

                resp.raise_for_status()
                return resp.json(), None

            except requests.Timeout as exc:
                last_status = "timeout"
                last_text = str(exc)
                logger.warning("Mistral timeout on attempt %s: %s", attempt, exc)
                time.sleep(min(2 ** (attempt - 1), 16) + random.uniform(0.2, 0.8))
            except requests.RequestException as exc:
                last_status = last_status or "network_error"
                last_text = str(exc)
                logger.warning("Mistral request error on attempt %s: %s", attempt, exc)
                time.sleep(min(2 ** (attempt - 1), 16) + random.uniform(0.2, 0.8))

        return None, {"status": last_status, "body": last_text}

    def normalize_snapshot_gherkin(self, text: str) -> str:
        normalized = re.sub(r"(?<!<)\b\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d+)?\b(?!>)", "<value>", text)
        normalized = re.sub(r"\b<value>\s*/\s*<value>\b", "<value>", normalized)
        normalized = re.sub(r"\b<value>\s*%\b", "<value>%", normalized)
        return normalized

    def generate_gherkin(self, frames: list[str], job_id: str, repository: JobRepository) -> str:
        if not MISTRAL_API_KEY:
            raise MissingApiKeyError("MISTRAL_API_KEY not found in environment")

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты QA-инженер. Генерируешь тест-кейсы ТОЛЬКО по фактам, видимым на предоставленных кадрах UI.\n"
                    "ЖЕСТКИЕ ПРАВИЛА:\n"
                    "1) Не используй предположения о действиях пользователя.\n"
                    "2) Разрешены только snapshot-проверки текущего состояния экрана.\n"
                    "3) Если элемент не читается или есть сомнения, не упоминай его.\n"
                    "4) Все числа, счетчики, даты и динамические значения заменяй на <value>.\n"
                    "5) Не используй точные координаты и привязки к расположению.\n"
                    "6) Не интерпретируй иконки без текста.\n"
                    "7) Ответ строго в Gherkin, без markdown.\n"
                    "8) Каждый Scenario должен содержать только Then/And.\n"
                    "9) Если кадры показывают разные состояния, делай отдельные Scenario.\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    [
                        {
                            "type": "text",
                            "text": (
                                "Сгенерируй 10-18 Scenario для проверки UI по этим кадрам. "
                                "Только проверяемые по кадрам утверждения."
                            ),
                        }
                    ]
                    + [{"type": "image_url", "image_url": f"data:image/jpeg;base64,{frame}"} for frame in frames]
                ),
            },
        ]

        payload = {
            "model": "ministral-14b-2512",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1500,
        }

        repository.add_log(job_id, "INFO", "ai request started")
        data, err = self.call_mistral_with_retry(payload, timeout=MISTRAL_TIMEOUT, max_attempts=5)
        if err:
            repository.add_log(job_id, "ERROR", f"ai request failed: {err['status']}")
            if err["status"] == 429:
                raise MistralRateLimitError("Mistral rate limit reached")
            raise MistralTemporaryError("Mistral temporary error")

        content = data["choices"][0]["message"]["content"].strip()
        if content.lower().startswith("gherkin"):
            content = content[len("gherkin") :].strip()
        if "```" in content:
            content = content.replace("```", "").strip()
        return self.normalize_snapshot_gherkin(content).strip()


generator = VideoTestGenerator()


def json_error(message: str, code: str, status_code: int):
    return jsonify({"status": "error", "error": {"code": code, "message": message}}), status_code


def get_session() -> Session:
    return SessionLocal()


def validate_upload(file_storage) -> tuple[bytes, str]:
    if file_storage is None or not file_storage.filename:
        raise InvalidVideoFormatError("Video file is required")

    if file_storage.mimetype not in ALLOWED_MIME_TYPES or not file_storage.filename.lower().endswith(".mp4"):
        raise InvalidVideoFormatError("Only video/mp4 files are supported")

    video_bytes = file_storage.read()
    if not video_bytes:
        raise InvalidVideoFormatError("Uploaded file is empty")

    if len(video_bytes) > MAX_VIDEO_SIZE_BYTES:
        raise VideoTooLargeError(f"Video exceeds {MAX_VIDEO_SIZE_MB} MB limit")

    return video_bytes, file_storage.mimetype


def store_uploaded_video(job_id: str, original_name: str, video_bytes: bytes) -> tuple[str, Path]:
    safe_name = Path(original_name).name or "video.mp4"
    video_name = f"{job_id}_{safe_name}"
    video_path = VIDEOS_DIR / video_name
    video_path.write_bytes(video_bytes)
    return video_name, video_path


def serialize_job(job: Job) -> dict:
    response = {
        "id": job.id,
        "service_type": job.service_type,
        "status": job.status,
        "title": job.title,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat(),
        "error_message": job.error_message,
    }
    if job.job_input:
        response["input"] = job.job_input.payload_json
    return response


def serialize_result(job: Job) -> dict:
    result = job.job_result
    return {
        "job_id": job.id,
        "status": job.status,
        "gherkin_text": result.gherkin_text if result else None,
        "result_json": result.result_json if result else None,
        "error_message": job.error_message,
    }


def serialize_artifact(artifact: Artifact) -> dict:
    return {
        "id": artifact.id,
        "job_id": artifact.job_id,
        "artifact_type": artifact.artifact_type,
        "file_name": artifact.file_name,
        "file_path": artifact.file_path,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
        "created_at": artifact.created_at.isoformat(),
    }


def run_video_job(file_storage):
    session = get_session()
    repository = JobRepository(session)
    job_id: Optional[str] = None

    try:
        logger.info("Upload started: filename=%s content_type=%s", file_storage.filename, file_storage.mimetype)
        video_bytes, content_type = validate_upload(file_storage)
        job_id = repository.create_job(file_storage.filename, content_type, len(video_bytes))
        session.commit()

        video_name, video_path = store_uploaded_video(job_id, file_storage.filename, video_bytes)
        repository.add_artifact(job_id, UPLOADED_VIDEO_TYPE, video_name, video_path, content_type)
        repository.add_log(job_id, "INFO", "video uploaded")
        repository.set_job_processing(job_id)
        session.commit()

        frames = generator.extract_frames(video_path, job_id, repository)
        repository.update_job_input(job_id, framesExtracted=len(frames))
        session.commit()

        gherkin = generator.generate_gherkin(frames, job_id, repository)
        repository.set_job_done(job_id, gherkin, len(frames))
        repository.add_log(job_id, "INFO", "job completed")
        session.commit()

        return (
            {
                "status": "success",
                "frames_extracted": len(frames),
                "gherkin": gherkin,
            },
            200,
        )
    except VideoProcessingError as exc:
        logger.exception("Video processing error for job_id=%s: %s", job_id, exc)
        session.rollback()
        if job_id:
            try:
                repository.set_job_failed(job_id, str(exc))
                repository.add_log(job_id, "ERROR", str(exc))
                session.commit()
            except SQLAlchemyError:
                session.rollback()
        return json_error(str(exc), exc.code, exc.status_code)
    except (SQLAlchemyError, OSError) as exc:
        logger.exception("Persistence error for job_id=%s: %s", job_id, exc)
        session.rollback()
        if job_id:
            try:
                repository.set_job_failed(job_id, str(exc))
                repository.add_log(job_id, "ERROR", str(exc))
                session.commit()
            except SQLAlchemyError:
                session.rollback()
        return json_error("Failed to persist job data", "database_error", 500)
    except Exception as exc:
        logger.exception("Unhandled error for job_id=%s: %s", job_id, exc)
        session.rollback()
        if job_id:
            try:
                repository.set_job_failed(job_id, str(exc))
                repository.add_log(job_id, "ERROR", str(exc))
                session.commit()
            except SQLAlchemyError:
                session.rollback()
        return json_error(str(exc), "internal_error", 500)
    finally:
        session.close()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "UP", "service": "video-analysis"})


@app.route("/upload-video", methods=["POST"])
def upload_video():
    if "video" not in request.files:
        return json_error("File field must be named 'video'", "missing_video_field", 400)
    return run_video_job(request.files["video"])


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    session = get_session()
    try:
        repository = JobRepository(session)
        job = repository.get_job(job_id)
        if job is None:
            return json_error("Job not found", "job_not_found", 404)
        return jsonify(serialize_job(job))
    finally:
        session.close()


@app.route("/jobs/<job_id>/status", methods=["GET"])
def get_job_status(job_id: str):
    session = get_session()
    try:
        repository = JobRepository(session)
        job = repository.get_job(job_id)
        if job is None:
            return json_error("Job not found", "job_not_found", 404)

        response = {
            "jobId": job.id,
            "status": job.status,
            "filename": job.title,
            "createdAt": job.created_at.isoformat(),
            "updatedAt": job.updated_at.isoformat(),
        }
        if job.error_message:
            response["error"] = {
                "code": "processing_error" if job.status == JOB_FAILED else "job_error",
                "message": job.error_message,
                "statusCode": 500 if job.status == JOB_FAILED else 400,
            }
        if job.job_result and job.job_result.result_json:
            response["processedAt"] = job.finished_at.isoformat() if job.finished_at else None
            response["framesExtracted"] = job.job_result.result_json.get("framesExtracted")
        return jsonify(response)
    finally:
        session.close()


@app.route("/jobs/<job_id>/result", methods=["GET"])
def get_job_result(job_id: str):
    session = get_session()
    try:
        repository = JobRepository(session)
        job = repository.get_job(job_id)
        if job is None:
            return json_error("Job not found", "job_not_found", 404)
        if job.status == JOB_FAILED:
            return jsonify(serialize_result(job)), 500
        if job.status != JOB_DONE:
            return jsonify({"job_id": job_id, "status": job.status, "message": "Result is not ready yet"}), 202
        return jsonify(serialize_result(job))
    finally:
        session.close()


@app.route("/jobs/<job_id>/artifacts", methods=["GET"])
def get_job_artifacts(job_id: str):
    session = get_session()
    try:
        repository = JobRepository(session)
        job = repository.get_job(job_id)
        if job is None:
            return json_error("Job not found", "job_not_found", 404)
        return jsonify([serialize_artifact(artifact) for artifact in job.artifacts])
    finally:
        session.close()


@app.route("/jobs", methods=["GET"])
def list_jobs():
    session = get_session()
    try:
        repository = JobRepository(session)
        service_type = request.args.get("service_type")
        status = request.args.get("status")
        limit = min(max(int(request.args.get("limit", "20")), 1), 100)
        offset = max(int(request.args.get("offset", "0")), 0)
        jobs = repository.list_jobs(service_type=service_type, status=status, limit=limit, offset=offset)
        return jsonify(
            {
                "items": [serialize_job(job) for job in jobs],
                "limit": limit,
                "offset": offset,
            }
        )
    except ValueError:
        return json_error("limit and offset must be integers", "invalid_pagination", 400)
    finally:
        session.close()


@app.route("/get-test-cases", methods=["GET"])
def get_test_cases():
    session = get_session()
    try:
        repository = JobRepository(session)
        latest_result = repository.get_latest_successful_result()
        if latest_result is None or not latest_result.gherkin_text:
            return json_error("No successful test cases found", "test_cases_not_found", 404)
        return jsonify({"status": "success", "gherkin": latest_result.gherkin_text, "job_id": latest_result.job_id})
    finally:
        session.close()


@app.route("/jobs/<job_id>/feature", methods=["GET"])
def download_feature(job_id: str):
    session = get_session()
    try:
        repository = JobRepository(session)
        job = repository.get_job(job_id)
        if job is None:
            return json_error("Job not found", "job_not_found", 404)
        if job.status == JOB_FAILED:
            return jsonify(serialize_result(job)), 500
        if job.status != JOB_DONE or job.job_result is None or not job.job_result.gherkin_text:
            return jsonify({"job_id": job_id, "status": job.status, "message": "Feature file is not ready yet"}), 202

        response = make_response(job.job_result.gherkin_text)
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{job_id}.feature"'
        return response
    finally:
        session.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
