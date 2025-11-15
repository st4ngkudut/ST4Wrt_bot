#!/bin/sh

print() { printf "%s\n" "$1"; }

set -e

print "Menginstal dependensi..."
opkg update
opkg install python3 python3-pip git git-http wrtbwmon speedtest-go etherwake nano

print "Menginstal library Python..."
pip install python-telegram-bot python-dotenv python-telegram-bot[job-queue]

BOT_DIR="/root/ST4Wrt-bot"

print "Menyiapkan direktori..."
if [ -d "$BOT_DIR" ]; then
    print "Direktori proyek sudah ada."
    cd "$BOT_DIR"
else
    git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
    cd "$BOT_DIR"
fi

print "Konfigurasi Bot..."

printf "Token Bot: "
read RAWTOKEN
TOKEN=$(echo "$RAWTOKEN" | tr -cd 'A-Za-z0-9:_-')
while [ -z "$TOKEN" ]; do
    printf "Token Bot: "
    read RAWTOKEN
    TOKEN=$(echo "$RAWTOKEN" | tr -cd 'A-Za-z0-9:_-')
done

printf "Admin ID (angka): "
read RAWID
ADMINID=$(echo "$RAWID" | tr -cd '0-9')
while [ -z "$ADMINID" ]; do
    printf "Admin ID (angka): "
    read RAWID
    ADMINID=$(echo "$RAWID" | tr -cd '0-9')
done

printf "WiFi Tamu (opsional, kosongkan jika tidak ada): "
read GUEST

cat <<EOF > .env
TELEGRAM_BOT_TOKEN="$TOKEN"
TELEGRAM_ADMIN_ID="$ADMINID"
EOF

if [ -n "$GUEST" ]; then
    echo "GUEST_WIFI_IFACE=\"$GUEST\"" >> .env
fi

touch device_aliases.json
[ ! -s device_aliases.json ] && echo "{}" > device_aliases.json

echo ".env" > .gitignore
echo "device_aliases.json" >> .gitignore

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
    procd_set_param dir "$BOT_DIR"
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF

chmod +x "$INIT"
"$INIT" enable
"$INIT" start

print "Instalasi selesai. Bot berjalan."
print "Cek status: /etc/init.d/st4wrt-bot status"
