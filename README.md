# ComfyUI + provisioning dashboard untuk Colab

`setup_comfyui.py` menginstal ComfyUI dan ComfyUI-Manager, lalu menjalankan:

| Layanan | Port default | Akses publik |
| --- | --- | --- |
| ComfyUI | `8188` | Cloudflare, Pinggy, atau Localtunnel |
| Provisioning dashboard | `8189` | Cloudflare Tunnel tersendiri |

Kedua server **dan kedua tunnel** memakai `nohup`, stdin terputus dari terminal, process session tersendiri, dan log file. Setup keluar setelah mencetak kedua URL; terminal atau tab dashboard boleh ditutup. **Runtime Colab harus tetap aktif**: `nohup` tidak mempertahankan VM setelah runtime berhenti/reset. URL Quick Tunnel berubah saat tunnel dijalankan ulang.

## Jalankan di Terminal Colab

```bash
git clone https://github.com/azunzios/colab-ltx-setup.git
cd colab-ltx-setup
python3 setup_comfyui.py --dir /content/ComfyUI
```

Atau unduh satu file (dashboard sudah tertanam di dalam script):

```bash
curl -fLsS https://raw.githubusercontent.com/azunzios/colab-ltx-setup/main/setup_comfyui.py -o setup_comfyui.py
python3 setup_comfyui.py --dir /content/ComfyUI
```

`--dir` adalah **root ComfyUI yang berisi `main.py`, `models/`, dan `custom_nodes/`**, bukan `/content`. Default pada Colab adalah `/content/ComfyUI`. Perlu Linux, Python 3.10+, `git`, `wget`, `nohup`, serta dukungan `venv`/`pip`. Script mengunduh `cloudflared` otomatis. `hf` dipasang di environment tools terpisah agar versi library-nya tidak mengubah dependensi ComfyUI.

Jika ComfyUI sudah terpasang:

```bash
python3 setup_comfyui.py --dir /content/ComfyUI --skip-install
```

Buka URL **Dashboard**, lalu masukkan password dashboard yang dicetak terminal. Password ini untuk dashboard, terpisah dari login Hugging Face. Semua API provisioning memerlukan password; path file dibatasi ke workspace ComfyUI. Simpan password karena dashboard bisa menginstal kode/paket. ComfyUI sendiri tetap mengikuti mekanisme akses ComfyUI yang ada.

## Fitur dashboard

- **Download URL langsung** melalui `wget --continue`, dengan retry jaringan. File sementara memakai `.part` dan baru menjadi nama final setelah sukses. Tombol Retry memakai file parsial yang sama. File final yang sudah ada tidak ditimpa.
- **Download Hugging Face**: masukkan `owner/repo`, path satu file dalam repo, revision, kategori dan subfolder tujuan. File dipindahkan langsung ke kategori ComfyUI; misalnya `split_files/loras/foo.safetensors` menjadi `models/loras/foo.safetensors`. Download gagal/batal menyimpan staging untuk Retry; staging yang berhasil dibersihkan.
- **Kategori model**: checkpoints, diffusion_models, unet, loras, vae, text_encoders, clip, clip_vision, controlnet, upscale_models, embeddings, dan style_models. Subfolder tambahan opsional.
- **Login device code Hugging Face**, cek akun, dan log login yang dapat disalin.
- **Custom nodes** dari URL repository GitHub, dengan opsi instal `requirements.txt`. Jika clone sukses tetapi dependensi gagal, Retry melanjutkan pemasangan dependensi tanpa clone ulang.
- **Dependensi** dari file requirements di dalam ComfyUI atau nama paket PyPI. Pemasangan memakai interpreter Python yang sama dengan ComfyUI. Tersedia `pip check` untuk melihat konflik paket.
- **Resource**: penggunaan disk, RAM, CPU load, GPU/VRAM/utilisasi/suhu melalui `nvidia-smi`, status layanan, dan permintaan unload model/free memory ke ComfyUI.
- **Antrean**, cancel, retry, riwayat dan log pekerjaan; daftar file per kategori beserta ukurannya; log ComfyUI dan tunnel. Antrean dijalankan satu per satu. Setelah restart dashboard, pekerjaan yang terputus ditandai `interrupted` dan bisa di-Retry; tidak otomatis dijalankan lagi.

Restart ComfyUI setelah memasang custom node atau dependensi baru. Untuk restart seluruh stack, jalankan `--stop`, lalu `--skip-install` seperti di bawah. File `.part` dan staging HF yang gagal tetap memakai disk sampai di-Retry atau dibersihkan secara manual.

## Login Hugging Face tanpa token manual

Klik **Login Hugging Face**. Dashboard menjalankan:

```bash
hf auth login --force --format agent
```

Buka URL Hugging Face pada log pekerjaan, salin device code, lalu setujui login di browser. CLI menunggu sampai berhasil atau kode kedaluwarsa; tidak meminta Anda menempelkan HF token. Klik **Cek akun** untuk memverifikasi login. Kredensial disimpan oleh HF CLI untuk download berikutnya. Untuk model gated, setujui akses/lisensi pada halaman model terlebih dahulu.

Perintah CLI resminya adalah `hf auth login`. Mode `--format agent` membuat alur device code dapat berjalan tanpa terminal interaktif: [dokumentasi HF CLI](https://huggingface.co/docs/huggingface_hub/en/guides/cli). Login sendiri ikut antrean; batalkan download yang sedang berjalan bila ingin login segera. Memakai sumber Hugging Face pada formulir download akan menggunakan kredensial CLI; URL `wget` biasa tidak menambahkan kredensial HF.

## Status, stop, dan log

```bash
# Melihat PID, kedua URL, password dashboard, dan lokasi log
python3 setup_comfyui.py --dir /content/ComfyUI --status

# Hentikan server, tunnel, dan subprocess provisioning aktif
python3 setup_comfyui.py --dir /content/ComfyUI --stop

# Mulai lagi tanpa menginstal ulang ComfyUI
python3 setup_comfyui.py --dir /content/ComfyUI --skip-install
```

State, PID beserta identitas proses, dan password disimpan di `/content/ComfyUI/.provisioning/state.json` (mode `600`; direktori runtime `700`). Log layanan ada di `.provisioning/logs/`, log/riwayat pekerjaan di `.provisioning/jobs/`. Setup hanya menghentikan process group yang sesuai identitas tersimpan. Menjalankan setup lagi saat layanan aktif akan menampilkan status tanpa membuat proses duplikat. Jika hanya sebagian layanan aktif, gunakan `--stop` lalu jalankan ulang.

## Opsi CLI

| Opsi | Default | Fungsi |
| --- | --- | --- |
| `--dir PATH` | `/content/ComfyUI` di Colab | Root ComfyUI |
| `--host HOST` | `127.0.0.1` | Host server ComfyUI |
| `--port PORT` | `8188` | Port ComfyUI |
| `--dashboard-port PORT` | `8189` | Port dashboard (selalu bind ke loopback) |
| `--tunnel cloudflare\|pinggy\|localtunnel` | `cloudflare` | Penyedia tunnel ComfyUI; dashboard selalu Cloudflare |
| `--skip-install` | off | Lewati clone dan pip install ComfyUI/Manager |
| `--no-manager` | off | Lewati instalasi ComfyUI-Manager |
| `--update` | off | `git pull --ff-only` ComfyUI dan Manager saat instalasi |
| `--install-only` | off | Siapkan ComfyUI dan tools tanpa menjalankan layanan |
| `--extra-args='...'` | `--dont-print-server` | Argumen tambahan ComfyUI, mendukung kutip/spasi |
| `--status` | off | Tampilkan status dan kredensial dashboard |
| `--stop` | off | Hentikan semua layanan dan pekerjaan aktif |

Pinggy memerlukan `ssh`; Localtunnel memerlukan Node.js/`npx`. Contoh:

```bash
python3 setup_comfyui.py --dir /content/ComfyUI --skip-install --tunnel pinggy
python3 setup_comfyui.py --dir /content/ComfyUI --skip-install --dashboard-port 8190 --extra-args='--lowvram --dont-print-server'
```

ComfyUI tetap dijalankan dengan `--enable-cors-header '*'`, dan Cloudflare dengan `--http-host-header`, untuk mendukung akses lewat reverse proxy. Dashboard tidak membuka CORS dan setiap API memerlukan Authorization. Cloudflare Quick Tunnel menyediakan URL acak tanpa akun: [dokumentasi Cloudflare](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Pengujian lokal

```bash
python -m unittest discover -s tests -v
python -m py_compile setup_comfyui.py
```

Pengujian tidak menginstal ComfyUI, tidak login ke akun HF, dan tidak mengunduh model besar. Menjalankan GPU ComfyUI dan tunnel publik perlu diverifikasi pada runtime Linux/Colab.
