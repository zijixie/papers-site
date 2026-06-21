#!/usr/bin/env python3
"""
Git-pull watcher: every PAPERS_POLL_SECONDS, pulls the repo and processes any
PDFs in pending/. PDF filenames encode the theme:
  {slug}_{timestamp}_{safe_name}.pdf
  slug: theme1 = 设计介入跨学科协作, theme2 = 设计转译, theme3 = 新主题
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PENDING_DIR = REPO / "pending"
TEMP_DIR = REPO / ".upload_jobs"
POLL_SECONDS = int(os.getenv("PAPERS_POLL_SECONDS", "5"))

THEME_SLUGS: dict[str, str] = {
    "theme1": "设计介入跨学科协作",
    "theme2": "设计转译",
    "theme3": "新主题",
}


def load_dotenv() -> None:
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings_json() -> None:
    """Force-load LLM credentials from ~/.claude/settings.json (overrides .env)."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print("[watcher] ~/.claude/settings.json not found, using .env for LLM config", flush=True)
        return
    try:
        import re
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
        print(
            f"[watcher] LLM config: base_url={env.get('ANTHROPIC_BASE_URL')}, "
            f"model={os.environ.get('LLM_MODEL')}",
            flush=True,
        )
    except Exception as e:
        print(f"[watcher] Could not read settings.json: {e}", flush=True)


def git_pull() -> None:
    token = os.getenv("GITHUB_TOKEN")
    env = os.environ.copy()
    if token:
        with tempfile.TemporaryDirectory(prefix="papers-git-") as tmpdir:
            askpass = Path(tmpdir) / "askpass.sh"
            askpass.write_text(
                "#!/usr/bin/env sh\n"
                "case \"$1\" in\n"
                "*Username*) printf 'x-access-token\\n' ;;\n"
                "*Password*) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
                "*) printf '\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
    else:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
    if result.returncode != 0 and "Already up to date" not in result.stdout:
        print(f"[watcher] git pull: {result.stderr.strip() or result.stdout.strip()}", flush=True)


def extract_theme_and_name(stem: str) -> tuple[str, str]:
    """Return (theme, display_name) from pending filename stem."""
    for slug, theme in THEME_SLUGS.items():
        prefix = slug + "_"
        if stem.startswith(prefix):
            rest = stem[len(prefix):]
            # Remove leading timestamp digits and underscore: 1234567890_OriginalName
            parts = rest.split("_", 1)
            if len(parts) == 2 and parts[0].isdigit():
                rest = parts[1]
            # Replace underscores with spaces for display
            display = rest.replace("_", " ").replace(".pdf", "")
            return theme, display
    return "新主题", stem.replace("_", " ")


def process_pending(server: object) -> None:
    pdfs = sorted(PENDING_DIR.glob("*.pdf"))
    if not pdfs:
        return

    for pdf_path in pdfs:
        theme, display_name = extract_theme_and_name(pdf_path.stem)
        print(f"[watcher] Found: {pdf_path.name} → theme: {theme}, title: {display_name}", flush=True)

        # Copy to gitignored temp dir so we can delete the pending file
        TEMP_DIR.mkdir(exist_ok=True)
        temp_pdf = TEMP_DIR / f"proc_{int(time.time())}_{pdf_path.name}"
        shutil.copy2(pdf_path, temp_pdf)

        # Delete pending file now so git add -A (in commit_and_maybe_push) picks up the deletion
        pdf_path.unlink()

        try:
            print(f"[watcher] Translating: {temp_pdf.name}", flush=True)
            server.translate_pdf_to_site(temp_pdf, theme, display_name=display_name)  # commits + pushes
            print(f"[watcher] Done: {pdf_path.name}", flush=True)
        except Exception as exc:
            print(f"[watcher] Error processing {pdf_path.name}: {exc}", flush=True)
            # Re-add the pending file so it can be retried after a fix
            try:
                shutil.copy2(temp_pdf, pdf_path)
            except Exception:
                pass
        finally:
            temp_pdf.unlink(missing_ok=True)


def main() -> int:
    load_dotenv()
    load_settings_json()

    # Import server AFTER env vars are set (BASE_URL and MODEL are read at module import)
    sys.path.insert(0, str(REPO))
    from backend import server  # noqa: PLC0415

    PENDING_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    print(f"[watcher] Started. Polling every {POLL_SECONDS}s for PDFs in pending/", flush=True)
    while True:
        try:
            git_pull()
            process_pending(server)
        except Exception as exc:
            print(f"[watcher] Unexpected error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
