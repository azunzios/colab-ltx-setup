#!/usr/bin/env python3
"""
Automated ComfyUI Installer & Tunnel Launcher for Google Colab Terminal.
Repository: https://github.com/azunzios/colab-ltx-setup
"""

import argparse
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{Colors.CYAN}[INFO]{Colors.END} {msg}", flush=True)


def log_success(msg: str) -> None:
    print(f"{Colors.GREEN}[SUCCESS]{Colors.END} {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"{Colors.WARNING}[WARN]{Colors.END} {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{Colors.FAIL}[ERROR]{Colors.END} {msg}", flush=True)


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """Run a shell command and stream its output to the terminal."""
    log_info(f"Menjalankan: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    return_code = process.wait()
    if check and return_code != 0:
        raise RuntimeError(f"Perintah gagal dengan kode keluar {return_code}: {' '.join(cmd)}")
    return return_code


def get_default_base_dir() -> Path:
    """Return /content if running on Google Colab, otherwise current directory."""
    colab_dir = Path("/content")
    if colab_dir.exists() and colab_dir.is_dir():
        return colab_dir
    return Path.cwd()


def install_cloudflared(bin_dir: Path) -> Path:
    """Ensure cloudflared binary is installed and executable."""
    existing = shutil.which("cloudflared")
    if existing:
        log_success(f"cloudflared sudah terinstal di: {existing}")
        return Path(existing)

    bin_dir.mkdir(parents=True, exist_ok=True)
    target_path = bin_dir / "cloudflared"
    if target_path.exists():
        target_path.chmod(0o755)
        log_success(f"cloudflared ditemukan di: {target_path}")
        return target_path

    arch = platform.machine().lower()
    if arch in ("aarch64", "arm64"):
        binary_name = "cloudflared-linux-arm64"
    else:
        binary_name = "cloudflared-linux-amd64"

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{binary_name}"
    log_info(f"Mengunduh cloudflared dari {url} ...")
    urllib.request.urlretrieve(url, str(target_path))
    target_path.chmod(0o755)
    log_success(f"cloudflared berhasil diinstal di: {target_path}")
    return target_path


def install_comfyui(
    comfy_dir: Path,
    install_manager: bool = True,
    update: bool = False,
) -> None:
    """Clone ComfyUI, ComfyUI-Manager, and install Python requirements."""
    if not comfy_dir.exists():
        log_info(f"Meng-clone ComfyUI ke {comfy_dir} ...")
        run_cmd(["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)])
    else:
        log_info(f"Direktori ComfyUI sudah ada di {comfy_dir}.")
        if update:
            log_info("Memperbarui repository ComfyUI...")
            run_cmd(["git", "pull"], cwd=comfy_dir, check=False)

    log_info("Menginstal dependensi Python ComfyUI...")
    req_file = comfy_dir / "requirements.txt"
    run_cmd([sys.executable, "-m", "pip", "install", "-r", str(req_file)], cwd=comfy_dir)

    if install_manager:
        custom_nodes_dir = comfy_dir / "custom_nodes"
        custom_nodes_dir.mkdir(parents=True, exist_ok=True)
        manager_dir = custom_nodes_dir / "ComfyUI-Manager"
        if not manager_dir.exists():
            log_info("Meng-clone ComfyUI-Manager...")
            run_cmd(
                [
                    "git",
                    "clone",
                    "https://github.com/ltdrdata/ComfyUI-Manager.git",
                    str(manager_dir),
                ]
            )
        elif update:
            log_info("Memperbarui ComfyUI-Manager...")
            run_cmd(["git", "pull"], cwd=manager_dir, check=False)

        manager_req = manager_dir / "requirements.txt"
        if manager_req.exists():
            log_info("Menginstal dependensi ComfyUI-Manager...")
            run_cmd(
                [sys.executable, "-m", "pip", "install", "-r", str(manager_req)],
                cwd=manager_dir,
                check=False,
            )

    log_success("Instalasi ComfyUI selesai!")


def wait_for_port(host: str, port: int, stop_event: threading.Event, timeout: int = 300) -> bool:
    """Wait until a TCP port is accepting connections."""
    start_time = time.time()
    while not stop_event.is_set():
        if time.time() - start_time > timeout:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(1.0)
    return False


def print_tunnel_banner(provider: str, public_url: str, host: str, port: int, extra_line: str = "") -> None:
    extra_block = f"  {extra_line}\n" if extra_line else ""
    banner = (
        f"\n{Colors.GREEN}{Colors.BOLD}"
        + "=" * 70
        + f"\n  COMFYUI SIAP DIAKSES MELALUI INTERNET ({provider.upper()} TUNNEL)\n"
        + f"  URL Publik : {Colors.UNDERLINE}{public_url}{Colors.END}{Colors.GREEN}{Colors.BOLD}\n"
        + extra_block
        + f"  URL Lokal  : http://{host}:{port}\n"
        + "=" * 70
        + f"{Colors.END}\n"
    )
    print(banner, flush=True)


def start_cloudflare_tunnel(
    cloudflared_bin: Path,
    host: str,
    port: int,
    stop_event: threading.Event,
) -> subprocess.Popen | None:
    """Wait for local ComfyUI server to listen, then start cloudflared tunnel."""
    log_info(f"Menunggu server ComfyUI aktif di {host}:{port} sebelum membuka Cloudflare Tunnel...")
    if not wait_for_port(host, port, stop_event):
        if not stop_event.is_set():
            log_error(f"Waktu habis menunggu ComfyUI di {host}:{port}.")
        return None

    log_info("ComfyUI sudah aktif! Membuka koneksi Cloudflare Tunnel...")
    tunnel_proc = subprocess.Popen(
        [
            str(cloudflared_bin),
            "tunnel",
            "--url",
            f"http://{host}:{port}",
            "--http-host-header",
            f"{host}:{port}",
            "--no-autoupdate",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    url_found = False

    def monitor_tunnel_output() -> None:
        nonlocal url_found
        assert tunnel_proc.stdout is not None
        for line in tunnel_proc.stdout:
            match = url_pattern.search(line)
            if match and not url_found:
                url_found = True
                public_url = match.group(0)
                print_tunnel_banner("Cloudflare", public_url, host, port)

    reader_thread = threading.Thread(target=monitor_tunnel_output, daemon=True)
    reader_thread.start()
    return tunnel_proc


def start_pinggy_tunnel(
    host: str,
    port: int,
    stop_event: threading.Event,
) -> subprocess.Popen | None:
    """Wait for local ComfyUI server to listen, then start Pinggy SSH tunnel."""
    log_info(f"Menunggu server ComfyUI aktif di {host}:{port} sebelum membuka Pinggy Tunnel...")
    if not wait_for_port(host, port, stop_event):
        if not stop_event.is_set():
            log_error(f"Waktu habis menunggu ComfyUI di {host}:{port}.")
        return None

    log_info("ComfyUI sudah aktif! Membuka koneksi Pinggy Tunnel via SSH...")
    tunnel_proc = subprocess.Popen(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ServerAliveInterval=30",
            "-p",
            "443",
            f"-R0:{host}:{port}",
            "a.pinggy.io",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"https://[a-zA-Z0-9.-]+\.pinggy\.link")
    url_found = False

    def monitor_tunnel_output() -> None:
        nonlocal url_found
        assert tunnel_proc.stdout is not None
        for line in tunnel_proc.stdout:
            match = url_pattern.search(line)
            if match and not url_found:
                url_found = True
                public_url = match.group(0)
                print_tunnel_banner("Pinggy", public_url, host, port)

    reader_thread = threading.Thread(target=monitor_tunnel_output, daemon=True)
    reader_thread.start()
    return tunnel_proc


def start_localtunnel(
    host: str,
    port: int,
    stop_event: threading.Event,
) -> subprocess.Popen | None:
    """Wait for local ComfyUI server to listen, then start Localtunnel via npx."""
    log_info(f"Menunggu server ComfyUI aktif di {host}:{port} sebelum membuka Localtunnel...")
    if not wait_for_port(host, port, stop_event):
        if not stop_event.is_set():
            log_error(f"Waktu habis menunggu ComfyUI di {host}:{port}.")
        return None

    public_ip = ""
    try:
        with urllib.request.urlopen("https://ipv4.icanhazip.com", timeout=5) as resp:
            public_ip = resp.read().decode("utf-8").strip()
    except Exception:
        pass

    log_info("ComfyUI sudah aktif! Membuka koneksi Localtunnel...")
    tunnel_proc = subprocess.Popen(
        ["npx", "--yes", "localtunnel", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.loca\.lt")
    url_found = False

    def monitor_tunnel_output() -> None:
        nonlocal url_found
        assert tunnel_proc.stdout is not None
        for line in tunnel_proc.stdout:
            match = url_pattern.search(line)
            if match and not url_found:
                url_found = True
                public_url = match.group(0)
                extra = f"Password Tunnel (IP Colab) : {public_ip}" if public_ip else ""
                print_tunnel_banner("Localtunnel", public_url, host, port, extra_line=extra)

    reader_thread = threading.Thread(target=monitor_tunnel_output, daemon=True)
    reader_thread.start()
    return tunnel_proc


def parse_args() -> argparse.Namespace:
    default_base = get_default_base_dir()
    parser = argparse.ArgumentParser(
        description="Instalasi otomatis ComfyUI + Tunneling untuk Google Colab Terminal."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=default_base / "ComfyUI",
        help="Lokasi direktori instalasi ComfyUI (default: /content/ComfyUI di Colab)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host untuk menjalankan ComfyUI (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8188,
        help="Port untuk menjalankan ComfyUI (default: 8188)",
    )
    parser.add_argument(
        "--tunnel",
        type=str,
        choices=["cloudflare", "pinggy", "localtunnel"],
        default="cloudflare",
        help="Penyedia tunnel internet: cloudflare (default), pinggy, atau localtunnel",
    )
    parser.add_argument(
        "--no-manager",
        action="store_true",
        help="Lewati instalasi ComfyUI-Manager",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Perbarui ComfyUI dan ComfyUI-Manager jika sudah terinstal",
    )
    parser.add_argument(
        "--install-only",
        action="store_true",
        help="Hanya lakukan instalasi tanpa menjalankan server dan tunnel",
    )
    parser.add_argument(
        "--extra-args",
        type=str,
        default="--dont-print-server",
        help="Argumen tambahan untuk main.py ComfyUI",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    comfy_dir: Path = args.dir.resolve()
    bin_dir = comfy_dir.parent / ".bin"

    print(
        f"{Colors.HEADER}{Colors.BOLD}"
        "============================================================\n"
        "       ComfyUI + Tunnel Automation Script (Colab)           \n"
        "============================================================"
        f"{Colors.END}",
        flush=True,
    )

    try:
        cloudflared_bin: Path | None = None
        if args.tunnel == "cloudflare":
            cloudflared_bin = install_cloudflared(bin_dir)

        install_comfyui(
            comfy_dir=comfy_dir,
            install_manager=not args.no_manager,
            update=args.update,
        )

        if args.install_only:
            log_success("Mode --install-only selesai. Keluar.")
            return

        stop_event = threading.Event()
        tunnel_holder: dict[str, subprocess.Popen | None] = {"proc": None}

        def tunnel_worker() -> None:
            if args.tunnel == "cloudflare":
                assert cloudflared_bin is not None
                tunnel_holder["proc"] = start_cloudflare_tunnel(
                    cloudflared_bin=cloudflared_bin,
                    host=args.host,
                    port=args.port,
                    stop_event=stop_event,
                )
            elif args.tunnel == "pinggy":
                tunnel_holder["proc"] = start_pinggy_tunnel(
                    host=args.host,
                    port=args.port,
                    stop_event=stop_event,
                )
            elif args.tunnel == "localtunnel":
                tunnel_holder["proc"] = start_localtunnel(
                    host=args.host,
                    port=args.port,
                    stop_event=stop_event,
                )

        tunnel_thread = threading.Thread(target=tunnel_worker, daemon=True)
        tunnel_thread.start()

        # Penting: --enable-cors-header * mencegah error HTTP 403 Forbidden
        # dari origin_only_middleware bawaan ComfyUI saat diakses via Cloudflare/Reverse Proxy.
        comfy_cmd = [
            sys.executable,
            "main.py",
            "--listen",
            args.host,
            "--port",
            str(args.port),
            "--enable-cors-header",
            "*",
        ]
        if args.extra_args.strip():
            comfy_cmd.extend(args.extra_args.strip().split())

        log_info(f"Menjalankan server ComfyUI: {' '.join(comfy_cmd)}")
        comfy_proc = subprocess.Popen(comfy_cmd, cwd=str(comfy_dir))

        try:
            comfy_proc.wait()
        except KeyboardInterrupt:
            log_warn("\nInterupsi diterima (Ctrl+C). Menghentikan ComfyUI & Tunnel...")
        finally:
            stop_event.set()
            if comfy_proc.poll() is None:
                comfy_proc.terminate()
                try:
                    comfy_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    comfy_proc.kill()

            tunnel_proc = tunnel_holder.get("proc")
            if tunnel_proc and tunnel_proc.poll() is None:
                tunnel_proc.terminate()
                try:
                    tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tunnel_proc.kill()

            log_success("Semua proses telah dihentikan.")

    except Exception as exc:
        log_error(f"Terjadi kesalahan: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
