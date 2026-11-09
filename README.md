# Colab ComfyUI + Cloudflare / Pinggy / Localtunnel Setup

Script otomatisasi berbasis Python (`setup_comfyui.py`) untuk menginstal **ComfyUI** (beserta **ComfyUI-Manager**) dan menjalankannya dengan **Cloudflare Tunnel (`trycloudflare.com`)**, **Pinggy (`pinggy.link`)**, atau **Localtunnel (`loca.lt`)** langsung dari **Terminal Google Colab**.

## Fitur Utama
- **Bebas `HTTP ERROR 403` pada Cloudflare Tunnel**: Secara otomatis menyertakan `--enable-cors-header *` pada ComfyUI dan `--http-host-header` pada `cloudflared` untuk menonaktifkan pemblokiran `origin_only_middleware` bawaan ComfyUI yang biasanya memicu *Access Denied (HTTP ERROR 403)*.
- **Multi-Tunnel Support**: Mendukung `cloudflare` (default), `pinggy` (via SSH, tanpa instalasi tambahan), dan `localtunnel` (via `npx`).
- **Otomatisasi Penuh via Terminal**: Clone repository ComfyUI, instal seluruh dependensi `requirements.txt`, dan pasang `ComfyUI-Manager`.
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

### Menggunakan Tunnel Alternatif (Jika Cloudflare Bermasalah)
```bash
# Menggunakan Pinggy Tunnel (*.pinggy.link)
python3 setup_comfyui.py --tunnel pinggy

# Menggunakan Localtunnel (*.loca.lt)
python3 setup_comfyui.py --tunnel localtunnel
```

---

## Opsi Argumen CLI

| Argumen | Default | Deskripsi |
| :--- | :--- | :--- |
| `--tunnel {cloudflare,pinggy,localtunnel}` | `cloudflare` | Penyedia tunnel internet (`cloudflare`, `pinggy`, atau `localtunnel`) |
| `--dir PATH` | `/content/ComfyUI` | Direktori target instalasi ComfyUI |
| `--host HOST` | `127.0.0.1` | Host lokal tempat ComfyUI berjalan |
| `--port PORT` | `8188` | Port lokal tempat ComfyUI berjalan |
| `--no-manager` | `False` | Lewati instalasi `ComfyUI-Manager` |
| `--update` | `False` | Lakukan `git pull` pada ComfyUI & ComfyUI-Manager jika sudah ada |
| `--install-only` | `False` | Hanya instal ComfyUI dan `cloudflared` tanpa menyalakan server |
| `--extra-args "..."` | `--dont-print-server` | Argumen tambahan yang diteruskan ke `main.py` ComfyUI |

---

## Mengapa Sebelumnya Muncul `HTTP ERROR 403` di `trycloudflare.com`?

Pesan:
> `Access to xxxx.trycloudflare.com was denied`
> `You don't have authorization to view this page.`
> `HTTP ERROR 403`

**Bukan disebabkan oleh Cloudflare yang memblokir IP Google Colab**, melainkan berasal dari middleware keamanan internal ComfyUI (`create_origin_only_middleware` di `server.py`). Ketika Anda mengakses ComfyUI melalui `https://xxxx.trycloudflare.com`, browser mengirimkan header `Origin: https://...` sedangkan koneksi lokal `cloudflared` ke ComfyUI menggunakan `http://127.0.0.1:8188`. Ketidakcocokan protokol/origin ini membuat ComfyUI menolak request dengan status `403 Forbidden` kosong.

Script ini mengatasinya secara permanen dengan:
1. Menjalankan ComfyUI menggunakan flag `--enable-cors-header *` (sehingga `origin_only_middleware` digantikan oleh `cors_middleware`).
2. Menjalankan `cloudflared` dengan `--http-host-header 127.0.0.1:8188`.
