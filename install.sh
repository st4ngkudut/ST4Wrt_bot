#!/bin/sh

# ==============================================================================
# Skrip Instalasi Otomatis ST4Wrt Bot (Versi Perbaikan)
# ==============================================================================

# Fungsi untuk mencetak teks berwarna
print_info() {
    printf "\033[34m%s\033[0m\n" "$1"
}
print_success() {
    printf "\033[32m%s\033[0m\n" "$1"
}
print_warning() {
    printf "\033[33m%s\033[0m\n" "$1"
}
print_error() {
    printf "\033[31m%s\033[0m\n" "$1"
}

# Hentikan skrip jika ada error
set -e

# --- Langkah 1: Instalasi Dependensi ---
print_info "➡️ [Langkah 1/5] Memperbarui daftar paket dan menginstal dependensi..."
opkg update
opkg install python3 python3-pip git git-http wrtbwmon speedtest-go etherwake nano

print_success "✅ Dependensi sistem berhasil diinstal."
sleep 3 && clear

# --- Langkah 2: Instal Library Python ---
print_info "➡️ [Langkah 2/5] Menginstal library Python yang dibutuhkan..."
pip install python-telegram-bot python-dotenv

print_success "✅ Library Python berhasil diinstal."
sleep 3 && clear

# --- Langkah 3: Penyiapan Direktori dan File Proyek ---
BOT_DIR="/root/ST4Wrt-bot"
print_info "➡️ [Langkah 3/5] Menyiapkan direktori proyek di $BOT_DIR..."

if [ -d "$BOT_DIR" ]; then
    print_warning "⚠️ Direktori $BOT_DIR sudah ada. Melewatkan kloning dari GitHub."
    cd "$BOT_DIR"
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
    cd "$BOT_DIR"
    print_success "✅ Repositori berhasil diklon."
fi

# Buat file-file konfigurasi jika belum ada
touch .env
# Inisialisasi device_aliases.json jika belum ada atau kosong
if [ ! -s "device_aliases.json" ]; then
    echo "{}" > device_aliases.json
fi
# Siapkan .gitignore
echo ".env" > .gitignore
echo "device_aliases.json" >> .gitignore

print_success "✅ File proyek dan konfigurasi berhasil disiapkan."
sleep 3 && clear

# --- Langkah 4: Konfigurasi .env Interaktif ---
print_info "➡️ [Langkah 4/5] Meminta informasi untuk konfigurasi..."

# Meminta Token Bot hingga input valid
while true; do
    printf "Silakan masukkan Token Bot Anda dari @BotFather: "
    read -r TELEGRAM_BOT_TOKEN
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        break
    else
        print_error "Token Bot tidak boleh kosong. Silakan coba lagi."
    fi
done

# Meminta Admin ID hingga input valid
while true; do
    printf "Silakan masukkan Admin ID Telegram Anda (hanya angka): "
    read -r TELEGRAM_ADMIN_ID
    if [ -n "$TELEGRAM_ADMIN_ID" ] && echo "$TELEGRAM_ADMIN_ID" | grep -qE '^[0-9]+$'; then
        break
    else
        print_error "Admin ID harus berupa angka dan tidak boleh kosong. Silakan coba lagi."
    fi
done

# Meminta pengaturan opsional (Guest WiFi)
printf "Masukkan nama interface WiFi Tamu (contoh: wlan1-1). Biarkan kosong jika tidak ada: "
read -r GUEST_WIFI_IFACE

# Menulis konfigurasi ke file .env
# [PERBAIKAN] Menggunakan 'EOF' untuk mencegah ekspansi variabel oleh shell
cat > .env << 'EOF'
# Konfigurasi ST4Wrt Bot
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
TELEGRAM_ADMIN_ID="$TELEGRAM_ADMIN_ID"
EOF

# [PERBAIKAN] Hanya tambahkan GUEST_WIFI_IFACE jika pengguna memasukkan nilainya
if [ -n "$GUEST_WIFI_IFACE" ]; then
    # Menambahkan baris baru jika file .env tidak berakhir dengan baris baru
    [ -n "$(tail -c1 .env)" ] && echo "" >> .env
    echo "# (Opsional) Untuk fitur WiFi Tamu" >> .env
    echo "GUEST_WIFI_IFACE=\"$GUEST_WIFI_IFACE\"" >> .env
fi

print_success "✅ File .env berhasil dibuat."
sleep 3 && clear

# --- Langkah 5: Membuat dan Mengaktifkan Layanan init.d ---
print_info "➡️ [Langkah 5/5] Membuat layanan autostart..."
INIT_SCRIPT_PATH="/etc/init.d/st4wrt-bot"

# Menulis skrip init.d
cat > "$INIT_SCRIPT_PATH" << 'EOF'
#!/bin/sh /etc/rc.common

NAME=st4wrt-bot
BOT_DIR="/root/ST4Wrt-bot"
BOT_COMMAND="/usr/bin/python3 ${BOT_DIR}/bot.py"

START=99
STOP=10

USE_PROCD=1

start_service() {
    procd_open_instance "$NAME"
    procd_set_param command $BOT_COMMAND
    procd_set_param dir "$BOT_DIR"
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}

service_triggers() {
    procd_add_reload_trigger "$NAME"
}
EOF

chmod +x "$INIT_SCRIPT_PATH"
"$INIT_SCRIPT_PATH" enable
"$INIT_SCRIPT_PATH" start

print_success "✅ Layanan bot berhasil dibuat, diaktifkan, dan dijalankan!"
print_info "=========================================================="
print_info "🎉 Instalasi Selesai! 🎉"
print_info "Bot Anda sekarang berjalan di latar belakang."
print_info "Cek status dengan: /etc/init.d/st4wrt-bot status"
print_info "Buka Telegram dan kirim /start ke bot Anda."
print_info "=========================================================="
