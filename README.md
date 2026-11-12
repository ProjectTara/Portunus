# Portunus

Portunus menampilkan dan mengendalikan layar iPhone dari PC Windows lewat kabel USB. Klik dan seret di layar untuk menyentuh iPhone, ketik langsung dengan keyboard PC, tukar teks lewat clipboard, dan baca teks dari layar dengan OCR.

Nama Portunus diambil dari dewa Romawi penjaga kunci, pintu, dan pelabuhan. Aplikasi ini berperan serupa: menjaga satu pintu (port USB) antara PC dan iPhone, dan hanya membukanya untuk jendelanya sendiri.

Portunus tidak berafiliasi dengan Apple. "iPhone" adalah merek dagang Apple Inc.

## Kebutuhan

- Windows 10 atau 11, 64-bit.
- iPhone dengan iOS 27 atau lebih baru.
- Kabel USB yang mendukung data (bukan kabel khusus charge).
- Python 3.13 (wajib untuk mode Wi-Fi).

## Persiapan pertama kali

Ikuti urutan ini sekali saja.

1. Pasang **Apple Devices** dari Microsoft Store. Aplikasi ini memasang driver USB iPhone dan layanan Apple Mobile Device.
2. Pasang **HEVC Video Extensions** dari Microsoft Store. iPhone mengirim video H.265. Tanpa ekstensi ini, layar tampil hitam.
3. Colok iPhone, buka kuncinya, lalu ketuk **Percayai** saat iPhone bertanya.
4. Aktifkan Developer Mode di iPhone: Settings, Privacy & Security, Developer Mode, lalu restart iPhone dan ketuk Turn On. Jika menu Developer Mode tidak ada, buka Portunus dan tekan **Munculkan menu Developer Mode** di bagian Persiapan.

Saat pertama kali tersambung, Portunus memasang Developer Disk Image di iPhone. Langkah ini butuh internet dan hanya terjadi sekali.

## Menjalankan dari kode sumber

Nama paket di PyPI: `portunus-mirror`. Nama modul Python: `portunus`.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python -m portunus
```

`requirements.lock` mengunci seluruh 108 paket, termasuk dependensi tidak langsung, dengan hash SHA-256 untuk Windows 64-bit dan Python 3.13. Pip menolak memasang file yang hash-nya berbeda. Semua versi dirilis paling lambat 27 Oktober 2026, minimal dua minggu sebelum dikunci. Untuk Python versi lain, pakai `requirements.txt`. File itu hanya mengunci dependensi langsung.

Paket `winrt` di `requirements.txt` hanya dipakai untuk OCR dan audio Bluetooth. Jika gagal terpasang, Portunus tetap berjalan tanpa dua fitur itu.

## Membuat .exe

```powershell
.venv\Scripts\activate
build.cmd
```

Hasilnya ada di `dist\Portunus\Portunus.exe`. File ini tidak ditandatangani, jadi Windows SmartScreen akan bertanya sekali saat pertama dijalankan. Portunus tidak pernah menghapus tanda keamanan (Mark of the Web) dari file apa pun.

## Cara pakai

| Aksi | Cara |
| --- | --- |
| Sentuh, geser | Klik atau seret di layar |
| Gulir | Roda mouse atau dua jari di touchpad |
| Mengetik | Klik layar sekali, lalu ketik. Klik di luar layar untuk berhenti |
| Salin, tempel, urungkan di iPhone | Ctrl+C, Ctrl+V, Ctrl+Z (Ctrl berfungsi sebagai Command) |
| Beranda | Ctrl+Shift+H |
| Kunci iPhone | Ctrl+Shift+L |
| Tempel teks PC ke iPhone | Ctrl+Shift+V |
| Salin pilihan di iPhone ke PC | Ctrl+Shift+C |
| Baca teks dari layar (OCR) | Ctrl+Shift+O, lalu seret kotak atau klik sekali untuk seluruh layar |
| Simpan gambar layar | Ctrl+Shift+S |
| Putar tampilan (misalnya video YouTube layar penuh) | Tombol Putar atau Ctrl+Shift+]. Shift-klik, klik kanan, atau Ctrl+Shift+[ untuk arah sebaliknya |
| Mode fokus: layar penuh, sidebar muncul di tepi kiri | F11 (Esc atau F11 untuk keluar) |
| Lembar semua pintasan | ? |
| Panel diagnostik | Ctrl+Shift+D |

Suara iPhone diputar di PC lewat Bluetooth (uji coba), karena audio lewat USB hanya didukung di macOS. Caranya: di PC buka Settings, Bluetooth & devices, Add device, Bluetooth, lalu pilih iPhone (Bluetooth iPhone harus menyala dan iPhone membuka Settings, Bluetooth). Setelah terpasang, buka Pengaturan Portunus, pilih iPhone di bagian Suara, lalu tekan Putar di PC.

## Keamanan

Portunus menjalankan satu server HTTP kecil di `127.0.0.1` dengan port acak. Server ini hanya untuk jendela Portunus sendiri, dan setiap permintaan melewati pemeriksaan yang sama:

- Header `Host` harus persis `127.0.0.1:<port>`. Ini menolak serangan DNS rebinding.
- `Origin` dan `Sec-Fetch-Site`, jika ada, harus berasal dari Portunus sendiri.
- Semua endpoint selain file tampilan butuh token acak per sesi. Token ini diberikan ke halaman lewat jembatan JavaScript pywebview dan tidak pernah muncul di URL.
- Permintaan POST harus `application/json`, dengan batas ukuran per endpoint. Form dari situs lain tidak bisa memenuhi syarat ini tanpa preflight CORS, dan Portunus tidak pernah menyetujui preflight.
- Hanya endpoint yang terdaftar yang diteruskan ke mesin siaran. Endpoint debug dan riset di `pymobiledevice3` tidak bisa dijangkau.
- Mesin siaran berjalan di port acak lain dengan password acak yang hanya diketahui proses Portunus.
- Pemeriksaan di atas berjalan hanya dari header, sebelum satu byte body pun dibaca. Pengirim tanpa izin tidak bisa membuat Portunus menampung data.
- Batas waktu: 10 detik untuk seluruh header, 30 detik untuk body, 120 detik untuk koneksi diam. Koneksi serentak dibatasi 48.
- Sebelum mengirim password atau input apa pun ke mesin siaran, Portunus meminta sistem operasi memastikan port itu hanya dibuka oleh proses Portunus sendiri. Jika ada program lain di port yang sama, sambungan dibatalkan.
- Token hanya diberikan ke halaman yang berada di alamat Portunus sendiri.
- Halaman memakai Content-Security-Policy ketat tanpa script inline, dan tidak memakai `innerHTML` atau sejenisnya. Semua teks dari iPhone ditampilkan sebagai teks biasa.

Yang tidak disimpan: PIN, kata sandi, isi clipboard, teks hasil OCR, dan apa pun yang diketik. Pengaturan di `%APPDATA%\Portunus\settings.json` hanya berisi tema, kecepatan gulir, dan dua sakelar. Log di `%LOCALAPPDATA%\Portunus\portunus.log` hanya mencatat status sambungan.

Portunus tidak mengubah apa pun di iPhone tanpa aksi Anda. Perubahan hanya terjadi saat pairing (iOS menampilkan dialognya sendiri), saat Developer Disk Image dipasang ketika Anda menekan Hubungkan, dan saat Anda menekan Munculkan menu Developer Mode.

## Sambungan Wi-Fi (uji coba)

Portunus bisa menyambung tanpa kabel bila PC dan iPhone berada di jaringan Wi-Fi yang sama.

1. Buka **Pengaturan**, centang **Izinkan menyambung iPhone tanpa kabel**. Pilihan ini mati secara bawaan.
2. Colok iPhone sekali dengan kabel, lalu tekan **Aktifkan Wi-Fi** di baris iPhone itu. Portunus membuat izin Wi-Fi lewat USB yang sudah dipercaya, tanpa dialog tambahan di iPhone.
3. Lain kali iPhone muncul di daftar dengan label **Wi-Fi**. Tekan **Hubungkan** tanpa kabel. Layar iPhone harus menyala.

Keamanan: perangkat di jaringan dicocokkan dengan izinnya lewat tanda kriptografis sebelum data apa pun dikirim, dan terowongannya terenkripsi dengan kunci izin itu. Tidak ada port yang dibuka di PC dan tidak perlu hak admin. Portunus tidak mengubah setelan apa pun di iPhone. **Cabut Wi-Fi** menghapus izin di PC ini; untuk mencabut dari sisi iPhone juga, buka Settings, Developer, Clear Trusted Computers. Mematikan pilihan Wi-Fi di Pengaturan langsung memutus sesi Wi-Fi yang sedang berjalan.

Latensi bergantung pada jaringan; router 5 GHz terasa jauh lebih baik. Android tidak didukung: Portunus memakai protokol Apple.

## Alat pengembang

`tools/ui_sim.py` menjalankan UI asli di Firefox headless dengan backend tiruan:

- `python tools/ui_sim.py test` menguji tampilan di 8 ukuran layar ponsel dan tablet, tegak dan miring, 6 ukuran jendela, pantulan nyala dan mati (192 kasus).
- `python tools/ui_sim.py video simulasi.mp4` merekam video simulasi alur aplikasi.

Butuh `selenium`, Firefox dengan geckodriver (variabel `FIREFOX`, `GECKODRIVER`), dan `ffmpeg`.

## Rilis lewat GitHub dan pembaruan satu klik

Setiap kali Anda menerbitkan Release di github.com, GitHub Actions menjalankan test, membangun `Portunus.exe`, menandatanganinya, lalu menempelkan `Portunus-win64.zip` ke Release itu. Portunus yang sudah terpasang akan menampilkan tombol **Perbarui**. Satu klik mengunduh, memverifikasi, memasang, dan membuka ulang Portunus.

Repo harus **Public**. Pembaruan otomatis membaca Release tanpa login, jadi Release di repo privat tidak terlihat.

### Pertama kali (sekali saja)

1. Ekstrak proyek, misalnya ke `D:\Proyek\portunus`, lalu buka PowerShell di folder itu.
2. Buat kunci tanda tangan:
   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install --require-hashes -r requirements.lock
   .venv\Scripts\python tools\keygen.py
   ```
   Kunci publik otomatis ditulis ke `portunus\update_key.py`. Kunci privat tampil sekali di layar. Simpan di password manager. Jangan pernah diunggah atau dikirim ke siapa pun.
3. Di github.com, klik **New repository**. Beri nama `portunus`, pilih **Public**, dan jangan centang README, .gitignore, atau license.
4. Di halaman repo kosong, klik **uploading an existing file**. Dari File Explorer, pilih lalu seret isi folder proyek **kecuali** `.venv`, `dist`, `build`, `release`, dan `__pycache__`. Upload lewat web tidak membaca `.gitignore`. Yang diseret: `.github`, `portunus`, `tests`, `tools`, `.gitignore`, `build.cmd`, `LICENSE`, `pyproject.toml`, `README.md`, `requirements.lock`, `requirements.txt`. Klik **Commit changes**.
5. Periksa bahwa folder `.github/workflows` ada di repo. Jika tidak ada, klik **Add file, Create new file**, ketik nama `.github/workflows/release.yml`, tempel isi file itu dari komputer, lalu commit. Ulangi untuk `ci.yml`.
6. Buka **Settings, Secrets and variables, Actions, New repository secret**. Name: `PORTUNUS_SIGNING_KEY`. Secret: kunci privat dari langkah 2.
7. Buka **Releases, Draft a new release**. Di **Choose a tag**, ketik `v0.2.0` lalu pilih **Create new tag: v0.2.0 on publish**. Isi judul, lalu klik **Publish release**.
8. Pantau di tab **Actions**. Setelah sekitar 10 menit, tiga file muncul di Release itu: `Portunus-win64.zip`, `portunus-manifest.json`, dan `portunus-manifest.json.sig`.
9. Unduh `Portunus-win64.zip`, ekstrak ke `%LOCALAPPDATA%\Programs\Portunus`, lalu jalankan `Portunus.exe`. Jangan taruh di Program Files, karena folder itu tidak bisa ditulis dan pembaruan otomatis akan mati.

### Setiap ada pembaruan

1. Di github.com, ubah versi di `portunus/__init__.py` dan `pyproject.toml` lewat ikon pensil, misalnya menjadi `0.2.1`, lalu commit. Untuk mengganti file lain, buka **Add file, Upload files** di folder yang sama, seret file baru (file lama dengan nama sama akan ditimpa), lalu commit.
2. Buka **Releases, Draft a new release**, buat tag `v0.2.1`, lalu **Publish release**.
3. Selesai. Setelah Actions selesai, Portunus menampilkan tombol **Perbarui** di kiri bawah.

Tag harus sama dengan `__version__`. Jika berbeda, Actions sengaja gagal. Jika Actions gagal, hapus Release dan tag-nya (**Releases**, buka rilisnya, **Delete**, lalu di **Tags** hapus tag-nya), perbaiki, lalu terbitkan lagi.

### Pengaman pembaruan

- Manifest rilis (versi, ukuran, SHA-256 zip) ditandatangani Ed25519. Kunci publiknya tertanam di aplikasi. Tanpa kunci privat, rilis palsu ditolak, termasuk jika akun GitHub Anda diambil alih.
- Hanya versi yang lebih baru yang dipasang. Rilis lama tidak bisa disodorkan sebagai versi baru.
- Unduhan hanya dari `https://` di host milik GitHub, termasuk saat redirect.
- Isi zip diperiksa sebelum dibuka: tidak boleh ada `..`, path absolut, huruf drive, alternate data stream, atau symlink.
- Folder lama disimpan sebagai cadangan selama penggantian. Jika penyalinan gagal, folder lama dikembalikan.
- Workflow memakai action yang dikunci ke SHA commit, dependensi berhash, dan izin token minimal.

Disarankan juga: di **Settings, Environments, release**, tambahkan diri Anda sebagai *required reviewer* agar setiap build rilis butuh persetujuan klik. Aktifkan juga autentikasi dua faktor di akun GitHub.

Jika kunci privat hilang, salinan yang sudah terpasang tidak bisa diperbarui otomatis lagi. Buat kunci baru, terbitkan rilis, dan minta pengguna mengunduh manual sekali.

## Menjalankan test

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

Test memeriksa pemeriksaan keamanan (termasuk body besar tanpa izin, header lambat, banjir koneksi, dan program asing di port mesin siaran), penerusan ke mesin siaran palsu, skema pengaturan, dan penyusunan paragraf OCR. Test ini tidak butuh iPhone dan berjalan di sistem operasi apa pun.

## Batasan

- Suara lewat USB belum didukung di Windows, karena `pymobiledevice3` hanya bisa men-decode audio iPhone di macOS. Gunakan opsi Bluetooth.
- Mirroring layar membutuhkan iOS 27 atau lebih baru.
- Saat layar bergerak cepat, iPhone kadang menurunkan resolusi sebentar. Ini keputusan encoder di iPhone.

## Pihak ketiga

Ikon dari [Lucide](https://lucide.dev) v1.63.0 (lisensi ISC), disalin sebagai SVG ke `portunus/ui/index.html`.

## Lisensi

GPL-3.0. Portunus dibangun di atas [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), yang juga berlisensi GPL-3.0.
