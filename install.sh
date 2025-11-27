#!/bin/sh

print() { printf "%s\n" "$1"; }

set -e

print "Menginstal dependensi..."
opkg update
opkg install python3 python3-pip git git-http wrtbwmon speedtest-go etherwake nano

print "Menginstal library Python..."
pip install python-telegram-bot python-dotenv "python-telegram-bot[job-queue]"

BOT_DIR="/root/ST4Wrt-bot"

print "Menyiapkan direktori..."
if [ -d "$BOT_DIR" ]; then
    print "Direktori proyek sudah ada."
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
fi

cd "$BOT_DIR"

print "Konfigurasi Bot..."

### ==============================
###   INPUT TOKEN BOT (AMAN)
### ==============================
while true; do
    printf "Token Bot: "
    read RAWTOKEN

    # Bersihkan karakter ilegal
    TOKEN=$(printf "%s" "$RAWTOKEN" | sed 's/[^A-Za-z0-9:_-]//g')

    if [ -n "$TOKEN" ]; then
        break
    fi

    print "❌ Token tidak valid, coba lagi."
done


### ==============================
###   INPUT ADMIN ID
### ==============================
while true; do
    printf "Admin ID (angka): "
    read RAWID

    ADMINID=$(printf "%s" "$RAWID" | sed 's/[^0-9]//g')

    if [ -n "$ADMINID" ]; then
        break
    fi

    print "❌ Admin ID harus angka."
done


### ==============================
###   INPUT OPSIONAL WIFI TAMU
### ==============================
printf "WiFi Tamu (opsional, kosongkan jika tidak ada): "
read GUEST


### ==============================
###   BUAT FILE .env AMAN
### ==============================
print "Membuat file .env..."

{
    echo "TELEGRAM_BOT_TOKEN=\"$TOKEN\""
    echo "TELEGRAM_ADMIN_ID=\"$ADMINID\""
    [ -n "$GUEST" ] && echo "GUEST_WIFI_IFACE=\"$GUEST\""
} > .env


### ==============================
###   FILE device_aliases.json
### ==============================
[ ! -f device_aliases.json ] && echo "{}" > device_aliases.json
[ ! -f .gitignore ] && touch .gitignore

grep -qxF '.env' .gitignore || echo '.env' >> .gitignore
grep -qxF 'device_aliases.json' .gitignore || echo 'device_aliases.json' >> .gitignore


### ==============================
###   BUAT LAYANAN INITD
### ==============================
print "Membuat layanan bot..."

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
"$INIT" enable
"$INIT" restart

print "Instalasi selesai. Bot berjalan."
print "Cek status: /etc/init.d/st4wrt-bot status"
