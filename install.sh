#!/bin/sh

# ===============================
#   WARNA & PRINT SAFE
# ===============================
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
BLUE="\033[36m"
RESET="\033[0m"

msg() { printf "%s\n" "$1"; }
step() { clear; printf "${BLUE}=============================\n==> %s\n=============================${RESET}\n" "$1"; }
ok()   { printf "${GREEN}✔ %s${RESET}\n" "$1"; }
err()  { printf "${RED}✖ %s${RESET}\n" "$1"; }

set -e


# ===============================
#   1. INSTALL DEPENDENCIES
# ===============================
step "Menginstal dependensi sistem..."
opkg update >/dev/null 2>&1
opkg install python3 python3-pip git git-http wrtbwmon speedtest-go etherwake nano >/dev/null 2>&1
ok "Dependensi sistem terinstal."
sleep 1


# ===============================
#   2. INSTALL LIB PYTHON
# ===============================
step "Menginstal library Python..."
pip install python-telegram-bot python-dotenv "python-telegram-bot[job-queue]" >/dev/null 2>&1
ok "Library Python terinstal."
sleep 1


# ===============================
#   3. SIAPKAN FOLDER BOT
# ===============================
step "Menyiapkan direktori bot..."

BOT_DIR="/root/ST4Wrt-bot"

if [ -d "$BOT_DIR" ]; then
    ok "Direktori sudah ada, melanjutkan."
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR" >/dev/null 2>&1
    ok "Repository berhasil di-clone."
fi

cd "$BOT_DIR"
sleep 1


# ===============================
#   4. KONFIGURASI BOT
# ===============================
step "Konfigurasi Bot Telegram"

# Token
while true; do
    printf " • Token Bot: "
    read RAWTOKEN
    TOKEN=$(printf "%s" "$RAWTOKEN" | sed 's/[^A-Za-z0-9:_-]//g')
    [ -n "$TOKEN" ] && break
    err "Token tidak valid!"
done

# Admin ID
while true; do
    printf " • Admin ID (angka): "
    read RAWID
    ADMINID=$(printf "%s" "$RAWID" | sed 's/[^0-9]//g')
    [ -n "$ADMINID" ] && break
    err "Admin ID harus angka!"
done

printf " • WiFi Tamu (opsional): "
read GUEST
sleep 1


# ===============================
#   5. TULIS FILE .ENV
# ===============================
step "Menyimpan file .env..."

{
    echo "TELEGRAM_BOT_TOKEN=\"$TOKEN\""
    echo "TELEGRAM_ADMIN_ID=\"$ADMINID\""
    [ -n "$GUEST" ] && echo "GUEST_WIFI_IFACE=\"$GUEST\""
} > .env

touch device_aliases.json
[ ! -s device_aliases.json ] && echo "{}" > device_aliases.json

grep -qxF '.env' .gitignore || echo '.env' >> .gitignore
grep -qxF 'device_aliases.json' .gitignore || echo 'device_aliases.json' >> .gitignore

ok ".env berhasil dibuat."
sleep 1


# ===============================
#   6. BUAT LAYANAN BOT
# ===============================
step "Membuat layanan bot..."

INIT="/etc/init.d/st4wrt-bot"

cat <<'EOF' > "$INIT"
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
    procd_set_param respawn
    procd_set_param dir "$BOT_DIR"
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF

chmod +x "$INIT"
$INIT enable >/dev/null 2>&1
$INIT restart >/dev/null 2>&1

ok "Layanan bot berhasil dibuat."
sleep 1


# ===============================
#   7. SELESAI
# ===============================
step "Instalasi Selesai 🎉"

ok "Bot Telegram sedang berjalan."

printf "\nCek status bot:\n  ${YELLOW}/etc/init.d/st4wrt-bot status${RESET}\n"
printf "Lihat log bot:\n  ${YELLOW}logread -f${RESET}\n\n"

"$INIT" enable
"$INIT" restart

print "Instalasi selesai. Bot berjalan."
print "Cek status: /etc/init.d/st4wrt-bot status"
