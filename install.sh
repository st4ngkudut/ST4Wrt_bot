#!/bin/sh

# ==============================================================================
# Skrip Instalasi Otomatis ST4Wrt Bot (Versi Perbaikan)
# ==============================================================================

# Fungsi warna teks
print_info()     { printf "\033[34m%s\033[0m\n" "$1"; }
print_success()  { printf "\033[32m%s\033[0m\n" "$1"; }
print_warning()  { printf "\033[33m%s\033[0m\n" "$1"; }
print_error()    { printf "\033[31m%s\033[0m\n" "$1"; }

# Hentikan skrip jika error
set -e

# --- Cek versi Python (PTB butuh Python ≥ 3.10) ---
print_info "➡️ Memeriksa versi Python..."
python3 - << 'EOF'
import sys
if sys.version_info < (3,10):
    print("Python 3.10 atau lebih baru diperlukan! Update firmware OpenWrt Anda.")
    exit(1)
EOF

print_success "✅ Versi Python memenuhi syarat."

# --- Langkah 1: Install Dependensi ---
print_info "➡️ [Langkah 1/5] Memperbarui paket dan menginstal dependensi..."
opkg update
opkg install python3 python3-pip git git-http wrtbwmon etherwake nano || true
opkg install speedtest-go || print_warning "⚠️ speedtest-go tidak tersedia untuk arsitektur ini."

print_success "✅ Dependensi sistem berhasil diinstal."
sleep 2 && clear

# --- Langkah 2: Install library Python ---
print_info "➡️ [Langkah 2/5] Menginstal library Python..."

# OpenWrt kadang butuh --break-system-packages
pip install --break-system-packages python-telegram-bot python-dotenv python-telegram-bot[job-queue]

print_success "✅ Library Python berhasil diinstal."
sleep 2 && clear

# --- Langkah 3: Siapkan Direktori Proyek ---
BOT_DIR="/root/ST4Wrt-bot"
print_info "➡️ [Langkah 3/5] Menyiapkan direktori proyek di $BOT_DIR..."

if [ -d "$BOT_DIR" ]; then
    print_warning "⚠️ Direktori sudah ada. Skip kloning Git."
    cd "$BOT_DIR"
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
    cd "$BOT_DIR"
    print_success "✅ Repositori berhasil diklon."
fi

# Siapkan file penting
touch .env
[ -s device_aliases.json ] || echo "{}" > device_aliases.json

echo ".env" > .gitignore
echo "device_aliases.json" >> .gitignore

print_success "✅ File proyek & konfigurasi siap."
sleep 2 && clear

# --- Langkah 4: Konfigurasi .env ---
print_info "➡️ [Langkah 4/5] Konfigurasi Bot..."

# Ambil input Token Bot
while true; do
    printf "Masukkan Token Bot Telegram Anda: "
    read -r TELEGRAM_BOT_TOKEN
    [ -n "$TELEGRAM_BOT_TOKEN" ] && break
    print_error "Token tidak boleh kosong!"
done

# Ambil Admin ID
while true; do
    printf "Masukkan Admin ID Telegram Anda (angka): "
    read -r TELEGRAM_ADMIN_ID
    echo "$TELEGRAM_ADMIN_ID" | grep -qE '^[0-9]+$' && break
    print_error "Admin ID harus angka!"
done

printf "Masukkan interface WiFi Tamu (opsional, contoh: wlan1-1): "
read -r GUEST_WIFI_IFACE

# Buat file .env (perbaikan ekspansi variabel)
cat > .env << EOF
# Konfigurasi ST4Wrt Bot
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
TELEGRAM_ADMIN_ID="$TELEGRAM_ADMIN_ID"
EOF

# Tambah WiFi tamu jika ada
if [ -n "$GUEST_WIFI_IFACE" ]; then
    echo "" >> .env
    echo "# Interface WiFi tamu" >> .env
    echo "GUEST_WIFI_IFACE=\"$GUEST_WIFI_IFACE\"" >> .env
fi

# Keamanan .env
chmod 600 .env

print_success "✅ File .env berhasil dibuat."
sleep 2 && clear

# --- Langkah 5: Membuat Layanan init.d ---
print_info "➡️ [Langkah 5/5] Membuat layanan autostart..."

INIT_SCRIPT_PATH="/etc/init.d/st4wrt-bot"

cat > "$INIT_SCRIPT_PATH" << 'EOF'
#!/bin/sh /etc/rc.common

NAME=st4wrt-bot
BOT_DIR="/root/ST4Wrt-bot"

START=99
STOP=10

USE_PROCD=1

start_service() {
    procd_open_instance "$NAME"
    procd_set_param command /usr/bin/python3 $BOT_DIR/bot.py
    procd_set_param dir "$BOT_DIR"
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}

stop_service() {
    echo "Menghentikan bot..."
}
EOF

chmod +x "$INIT_SCRIPT_PATH"
"$INIT_SCRIPT_PATH" enable
"$INIT_SCRIPT_PATH" start

print_success "✅ Layanan bot berhasil dibuat dan dijalankan!"
print_info "==============================================================="
print_info "🎉 Instalasi selesai!"
print_info "Perintah penting:"
print_info "  👉 Cek status bot : /etc/init.d/st4wrt-bot status"
print_info "  👉 Mulai ulang    : /etc/init.d/st4wrt-bot restart"
print_info "  👉 Log realtime   : logread -f"
print_info "==============================================================="
