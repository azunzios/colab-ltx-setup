# Colab ComfyUI + Cloudflare Tunnel Setup

Script otomatisasi berbasis Python (`setup_comfyui.py`) untuk menginstal **ComfyUI** (beserta **ComfyUI-Manager**) dan menjalankannya dengan **Cloudflare Tunnel (`trycloudflare.com`)** langsung dari **Terminal Google Colab** tanpa perlu menjalankan sel-sel notebook satu per satu.

## Fitur Utama
- **Otomatisasi Penuh via Terminal**: Clone repository ComfyUI, instal seluruh dependensi `requirements.txt`, dan pasang `ComfyUI-Manager`.
- **Cloudflare Tunnel Terintegrasi**: Mengunduh binary `cloudflared` secara otomatis (tanpa perlu `apt`/`dpkg` root lock), menunggu port lokal ComfyUI siap (`127.0.0.1:8188`), lalu menampilkan tautan publik `https://xxxx.trycloudflare.com` di terminal.
- **Fleksibel & Idempoten**: Aman dijalankan berulang kali; tidak akan mengunduh ulang file yang sudah ada kecuali diminta (`--update`).

---

## Cara Penggunaan di Terminal Google Colab

Buka **Terminal** di Google Colab, lalu jalankan salah satu metode berikut:

### Metode 1: Clone Repository & Jalankan
```bash
git clone https://github.com/azunzios/colab-ltx-setup.git
cd colab-ltx-setup
python3 setup_comfyui.py
```

### Metode 2: One-Liner (Langsung Eksekusi)
```bash
curl -sL https://raw.githubusercontent.com/azunzios/colab-ltx-setup/main/setup_comfyui.py -o setup_comfyui.py && python3 setup_comfyui.py
```

---

## Opsi Argumen CLI

Anda dapat menyesuaikan perilaku script menggunakan argumen tambahan:

| Argumen | Default | Deskripsi |
| :--- | :--- | :--- |
| `--dir PATH` | `/content/ComfyUI` | Direktori target instalasi ComfyUI |
| `--host HOST` | `127.0.0.1` | Host lokal tempat ComfyUI berjalan |
| `--port PORT` | `8188` | Port lokal tempat ComfyUI berjalan |
| `--no-manager` | `False` | Lewati instalasi `ComfyUI-Manager` |
| `--update` | `False` | Lakukan `git pull` pada ComfyUI & ComfyUI-Manager jika sudah ada |
| `--install-only` | `False` | Hanya instal ComfyUI dan `cloudflared` tanpa menyalakan server |
| `--extra-args "..."` | `--dont-print-server` | Argumen tambahan yang diteruskan ke `main.py` ComfyUI |

### Contoh Penggunaan Lanjutan
```bash
# Hanya instalasi saja tanpa menjalankan server
python3 setup_comfyui.py --install-only

# Menjalankan di port 8189 dengan argumen tambahan --highvram
python3 setup_comfyui.py --port 8189 --extra-args "--dont-print-server --highvram"
```
