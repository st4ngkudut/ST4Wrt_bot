#!/bin/sh

# ==========================================
#  TAMPILAN & WARNA
# ==========================================
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
BLUE="\033[1;36m"
CYAN="\033[1;36m"
RESET="\033[0m"

LOG_FILE="/tmp/bot_install.log"
> "$LOG_FILE" # Bersihkan log lama

# ==========================================
#  FUNGSI UI/UX
# ==========================================
print_banner() {
    clear
    printf "${CYAN}"
    printf "=================================================\n"
    printf "   🚀 ST4Wrt Telegram Bot - Auto Installer 🚀   \n"
    printf "=================================================\n"
    printf "${RESET}Log instalasi disimpan di: ${YELLOW}$LOG_FILE${RESET}\n\n"
}

run_task() {
    local task_name="$1"
    shift
    printf "${BLUE}[*] ${task_name}... ${RESET}"
    
    if "$@" >> "$LOG_FILE" 2>&1; then
        printf "\r\033[K${GREEN}[✔] ${task_name} - Berhasil${RESET}\n"
        return 0
    else
        printf "\r\033[K${RED}[✖] ${task_name} - Gagal${RESET}\n"
        return 1
    fi
}

run_optional_task() {
    local task_name="$1"
    shift
    printf "${BLUE}[*] ${task_name}... ${RESET}"
    
    if "$@" >> "$LOG_FILE" 2>&1; then
        printf "\r\033[K${GREEN}[✔] ${task_name} - Berhasil${RESET}\n"
    else
        printf "\r\033[K${YELLOW}[!] ${task_name} - Dilewati (Tidak tersedia/Gagal)${RESET}\n"
    fi
}

print_banner

# ==========================================
#  0. DETEKSI PACKAGE MANAGER (APK / OPKG)
# ==========================================
printf "${BLUE}[*] Mendeteksi Package Manager sistem... ${RESET}"
if command -v apk >/dev/null 2>&1; then
    PKG_MANAGER="apk"
    PKG_UPDATE="apk update"
    PKG_INSTALL="apk add"
    printf "\r\033[K${GREEN}[✔] Package Manager: APK (OpenWrt Baru)${RESET}\n"
elif command -v opkg >/dev/null 2>&1; then
    PKG_MANAGER="opkg"
    PKG_UPDATE="opkg update"
    PKG_INSTALL="opkg install"
    printf "\r\033[K${GREEN}[✔] Package Manager: OPKG (OpenWrt Lama)${RESET}\n"
else
    printf "\r\033[K${RED}[✖] Error: Tidak dapat menemukan apk atau opkg!${RESET}\n"
    exit 1
fi

# ==========================================
#  1. UPDATE & CORE PACKAGES
# ==========================================
run_task "Memperbarui repositori ($PKG_MANAGER update)" $PKG_UPDATE

run_task "Menginstal paket dasar (Python, Git, dll)" \
    $PKG_INSTALL python3 python3-pip git git-http etherwake nano

# ==========================================
#  2. OPTIONAL & CUSTOM PACKAGES
# ==========================================
run_optional_task "Menginstal speedtest-go" $PKG_INSTALL speedtest-go

# Smart Fallback untuk wrtbwmon
printf "${BLUE}[*] Menginstal wrtbwmon... ${RESET}"
if $PKG_INSTALL wrtbwmon >> "$LOG_FILE" 2>&1; then
    printf "\r\033[K${GREEN}[✔] Menginstal wrtbwmon (via $PKG_MANAGER) - Berhasil${RESET}\n"
else
    if wget -q -O /usr/bin/wrtbwmon https://raw.githubusercontent.com/pyrovski/wrtbwmon/master/wrtbwmon >> "$LOG_FILE" 2>&1; then
        chmod +x /usr/bin/wrtbwmon
        printf "\r\033[K${GREEN}[✔] Menginstal wrtbwmon (Unduh Manual) - Berhasil${RESET}\n"
    else
        printf "\r\033[K${YELLOW}[!] Menginstal wrtbwmon - Dilewati (Gagal mengunduh)${RESET}\n"
    fi
fi

# ==========================================
#  3. PYTHON LIBRARIES (Smart PIP)
# ==========================================
printf "${BLUE}[*] Menginstal library Python (Telegram Bot)... ${RESET}"
# Coba install normal dulu (untuk OpenWrt lama)
if pip install python-telegram-bot python-dotenv "python-telegram-bot[job-queue]" >> "$LOG_FILE" 2>&1; then
    printf "\r\033[K${GREEN}[✔] Menginstal library Python (Mode Standar) - Berhasil${RESET}\n"
# Jika gagal, gunakan flag --break-system-packages (untuk OpenWrt baru / Python 3.11+)
elif pip install --break-system-packages python-telegram-bot python-dotenv "python-telegram-bot[job-queue]" >> "$LOG_FILE" 2>&1; then
    printf "\r\033[K${GREEN}[✔] Menginstal library Python (Mode PEP 668) - Berhasil${RESET}\n"
else
    printf "\r\033[K${RED}[✖] Menginstal library Python - Gagal. Cek log!${RESET}\n"
    exit 1
fi

# ==========================================
#  4. CLONE REPOSITORY
# ==========================================
BOT_DIR="/root/ST4Wrt-bot"
if [ -d "$BOT_DIR" ]; then
    printf "${GREEN}[✔] Direktori bot sudah ada, melewati clone git.${RESET}\n"
else
    run_task "Mengunduh *source code* Bot" \
        git clone https://github.com/st4ngkudut/ST4Wrt_bot.git "$BOT_DIR"
fi

cd "$BOT_DIR" || exit 1

# ==========================================
#  5. INTERAKTIF: KONFIGURASI BOT
# ==========================================
printf "\n${CYAN}--- Konfigurasi Bot Telegram ---${RESET}\n"

while true; do
    printf "🔑 ${YELLOW}Masukkan Token Bot:${RESET} "
    read RAWTOKEN
    TOKEN=$(printf "%s" "$RAWTOKEN" | sed 's/[^A-Za-z0-9:_-]//g')
    [ -n "$TOKEN" ] && break
    printf "${RED}Token tidak boleh kosong!\n${RESET}"
done

while true; do
    printf "👤 ${YELLOW}Masukkan Admin ID (Angka):${RESET} "
    read RAWID
    ADMINID=$(printf "%s" "$RAWID" | sed 's/[^0-9]//g')
    [ -n "$ADMINID" ] && break
    printf "${RED}Admin ID harus berupa angka!\n${RESET}"
done

printf "📶 ${YELLOW}Interface WiFi Tamu (Kosongi jika tidak ada, misal: wlan1):${RESET} "
read GUEST

{
    echo "TELEGRAM_BOT_TOKEN=\"$TOKEN\""
    echo "TELEGRAM_ADMIN_ID=\"$ADMINID\""
    [ -n "$GUEST" ] && echo "GUEST_WIFI_IFACE=\"$GUEST\""
} > .env

touch device_aliases.json
[ ! -s device_aliases.json ] && echo "{}" > device_aliases.json

printf "${GREEN}[✔] File konfigurasi (.env) berhasil disimpan.${RESET}\n"

# ==========================================
#  6. SETUP LAYANAN (SERVICE PROCD)
# ==========================================
INIT="/etc/init.d/st4wrt-bot"

cat <<'EOF' > "$INIT"
#!/bin/sh /etc/rc.common
NAME=st4wrt-bot
BOT_DIR="/root/ST4Wrt-bot"
BOT_COMMAND="/usr/bin/python3 -u ${BOT_DIR}/bot.py"
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

run_task "Mendaftarkan dan menjalankan layanan latar belakang" \
    sh -c "$INIT enable && $INIT restart"

# ==========================================
#  7. SELESAI
# ==========================================
printf "\n${CYAN}=================================================${RESET}\n"
printf "${GREEN} 🎉 INSTALASI SELESAI & BOT SEDANG BERJALAN 🎉 ${RESET}\n"
printf "${CYAN}=================================================${RESET}\n"
printf "Perintah yang berguna:\n"
printf " 🔹 Cek status layanan : ${YELLOW}/etc/init.d/st4wrt-bot status${RESET}\n"
printf " 🔹 Restart bot        : ${YELLOW}/etc/init.d/st4wrt-bot restart${RESET}\n"
printf " 🔹 Hentikan bot       : ${YELLOW}/etc/init.d/st4wrt-bot stop${RESET}\n"
printf " 🔹 Lihat Log Bot      : ${YELLOW}logread -e st4wrt-bot -f${RESET}\n\n"
