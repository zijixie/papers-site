#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from backend import server


QUEUE_BRANCH = os.getenv("PAPERS_QUEUE_BRANCH", "upload-queue")
QUEUE_DIR = Path(os.getenv("PAPERS_QUEUE_DIR", server.REPO.parent / "papers-site-upload-queue")).resolve()
POLL_SECONDS = int(os.getenv("PAPERS_QUEUE_POLL_SECONDS", "5"))
MAX_WORKERS = int(os.getenv("PAPERS_QUEUE_WORKERS", "3"))
OWNER = os.getenv("PAPERS_GITHUB_OWNER", "zijixie")
REPO_NAME = os.getenv("PAPERS_GITHUB_REPO", "papers-site")
API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPO_NAME}"
publish_lock = threading.Lock()
queue_lock = threading.Lock()
active_jobs: set[str] = set()
active_lock = threading.Lock()


def main() -> int:
    ensure_queue_clone()
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    while True:
        try:
            sync_queue()
            for job_dir in sorted((QUEUE_DIR / "incoming").glob("job_*")):
                if job_dir.is_dir() and should_submit(job_dir.name):
                    with active_lock:
                        active_jobs.add(job_dir.name)
                    executor.submit(process_queue_job, job_dir)
        except Exception as exc:
            print(f"[queue-worker] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def should_submit(job_id: str) -> bool:
    if is_done(job_id):
        return False
    status_path = QUEUE_DIR / "status" / f"{job_id}.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")).get("status")
            if status in {"complete", "failed"}:
                return False
        except json.JSONDecodeError:
            pass
    with active_lock:
        return job_id not in active_jobs


def ensure_queue_clone() -> None:
    if (QUEUE_DIR / ".git").exists():
        return
    if QUEUE_DIR.exists():
        shutil.rmtree(QUEUE_DIR)
    remote = run(["git", "remote", "get-url", "origin"], cwd=server.REPO).stdout.strip()
    run_git(["git", "clone", "--branch", QUEUE_BRANCH, remote, str(QUEUE_DIR)], cwd=server.REPO)


def sync_queue() -> None:
    run_git(["git", "fetch", "origin", QUEUE_BRANCH], cwd=QUEUE_DIR)
    run(["git", "reset", "--hard", f"origin/{QUEUE_BRANCH}"], cwd=QUEUE_DIR)


def process_queue_job(job_dir: Path) -> None:
    job_id = job_dir.name
    job_json = job_dir / "job.json"
    input_pdf = job_dir / "input.pdf"
    if not job_json.exists() or not input_pdf.exists():
        return

    job = json.loads(job_json.read_text(encoding="utf-8"))
    theme = job.get("theme") or "新主题"
    filename = job.get("filename") or input_pdf.name

    try:
        write_status(job_id, "extracting", "抽取中", 30, "正在从 PDF 抽取正文、标题和段落。")
        sync_main()
        write_status(job_id, "translating", "翻译中", 64, "正在核验出版信息并翻译全文。")
        def progress_cb(progress: int, message: str) -> None:
            write_status(job_id, "translating", "翻译中", progress, message)

        prepared = server.prepare_pdf_translation(input_pdf, progress_cb=progress_cb, fallback_title=filename)
        write_status(job_id, "deploying", "部署中", 90, "正在生成网页并部署到 GitHub Pages。")
        with publish_lock:
            sync_main()
            url = server.publish_pdf_translation(input_pdf, theme, prepared)
        write_status(job_id, "complete", "完成", 100, "新论文页面已生成并部署。", url=url)
        mark_done(job_id)
    except Exception as exc:
        write_status(job_id, "failed", "失败", 100, str(exc))
    finally:
        with active_lock:
            active_jobs.discard(job_id)


def sync_main() -> None:
    run_git(["git", "fetch", "origin", "main"], cwd=server.REPO)
    run(["git", "reset", "--hard", "origin/main"], cwd=server.REPO)


def write_status(
    job_id: str,
    status: str,
    label: str,
    progress: int,
    message: str,
    url: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "label": label,
        "progress": progress,
        "message": message,
        "updated_at": int(time.time()),
    }
    if url:
        payload["url"] = url
    put_github_file(
        f"status/{job_id}.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        f"Update status for {job_id}: {status}",
    )


def mark_done(job_id: str) -> None:
    with queue_lock:
        done_path = QUEUE_DIR / "done" / f"{job_id}.json"
        done_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"job_id": job_id, "done_at": int(time.time())}
        done_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        put_github_file(
            f"done/{job_id}.json",
            json.dumps(payload, indent=2) + "\n",
            f"Mark {job_id} done",
        )


def is_done(job_id: str) -> bool:
    return (QUEUE_DIR / "done" / f"{job_id}.json").exists()


def put_github_file(path: str, text: str, message: str) -> None:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("缺少 GITHUB_TOKEN，无法写入队列状态。")
    url = f"{API_ROOT}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for _ in range(3):
        sha = None
        get_res = requests.get(url, headers=headers, params={"ref": QUEUE_BRANCH, "t": str(time.time())}, timeout=20)
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")
        elif get_res.status_code != 404:
            raise RuntimeError(f"GitHub status lookup failed: {get_res.status_code} {get_res.text}")

        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": QUEUE_BRANCH,
        }
        if sha:
            body["sha"] = sha
        put_res = requests.put(url, headers=headers, json=body, timeout=30)
        if put_res.status_code in (200, 201):
            return
        if put_res.status_code != 409:
            raise RuntimeError(f"GitHub status update failed: {put_res.status_code} {put_res.text}")
        time.sleep(1)
    raise RuntimeError(f"GitHub status update failed after retry: {path}")


def run(cmd: list[str], cwd: Path, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=60)
    if proc.returncode and not allow_fail:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


def run_git(cmd: list[str], cwd: Path, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return run(cmd, cwd, allow_fail=allow_fail)
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
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=env, timeout=60)
        if proc.returncode and not allow_fail:
            raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
        return proc


if __name__ == "__main__":
    raise SystemExit(main())
