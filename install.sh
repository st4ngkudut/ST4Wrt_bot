#!/bin/sh

print_info()    { printf "\033[34m%s\033[0m\n" "$1"; }
print_success() { printf "\033[32m%s\033[0m\n" "$1"; }
print_warning() { printf "\033[33m%s\033[0m\n" "$1"; }
print_error()   { printf "\033[31m%s\033[0m\n" "$1"; }

set -e

print_info "Memeriksa versi Python..."
python3 - << 'EOF'
import sys
if sys.version_info < (3,10):
    print("Python 3.10 diperlukan.")
    exit(1)
EOF
print_success "Python OK."

print_info "Menginstal dependensi..."
opkg update
opkg install python3 python3-pip git git-http wrtbwmon etherwake nano || true
opkg install speedtest-go || print_warning "speedtest-go tidak tersedia."
print_success "Dependensi terinstal."

BOT_DIR="/root/ST4Wrt-bot"

print_info "Menyiapkan direktori..."
if [ -d "$BOT_DIR" ]; then
    print_warning "Direktori proyek sudah ada."
    cd "$BOT_DIR"
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
    cd "$BOT_DIR"
fi

touch .env
[ -s device_aliases.json ] || echo "{}" > device_aliases.json
echo ".env" > .gitignore
echo "device_aliases.json" >> .gitignore

print_info "Konfigurasi Bot..."

while true; do
    printf "Token Bot: "
    read TELEGRAM_BOT_TOKEN
    [ -n "$TELEGRAM_BOT_TOKEN" ] && break
    print_error "Token tidak boleh kosong."
done

while true; do
    printf "Admin ID (angka): "
    read TELEGRAM_ADMIN_ID
    case "$TELEGRAM_ADMIN_ID" in
        *[!0-9]*|"") print_error "Admin ID harus angka." ;;
        *) break ;;
    esac
done

printf "Interface WiFi tamu (opsional): "
read GUEST_WIFI_IFACE

echo "# ST4Wrt Bot Config" > .env
echo "TELEGRAM_BOT_TOKEN=\"$TELEGRAM_BOT_TOKEN\"" >> .env
echo "TELEGRAM_ADMIN_ID=\"$TELEGRAM_ADMIN_ID\"" >> .env

[ -n "$GUEST_WIFI_IFACE" ] && echo "GUEST_WIFI_IFACE=\"$GUEST_WIFI_IFACE\"" >> .env

chmod 600 .env
print_success ".env dibuat."

print_info "Menginstal library Python..."
pip install --break-system-packages python-telegram-bot python-dotenv python-telegram-bot[job-queue]
print_success "Library Python terinstal."

print_info "Membuat service..."

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
$INIT_SCRIPT_PATH enable
$INIT_SCRIPT_PATH start

print_success "Bot berhasil dijalankan."
print_info "Gunakan '/etc/init.d/st4wrt-bot status' untuk cek status."
