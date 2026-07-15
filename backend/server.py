#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import fitz
import requests
from PIL import Image
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings_json() -> None:
    """Load LLM credentials from ~/.claude/settings.json, overriding .env values."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        env = data.get("env", {})
        if env.get("ANTHROPIC_BASE_URL"):
            os.environ["LLM_BASE_URL"] = env["ANTHROPIC_BASE_URL"]
        if env.get("ANTHROPIC_AUTH_TOKEN"):
            os.environ["LLM_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
        if env.get("ANTHROPIC_MODEL"):
            # Strip Claude Code routing suffixes like [1m] before passing to gateway
            model = re.sub(r"\[.*?\]", "", env["ANTHROPIC_MODEL"]).strip()
            os.environ["LLM_MODEL"] = model
    except Exception:
        pass


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_settings_json()

BASE_URL = os.getenv("LLM_BASE_URL", "https://llm-gateway.momenta.works")
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6[1m]")
PASSWORD = os.getenv("PAPERS_UPLOAD_PASSWORD", "eden")
REPO = Path(os.getenv("PAPERS_SITE_REPO", Path(__file__).resolve().parents[1])).resolve()
PUBLIC_URL = os.getenv("PAPERS_SITE_PUBLIC_URL", "https://zijixie.github.io/papers-site").rstrip("/")
JOBS_DIR = Path(os.getenv("PAPERS_SITE_JOBS_DIR", REPO / ".upload_jobs")).resolve()
METRICS_PATH = Path(os.getenv("PAPERS_SITE_METRICS_JSON", Path(__file__).with_name("metrics.json"))).resolve()
GIT_PUSH = os.getenv("PAPERS_SITE_GIT_PUSH", "0") == "1"
LLM_TIMEOUT_SECONDS = int(os.getenv("PAPERS_LLM_TIMEOUT_SECONDS", "300"))

STATUS_LABELS = {
    "queued": "排队中",
    "uploading": "上传中",
    "extracting": "抽取中",
    "verifying": "核验中",
    "translating": "翻译中",
    "rendering": "生成网页中",
    "deploying": "部署中",
    "complete": "完成",
    "failed": "失败",
}

THEME_EN = {
    "设计介入跨学科协作": "Design-Mediated Interdisciplinary Collaboration",
    "设计转译": "Design Translation",
    "新主题": "New Theme",
}

app = FastAPI(title="Papers Site Upload Backend")
origins = [o.strip() for o in os.getenv(
    "PAPERS_SITE_CORS_ORIGINS",
    "https://zijixie.github.io,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@dataclass
class Job:
    id: str
    status: str = "queued"
    message: str = "任务已创建。"
    url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=int(os.getenv("PAPERS_SITE_WORKERS", "1")))


def set_job(job_id: str, status: str, message: str, url: str | None = None) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = status
        job.message = message
        if url:
            job.url = url


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    password: str = Form(...),
    theme: str = Form(...),
    pdf: UploadFile = File(...),
) -> dict[str, str]:
    if password != PASSWORD:
        raise HTTPException(status_code=403, detail="访问密码错误。")
    if theme not in THEME_EN:
        raise HTTPException(status_code=400, detail="主题分类无效。")
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传 PDF 文件。")

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_pdf = job_dir / "input.pdf"
    with input_pdf.open("wb") as f:
        shutil.copyfileobj(pdf.file, f)
    if input_pdf.read_bytes()[:4] != b"%PDF":
        input_pdf.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件内容不是有效 PDF。")

    with jobs_lock:
        jobs[job_id] = Job(id=job_id, status="queued", message="PDF 已上传，等待处理。")
    background_tasks.add_task(lambda: executor.submit(process_job, job_id, input_pdf, theme))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在。")
        return {
            "job_id": job.id,
            "status": job.status,
            "label": STATUS_LABELS.get(job.status, job.status),
            "message": job.error or job.message,
            "url": job.url,
        }


def process_job(job_id: str, input_pdf: Path, theme: str) -> None:
    try:
        set_job(job_id, "extracting", "正在从 PDF 抽取正文、标题和段落。")
        paragraphs = extract_pdf_paragraphs(input_pdf)
        if not paragraphs:
            raise RuntimeError("没有从 PDF 中抽取到可翻译文本。")

        set_job(job_id, "verifying", "正在核验 DOI、期刊名称和出版信息。")
        metadata = llm_extract_metadata(paragraphs)
        verified = verify_publication(metadata)
        metrics = lookup_metrics(verified)

        set_job(job_id, "translating", "正在逐段翻译全文，保留原文为主。")
        translations = translate_paragraphs(paragraphs)
        metadata = llm_refine_metadata(metadata, verified, metrics, paragraphs, translations)

        set_job(job_id, "rendering", "正在生成双语阅读页面并更新目录。")
        paper_no = next_paper_number()
        paper_file = f"paper{paper_no:02d}.html"
        save_original_pdf(input_pdf, paper_no)
        page_html = render_paper_html(paper_no, theme, metadata, verified, metrics, paragraphs, translations)
        (REPO / paper_file).write_text(page_html, encoding="utf-8")
        update_index(paper_no, theme, paper_file, metadata, verified, metrics)

        set_job(job_id, "deploying", "正在提交静态页面变更。")
        commit_and_maybe_push(paper_no, metadata)

        url = f"{PUBLIC_URL}/{paper_file}"
        set_job(job_id, "complete", "新论文页面已生成并提交部署。", url=url)
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "failed"
            job.error = str(exc)


def prepare_pdf_translation(input_pdf: Path, progress_cb: Any | None = None, fallback_title: str | None = None) -> dict[str, Any]:
    paragraphs = extract_pdf_paragraphs(input_pdf)
    if not paragraphs:
        raise RuntimeError("没有从 PDF 中抽取到可翻译文本。")

    metadata = llm_extract_metadata(paragraphs)
    verified = verify_publication(metadata)
    metrics = lookup_metrics(verified)
    translations = translate_paragraphs(paragraphs, progress_cb=progress_cb)
    metadata = llm_refine_metadata(metadata, verified, metrics, paragraphs, translations)
    apply_fallback_title(metadata, verified, fallback_title)
    return {
        "paragraphs": paragraphs,
        "metadata": metadata,
        "verified": verified,
        "metrics": metrics,
        "translations": translations,
    }


def publish_pdf_translation(input_pdf: Path, theme: str, prepared: dict[str, Any]) -> str:
    paper_no = next_paper_number()
    paper_file = f"paper{paper_no:02d}.html"
    save_original_pdf(input_pdf, paper_no)
    metadata = prepared["metadata"]
    verified = prepared["verified"]
    metrics = prepared["metrics"]
    paragraphs = prepared["paragraphs"]
    translations = prepared["translations"]
    page_html = render_paper_html(paper_no, theme, metadata, verified, metrics, paragraphs, translations)
    (REPO / paper_file).write_text(page_html, encoding="utf-8")
    update_index(paper_no, theme, paper_file, metadata, verified, metrics)
    commit_and_maybe_push(paper_no, metadata)
    return f"{PUBLIC_URL}/{paper_file}"


def translate_pdf_to_site(
    input_pdf: Path,
    theme: str,
    progress_cb: Any | None = None,
    display_name: str | None = None,
) -> str:
    fallback = display_name or input_pdf.name
    prepared = prepare_pdf_translation(input_pdf, progress_cb=progress_cb, fallback_title=fallback)
    return publish_pdf_translation(input_pdf, theme, prepared)


def apply_fallback_title(metadata: dict[str, Any], verified: dict[str, Any], fallback_title: str | None) -> None:
    title = text_value(metadata.get("title_original") or verified.get("title")).strip()
    if title and not re.fullmatch(r"paper\s*\d+", title, flags=re.I):
        return
    if fallback_title:
        metadata["title_original"] = Path(fallback_title).name
        metadata["title_zh"] = Path(fallback_title).name


def extract_pdf_paragraphs(pdf_path: Path) -> list[str]:
    doc = fitz.open(pdf_path)
    paragraphs: list[str] = []
    for page in doc:
        blocks = page.get_text("blocks")
        blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
        for block in blocks:
            text = normalize_text(block[4])
            if is_useful_paragraph(text):
                paragraphs.append(text)
    doc.close()
    return merge_short_lines(paragraphs)


def normalize_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_useful_paragraph(text: str) -> bool:
    if len(text) < 12:
        return False
    if re.fullmatch(r"[\d\s.,;:()\-–—]+", text):
        return False
    lower = text.lower()
    if lower.startswith(("http://", "https://")):
        return False
    return True


def merge_short_lines(paragraphs: list[str]) -> list[str]:
    merged: list[str] = []
    buf = ""
    for text in paragraphs:
        if len(text) < 80 and not re.match(r"^(abstract|introduction|\d+\.?\s+[A-Z])", text, re.I):
            buf = f"{buf} {text}".strip()
            continue
        if buf:
            merged.append(buf)
            buf = ""
        merged.append(text)
    if buf:
        merged.append(buf)
    return merged


def llm_key() -> str:
    key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY 环境变量。请检查 ~/.claude/settings.json 中的 ANTHROPIC_AUTH_TOKEN。")
    return key


def _make_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=llm_key(), base_url=BASE_URL)


def llm_chat(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_messages = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    system_text = "\n\n".join(system_parts)

    client = _make_anthropic_client()
    try:
        with hard_timeout(LLM_TIMEOUT_SECONDS):
            kwargs: dict[str, Any] = {
                "model": MODEL,
                "max_tokens": 8192,
                "temperature": temperature,
                "messages": user_messages,
            }
            if system_text:
                kwargs["system"] = system_text
            response = client.messages.create(**kwargs)
            return response.content[0].text.strip()
    except TimeoutError:
        raise RuntimeError(f"LLM request timed out after {LLM_TIMEOUT_SECONDS} seconds.")


class hard_timeout:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.previous: Any = None

    def __enter__(self) -> None:
        self.previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._raise_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.previous)

    @staticmethod
    def _raise_timeout(signum: int, frame: Any) -> None:
        raise TimeoutError()


def chat_json(messages: list[dict[str, str]], temperature: float = 0.1) -> Any:
    content = llm_chat(messages, temperature=temperature)
    try:
        return json.loads(extract_json_text(content))
    except Exception:
        log_llm_response(content)
        raise


def chat_text(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    return llm_chat(messages, temperature=temperature)


def log_llm_response(content: str) -> None:
    try:
        debug_dir = REPO / ".upload_jobs"
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / "last_llm_response.txt").write_text(content[:20000], encoding="utf-8")
    except OSError:
        pass


def extract_json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return text[start:end + 1]
    raise ValueError("LLM did not return valid JSON.")


def llm_extract_metadata(paragraphs: list[str]) -> dict[str, Any]:
    sample = "\n\n".join(paragraphs[:35])[:24000]
    try:
        data = chat_json([
            {"role": "system", "content": (
                "You extract metadata from academic papers. Return JSON only. "
                "Do not guess. Unknown values must be null or empty arrays. "
                "Never invent DOI, journal, ranking, quartile, impact factor, or publication venue."
            )},
            {"role": "user", "content": (
                "Extract this schema: title_original, title_zh, authors, venue, year, doi, "
                "abstract_original, abstract_zh, keywords_original, keywords_zh, one_sentence_zh, source_language. "
                "Paper text:\n" + sample
            )},
        ])
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def translate_paragraphs(paragraphs: list[str], progress_cb: Any | None = None) -> list[str]:
    translations: list[str] = []
    for i in range(0, len(paragraphs), 6):
        chunk = paragraphs[i:i + 6]
        if progress_cb:
            progress = 42 + int((i / max(len(paragraphs), 1)) * 36)
            progress_cb(progress, f"正在翻译第 {i + 1}-{min(i + len(chunk), len(paragraphs))} 段，共 {len(paragraphs)} 段。")
        try:
            data = chat_json([
                {"role": "system", "content": (
                    "Translate academic paper content into Simplified Chinese for a bilingual reading page. "
                    "Translate titles, abstracts, headings, prose paragraphs, captions, and keywords into natural Chinese. "
                    "Keep URLs, emails, DOI strings, author affiliations, copyright/license notices, citation boilerplate, "
                    "bibliography/reference entries, formulas, numbers, and proper nouns unchanged where appropriate. "
                    "If an item is not meaningful prose to translate, copy it unchanged. "
                    "Return JSON with key translations, an array with exactly the same length and order as input paragraphs."
                )},
                {"role": "user", "content": json.dumps({"paragraphs": chunk}, ensure_ascii=False)},
            ], temperature=0.2)
        except Exception:
            data = {}
        arr = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(arr, list) or len(arr) != len(chunk) or not translations_pass_quality_gate(chunk, arr):
            arr = [translate_one_text(p) for p in chunk]
        translations.extend(sanitize_translations(chunk, arr))
    return translations


def translate_one(text: str) -> str:
    try:
        data = chat_json([
            {"role": "system", "content": "Translate the academic paragraph to Simplified Chinese. Return JSON {\"translation\":\"...\"}."},
            {"role": "user", "content": text},
        ], temperature=0.2)
        return str(data.get("translation") or text)
    except Exception:
        translated = chat_text([
            {"role": "system", "content": "Translate the academic paragraph to Simplified Chinese. Return only the translated paragraph, no JSON."},
            {"role": "user", "content": text},
        ], temperature=0.2)
        return translated or text


def translate_one_text(text: str) -> str:
    if not should_translate_to_chinese(text):
        return text
    try:
        translated = chat_text([
            {"role": "system", "content": (
                "Translate this academic paper text into Simplified Chinese for a bilingual reading page. "
                "Translate prose, headings, captions, keywords, and titles. "
                "Keep citations, formulas, numbers, DOI, URLs, emails, and proper nouns unchanged where appropriate. "
                "Return only the translated paragraph, no JSON."
            )},
            {"role": "user", "content": text},
        ], temperature=0.2)
        if translated and translation_passes_quality_gate(text, translated):
            return translated
    except Exception:
        pass
    try:
        data = chat_json([
            {"role": "system", "content": (
                "Translate this academic paper text into Simplified Chinese. "
                "The output must contain Chinese for prose, headings, captions, keywords, and titles. "
                "Keep citations, formulas, numbers, DOI, URLs, emails, and proper nouns unchanged where appropriate. "
                "Return JSON only: {\"translation\":\"...\"}."
            )},
            {"role": "user", "content": text},
        ], temperature=0.1)
        translated = str(data.get("translation") or "")
        if translated and translation_passes_quality_gate(text, translated):
            return translated
    except Exception:
        pass
    if should_translate_to_chinese(text):
        return f"【自动翻译失败，保留原文】{text}"
    return text


def translations_pass_quality_gate(sources: list[str], translations: list[Any]) -> bool:
    return all(translation_passes_quality_gate(src, str(dst)) for src, dst in zip(sources, translations))


def sanitize_translations(sources: list[str], translations: list[Any]) -> list[str]:
    sanitized: list[str] = []
    for source, translated in zip(sources, translations):
        text = str(translated)
        if translation_passes_quality_gate(source, text):
            sanitized.append(text)
        elif should_translate_to_chinese(source):
            sanitized.append(f"【自动翻译失败，保留原文】{source}")
        else:
            sanitized.append(source)
    return sanitized


def translation_passes_quality_gate(source: str, translated: str) -> bool:
    if not should_translate_to_chinese(source):
        return True
    return cjk_count(translated) >= max(4, min(20, ascii_word_count(source) // 8))


def should_translate_to_chinese(text: str) -> bool:
    text = normalize_text(text)
    if not text or cjk_count(text) >= 4:
        return False
    if re.match(r"^\d+(\.\d+)*\.?\s+[A-Za-z]", text):
        return True
    words = ascii_word_count(text)
    if words < 4:
        return False
    lower = text.lower()
    if re.search(r"https?://|\b[\w.+-]+@[\w.-]+\.\w+\b", text):
        return False
    boilerplate_prefixes = (
        "design research society",
        "drs biennial conference series",
        "follow this and additional works at",
        "part of the ",
        "citation citation",
        "this research paper is brought to you",
        "this work is licensed under",
    )
    if lower.startswith(boilerplate_prefixes):
        return False
    if looks_like_reference_entry(text):
        return False
    if looks_like_affiliation_or_author_line(text):
        return False
    if lower.startswith(("abstract:", "keywords:", "figure ", "table ")):
        return True
    if len(text) < 140 and words >= 4:
        return True
    return has_sentence_punctuation(text)


def looks_like_reference_entry(text: str) -> bool:
    if re.match(r"^\[\d+\]\s+", text):
        return True
    if re.match(r"^\d+\.\s+[A-Z][A-Za-z-]+,\s+[A-Z]", text):
        return True
    return False


def looks_like_affiliation_or_author_line(text: str) -> bool:
    lower = text.lower()
    if len(text) > 180 or has_sentence_punctuation(text):
        return False
    affiliation_terms = (
        "university", "institute", "laboratory", "department", "school of",
        "college", "monash", "london", "australia", "united kingdom",
        "china", "usa",
    )
    return any(term in lower for term in affiliation_terms)


def has_sentence_punctuation(text: str) -> bool:
    return bool(re.search(r"[.!?。！？][\"')\]]?(?:\s|$)", text))


def ascii_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z\-]{1,}", text))


def cjk_count(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def llm_refine_metadata(
    metadata: dict[str, Any],
    verified: dict[str, Any],
    metrics: dict[str, Any] | None,
    paragraphs: list[str],
    translations: list[str],
) -> dict[str, Any]:
    try:
        data = chat_json([
            {"role": "system", "content": (
                "You prepare metadata for a bilingual academic reading site. "
                "Return JSON only. Do not invent publication facts. "
                "Ranking, quartile, impact factor, core-journal and top-percentile claims may only use the provided verified_metrics object. "
                "If verified_metrics is null, ranking_note_zh must be '期刊/会议分区与排名未核验'."
            )},
            {"role": "user", "content": json.dumps({
                "extracted_metadata": metadata,
                "verified_publication": verified,
                "verified_metrics": metrics,
                "first_original_paragraphs": paragraphs[:12],
                "first_translated_paragraphs": translations[:12],
                "schema": [
                    "title_original", "title_zh", "authors_display", "venue_display", "year",
                    "doi", "abstract_original", "abstract_zh", "keywords_zh", "keywords_original",
                    "one_sentence_zh", "ranking_note_zh", "badges"
                ],
            }, ensure_ascii=False)},
        ])
    except Exception:
        data = {}
    if isinstance(data, dict):
        return {**metadata, **data}
    return metadata


def verify_publication(metadata: dict[str, Any]) -> dict[str, Any]:
    doi = clean_doi(metadata.get("doi"))
    if doi:
        crossref = crossref_by_doi(doi)
        if crossref:
            return crossref

    title = str(metadata.get("title_original") or "").strip()
    if title:
        crossref = crossref_by_title(title)
        if crossref:
            return crossref

    return {
        "source": "pdf_llm_extraction_only",
        "verified": False,
        "title": metadata.get("title_original"),
        "venue": metadata.get("venue"),
        "year": metadata.get("year"),
        "doi": doi,
    }


def clean_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    match = re.search(r"10\.\d{4,9}/\S+", text)
    return match.group(0).rstrip(".,);") if match else None


def crossref_by_doi(doi: str) -> dict[str, Any] | None:
    try:
        res = requests.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        if res.status_code != 200:
            return None
        return parse_crossref_message(res.json().get("message", {}))
    except requests.RequestException:
        return None


def crossref_by_title(title: str) -> dict[str, Any] | None:
    try:
        res = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 1},
            timeout=15,
        )
        if res.status_code != 200:
            return None
        items = res.json().get("message", {}).get("items", [])
        if not items:
            return None
        return parse_crossref_message(items[0])
    except requests.RequestException:
        return None


def parse_crossref_message(msg: dict[str, Any]) -> dict[str, Any]:
    authors = []
    for author in msg.get("author", [])[:8]:
        name = " ".join(x for x in [author.get("given"), author.get("family")] if x)
        if name:
            authors.append(name)
    year = None
    for key in ("published-print", "published-online", "issued"):
        parts = msg.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            year = parts[0][0]
            break
    return {
        "source": "Crossref",
        "verified": True,
        "title": first(msg.get("title")),
        "venue": first(msg.get("container-title")),
        "publisher": msg.get("publisher"),
        "year": year,
        "doi": msg.get("DOI"),
        "type": msg.get("type"),
        "authors": authors,
        "url": msg.get("URL"),
    }


def first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def lookup_metrics(verified: dict[str, Any]) -> dict[str, Any] | None:
    try:
        metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    venue = str(verified.get("venue") or "").lower().strip()
    doi = str(verified.get("doi") or "").lower().strip()
    for item in metrics.get("venues", []):
        names = [str(x).lower().strip() for x in item.get("names", [])]
        dois = [str(x).lower().strip() for x in item.get("dois", [])]
        if (venue and venue in names) or (doi and doi in dois):
            return item
    return None


def next_paper_number() -> int:
    nums = []
    for path in REPO.glob("paper*.html"):
        match = re.fullmatch(r"paper(\d+)\.html", path.name)
        if match:
            nums.append(int(match.group(1)))
    return max(nums or [0]) + 1


def save_original_pdf(input_pdf: Path, paper_no: int) -> None:
    target_dir = REPO / "assets" / "papers" / f"paper{paper_no:02d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_pdf = target_dir / "original.pdf"
    if input_pdf.resolve() != target_pdf.resolve():
        shutil.copy2(input_pdf, target_pdf)
    render_pdf_page_images(input_pdf, target_dir)


def render_pdf_page_images(input_pdf: Path, target_dir: Path) -> None:
    pages_dir = target_dir / "pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(input_pdf)
    try:
        matrix = fitz.Matrix(1.5, 1.5)
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            image.save(pages_dir / f"page-{index:03d}.webp", "WEBP", quality=82, method=4)
    finally:
        doc.close()


def render_paper_html(
    paper_no: int,
    theme: str,
    metadata: dict[str, Any],
    verified: dict[str, Any],
    metrics: dict[str, Any] | None,
    paragraphs: list[str],
    translations: list[str],
) -> str:
    paper_id = f"paper{paper_no:02d}"
    title_original = text_value(metadata.get("title_original") or verified.get("title") or f"Paper {paper_no:02d}")
    title_zh = text_value(metadata.get("title_zh") or title_original)
    venue = text_value(metadata.get("venue_display") or verified.get("venue") or "Venue not verified")
    year = text_value(metadata.get("year") or verified.get("year") or "Year not verified")
    authors = text_value(metadata.get("authors_display") or ", ".join(verified.get("authors") or []) or "Authors not verified")
    doi = clean_doi(metadata.get("doi") or verified.get("doi"))
    keywords_zh = list_value(metadata.get("keywords_zh"))[:5]
    keywords_original = list_value(metadata.get("keywords_original"))[:5]
    abstract_original = text_value(metadata.get("abstract_original") or first_abstract(paragraphs))
    abstract_zh = text_value(metadata.get("abstract_zh") or first_abstract(translations))

    nav = render_nav(paper_no)
    doi_span = f'<span><a href="https://doi.org/{esc_attr(doi)}" target="_blank">DOI: {esc(doi)}</a></span>' if doi else '<span>DOI 未核验</span>'
    ranking_items = render_ranking_items(metrics, metadata)
    sections = sectionize(paragraphs, translations)
    section_html = "\n".join(render_section(sec) for sec in sections)
    original_pdf = f"assets/papers/{paper_id}/original.pdf"
    page_images_html = render_pdf_page_images_html(paper_id)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(f'{paper_no:02d} · {title_original}')}</title>
<link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="reader.css">
</head>
<body data-paper-id="{paper_id}">
{nav}

<div class="paper-hero">
  <div class="paper-num">Paper {paper_no:02d} &middot; {esc(theme)} &middot; {esc(venue)} {esc(str(year))}</div>
  <h1>{esc(title_original)}</h1>
  <h2>{esc(title_zh)}</h2>
  <div class="paper-meta">
    <span>{esc(authors)}</span>
    <span>{esc(venue)}</span><span>{esc(str(year))}</span>
    {doi_span}
    <span><a href="{esc_attr(original_pdf)}" target="_blank">Original PDF</a></span>
  </div>
  <div class="ranking-hero-box">
    {ranking_items}
  </div>
</div>

<div class="container">
<a class="back-btn" href="index.html">&larr; Back to Index</a>

<div class="abstract-card">
  <div class="abstract-title">Abstract &middot; 摘要</div>
  <div class="bilingual-cols source-first">
    <div class="lang-col en"><span class="lang-label en">Original</span>
      <p>{esc(abstract_original)}</p>
      {render_keywords("Keywords", keywords_original)}
    </div>
    <div class="lang-col zh"><span class="lang-label zh">中文翻译</span>
      <p>{esc(abstract_zh)}</p>
      {render_keywords("关键词", keywords_zh)}
    </div>
  </div>
</div>

{page_images_html}

{section_html}

</div>

<footer>
  <p>设计研究文献库 &middot; 医疗AI可解释性设计研究</p>
  <p style="margin-top:0.3rem;opacity:0.5;font-size:0.75rem">Publication metadata verified by {esc(verified.get("source") or "local extraction")} where available.</p>
</footer>
<script src="reader.js"></script>
</body></html>
"""


def render_nav(active_no: int) -> str:
    nums = sorted(
        int(m.group(1))
        for p in REPO.glob("paper*.html")
        if (m := re.fullmatch(r"paper(\d+)\.html", p.name))
    )
    if active_no not in nums:
        nums.append(active_no)
        nums.sort()
    links = ['<a href="index.html">目录</a>']
    for n in nums:
        cls = ' class="active"' if n == active_no else ""
        links.append(f'<a href="paper{n:02d}.html"{cls}>{n:02d}</a>')
    return f"""<nav>
  <a class="brand" href="index.html"><span class="brand-accent">&#9670;</span> 设计研究文献库</a>
  <div class="nav-links">
    {''.join(links)}
  </div>
</nav>"""


def render_ranking_items(metrics: dict[str, Any] | None, metadata: dict[str, Any]) -> str:
    if not metrics:
        return '<span class="rh-item">期刊/会议分区与排名未核验</span>'
    items = []
    for value in list_value(metrics.get("badges")):
        items.append(f'<span class="rh-item rh-blue">{esc(value)}</span>')
    for key in ("impact_factor", "quartile", "ranking", "top_percentile", "core"):
        value = metrics.get(key)
        if value:
            cls = "rh-green" if key in {"quartile", "top_percentile"} else "rh-accent"
            items.append(f'<span class="rh-item {cls}">{esc(str(value))}</span>')
    return "\n    ".join(items) or '<span class="rh-item">期刊/会议分区与排名未核验</span>'


def render_keywords(label: str, words: list[str]) -> str:
    if not words:
        return ""
    return "<p><strong>{}:</strong> {}</p>".format(esc(label), esc("；".join(words)))


def sectionize(paragraphs: list[str], translations: list[str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current = {"title": "Full Text · 全文", "pairs": []}
    heading_re = re.compile(
        r"^(\d+(\.\d+)*\.?\s+)?(abstract|introduction|background|methods?|methodology|results?|discussion|conclusion|references|acknowledg|appendix)\b",
        re.I,
    )
    for original, translated in zip(paragraphs, translations):
        if heading_re.match(original) and len(original) < 140 and current["pairs"]:
            sections.append(current)
            current = {"title": f"{original} · {translated}", "pairs": []}
            continue
        current["pairs"].append((original, translated))
    if current["pairs"]:
        sections.append(current)
    return sections


def render_section(section: dict[str, Any]) -> str:
    original_paragraphs = "\n        ".join(f"<p>{esc(original)}</p>" for original, _ in section["pairs"])
    translated_paragraphs = "\n        ".join(f"<p>{esc(translated)}</p>" for _, translated in section["pairs"])
    return f"""<div class="content-card">
  <div class="bilingual-block">
    <div class="section-title">{esc(section["title"])}</div>
    <div class="bilingual-cols source-first">
      <div class="lang-col en"><span class="lang-label en">Original</span>
        {original_paragraphs}
      </div>
      <div class="lang-col zh"><span class="lang-label zh">中文翻译</span>
        {translated_paragraphs}
      </div>
    </div>
  </div>
</div>"""


def render_pdf_page_images_html(paper_id: str) -> str:
    pages_dir = REPO / "assets" / "papers" / paper_id / "pages"
    images = sorted(pages_dir.glob("page-*.webp"))
    if not images:
        return ""
    items = "\n".join(
        f'''    <figure class="pdf-page-figure">
      <img src="{esc_attr(f"assets/papers/{paper_id}/pages/{image.name}")}" alt="{esc_attr(f"{paper_id} page {idx}")}" loading="lazy">
      <figcaption>Page {idx}</figcaption>
    </figure>'''
        for idx, image in enumerate(images, start=1)
    )
    return f"""<details class="content-card pdf-pages-card">
  <summary>
    <span>Original Layout · 原始版面</span>
    <small>{len(images)} pages · 展开查看图、表和公式</small>
  </summary>
  <div class="pdf-pages-body">
    <p class="pdf-pages-note">以下为 PDF 原始页面渲染图，用于核对图、表、公式和复杂版面；正文翻译在下方继续。</p>
    <div class="pdf-page-list">
{items}
    </div>
  </div>
</details>"""


def update_index(
    paper_no: int,
    theme: str,
    paper_file: str,
    metadata: dict[str, Any],
    verified: dict[str, Any],
    metrics: dict[str, Any] | None,
) -> None:
    path = REPO / "index.html"
    content = path.read_text(encoding="utf-8")
    title_original = text_value(metadata.get("title_original") or verified.get("title") or f"Paper {paper_no:02d}")
    title_zh = text_value(metadata.get("title_zh") or title_original)
    venue = text_value(metadata.get("venue_display") or verified.get("venue") or "Venue not verified")
    year = text_value(metadata.get("year") or verified.get("year") or "Year not verified")
    authors = text_value(metadata.get("authors_display") or ", ".join(verified.get("authors") or []) or "Authors not verified")
    keywords = list_value(metadata.get("keywords_zh"))[:5] or ["关键词待核验"]
    ranking = text_value(metadata.get("ranking_note_zh") or "期刊/会议分区与排名未核验")
    desc = text_value(metadata.get("one_sentence_zh") or "全文已生成双语阅读页面，出版信息以页面内核验结果为准。")
    badges = render_card_badges(metrics)
    tags = "\n      ".join(
        f'<span class="tag tag-{(i % 5) + 1}">{esc(word)}</span>' for i, word in enumerate(keywords)
    )
    card = f"""

  <a class="card" href="{esc_attr(paper_file)}">
    <div class="card-num">Paper {paper_no:02d}</div>
    <h3>{esc(title_zh)}</h3>
    <h4>{esc(title_original)}</h4>
    <div class="card-journal">
      {badges}
      {esc(authors)} &middot; {esc(venue)} &middot; {esc(str(year))}
    </div>
    <div class="ranking-box">
      {esc(ranking)}
    </div>
    <div class="card-tags">
      {tags}
    </div>
    <p class="card-desc">{esc(desc)}</p>
  </a>
"""

    content = ensure_theme_section(content, theme)
    content = insert_card_into_theme(content, theme, card)
    content = update_index_counts(content, theme)
    path.write_text(content, encoding="utf-8")


def render_card_badges(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return '<span class="journal-badge q2">未核验排名</span>'
    parts = []
    if metrics.get("impact_factor"):
        parts.append(f'<span class="journal-badge if-score">{esc(str(metrics["impact_factor"]))}</span>')
    if metrics.get("quartile"):
        parts.append(f'<span class="journal-badge q1">{esc(str(metrics["quartile"]))}</span>')
    for badge in list_value(metrics.get("badges"))[:2]:
        parts.append(f'<span class="journal-badge top-conf">{esc(badge)}</span>')
    return "\n      ".join(parts) or '<span class="journal-badge q2">未核验排名</span>'


def update_index_counts(content: str, theme: str) -> str:
    total = len(list(REPO.glob("paper*.html")))
    content = re.sub(r"\d+ papers &middot;", f"{total} papers &middot;", content, count=1)
    content = re.sub(r"<p style=\"margin-top:0\.4rem;opacity:0\.5;font-size:0\.75rem\">\d+ papers", f'<p style="margin-top:0.4rem;opacity:0.5;font-size:0.75rem">{total} papers', content, count=1)
    return content


def ensure_theme_section(content: str, theme: str) -> str:
    if theme != "新主题" or "Theme 3 &middot; 新主题" in content:
        return content
    insert = """
<div class="section-divider"><hr></div>

<div class="section-header">
  <h2>Theme 3 &middot; 新主题</h2>
  <span class="section-label">New Theme</span>
</div>

<div class="card-grid">
</div>
"""
    return content.replace("\n<footer>", insert + "\n<footer>", 1)


def insert_card_into_theme(content: str, theme: str, card: str) -> str:
    header = {
        "设计介入跨学科协作": "Theme 1 &middot; 设计介入跨学科协作",
        "设计转译": "Theme 2 &middot; 设计转译",
        "新主题": "Theme 3 &middot; 新主题",
    }[theme]
    start = content.find(header)
    if start < 0:
        return content.replace("\n<footer>", card + "\n<footer>", 1)
    grid_start = content.find('<div class="card-grid">', start)
    if grid_start < 0:
        return content.replace("\n<footer>", card + "\n<footer>", 1)
    grid_end = find_matching_grid_end(content, grid_start)
    if grid_end < 0:
        return content.replace("\n<footer>", card + "\n<footer>", 1)
    return content[:grid_end] + card + content[grid_end:]


def find_matching_grid_end(content: str, grid_start: int) -> int:
    depth = 0
    pattern = re.compile(r"</?div\b[^>]*>", re.I)
    for match in pattern.finditer(content, grid_start):
        tag = match.group(0)
        if tag.startswith("</"):
            depth -= 1
            if depth == 0:
                return match.start()
        else:
            depth += 1
    return -1


def commit_and_maybe_push(paper_no: int, metadata: dict[str, Any]) -> None:
    subprocess.run(["git", "add", "-A"], cwd=REPO, check=True)
    title = text_value(metadata.get("title_original") or f"paper{paper_no:02d}")
    msg = f"Add translated paper {paper_no:02d}: {title[:80]}"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, text=True, capture_output=True, check=True)
    if not status.stdout.strip():
        return
    subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
    if GIT_PUSH:
        git_push()


def git_push() -> None:
    git_push_branch(REPO, "main")


def git_push_branch(repo: Path, branch: str) -> None:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        subprocess.run(["git", "push", "origin", branch], cwd=repo, check=True)
        return

    with tempfile.TemporaryDirectory(prefix="papers-git-") as tmpdir:
        askpass = Path(tmpdir) / "askpass.sh"
        askpass.write_text(
            "#!/usr/bin/env sh\n"
            "case \"$1\" in\n"
            "*Username*) printf '%s\\n' \"x-access-token\" ;;\n"
            "*Password*) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
            "*) printf '\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = os.environ.copy()
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.run(["git", "push", "origin", branch], cwd=repo, env=env, check=True)


def first_abstract(items: list[str]) -> str:
    for item in items[:20]:
        if len(item) > 80:
            return item
    return items[0] if items else ""


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x)
    return str(value)


def list_value(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[;,；，]", value) if x.strip()]
    return [str(value)]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def esc_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)
