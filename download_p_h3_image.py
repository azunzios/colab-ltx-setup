"""Unduh model dan workflow P-H3-Image untuk instalasi ComfyUI yang sudah ada."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen


MINIMAX = ("Comfy-Org/MiniMax-H3", "4b53a35191437de05370da548e59aeabe2a5a09a")
PRUNA = ("PrunaAI/P-Image-OSS-Comfy", "1bf3b0cee1bc1ff45408d0af7a246ae594fbdf74")
MODELS = (
    (MINIMAX, "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
     20970379616, "e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a"),
    (PRUNA, "text_encoders/p-h3-image_text_encoder_int8_convrot.safetensors",
     5120870504, "fb42c9ff0597e61729475d0b48b69b591ffe06d0b442a79dde22b14fadbe1c0f"),
    (PRUNA, "text_encoders/p-h3-image_prompt_upsampler_w4a8.safetensors",
     6724096116, "74b206c0e3133aa0d1983fd8658c1526a53863cdcca5184538388e927f4e989b"),
    (PRUNA, "vae/p-h3-image_vae_int8_convrot.safetensors",
     3146996440, "583e0d43354b6b833aa9a0815a5fac831b1ef6d0c19a7eaa0616980a3dab5ffe"),
    (PRUNA, "loras/p-h3-image_lora_bf16.safetensors",
     1956195360, "0e9013ae2873e3415dcb7bb59a9677293bdbdb6f663e138a79bb9bdced00ea61"),
)
TEMPLATES = "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/"
ASSETS = (
    ("templates/image_p_h3_t2i.json", "user/default/workflows/image_p_h3_t2i.json"),
    ("templates/image_p_h3_image_edit.json", "user/default/workflows/image_p_h3_image_edit.json"),
    ("input/image_p_h3_image_edit_atrium_day.png", "input/image_p_h3_image_edit_atrium_day.png"),
)


def path_from_text(value):
    return Path(os.path.expandvars(value.strip().strip('"').strip("'"))).expanduser().resolve()


def portable_candidates(base):
    return (base, base / "ComfyUI", base / "ComfyUI_windows_portable" / "ComfyUI")


def is_comfy_root(path):
    return ((path / "main.py").is_file() and (path / "folder_paths.py").is_file()) or (
        (path / "models").is_dir() and (path / "user").is_dir()
        and (path / "custom_nodes").is_dir()
    )


def desktop_paths():
    home = Path.home()
    configs = [home / "Library/Application Support/ComfyUI/config.json"]
    if roaming := os.environ.get("APPDATA"):
        configs.append(Path(roaming) / "ComfyUI/config.json")
    for config in configs:
        try:
            data = json.loads(config.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("basePath"), str) and data["basePath"]:
                yield path_from_text(data["basePath"])
        except (OSError, ValueError):
            continue
    if local := os.environ.get("LOCALAPPDATA"):
        yield from (Path(local) / "Comfy-Desktop/ComfyUI-Installs").glob("*/ComfyUI")


def detect_root():
    if configured := os.environ.get("COMFYUI_DIR"):
        for candidate in portable_candidates(path_from_text(configured)):
            if is_comfy_root(candidate):
                return candidate
    script_dir = Path(__file__).resolve().parent
    home = Path.home()
    bases = (Path("/content"), Path("/workspace"), home, home / "Documents",
             home / "Downloads", home / "OneDrive/Documents")
    candidates = [*desktop_paths()]
    for base in (*Path.cwd().parents, *script_dir.parents, *bases):
        if base != Path(base.anchor):
            candidates.extend(portable_candidates(base))
    candidates[:0] = (*portable_candidates(Path.cwd()), *portable_candidates(script_dir))
    return next((p.resolve() for p in candidates if is_comfy_root(p)), None)


def get_root(explicit):
    if not explicit:
        if detected := detect_root():
            return detected
        try:
            explicit = input("ComfyUI belum terdeteksi. Masukkan path direktori ComfyUI: ").strip()
        except EOFError:
            explicit = ""
    if explicit:
        for candidate in portable_candidates(path_from_text(explicit)):
            if is_comfy_root(candidate):
                return candidate
    raise RuntimeError("Direktori ComfyUI tidak ditemukan. Jalankan dengan --comfyui-dir PATH.")


def hf_command(root):
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "hf.exe" if os.name == "nt" else "hf"
    provisioned = root / ".provisioning/hf-tools" / bin_dir / executable
    if provisioned.is_file():
        return str(provisioned)
    if system_hf := shutil.which("hf"):
        return system_hf
    tools = root / ".provisioning/p-h3-hf-tools"
    hf = tools / bin_dir / executable
    if not hf.is_file():
        print("Menyiapkan Hugging Face CLI di environment terpisah...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tools)], check=True)
        python = tools / bin_dir / ("python.exe" if os.name == "nt" else "python")
        uv = shutil.which("uv")
        installer = ([uv, "--no-config", "pip", "install", "--python", str(python)] if uv else
                     [sys.executable, "-m", "pip", "--python", str(python), "install"])
        subprocess.run([*installer, "huggingface_hub>=1.20.1"], check=True)
    return str(hf)


def login_env(hf):
    env = os.environ.copy()
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_DISABLE_IMPLICIT_TOKEN"):
        env.pop(key, None)
    whoami = [hf, "auth", "whoami"]
    if subprocess.run(whoami, env=env, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        print("Login Hugging Face lewat browser dengan kode perangkat.", flush=True)
        subprocess.run([hf, "auth", "login", "--force", "--format", "agent"], env=env, check=True)
        subprocess.run(whoami, env=env, check=True)
    return env


def matches_model(path, size, checksum):
    if not path.is_file() or path.stat().st_size != size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == checksum


def download_models(root):
    models = root / "models"
    pending = []
    for repo, relative, size, checksum in MODELS:
        target = models / relative
        if target.is_file():
            print(f"Memeriksa {relative}...", flush=True)
            if matches_model(target, size, checksum):
                print("  SHA-256 cocok, dilewati.", flush=True)
                continue
        pending.append((repo, relative, size, checksum))
    if not pending:
        return
    hf = hf_command(root)
    env = login_env(hf)
    for (repo, revision), relative, size, checksum in pending:
        target = models / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Staging satu filesystem dengan tujuan agar file bisa dipindah setelah verifikasi.
        staging = target.parent / ".p-h3-download"
        staged = staging / relative
        command = [hf, "download", repo, relative, "--revision", revision,
                   "--local-dir", str(staging)]
        print(f"Mengunduh {relative} ({size / 1e9:.2f} GB)...", flush=True)
        subprocess.run(command, env=env, check=True)
        if not matches_model(staged, size, checksum):
            subprocess.run([*command, "--force-download"], env=env, check=True)
            if not matches_model(staged, size, checksum):
                raise RuntimeError(f"Verifikasi ukuran/SHA-256 gagal untuk {relative}.")
        staged.replace(target)
        print(f"Terverifikasi: {target}", flush=True)


def valid_asset(data, suffix):
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
    try:
        workflow = json.loads(data)
    except (ValueError, UnicodeError):
        return False
    return isinstance(workflow, dict) and bool(workflow.get("nodes"))


def download_assets(root):
    for source, relative in ASSETS:
        target = root / relative
        if target.is_file() and valid_asset(target.read_bytes(), target.suffix):
            print(f"Sudah ada: {relative}", flush=True)
            continue
        print(f"Mengunduh {relative}...", flush=True)
        with urlopen(TEMPLATES + source, timeout=60) as response:
            data = response.read()
        if not valid_asset(data, target.suffix):
            raise RuntimeError(f"File dari sumber resmi tidak valid: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.close()
                temporary.replace(target)
            finally:
                handle.close()
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfyui-dir", help="Folder ComfyUI atau folder induk portable.")
    parser.add_argument("--dry-run", action="store_true", help="Tampilkan tujuan tanpa mengunduh.")
    args = parser.parse_args()
    root = get_root(args.comfyui_dir)
    print(f"Direktori ComfyUI: {root}", flush=True)
    if args.dry_run:
        for _, relative, _, _ in MODELS:
            print(root / "models" / relative)
        for _, relative in ASSETS:
            print(root / relative)
        return
    download_models(root)
    download_assets(root)
    print("Selesai. Buka workflow P-H3-Image di ComfyUI. Diperlukan ComfyUI 0.44.0 atau lebih baru.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nDihentikan. Jalankan ulang untuk melanjutkan unduhan.")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"Gagal: {error}")
