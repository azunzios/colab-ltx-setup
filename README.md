# ComfyUI untuk Google Colab

Setup ComfyUI, ComfyUI-Manager, dan dashboard untuk download model serta mengelola custom nodes.

## Mulai

Jalankan di Terminal Colab:

```bash
git clone https://github.com/azunzios/colab-ltx-setup.git
cd colab-ltx-setup
python3 setup_comfyui.py --dir /content/ComfyUI --tunnel ngrok
```

Tempel authtoken dari [dashboard ngrok](https://dashboard.ngrok.com/get-started/your-authtoken) saat diminta. Input disembunyikan dan token disimpan untuk pemakaian berikutnya. Bisa juga memakai environment `NGROK_AUTHTOKEN`.

Buka URL **ComfyUI** yang dicetak terminal. Untuk dashboard, buka URL **Dashboard** dan masukkan password yang dicetak setup. Jika ngrok menampilkan **Visit Site**, klik untuk melanjutkan.

## Perbarui dan jalankan ulang

```bash
git pull --ff-only
python3 setup_comfyui.py --dir /content/ComfyUI --stop
python3 setup_comfyui.py --dir /content/ComfyUI --skip-install --tunnel ngrok
```

## Fitur

- Download model dari URL atau Hugging Face, dengan antrean, retry, dan validasi file.
- Login Hugging Face lewat device code di browser.
- Instal custom nodes dan dependensi Python.
- Pantau disk, RAM, GPU, VRAM, serta log layanan.
- Kompresi respons API dan preview sampler 256px otomatis jika didukung ComfyUI. Resolusi hasil tetap mengikuti workflow.
- ngrok berjalan tanpa inspeksi HTTP lokal. Dashboard memakai tunnel Cloudflare tersendiri.

## Perintah lain

```bash
# Status, URL, password dashboard, dan lokasi log
python3 setup_comfyui.py --dir /content/ComfyUI --status

# Hentikan layanan
python3 setup_comfyui.py --dir /content/ComfyUI --stop

# Semua opsi
python3 setup_comfyui.py --help
```

Pilihan tunnel: `ngrok`, `cloudflare` (default), `pinggy`, dan `localtunnel`. Gunakan `--no-web-optimizations` untuk mematikan optimasi otomatis atau `--extra-args='--lowvram --dont-print-server'` untuk argumen tambahan ComfyUI.

Perlu Linux/Colab, Python 3.10+, `git`, `wget`, `nohup`, dan `venv`. Terminal boleh ditutup setelah URL muncul; runtime Colab harus tetap aktif. Simpan file aktif di `/content/ComfyUI` untuk menghindari akses disk lewat Drive. Kecepatan transfer tetap bergantung pada koneksi dan rute tunnel.
