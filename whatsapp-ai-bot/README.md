# WhatsApp AI + Downloader (starter gratis)

Starter project memakai **WhatsApp Cloud API resmi**, Vercel (webhook), Supabase (riwayat AI), Gemini API (AI), dan service Python di Render (yt-dlp, stiker, TTS).

> Penting: ini starter yang perlu diisi dengan credential milikmu dan dites. Paket gratis memiliki batas; Render Free dapat sleep, dan Meta/Google dapat menerapkan kuota atau biaya sesuai akun/pemakaian. Jangan menganggap semua situs video pasti didukung.

## Isi folder
- `api/webhook.js` — webhook WhatsApp, AI chat, memory, translate, kalkulator teks/gambar, pemicu downloader.
- `downloader/main.py` — downloader video/audio, stiker WebP, text-to-speech.
- `downloader/Dockerfile` dan `downloader/render.yaml` — deployment Render.
- `supabase/schema.sql` — tabel memori chat.
- `.env.example` — daftar environment variables yang harus diisi.

## 1. Deploy webhook ke Vercel
1. Upload seluruh isi folder project ini ke repository GitHub baru.
2. Di Vercel, pilih **Add New → Project**, import repository tersebut, dan deploy.
3. Tambahkan environment variables dari `.env.example` di **Vercel → Project → Settings → Environment Variables**. Isi nilai sebenarnya; jangan pakai teks `replace_with...`.
4. Deploy ulang setelah menambah/mengubah environment variables.
5. URL webhook-nya adalah `https://NAMA-PROJECT.vercel.app/api/webhook`.

## 2. Siapkan WhatsApp Cloud API
1. Buat/siapkan aplikasi di Meta for Developers dan tambahkan produk WhatsApp.
2. Ambil `Phone Number ID` dan access token dari dashboard WhatsApp API.
3. Buat string acak sendiri untuk `WHATSAPP_VERIFY_TOKEN`, lalu isi nilai yang sama di Vercel dan kolom Verify token pada konfigurasi webhook Meta.
4. Callback URL: `https://NAMA-PROJECT.vercel.app/api/webhook`.
5. Verifikasi webhook dan subscribe ke field **messages**.
6. Ikuti ketentuan Meta untuk nomor tes/nomor bisnis dan template pesan bila diperlukan.

## 3. Siapkan Gemini
1. Buat API key di Google AI Studio.
2. Isi `GEMINI_API_KEY` di Vercel.
3. Model default `gemini-2.5-flash` dapat diubah lewat `GEMINI_MODEL` jika tidak tersedia di akun/region kamu.

## 4. Siapkan Supabase memory
1. Buat project Supabase.
2. Buka SQL Editor dan jalankan isi `supabase/schema.sql`.
3. Isi `SUPABASE_URL` dan `SUPABASE_SERVICE_ROLE_KEY` di Vercel. **Service role key hanya boleh berada di environment server**, jangan taruh di frontend atau kirim ke orang lain.

## 5. Deploy downloader Python ke Render
1. Pastikan folder `downloader/` ada di root repository.
2. Di Render, buat **New → Blueprint** dari repository yang sama. Render membaca `render.yaml` di root repository.
3. Jika Blueprint tidak mendeteksi file karena konfigurasi root directory, buat Web Service manual dengan root directory repository, runtime Docker, dan Dockerfile path `downloader/Dockerfile`.
4. Isi environment variables di Render:
   - `DOWNLOADER_SHARED_SECRET` — string acak panjang; nilainya harus sama dengan `DOWNLOADER_SHARED_SECRET` di Vercel.
   - `WHATSAPP_ACCESS_TOKEN` — token WhatsApp Cloud API yang sama.
   - `WHATSAPP_PHONE_NUMBER_ID` — Phone Number ID yang sama.
   - `GRAPH_API_VERSION` — default `v23.0`, sesuaikan bila dashboard Meta meminta versi lain.
5. Setelah service aktif, salin URL Render (misalnya `https://nama-service.onrender.com`) ke `DOWNLOADER_URL` di Vercel, lalu deploy ulang.
6. Cek `https://URL-RENDER/health`, hasil normal: `{"ok":true}`.

## 6. Perintah bot
- `menu`
- Chat biasa untuk AI
- `translate Inggris | selamat pagi`
- `calc (12+8)*3`
- `tts Halo, apa kabar?`
- Kirim foto soal matematika untuk dianalisis AI
- Kirim foto dengan caption `stiker` untuk membuat stiker
- Kirim URL video publik untuk unduh video
- `mp3 https://...` untuk unduh audio

## Batasan dan troubleshooting
- Downloader membatasi file media sampai 15 MB agar lebih mungkin dikirim melalui WhatsApp Cloud API. Beberapa format/situs bisa gagal; konten privat, login, DRM, dan perubahan situs tidak dijamin.
- Render Free bisa tidur setelah tidak aktif; permintaan pertama dapat lambat. Background job dapat terhenti bila service mati/restart, jadi ini tidak ideal untuk file besar.
- Gemini dan WhatsApp harus sudah aktif dan memenuhi syarat akun. Jendela percakapan, template, tarif, kuota, dan batas media mengikuti kebijakan Meta yang berlaku.
- Kalau `GEMINI_API_KEY belum diatur`, cek Vercel environment variables.
- Kalau `Unauthorized` dari downloader, pastikan `DOWNLOADER_SHARED_SECRET` sama persis di Vercel dan Render.
- Kalau webhook gagal verifikasi, samakan `WHATSAPP_VERIFY_TOKEN` antara Vercel dan Meta.
- Jangan pernah commit token, service-role key, atau secret ke GitHub.
