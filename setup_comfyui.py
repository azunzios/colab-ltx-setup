#!/usr/bin/env python3
"""ComfyUI + provisioning dashboard for Colab/Linux; standalone, no web framework."""

import argparse
import hashlib
import hmac
import json
import math
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath


MODEL_TYPES = (
    "checkpoints", "diffusion_models", "unet", "loras", "vae", "text_encoders",
    "clip", "clip_vision", "controlnet", "upscale_models", "embeddings", "style_models",
)
MODEL_EXTENSIONS = {".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx",
                    ".pkl", ".pickle", ".torchscript", ".weights", ".tflite", ".h5", ".hdf5", ".model", ".emb"}
HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
TUNNEL_PATTERNS = {
    "cloudflare": r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
    "pinggy": r"https://[a-zA-Z0-9.-]+\.pinggy\.link",
    "localtunnel": r"https://[a-zA-Z0-9-]+\.loca\.lt",
}


def log_info(message):
    print(f"[INFO] {message}", flush=True)


def run_cmd(cmd, cwd=None, check=True):
    log_info(shlex.join(map(str, cmd)))
    return subprocess.run(cmd, cwd=cwd, check=check).returncode


def get_default_base_dir():
    return Path("/content") if Path("/content").is_dir() else Path.cwd()


def install_cloudflared(bin_dir):
    existing = shutil.which("cloudflared")
    if existing:
        return Path(existing)
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "cloudflared"
    if not target.exists():
        arch = platform.machine().lower()
        if arch not in ("aarch64", "arm64", "x86_64", "amd64"):
            raise RuntimeError(f"Arsitektur cloudflared tidak didukung: {arch}")
        suffix = "arm64" if arch in ("aarch64", "arm64") else "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{suffix}"
        temporary = target.with_suffix(".part")
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(target)
    target.chmod(0o755)
    return target


def install_comfyui(comfy_dir, install_manager=True, update=False):
    comfy_dir.parent.mkdir(parents=True, exist_ok=True)
    if not comfy_dir.exists():
        run_cmd(["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)])
    elif update:
        run_cmd(["git", "pull", "--ff-only"], cwd=comfy_dir)
    if not (comfy_dir / "main.py").is_file():
        raise RuntimeError(f"main.py tidak ditemukan di {comfy_dir}")
    run_cmd([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=comfy_dir)
    if install_manager:
        manager = comfy_dir / "custom_nodes" / "ComfyUI-Manager"
        manager.parent.mkdir(parents=True, exist_ok=True)
        if not manager.exists():
            run_cmd(["git", "clone", "https://github.com/ltdrdata/ComfyUI-Manager.git", str(manager)])
        elif update:
            run_cmd(["git", "pull", "--ff-only"], cwd=manager)
        if (manager / "requirements.txt").is_file():
            run_cmd([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=manager)


def hf_cli_ready(hf):
    """An executable left by a failed install is not necessarily a usable CLI."""
    if not hf.is_file():
        return False
    try:
        result = subprocess.run([str(hf), "auth", "login", "--help"],
                                capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and "--format" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_tools(runtime):
    """Isolate the modern hf CLI so upgrading it cannot break ComfyUI packages."""
    for command in ("git", "wget", "nohup"):
        if not shutil.which(command):
            raise RuntimeError(f"Perintah {command} belum tersedia. Instal dahulu (apt-get install {command}).")
    tools = runtime / "hf-tools"
    hf = tools / "bin" / "hf"
    if hf_cli_ready(hf):
        return str(hf)

    # Colab's system Python can have venv but a missing/broken ensurepip.
    # Reuse partial environments without deleting any downloads or credentials.
    log_info("Menyiapkan/memperbaiki HF tools tanpa ensurepip...")
    run_cmd([sys.executable, "-m", "venv", "--without-pip", str(tools)])
    tools_python = str(tools / "bin" / "python")
    uv = shutil.which("uv")
    if uv:
        installer = [uv, "--no-config", "pip", "install", "--python", tools_python]
    else:
        # Host pip can manage a seedless venv (pip >= 22.3); pip is not needed
        # inside hf-tools. --python keeps all HF dependencies out of ComfyUI.
        installer = [sys.executable, "-m", "pip", "--python", tools_python, "install"]
    run_cmd([*installer, "--upgrade", "huggingface_hub>=1.20.1"])
    # Device-code auth needs the new CLI output mode. No token prompt fallback.
    if not hf_cli_ready(hf):
        raise RuntimeError(f"HF CLI belum dapat dijalankan setelah instalasi di {tools}. Periksa output installer.")
    return str(hf)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    temporary.replace(path)
    path.chmod(0o600)


def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def process_identity(pid):
    """Linux start ticks distinguish a saved PID from a later, unrelated process."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def is_running(entry):
    return bool(entry and entry.get("identity") and process_identity(entry["pid"]) == entry["identity"])


def stop_process(entry):
    if not is_running(entry):
        return
    pid = entry["pid"]
    # Every managed service owns its process group (start_new_session=True).
    try:
        if os.getpgid(pid) != pid:
            raise RuntimeError(f"Menolak menghentikan PID {pid}: bukan process group milik setup.")
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while is_running(entry) and time.monotonic() < deadline:
            time.sleep(0.1)
        # Children such as npx's node process can outlive their group leader.
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_stack(runtime, state):
    # Stop the worker first so it cannot begin another queued subprocess.
    stop_process(state.get("services", {}).get("dashboard"))
    for file in (runtime / "jobs").glob("*.json"):
        job = load_json(file)
        stop_process(job.get("process"))
        if job.get("status") in ("queued", "running"):
            job.update(status="interrupted", error="Layanan dihentikan. Klik Retry untuk melanjutkan.")
            job.pop("process", None)
            save_json(file, job)
    for entry in reversed(list(state.get("services", {}).values())):
        stop_process(entry)


def launch_detached(command, cwd, log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as output:
        proc = subprocess.Popen(
            ["nohup", *map(str, command)], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    entry = {"pid": proc.pid, "identity": process_identity(proc.pid), "log": str(log_path)}
    return proc, entry


def connect_host(host):
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def port_open(host, port):
    try:
        with socket.create_connection((connect_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_service(proc, host, port, log_path, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server keluar dengan kode {proc.returncode}. Lihat {log_path}")
        if port_open(host, port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Waktu habis menunggu {host}:{port}. Lihat {log_path}")


def tail_log(path, limit=24000):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def tunnel_command(provider, host, port, cloudflared):
    address = connect_host(host)
    if provider == "cloudflare":
        return [str(cloudflared), "tunnel", "--url", f"http://{address}:{port}",
                "--http-host-header", f"{address}:{port}", "--no-autoupdate"]
    if provider == "pinggy":
        return ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30", "-p", "443", f"-R0:{address}:{port}", "a.pinggy.io"]
    return ["npx", "--yes", "localtunnel", "--port", str(port), "--local-host", address]


def wait_for_tunnel(proc, log_path, provider, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Tunnel {provider} keluar. Lihat {log_path}")
        matches = re.findall(TUNNEL_PATTERNS[provider], tail_log(log_path))
        if matches:
            return matches[-1]
        time.sleep(0.5)
    raise RuntimeError(f"URL tunnel {provider} belum tersedia. Lihat {log_path}")


def show_status(runtime, reveal_password=False):
    state = load_json(runtime / "state.json")
    if not state:
        log_info(f"Belum ada layanan di {runtime}")
        return
    for name, entry in state.get("services", {}).items():
        status = "RUNNING" if is_running(entry) else "STOPPED"
        print(f"{name}: {status} (PID {entry['pid']}) | log: {entry['log']}")
    print(f"ComfyUI    : {state.get('comfy_url', '(belum ada tunnel)')}")
    print(f"Dashboard  : {state.get('dashboard_url', '(belum ada tunnel)')}")
    print(f"Lokal      : ComfyUI :{state.get('port')} | dashboard :{state.get('dashboard_port')}")
    if reveal_password:
        print(f"Password dashboard: {state.get('password', '')}")
    print(f"State      : {runtime / 'state.json'}")


def validate_relative(value, label="Path"):
    if not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} harus berupa path relatif.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError(f"{label} tidak boleh keluar dari direktori ComfyUI.")
    return path


def within(base, relative):
    relative = validate_relative(relative)
    target = (base / str(relative)).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError("Path/symlink keluar dari direktori tujuan.")
    return target


def http_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Gunakan URL http/https tanpa username/password.")
    if any(ord(char) < 32 for char in value):
        raise ValueError("URL tidak valid.")
    return value


def hf_url_file(url):
    parsed = urllib.parse.urlsplit(http_url(url))
    if parsed.hostname not in HF_HOSTS:
        raise ValueError("Gunakan URL file dari huggingface.co.")
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 5 or parts[2] not in ("blob", "resolve"):
        raise ValueError("URL Hugging Face harus menuju satu file (blob/resolve), bukan repository atau folder tree/.")
    repo = "/".join(parts[:2])
    revision = urllib.parse.unquote(parts[3])
    filename = urllib.parse.unquote("/".join(parts[4:]))
    if parsed.path.endswith("/") or filename.endswith("/"):
        raise ValueError("URL HF harus menunjuk file, bukan folder dengan akhiran /.")
    return repo, filename, revision


def normalize_download_url(url):
    """HF preview pages are HTML; /resolve/ serves the actual file and redirects."""
    url = http_url(url)
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname in HF_HOSTS:
        repo, filename, revision = hf_url_file(url)
        path = "/" + repo + "/resolve/" + urllib.parse.quote(revision, safe="") + "/" + urllib.parse.quote(filename, safe="/")
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        query["download"] = "true"
        return urllib.parse.urlunsplit(("https", "huggingface.co", path, urllib.parse.urlencode(query), ""))
    return url


def parse_hf_source(source, filename="", revision="main"):
    """Accept owner/repo + file, an hf:// file URI, or a blob/resolve URL."""
    embedded = ""
    if source.startswith(("https://", "http://")):
        source, embedded, revision = hf_url_file(source)
    elif source.startswith("hf://"):
        parts = source[5:].split("/", 2)
        if len(parts) < 2:
            raise ValueError("URI HF harus hf://owner/repo/path/file.safetensors.")
        name, separator, ref = parts[1].partition("@")
        source = parts[0] + "/" + name
        if separator:
            revision = urllib.parse.unquote(ref)
        embedded = urllib.parse.unquote(parts[2]) if len(parts) == 3 else ""
    if embedded:
        if filename and filename != embedded:
            raise ValueError("Path file berbeda dengan file pada URL/URI HF. Kosongkan field path file.")
        filename = embedded
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", source):
        raise ValueError("Isi owner/repo, URL file Hugging Face, atau hf://owner/repo/path/file.")
    validate_relative(filename, "File di repository")
    if filename.startswith("-") or filename.endswith("/") or any(c in filename for c in "*?["):
        raise ValueError("Pilih satu file yang persis, tanpa wildcard atau folder.")
    if not revision or revision.startswith("-") or len(revision) > 200:
        raise ValueError("Revision tidak valid.")
    return source, filename, revision


def model_filename(name):
    path = validate_relative(name, "Nama file")
    if len(path.parts) != 1 or path.suffix.lower() not in MODEL_EXTENSIONS:
        raise ValueError("Pilih satu file model, misalnya .safetensors, .gguf, .ckpt, .pt, .pth, .bin, atau .onnx; bukan folder/HTML/TXT.")
    return name


def model_content_error(path):
    """Inspect only a small prefix; never read multi-GB weights into RAM."""
    if not path.is_file():
        return "Hasil download bukan file biasa."
    with path.open("rb") as handle:
        prefix = handle.read(8192)
    if not prefix:
        return "File download kosong."
    stripped = prefix.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
    if stripped.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"<error")) or b"<html" in stripped[:1024]:
        return "Hasil download adalah halaman HTML/XML, bukan model. Periksa URL dan akses repository."
    if stripped.startswith(b"version https://git-lfs.github.com/spec/"):
        return "Hasil download adalah pointer Git LFS, bukan bobot model."
    # Safetensors starts with an 8-byte length followed by a potentially huge
    # JSON header; PyTorch ZIP/pickle and GGUF also have readable metadata.
    # Such binary prefixes must not be mistaken for a plain-text response.
    utf16 = prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not utf16 and (b"\x00" in prefix or prefix.startswith((b"PK\x03\x04", b"GGUF", b"\x80"))):
        return ""
    try:
        text = prefix.decode("utf-16" if utf16 else "utf-8-sig", errors="replace")
    except UnicodeError:
        return ""
    if text and sum(char.isprintable() or char in "\r\n\t" for char in text) / len(text) > 0.98:
        return "Hasil download berupa teks/JSON, bukan file model biner. Untuk model gated/private, login dan gunakan metode Hugging Face."
    return ""


def validate_model_file(path, min_mib):
    error = model_content_error(path)
    if error:
        raise ValueError(error)
    size = path.stat().st_size
    if size < min_mib * 1024**2:
        raise ValueError(f"Ukuran file {size / 1024**2:.3f} MiB di bawah batas {min_mib:g} MiB. Jika model memang kecil, turunkan batas ukuran lalu kirim ulang.")
    return size


def find_hf_file(staging, filename):
    """Select a file explicitly; never move a returned repository directory."""
    expected = within(staging, filename)
    if expected.is_file():
        return expected
    if expected.is_dir():
        raise ValueError("HF mengembalikan folder. Pilih path satu file model, bukan folder.")
    name = PurePosixPath(filename).name
    # Also tolerate CLIs that flatten their output or add a repository prefix.
    candidates = [path for path in staging.rglob("*") if path.name == name and path.is_file()
                  and not any(part.startswith(".") for part in path.relative_to(staging).parts)]
    if len(candidates) != 1:
        raise ValueError(f"HF harus menghasilkan tepat satu file {name}; ditemukan {len(candidates)} kandidat.")
    return within(staging, candidates[0].relative_to(staging).as_posix())


class JobCancelled(Exception):
    pass


class Provisioner:
    """One background worker serializes downloads and package installs; logs live on disk."""

    def __init__(self, comfy_dir, runtime, hf_command, python_command):
        self.comfy_dir = comfy_dir.resolve()
        self.runtime = runtime
        self.hf_command = hf_command
        self.python_command = python_command
        self.jobs_dir = runtime / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancelled = set()
        self.pending = queue.Queue()
        for file in self.jobs_dir.glob("*.json"):
            job = load_json(file)
            if job["status"] in ("queued", "running"):
                if os.name == "posix":
                    stop_process(job.get("process"))
                job["status"] = "interrupted"
                job["error"] = "Dashboard berhenti. Klik Retry untuk melanjutkan."
                job.pop("process", None)
                save_json(file, job)
            self.jobs[job["id"]] = job
        threading.Thread(target=self.worker, daemon=True).start()

    def persist(self, job):
        save_json(self.jobs_dir / f"{job['id']}.json", job)

    def submit(self, data):
        kind = data.get("kind")
        payload = {"kind": kind}
        if kind in ("wget", "hf"):
            category = data.get("category", "checkpoints")
            if category not in MODEL_TYPES:
                raise ValueError("Jenis model tidak dikenal.")
            destination = f"models/{category}"
            subfolder = str(data.get("subfolder", "")).strip()
            if subfolder:
                validate_relative(subfolder, "Subfolder")
                destination += "/" + subfolder
            within(self.comfy_dir, destination)
            payload.update(category=category, subfolder=subfolder, destination=destination)
            try:
                min_mib = float(data.get("min_mib", 1))
            except (ValueError, TypeError) as exc:
                raise ValueError("Batas ukuran harus angka dalam MiB.") from exc
            if not math.isfinite(min_mib) or not 0 <= min_mib <= 1024:
                raise ValueError("Batas ukuran harus antara 0 dan 1024 MiB.")
            payload["min_mib"] = min_mib
            if kind == "wget":
                url = normalize_download_url(str(data.get("url", "")).strip())
                filename = str(data.get("filename", "")).strip() or urllib.parse.unquote(
                    PurePosixPath(urllib.parse.urlsplit(url).path).name)
                model_filename(filename)
                payload.update(url=url, filename=filename)
            else:
                repo, filename, revision = parse_hf_source(str(data.get("repo", "")).strip(),
                    str(data.get("file", "")).strip(), str(data.get("revision", "main")).strip() or "main")
                model_filename(PurePosixPath(filename).name)
                payload.update(repo=repo, file=filename, revision=revision)
        elif kind == "node":
            url = http_url(str(data.get("url", "")).strip())
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname != "github.com" or not re.fullmatch(r"/[\w.-]+/[\w.-]+(?:\.git)?/?", parsed.path):
                raise ValueError("Gunakan URL repository GitHub, contoh https://github.com/owner/node.")
            name = parsed.path.rstrip("/").split("/")[-1].removesuffix(".git")
            within(self.comfy_dir, f"custom_nodes/{name}")
            payload.update(url=url, name=name, requirements=bool(data.get("requirements", True)))
        elif kind == "requirements":
            path = str(data.get("path", "requirements.txt")).strip()
            target = within(self.comfy_dir, path)
            if not target.is_file() or target.suffix != ".txt":
                raise ValueError("File requirements .txt tidak ditemukan di ComfyUI.")
            payload["path"] = path
        elif kind == "packages":
            raw_packages = data.get("packages", "")
            packages = raw_packages if isinstance(raw_packages, list) else str(raw_packages).split()
            if not packages or len(packages) > 30 or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+(?:,(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+)*)?", p)
                for p in packages
            ):
                raise ValueError("Isi nama paket PyPI dipisahkan spasi, contoh: numpy pillow==11.0.0")
            payload["packages"] = packages
        elif kind not in ("login", "whoami", "pip_check", "free"):
            raise ValueError("Jenis pekerjaan tidak dikenal.")
        with self.lock:
            if kind == "login" and any(j["kind"] == "login" and j["status"] in ("queued", "running") for j in self.jobs.values()):
                raise ValueError("Login sedang berjalan; lihat device code pada log pekerjaan.")
            if sum(j["status"] in ("queued", "running") for j in self.jobs.values()) >= 50:
                raise ValueError("Antrean penuh. Tunggu pekerjaan selesai.")
            job = {"id": uuid.uuid4().hex, "kind": kind, "payload": payload,
                   "status": "queued", "created": time.time(), "error": ""}
            self.jobs[job["id"]] = job
            self.persist(job)
            self.pending.put(job["id"])
            return dict(job)

    def snapshot(self):
        with self.lock:
            return [dict(j) for j in sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)][:100]

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "queued":
                job["status"] = "cancelled"
                self.persist(job)
            elif job["status"] == "running":
                self.cancelled.add(job_id)

    def check_cancel(self, job):
        with self.lock:
            if job["id"] in self.cancelled:
                raise JobCancelled()

    def command(self, job, command, cwd=None, timeout=None):
        self.check_cancel(job)
        with (self.jobs_dir / f"{job['id']}.log").open("ab", buffering=0) as output:
            output.write(("$ " + shlex.join(map(str, command)) + "\n").encode())
            env = dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", HF_HUB_DISABLE_PROGRESS_BARS="0")
            proc = subprocess.Popen(command, cwd=cwd or self.comfy_dir, env=env,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    start_new_session=(os.name == "posix"))
            started = time.monotonic()
            try:
                if os.name == "posix":
                    with self.lock:
                        job["process"] = {"pid": proc.pid, "identity": process_identity(proc.pid)}
                        self.persist(job)
                while proc.poll() is None:
                    self.check_cancel(job)
                    if timeout and time.monotonic() - started > timeout:
                        raise RuntimeError("Waktu pekerjaan habis. Klik Retry untuk mencoba lagi.")
                    time.sleep(0.2)
                if proc.returncode:
                    raise RuntimeError(f"Perintah gagal (exit {proc.returncode}). Periksa log pekerjaan.")
            finally:
                if proc.poll() is None:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                        proc.wait()
                with self.lock:
                    job.pop("process", None)
                    self.persist(job)

    def execute(self, job):
        p = job["payload"]
        kind = job["kind"]
        if kind in ("wget", "hf"):
            destination = within(self.comfy_dir, p["destination"])
            destination.mkdir(parents=True, exist_ok=True)
            name = p["filename"] if kind == "wget" else PurePosixPath(p["file"]).name
            target = within(destination, name)
            if target.exists():
                raise ValueError(f"File sudah ada: {target}. Pilih subfolder/nama lain agar tidak tertimpa.")
            if kind == "wget":
                partial = within(destination, name + ".part")
                if partial.exists() and model_content_error(partial):
                    self.quarantine(job, partial)
                self.command(job, ["wget", "--continue", "--max-redirect=20", "--tries=5", "--timeout=30", "--progress=dot:giga",
                                   "--output-document", str(partial), "--", p["url"]])
                self.check_cancel(job)
                source = partial
            else:
                # Keep staging on the same disk for atomic rename. Preserve repo subfolders
                # here, then flatten the selected file into the chosen Comfy model category.
                cache_key = hashlib.sha256(json.dumps({key: p[key] for key in ("repo", "file", "revision")}, sort_keys=True).encode()).hexdigest()
                staging = within(self.comfy_dir, f".provisioning/hf-downloads/{cache_key}")
                staging.mkdir(parents=True, exist_ok=True)
                self.command(job, [self.hf_command, "download", p["repo"], p["file"],
                                   "--revision", p["revision"], "--local-dir", str(staging)])
                source = find_hf_file(staging, p["file"])
            self.check_cancel(job)
            try:
                size = validate_model_file(source, p.get("min_mib", 1))
            except ValueError as exc:
                rejected = self.quarantine(job, source)
                raise ValueError(f"{exc} File dikarantina di {rejected}; tidak dimasukkan ke models/.") from exc
            # Both download methods publish only the selected, validated file.
            source.replace(target)
            with self.lock:
                job["result"] = {"path": str(target), "size": size}
                self.persist(job)
            with (self.jobs_dir / f"{job['id']}.log").open("a", encoding="utf-8") as output:
                output.write(f"\nFile model tersimpan: {target}\nUkuran: {size} bytes\n")
            if kind == "hf":
                shutil.rmtree(staging)
        elif kind == "node":
            target = within(self.comfy_dir, f"custom_nodes/{p['name']}")
            if not target.exists():
                staging = within(self.comfy_dir, f"custom_nodes/.install-{job['id']}")
                staging.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.command(job, ["git", "clone", "--depth", "1", "--", p["url"], str(staging)])
                    staging.replace(target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            else:
                # A dependency failure can be retried without cloning over the node.
                origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=target,
                                        capture_output=True, text=True, check=True, timeout=10).stdout.strip()
                if origin.rstrip("/").removesuffix(".git") != p["url"].rstrip("/").removesuffix(".git"):
                    raise ValueError("Custom node sudah ada dengan URL origin berbeda.")
            requirements = within(target, "requirements.txt")
            if p["requirements"] and requirements.is_file():
                self.command(job, [self.python_command, "-m", "pip", "install", "-r", str(requirements)], cwd=target)
        elif kind == "requirements":
            target = within(self.comfy_dir, p["path"])
            self.command(job, [self.python_command, "-m", "pip", "install", "-r", str(target)], cwd=target.parent)
        elif kind == "packages":
            self.command(job, [self.python_command, "-m", "pip", "install", *p["packages"]])
        elif kind == "login":
            self.command(job, [self.hf_command, "auth", "login", "--force", "--format", "agent"], timeout=1200)
        elif kind == "whoami":
            self.command(job, [self.hf_command, "auth", "whoami"], timeout=45)
        elif kind == "pip_check":
            self.command(job, [self.python_command, "-m", "pip", "check"], timeout=120)
        elif kind == "free":
            state = load_json(self.runtime / "state.json")
            address = f"http://{connect_host(state['host'])}:{state['port']}/free"
            request = urllib.request.Request(address, data=b'{"unload_models":true,"free_memory":true}',
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()

    def quarantine(self, job, source):
        target = within(self.comfy_dir, f".provisioning/rejected/{job['id']}-{source.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.with_name(uuid.uuid4().hex + "-" + target.name)
        source.replace(target)
        return target

    def worker(self):
        while True:
            job_id = self.pending.get()
            with self.lock:
                job = self.jobs[job_id]
                if job["status"] == "cancelled":
                    self.pending.task_done()
                    continue
                job.update(status="running", started=time.time())
                self.persist(job)
            status, error = "completed", ""
            try:
                self.execute(job)
            except JobCancelled:
                status = "cancelled"
            except Exception as exc:
                status, error = "failed", str(exc)
            with self.lock:
                job.update(status=status, error=error, finished=time.time())
                self.cancelled.discard(job_id)
                self.persist(job)
            self.pending.task_done()


def resource_snapshot(comfy_dir, runtime):
    disk = shutil.disk_usage(comfy_dir)
    memory = {}
    try:
        memory = {line.split(":")[0]: int(line.split()[1]) * 1024
                  for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:", "MemAvailable:"))}
    except OSError:
        pass
    gpu = "nvidia-smi tidak tersedia"
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                                     "--format=csv,noheader"], capture_output=True, text=True, timeout=4)
            gpu = result.stdout.strip() if result.returncode == 0 else "GPU belum tersedia"
        except subprocess.TimeoutExpired:
            gpu = "GPU query timeout"
    state = load_json(runtime / "state.json")
    return {"disk": {"total": disk.total, "used": disk.used, "free": disk.free}, "memory": memory,
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [], "gpu": gpu,
            "comfy_dir": str(comfy_dir), "comfy_url": state.get("comfy_url", ""),
            "services": {name: is_running(entry) for name, entry in state.get("services", {}).items()}}


def inventory(comfy_dir, category):
    if category not in MODEL_TYPES:
        raise ValueError("Jenis model tidak dikenal.")
    base = within(comfy_dir, f"models/{category}")
    files = []
    if base.exists():
        for root, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    continue
                files.append({"path": str(path.relative_to(base)), "size": path.stat().st_size,
                              "partial": name.endswith(".part")})
                if len(files) >= 2000:
                    return {"files": files, "truncated": True}
    return {"files": files, "truncated": False}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def dashboard_handler(provisioner, password):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never print request bodies or Authorization headers.
            log_info(f"dashboard {self.command} {self.path.split('?')[0]}")

        def reply(self, code, data, content_type="application/json"):
            body = data.encode("utf-8") if isinstance(data, str) else json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            provided = self.headers.get("Authorization", "")
            if not hmac.compare_digest(provided.encode(), ("Bearer " + password).encode()):
                self.reply(401, {"error": "Password dashboard salah atau belum diisi."})
                return False
            return True

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path)
            if path.path == "/":
                return self.reply(200, DASHBOARD_HTML, "text/html")
            if not self.authorized():
                return
            try:
                if path.path == "/api/status":
                    result = resource_snapshot(provisioner.comfy_dir, provisioner.runtime)
                    result["categories"] = MODEL_TYPES
                    return self.reply(200, result)
                if path.path == "/api/jobs":
                    return self.reply(200, provisioner.snapshot())
                if path.path == "/api/files":
                    category = urllib.parse.parse_qs(path.query).get("category", ["checkpoints"])[0]
                    return self.reply(200, inventory(provisioner.comfy_dir, category))
                if path.path == "/api/log":
                    params = urllib.parse.parse_qs(path.query)
                    job_id = params.get("job", [""])[0]
                    if job_id:
                        with provisioner.lock:
                            if job_id not in provisioner.jobs:
                                raise ValueError("Job tidak ditemukan.")
                        file = provisioner.jobs_dir / f"{job_id}.log"
                    else:
                        service = params.get("service", ["comfy"])[0]
                        entry = load_json(provisioner.runtime / "state.json").get("services", {}).get(service)
                        if not entry:
                            raise ValueError("Layanan tidak ditemukan.")
                        file = Path(entry["log"])
                    return self.reply(200, {"text": tail_log(file)})
                self.reply(404, {"error": "Endpoint tidak ditemukan."})
            except (ValueError, KeyError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("Ukuran request tidak valid (maksimal 32 KB).")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Gunakan Content-Type application/json.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Request harus objek JSON.")
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/jobs":
                    return self.reply(202, provisioner.submit(data))
                if path == "/api/cancel":
                    provisioner.cancel(data["id"])
                    return self.reply(200, {"ok": True})
                if path == "/api/retry":
                    with provisioner.lock:
                        old = provisioner.jobs[data["id"]]
                        if old["status"] in ("queued", "running", "completed"):
                            raise ValueError("Retry hanya untuk pekerjaan gagal, batal, atau terinterupsi.")
                        payload = dict(old["payload"])
                    # The payload-derived HF staging path is shared by retries.
                    return self.reply(202, provisioner.submit(payload))
                self.reply(404, {"error": "Endpoint tidak ditemukan."})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": str(exc)})
    return Handler


DASHBOARD_HTML = r'''<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ComfyUI Provisioning</title><style>
:root{color-scheme:dark;--bg:#10141d;--panel:#191f2c;--line:#30394d;--muted:#a4b1ca;--accent:#8bdbb1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eff3fc;font:15px/1.55 system-ui,sans-serif}
main{width:100%;margin:0;padding:22px 24px}header{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:16px}
h1{font-size:28px;letter-spacing:-.8px;margin:0}h2{font-size:18px;margin:0 0 15px}p{margin:8px 0 16px}.muted,small{color:var(--muted)}
header>div{min-width:0;max-width:100%}#rootPath,#destinationPreview{overflow-wrap:anywhere}.job>div{min-width:0}
.badge{font-size:12px;letter-spacing:1.4px;color:var(--accent)}.grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:16px;align-items:start}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;min-width:0}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) clamp(360px,30vw,560px);gap:16px;align-items:start}.panels{min-width:0}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.tabs button{background:#252e41;color:#cad6ef}.tabs button[aria-pressed=true]{background:var(--accent);color:#11271d}.fields{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}.queue{position:sticky;top:16px;max-height:calc(100vh - 32px);display:flex;flex-direction:column}.queue #jobs{max-height:230px;overflow:auto}.queue pre{min-height:180px;max-height:42vh;flex:1}.files-scroll{max-height:60vh;overflow:auto}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:14px 0}.metric{padding:12px 16px;border:1px solid var(--line);border-radius:12px}.metric strong{display:block;font-size:20px;margin-top:4px}
.model-settings{grid-template-columns:1fr 1fr 1fr}.fields>div{min-width:0}.fields input,.fields select{min-width:0}
label{display:block;color:#c6d1e7;font-size:13px;margin:12px 0 5px}input,select,button{font:inherit;border-radius:7px;padding:10px 12px;border:1px solid var(--line)}input,select{width:100%;background:#101622;color:#fff}input[type=checkbox]{width:auto}
button,.button{cursor:pointer;background:var(--accent);color:#11271d;border:0;font-weight:650;text-decoration:none;display:inline-block;border-radius:7px;padding:10px 14px;font-size:14px}
button.secondary,.button.secondary{background:#2c364c;color:#e6efff}button:disabled{opacity:.5;cursor:wait}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:16px}.row>*{flex:none}
.full{grid-column:1/-1}.hidden,[hidden]{display:none!important}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;background:#0b1019;padding:16px;border-radius:9px;font:12px/1.6 ui-monospace,monospace}
.job{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.job .row{margin:0;flex-direction:column;align-items:stretch;gap:6px}.job small{display:block;overflow-wrap:anywhere}.completed{color:#8bdbb1}.failed,.cancelled,.interrupted{color:#ffb8a1}
#notice{position:sticky;top:10px;background:#23344b;border:1px solid #617ca8;padding:12px 16px;border-radius:9px;margin-bottom:16px;z-index:2;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:9px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}td:last-child{white-space:nowrap}progress{width:100%;accent-color:var(--accent)}a{color:var(--accent)}
@media(max-width:1180px){.panels>.grid{grid-template-columns:1fr}}@media(max-width:1000px){.workspace{grid-template-columns:1fr}.panels>.grid{grid-template-columns:1fr 1fr}.queue{position:static;max-height:none}.queue #jobs{max-height:250px}}
@media(max-width:760px){.panels>.grid,.fields{grid-template-columns:1fr}.metrics{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}.job{grid-template-columns:1fr}.job .row{flex-direction:row}main{padding:18px 14px}}
</style></head><body><main>
<header><div><div class="badge">COLAB WORKSPACE / PROVISIONING</div><h1>ComfyUI control desk</h1><div id="rootPath" class="muted">Download, siapkan, lalu mulai berkarya.</div></div><div class="row"><a id="comfyLink" class="button hidden" target="_blank" rel="noopener">Buka ComfyUI ↗</a><button id="logout" class="secondary hidden">Kunci dashboard</button></div></header>
<div id="notice" role="status" hidden></div>
<section id="loginCard" class="card" style="max-width:520px"><h2>Masuk ke workspace</h2><p class="muted">Gunakan password dashboard yang tercetak di terminal setup atau perintah --status.</p><form id="loginForm"><label for="password">Password dashboard</label><input id="password" type="password" autocomplete="current-password" required><div class="row"><button>Masuk</button></div></form></section>
<div id="app" class="hidden">
<div class="metrics"><div class="metric"><small>DISK TERSEDIA</small><strong id="disk">—</strong><progress id="diskBar" max="100" value="0"></progress></div><div class="metric"><small>RAM TERPAKAI</small><strong id="ram">—</strong><small id="load">—</small></div><div class="metric"><small>GPU / VRAM / UTILISASI / SUHU</small><div id="gpu" style="margin-top:8px;white-space:pre-line">—</div></div></div>
<nav class="tabs" aria-label="Fitur provisioning"><button data-tab="downloads" aria-pressed="true">Download & akun HF</button><button data-tab="dependencies" aria-pressed="false">Custom nodes & dependensi</button><button data-tab="resources" aria-pressed="false">Resource & file model</button></nav>
<div class="workspace"><div class="panels"><div class="grid">
<section class="card" data-panel="downloads"><h2>Download model & LoRA</h2><p class="muted">Pilih satu file dan kategori tujuan.</p><form id="downloadForm">
<label for="method">Sumber</label><select id="method" name="kind"><option value="wget">URL langsung · wget (resume)</option><option value="hf">Hugging Face · satu file</option></select>
<div id="wgetFields"><label for="url">URL file langsung</label><input id="url" name="url" type="url" placeholder="https://…/model.safetensors"><label for="filename">Nama file (opsional)</label><input id="filename" name="filename" placeholder="model.safetensors"></div>
<div id="hfFields" class="hidden"><label for="repo">Repository, URL file, atau hf:// URI</label><input id="repo" name="repo" placeholder="hf://owner/model/text_encoders/model.safetensors"><div class="fields"><div><label for="hfFile">Path file (untuk owner/repo)</label><input id="hfFile" name="file" placeholder="Kosongkan jika memakai URL/URI"></div><div><label for="revision">Revision (untuk owner/repo)</label><input id="revision" name="revision" value="main"></div></div><small>Folder repo tidak ikut disimpan.</small></div>
<div class="fields model-settings"><div><label for="category">Kategori tujuan</label><select id="category" name="category"></select></div><div><label for="subfolder">Subfolder</label><input id="subfolder" name="subfolder" placeholder="Opsional"></div><div><label for="minMib">Minimum (MiB)</label><input id="minMib" name="min_mib" type="number" min="0" max="1024" step="any" value="1" required></div></div><small>Atur minimum 0 untuk model kecil. HTML, teks, JSON error, dan pointer LFS tetap ditolak.</small><div class="row"><button>Tambahkan ke antrean</button></div></form></section>
<section class="card" data-panel="downloads"><h2>Akun Hugging Face</h2><p class="muted">Login lewat browser dengan device code. Tidak perlu memasukkan token ke dashboard.</p><div class="row"><button data-kind="login">Login Hugging Face</button><button data-kind="whoami" class="secondary">Cek akun</button></div><p><small>Buka URL Hugging Face dan salin kode dari log pekerjaan. Kode memiliki masa berlaku.</small></p><a href="https://huggingface.co/oauth/device" target="_blank" rel="noopener">Buka halaman device code ↗</a><hr style="border:0;border-top:1px solid var(--line);margin:22px 0"><h2>File masuk ke mana?</h2><p class="muted" id="destinationPreview">Pilih kategori tujuan.</p><p><small>URL halaman Hugging Face /blob/ otomatis diubah menjadi /resolve/ untuk mengambil file aslinya. Untuk model private/gated, gunakan metode Hugging Face dan pastikan akses model sudah disetujui.</small></p></section>
<section class="card" data-panel="resources" hidden><h2>Resource & layanan</h2><div id="services" class="muted">—</div><div class="row"><button data-kind="free" class="secondary">Unload model / bebaskan VRAM</button><button data-kind="pip_check" class="secondary">Cek konflik dependensi</button></div><p><small>Unload meminta ComfyUI melepas model; hasilnya bergantung pada pekerjaan yang sedang berjalan. Restart ComfyUI setelah memasang custom node atau dependensi baru.</small></p><div class="row"><button data-service="comfy" class="secondary">Log ComfyUI</button><button data-service="comfy_tunnel" class="secondary">Log tunnel ComfyUI</button><button data-service="dashboard_tunnel" class="secondary">Log tunnel dashboard</button></div></section>
<section class="card" data-panel="dependencies" hidden><h2>Custom nodes</h2><form id="nodeForm"><input type="hidden" name="kind" value="node"><label for="nodeUrl">URL repository GitHub</label><input id="nodeUrl" name="url" type="url" placeholder="https://github.com/owner/custom-node" required><p><label><input type="checkbox" id="nodeRequirements" checked> Instal requirements.txt node setelah clone</label></p><small>Instal hanya repository yang Anda percayai. Node dan paket memakai environment Python ComfyUI.</small><div class="row"><button>Instal custom node</button></div></form></section>
<section class="card" data-panel="dependencies" hidden><h2>Dependensi Python</h2><form id="requirementsForm"><input type="hidden" name="kind" value="requirements"><label for="reqPath">File requirements relatif terhadap ComfyUI</label><input id="reqPath" name="path" value="requirements.txt" required><div class="row"><button class="secondary">Instal requirements</button></div></form><form id="packagesForm"><input type="hidden" name="kind" value="packages"><label for="packages">Paket PyPI (pisahkan dengan spasi)</label><input id="packages" name="packages" placeholder="pillow safetensors" required><div class="row"><button class="secondary">Instal paket</button></div></form></section>
<section class="card" data-panel="resources" hidden><h2>File model</h2><label for="inventoryCategory">Kategori</label><select id="inventoryCategory"></select><div class="row"><button id="refreshFiles" class="secondary">Muat daftar file</button></div><p class="muted" id="fileCount"></p><div class="files-scroll"><table><thead><tr><th>Path</th><th>Ukuran</th></tr></thead><tbody id="files"></tbody></table></div></section>
</div></div><aside class="card queue" aria-label="Antrean dan log"><h2>Antrean pekerjaan</h2><p class="muted">Berjalan berurutan. Aman menutup tab browser.</p><div id="jobs">Belum ada pekerjaan.</div><h2 style="margin-top:20px" id="logTitle">Log pekerjaan</h2><pre id="log" tabindex="0" aria-live="off">Pilih pekerjaan atau log layanan.</pre><div class="row"><button id="copyLog" class="secondary">Salin log / device code</button></div></aside></div></div></main><script>
let credential=sessionStorage.getItem('comfy-provisioning-password')||'', selection=null, refreshing=false;
const el=id=>document.getElementById(id), bytes=n=>{if(n==null)return '—';const units=['B','KiB','MiB','GiB','TiB'];let i=0;while(n>=1024&&i<units.length-1){n/=1024;i++;}return n.toFixed(i?2:0)+' '+units[i];};
function notice(message){el('notice').textContent=message;el('notice').hidden=false;}
async function api(path,data){const response=await fetch(path,{method:data?'POST':'GET',headers:{'Authorization':'Bearer '+credential,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});const result=await response.json();if(!response.ok){if(response.status===401)lock();throw Error(result.error||'Request gagal');}return result;}
function lock(){credential='';sessionStorage.removeItem('comfy-provisioning-password');el('loginCard').classList.remove('hidden');el('app').classList.add('hidden');el('logout').classList.add('hidden');el('comfyLink').classList.add('hidden');selection=null;el('log').textContent='Pilih pekerjaan atau log layanan.';}
async function connect(){const s=await api('/api/status');el('loginCard').classList.add('hidden');el('app').classList.remove('hidden');el('logout').classList.remove('hidden');el('notice').hidden=true;for(const id of ['category','inventoryCategory']){el(id).replaceChildren();for(const name of s.categories){const o=document.createElement('option');o.value=name;o.textContent=name;el(id).append(o);}}sessionStorage.setItem('comfy-provisioning-password',credential);renderStatus(s);updateDestination();await refresh();}
function renderStatus(s){el('rootPath').textContent=s.comfy_dir;el('disk').textContent=bytes(s.disk.free)+' / '+bytes(s.disk.total);el('diskBar').value=100*s.disk.used/s.disk.total;el('ram').textContent=s.memory.MemTotal?bytes(s.memory.MemTotal-s.memory.MemAvailable)+' / '+bytes(s.memory.MemTotal):'Tidak tersedia';el('load').textContent='CPU load (1 / 5 / 15 m): '+s.load.map(n=>n.toFixed(2)).join(' / ');el('gpu').textContent=s.gpu;el('services').textContent=Object.entries(s.services).map(([n,r])=>n+': '+(r?'aktif':'berhenti')).join(' · ');if(/^https?:\/\//.test(s.comfy_url)){el('comfyLink').href=s.comfy_url;el('comfyLink').classList.remove('hidden');}}
async function submit(data){try{const j=await api('/api/jobs',data);selection={job:j.id};el('logTitle').textContent='Log: '+j.kind;notice('Pekerjaan masuk antrean. Device code dan progres tersedia di log.');await refresh();}catch(e){notice(e.message);}}
el('loginForm').onsubmit=async e=>{e.preventDefault();credential=el('password').value;try{await connect();el('password').value='';}catch(err){notice(err.message);}};el('logout').onclick=lock;
el('method').onchange=()=>{const hf=el('method').value==='hf';el('hfFields').classList.toggle('hidden',!hf);el('wgetFields').classList.toggle('hidden',hf);};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));document.querySelectorAll('[data-panel]').forEach(panel=>panel.hidden=panel.dataset.panel!==button.dataset.tab);});
function updateDestination(){const root=el('rootPath').textContent,category=el('category').value,folder=el('subfolder').value.trim();el('destinationPreview').textContent=root+'/models/'+category+(folder?'/'+folder:'')+'/<nama-file-model>';}
el('category').onchange=updateDestination;el('subfolder').oninput=updateDestination;
for(const id of ['downloadForm','nodeForm','requirementsForm','packagesForm'])el(id).onsubmit=e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));if(id==='nodeForm')data.requirements=el('nodeRequirements').checked;submit(data);};
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=()=>submit({kind:b.dataset.kind}));
document.querySelectorAll('[data-service]').forEach(b=>b.onclick=()=>{selection={service:b.dataset.service};el('logTitle').textContent='Log: '+b.dataset.service;refresh().catch(e=>notice(e.message));});
async function jobAction(action,id){try{const result=await api('/api/'+action,{id});if(action==='retry')selection={job:result.id};await refresh();}catch(e){notice(e.message);}}
function renderJobs(jobs){
 const container=el('jobs');container.replaceChildren();if(!jobs.length)container.textContent='Belum ada pekerjaan.';
 for(const j of jobs){
  const item=document.createElement('div');item.className='job';
  const info=document.createElement('div'),title=document.createElement('strong'),desc=document.createElement('small');
  title.className=j.status;title.textContent=j.kind+' · '+j.status;
  desc.textContent=j.error||(j.result?j.result.path+' · '+bytes(j.result.size):'')||j.payload.file||j.payload.filename||j.payload.name||new Date(j.created*1000).toLocaleString();
  info.append(title,desc);const actions=document.createElement('div');actions.className='row';
  const log=document.createElement('button');log.className='secondary';log.textContent='Lihat log';
  log.onclick=()=>{selection={job:j.id};el('logTitle').textContent='Log: '+j.kind;refresh().catch(e=>notice(e.message));};actions.append(log);
  if(['queued','running','failed','cancelled','interrupted'].includes(j.status)){
   const b=document.createElement('button');b.className='secondary';const action=['queued','running'].includes(j.status)?'cancel':'retry';
   b.textContent=action==='cancel'?'Batalkan':'Retry';b.onclick=()=>jobAction(action,j.id);actions.append(b);
  }
  item.append(info,actions);container.append(item);
 }
}
async function refresh(){if(!credential||refreshing)return;refreshing=true;try{const [s,jobs]=await Promise.all([api('/api/status'),api('/api/jobs')]);renderStatus(s);renderJobs(jobs);if(selection){const result=await api('/api/log?'+new URLSearchParams(selection));const log=el('log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;log.textContent=result.text||'Menunggu pekerjaan dimulai…';if(atBottom)log.scrollTop=log.scrollHeight;}}finally{refreshing=false;}}
el('copyLog').onclick=async()=>{try{await navigator.clipboard.writeText(el('log').textContent);notice('Log disalin.');}catch{notice('Pilih teks pada log, lalu salin secara manual.');}};
el('refreshFiles').onclick=async()=>{try{const data=await api('/api/files?category='+encodeURIComponent(el('inventoryCategory').value));el('files').replaceChildren();for(const f of data.files){const tr=document.createElement('tr'),name=document.createElement('td'),size=document.createElement('td');name.textContent=f.path+(f.partial?' (belum selesai)':'');size.textContent=bytes(f.size);tr.append(name,size);el('files').append(tr);}el('fileCount').textContent=data.files.length+' file'+(data.truncated?' (dibatasi 2000)':'');}catch(e){notice(e.message);}};
setInterval(()=>refresh().catch(e=>notice(e.message)),4000);if(credential)connect().catch(e=>notice(e.message));
</script></body></html>'''


def run_dashboard(args):
    runtime = args.dir.resolve() / ".provisioning"
    state = load_json(runtime / "state.json")
    provisioner = Provisioner(args.dir, runtime, state["hf_command"], state["python_command"])
    server = DashboardServer(("127.0.0.1", args.dashboard_port), dashboard_handler(provisioner, state["password"]))
    log_info(f"Dashboard aktif di 127.0.0.1:{args.dashboard_port}")
    server.serve_forever()


def launch_stack(args, comfy_dir, runtime, hf_command, cloudflared):
    state_path = runtime / "state.json"
    old = load_json(state_path)
    if any(is_running(entry) for entry in old.get("services", {}).values()):
        show_status(runtime, reveal_password=True)
        raise RuntimeError("Sebagian/semua layanan sudah berjalan. Gunakan --stop sebelum menjalankan ulang.")
    for host, port in ((args.host, args.port), ("127.0.0.1", args.dashboard_port)):
        if port_open(host, port):
            raise RuntimeError(f"Port {host}:{port} sudah dipakai proses lain.")
    state = {"host": args.host, "port": args.port, "dashboard_port": args.dashboard_port,
             "comfy_dir": str(comfy_dir), "python_command": sys.executable, "hf_command": hf_command,
             "password": old.get("password") or secrets.token_urlsafe(24), "services": {}}
    save_json(state_path, state)

    def start(name, command, cwd):
        logfile = runtime / "logs" / f"{name}-{time.time_ns()}.log"
        proc, entry = launch_detached(command, cwd, logfile)
        state["services"][name] = entry
        save_json(state_path, state)
        return proc, logfile

    try:
        comfy_cmd = [sys.executable, "-u", "main.py", "--listen", args.host, "--port", str(args.port),
                     "--enable-cors-header", "*"]
        comfy_cmd.extend(shlex.split(args.extra_args))
        comfy_proc, comfy_log = start("comfy", comfy_cmd, comfy_dir)
        dashboard_proc, dashboard_log = start("dashboard", [sys.executable, "-u", str(Path(__file__).resolve()),
             "--dir", str(comfy_dir), "--dashboard-port", str(args.dashboard_port), "--serve-dashboard"], comfy_dir)
        wait_for_service(dashboard_proc, "127.0.0.1", args.dashboard_port, dashboard_log, timeout=30)
        # Bring provisioning online first, even while ComfyUI imports GPU dependencies.
        dashboard_tunnel, dashboard_tunnel_log = start("dashboard_tunnel", tunnel_command(
            "cloudflare", "127.0.0.1", args.dashboard_port, cloudflared), comfy_dir)
        state["dashboard_url"] = wait_for_tunnel(dashboard_tunnel, dashboard_tunnel_log, "cloudflare")
        save_json(state_path, state)
        print(f"\nDashboard: {state['dashboard_url']}\nPassword : {state['password']}\n", flush=True)
        wait_for_service(comfy_proc, args.host, args.port, comfy_log)
        comfy_tunnel, comfy_tunnel_log = start("comfy_tunnel", tunnel_command(args.tunnel, args.host, args.port, cloudflared), comfy_dir)
        state["comfy_url"] = wait_for_tunnel(comfy_tunnel, comfy_tunnel_log, args.tunnel)
        save_json(state_path, state)
        show_status(runtime, reveal_password=True)
        if args.tunnel == "localtunnel":
            log_info("Jika Localtunnel meminta password, gunakan IP publik runtime Colab.")
        log_info("ComfyUI, dashboard, dan kedua tunnel berjalan dengan nohup. Terminal boleh ditutup.")
    except BaseException:
        stop_stack(runtime, state)
        raise


def parse_args():
    parser = argparse.ArgumentParser(description="ComfyUI + provisioning dashboard + dua tunnel, memakai nohup (Colab/Linux).")
    parser.add_argument("--dir", type=Path, default=get_default_base_dir() / "ComfyUI",
                        help="Root ComfyUI, berisi main.py dan models/ (default Colab: /content/ComfyUI)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--dashboard-port", type=int, default=8189)
    parser.add_argument("--tunnel", choices=list(TUNNEL_PATTERNS), default="cloudflare",
                        help="Tunnel ComfyUI; dashboard selalu memakai Cloudflare")
    parser.add_argument("--no-manager", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--skip-install", action="store_true", help="Pakai instalasi ComfyUI yang ada; tools provisioning tetap dicek")
    parser.add_argument("--extra-args", default="--dont-print-server")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="Cetak PID, URL, password dashboard, dan log")
    actions.add_argument("--stop", action="store_true", help="Hentikan layanan dan tunnel milik setup ini")
    actions.add_argument("--serve-dashboard", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 1 <= args.dashboard_port <= 65535):
        parser.error("Port harus antara 1 dan 65535.")
    if args.port == args.dashboard_port:
        parser.error("Port ComfyUI dan dashboard harus berbeda.")
    return args


def main():
    args = parse_args()
    if os.name != "posix" or platform.system() != "Linux":
        raise RuntimeError("Jalankan setup di Google Colab atau Linux (nohup dan /proc diperlukan).")
    comfy_dir = args.dir.expanduser().resolve()
    runtime = comfy_dir / ".provisioning"
    if args.serve_dashboard:
        run_dashboard(args)
        return
    if args.status:
        show_status(runtime, reveal_password=True)
        return
    # The parent lock also covers an absent ComfyUI directory, before git clone.
    import fcntl
    comfy_dir.parent.mkdir(parents=True, exist_ok=True)
    with (comfy_dir.parent / f".{comfy_dir.name}-setup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Setup/stop lain sedang berjalan untuk direktori ComfyUI ini.") from exc
        manage_stack(args, comfy_dir, runtime)


def manage_stack(args, comfy_dir, runtime):
    if args.stop:
        state = load_json(runtime / "state.json")
        stop_stack(runtime, state)
        log_info("ComfyUI, dashboard, dan kedua tunnel telah dihentikan.")
        return
    existing = load_json(runtime / "state.json")
    if any(is_running(entry) for entry in existing.get("services", {}).values()):
        show_status(runtime, reveal_password=True)
        log_info("Layanan sudah berjalan. Gunakan --stop sebelum mengganti konfigurasi atau instalasi.")
        return
    if args.skip_install:
        if not (comfy_dir / "main.py").is_file():
            raise RuntimeError(f"main.py tidak ditemukan di {comfy_dir}; jangan pakai --skip-install untuk instalasi baru.")
    else:
        install_comfyui(comfy_dir, install_manager=not args.no_manager, update=args.update)
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    hf_command = install_tools(runtime)
    cloudflared = install_cloudflared(comfy_dir.parent / ".bin")
    if args.install_only:
        log_info("Instalasi selesai. Jalankan kembali dengan --skip-install untuk memulai layanan.")
        return
    required = {"pinggy": "ssh", "localtunnel": "npx"}.get(args.tunnel)
    if required and not shutil.which(required):
        raise RuntimeError(f"Tunnel {args.tunnel} membutuhkan {required} di PATH.")
    launch_stack(args, comfy_dir, runtime, hf_command, cloudflared)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup dibatalkan.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
