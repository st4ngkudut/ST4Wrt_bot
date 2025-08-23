import logging
import os
import re
import shutil
import json
import subprocess
import time
import asyncio
from datetime import timedelta, datetime
from pathlib import Path
from typing import Any, Optional, List, Dict
from functools import wraps

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
                          ContextTypes, JobQueue)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- BAGIAN 1: KONFIGURASI DAN FUNGSI PEMBANTU ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Lokasi file
BOT_DIR = Path(__file__).parent
KNOWN_DEVICES_FILE = BOT_DIR / "known_devices.json"
ALIAS_FILE = BOT_DIR / "device_aliases.json"

# Variabel Global untuk Alias
DEVICE_ALIASES = {}

def load_aliases():
    global DEVICE_ALIASES
    try:
        if ALIAS_FILE.exists():
            DEVICE_ALIASES = json.loads(ALIAS_FILE.read_text())
        else:
            ALIAS_FILE.write_text("{}")
            DEVICE_ALIASES = {}
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Gagal memuat alias: {e}")
        DEVICE_ALIASES = {}

def save_aliases():
    try:
        ALIAS_FILE.write_text(json.dumps(DEVICE_ALIASES, indent=4))
    except IOError as e:
        logger.error(f"Gagal menyimpan alias: {e}")

def run_cmd(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return f"Error executing command: {e}"

def read_file(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""

def safe_search(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text)
    return match.group(1) if match else None

def format_bytes(b: Optional[float], per_second: bool = False) -> str:
    if b is None or b == 0: return "0 B" if not per_second else "0 B/s"
    suffix = "/s" if per_second else ""
    gb, mb, kb = 1024**3, 1024**2, 1024
    if abs(b) >= gb: return f"{b/gb:.2f} GB{suffix}"
    if abs(b) >= mb: return f"{b/mb:.2f} MB{suffix}"
    if abs(b) >= kb: return f"{b/kb:.2f} KB{suffix}"
    return f"{int(b)} B{suffix}"

def create_bar(p: float, length: int = 15) -> str:
    if not 0 <= p <= 100: p = max(0, min(100, p))
    filled_len = round(length * p / 100)
    return f"[{'█' * filled_len}{'░' * (length - filled_len)}]"

def escape_markdown_v1(text: str) -> str:
    escape_chars = r'[_*`]'
    return re.sub(f'([{re.escape(escape_chars)}])', ' ', text)

# --- BAGIAN 2: KEAMANAN (DEKORATOR ADMIN) ---

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        admin_id = os.getenv("TELEGRAM_ADMIN_ID")
        if not admin_id:
            logger.warning("TELEGRAM_ADMIN_ID tidak diatur.")
            return await func(update, context, *args, **kwargs)
        if str(user_id) != str(admin_id):
            logger.warning(f"Akses ditolak untuk user ID: {user_id}")
            if update.callback_query:
                await update.callback_query.answer("🔒 Akses ditolak. Anda bukan admin.", show_alert=True)
            else:
                await update.message.reply_text("🔒 Akses ditolak. Anda bukan admin.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- BAGIAN 3: FUNGSI PENGAMBILAN DATA ---

def get_wan_interfaces_info() -> list:
    wan_interfaces = []
    route_output = run_cmd(["ip", "route", "show", "default"])
    unique_ifaces = set(re.findall(r'dev\s+([^\s]+)', route_output))
    for iface in unique_ifaces:
        addr_raw, speed_str = run_cmd(["ip", "addr", "show", iface]), "Dinamis"
        ip = safe_search(r'inet\s+([\d.]+)', addr_raw) or safe_search(r'inet\s+([\d.]+)\s+peer', addr_raw)
        specific_route_line = next((line for line in route_output.splitlines() if f"dev {iface}" in line), "")
        gateway = safe_search(r'via\s+([^\s]+)', specific_route_line)
        if (speed_from_sys := read_file(f"/sys/class/net/{iface}/speed")) and speed_from_sys != '-1': speed_str = f"{speed_from_sys} Mbps"
        elif speed_match := safe_search(r'Speed:\s+(\d+Mb/s)', run_cmd(["ethtool", iface])): speed_str = speed_match.replace("Mb/s", " Mbps")
        wan_interfaces.append({ "name": iface, "ip": ip or "N/A", "gateway": gateway or "N/A", "speed": speed_str, "rx": int(read_file(f"/sys/class/net/{iface}/statistics/rx_bytes") or 0), "tx": int(read_file(f"/sys/class/net/{iface}/statistics/tx_bytes") or 0) })
    return wan_interfaces

def get_dhcp_leases() -> List[dict]:
    leases_file, devices = read_file("/tmp/dhcp.leases"), []
    for line in leases_file.splitlines():
        if not line.strip(): continue
        try:
            parts = line.split()
            if len(parts) >= 4: devices.append({'mac': parts[1].upper(), 'ip': parts[2], 'name': parts[3]})
        except IndexError: continue
    return sorted(devices, key=lambda x: x['name'])

def get_traffic_usage() -> Dict[str, Dict[str, int]]:
    """
    Mengambil data penggunaan dari wrtbwmon (versi baru dengan format CSV).
    Versi ini memperbaiki logika kolom download/upload.
    """
    usage_data = {}
    db_path = Path("/tmp/usage.db")

    if not db_path.exists():
        logger.warning("wrtbwmon database (/tmp/usage.db) tidak ditemukan.")
        return usage_data

    try:
        with open(db_path, 'r') as f:
            for line in f:
                # Lewati baris header yang dimulai dengan '#'
                if line.startswith('#'):
                    continue
                
                # Memisahkan data dengan koma (,)
                parts = line.strip().split(',')
                
                # Format baru: mac,ip,iface,speed_in,speed_out,in,out,...
                if len(parts) >= 7:
                    mac = parts[0].upper()
                    
                    # Logika ditukar agar benar
                    # parts[5] adalah 'in' -> UPLOAD
                    # parts[6] adalah 'out' -> DOWNLOAD
                    upload_bytes = int(parts[5])
                    download_bytes = int(parts[6])
                    
                    # Gabungkan data jika MAC sudah ada (wrtbwmon bisa mencatat LAN/WLAN terpisah)
                    if mac in usage_data:
                        usage_data[mac]["down"] += download_bytes
                        usage_data[mac]["up"] += upload_bytes
                    else:
                        usage_data[mac] = {
                            "down": download_bytes,
                            "up": upload_bytes
                        }
    except Exception as e:
        logger.error(f"Gagal membaca atau mem-parsing /tmp/usage.db: {e}")

    return usage_data

def get_combined_device_list() -> List[dict]:
    dhcp_devices, traffic_data = get_dhcp_leases(), get_traffic_usage()
    combined_list = []
    for device in dhcp_devices:
        mac = device['mac']
        device['name'] = DEVICE_ALIASES.get(mac, device['name'])
        usage = traffic_data.get(mac, {'down': 0, 'up': 0})
        device['down'], device['up'] = usage['down'], usage['up']
        combined_list.append(device)
    return sorted(combined_list, key=lambda x: x['name'])

def get_blocked_devices() -> List[Dict]:
    blocked_devices = []
    rule_pattern = re.compile(r"firewall\.(bot_block_\w+)=rule")
    uci_output = run_cmd(["uci", "show", "firewall"])
    for match in rule_pattern.finditer(uci_output):
        rule_section = match.group(1)
        name_match = re.search(rf"{re.escape(rule_section)}\.name='Block:([^']+)'", uci_output)
        mac_match = re.search(rf"{re.escape(rule_section)}\.src_mac='([^']+)'", uci_output)
        if name_match and mac_match:
            blocked_devices.append({"name": name_match.group(1), "mac": mac_match.group(1).upper()})
    return blocked_devices

def get_full_stats() -> dict[str, Any]:
    wan_interfaces = get_wan_interfaces_info()
    lan_ip = safe_search(r'inet\s+([\d.]+)', run_cmd(["ip", "addr", "show", "br-lan"])) or "N/A"
    mem_info = read_file("/proc/meminfo")
    mem_total, mem_available = int(safe_search(r"MemTotal:\s+(\d+)", mem_info) or 0) * 1024, int(safe_search(r"MemAvailable:\s+(\d+)", mem_info) or 0) * 1024
    swap_total, swap_free = int(safe_search(r"SwapTotal:\s+(\d+)", mem_info) or 0) * 1024, int(safe_search(r"SwapFree:\s+(\d+)", mem_info) or 0) * 1024
    openwrt_version = safe_search(r'PRETTY_NAME="([^"]+)"', read_file("/etc/os-release")) or read_file("/etc/banner") or "N/A"
    disk_usage = {}
    if len(lines := run_cmd(['df', '-P', '/']).splitlines()) > 1 and len(parts := lines[1].split()) >= 5: disk_usage = {'total': int(parts[1])*1024, 'used': int(parts[2])*1024, 'percent': float(parts[4].replace('%',''))}
    wifi_details = []
    try:
        wifi_status = json.loads(run_cmd(["ubus", "call", "network.wireless", "status"]) or "{}")
        for radio_data in wifi_status.values():
            if not radio_data.get("up"): continue
            channel = radio_data.get("config", {}).get("channel", "N/A")
            for iface in radio_data.get("interfaces", []):
                if (ssid := iface.get("config", {}).get("ssid")) and (ifname := iface.get("ifname")):
                    mode = iface.get("config", {}).get("mode", "N/A").upper()
                    client_count = len(re.findall(r"Station", run_cmd(["iw", "dev", ifname, "station", "dump"])))
                    rx, tx = int(read_file(f"/sys/class/net/{ifname}/statistics/rx_bytes") or 0), int(read_file(f"/sys/class/net/{ifname}/statistics/tx_bytes") or 0)
                    bitrate = safe_search(r"Bit Rate:\s+([\d.]+\s*MBit/s)", run_cmd(["iwinfo", ifname, "info"]))
                    wifi_details.append({ "ssid": ssid, "mode": mode, "channel": channel, "clients": client_count, "rx": rx, "tx": tx, "bitrate": bitrate or "N/A" })
    except (json.JSONDecodeError, Exception) as e: logger.error(f"Gagal mengambil detail WiFi: {e}")
    uptime_str = read_file("/proc/uptime").split()[0] if read_file("/proc/uptime") else "0"
    stats = {
        "sistem": { "model": read_file("/tmp/sysinfo/model") or "N/A", "arch": safe_search(r"model name\s*:\s+(.*)", read_file("/proc/cpuinfo")) or "N/A", "openwrt_version": openwrt_version.strip(), "kernel": run_cmd(["uname", "-r"]), "uptime": str(timedelta(seconds=int(float(uptime_str)))), "load_avg": read_file("/proc/loadavg").split()[0] if read_file("/proc/loadavg") else "N/A", "device_time": run_cmd(['date']), "memory_used": mem_total - mem_available, "memory_total": mem_total, "swap_used": swap_total - swap_free, "swap_total": swap_total, "disk_usage": disk_usage, },
        "jaringan": { "wan_interfaces": wan_interfaces, "lan_ip": lan_ip, "dns": ", ".join(re.findall(r"nameserver\s+([\d.]+)", read_file("/tmp/resolv.conf.d/resolv.conf.auto"))) or "N/A", "dhcp_leases": len(get_dhcp_leases()), "wifi_details": wifi_details },
    }
    return stats

def get_live_stats(prev_stats: dict) -> tuple[dict, dict]:
    interfaces = prev_stats.get('interfaces', [])
    mem_info, mem_total, mem_available = read_file("/proc/meminfo"), 0, 0
    if mem_info: mem_total, mem_available = int(safe_search(r"MemTotal:\s+(\d+)", mem_info) or 0) * 1024, int(safe_search(r"MemAvailable:\s+(\d+)", mem_info) or 0) * 1024
    cpu_line, cpu_parts = read_file("/proc/stat").splitlines()[0], []
    if cpu_line: cpu_parts = [int(p) for p in cpu_line.split()[1:]]
    cpu_total, cpu_idle = sum(cpu_parts), cpu_parts[3] if len(cpu_parts) > 3 else 0
    disk_stats_raw = read_file("/proc/diskstats")
    disk_match = re.search(r'\s+(sda|vda|nvme\w+|mmcblk\d+)\s+', disk_stats_raw)
    current_disk_read, current_disk_write = 0, 0
    if disk_match and (parts_match := re.search(rf'\s+{disk_match.group(1)}\s+(.*)', disk_stats_raw)) and len(parts := parts_match.group(1).split()) > 6:
        current_disk_read, current_disk_write = int(parts[2]) * 512, int(parts[6]) * 512
    time_now = time.time()
    time_delta = time_now - prev_stats.get('time', time_now)
    if time_delta < 0.5: time_delta = 0.5
    cpu_delta_total, cpu_delta_idle = cpu_total - prev_stats.get('cpu_total', 0), cpu_idle - prev_stats.get('cpu_idle', 0)
    cpu_percent = (100 * (cpu_delta_total - cpu_delta_idle) / cpu_delta_total) if cpu_delta_total > 0 else 0
    disk_rw_speed = ((current_disk_read - prev_stats.get('disk_read', 0)) + (current_disk_write - prev_stats.get('disk_write', 0))) / time_delta
    interfaces_data, next_net_stats = [], {}
    for iface in interfaces:
        current_rx, current_tx = int(read_file(f"/sys/class/net/{iface}/statistics/rx_bytes") or 0), int(read_file(f"/sys/class/net/{iface}/statistics/tx_bytes") or 0)
        net_down_speed, net_up_speed = (current_rx - prev_stats.get(f'rx_{iface}', current_rx)) / time_delta, (current_tx - prev_stats.get(f'tx_{iface}', current_tx)) / time_delta
        max_speed_str = os.getenv(f"MAX_SPEED_{iface}")
        down_mbps, up_mbps = (map(float, max_speed_str.split(',')) if max_speed_str and ',' in max_speed_str else (float(os.getenv("NET_MAX_DOWNLOAD_MBPS", 100)), float(os.getenv("NET_MAX_UPLOAD_MBPS", 10))))
        net_down_percent, net_up_percent = (net_down_speed*8 / (down_mbps * 1024**2)) * 100 if down_mbps > 0 else 0, (net_up_speed*8 / (up_mbps * 1024**2)) * 100 if up_mbps > 0 else 0
        interfaces_data.append({ 'name': iface, 'net_up_speed': net_up_speed, 'net_down_speed': net_down_speed, 'net_up_percent': net_up_percent, 'net_down_percent': net_down_percent })
        next_net_stats[f'rx_{iface}'], next_net_stats[f'tx_{iface}'] = current_rx, current_tx
    MAX_DISK_SPEED_BPS = float(os.getenv("MAX_DISK_SPEED_BPS", 50 * 1024 * 1024))
    live_data = {'cpu_percent': cpu_percent, 'ram_percent': ((mem_total - mem_available) / mem_total) * 100 if mem_total > 0 else 0, 'disk_rw_speed': disk_rw_speed, 'disk_percent': (disk_rw_speed / MAX_DISK_SPEED_BPS) * 100 if MAX_DISK_SPEED_BPS > 0 else 0, 'interfaces_data': interfaces_data }
    next_stats = { 'time': time_now, 'cpu_total': cpu_total, 'cpu_idle': cpu_idle, 'disk_read': current_disk_read, 'disk_write': current_disk_write, 'interfaces': interfaces, **next_net_stats }
    return live_data, next_stats


# --- BAGIAN 4: FUNGSI FORMAT TAMPILAN ---

def format_full_stats(all_stats: dict) -> str:
    sys, net = all_stats.get("sistem", {}), all_stats.get("jaringan", {})
    def ljust_label(label: str, width: int = 10) -> str: return f"`{label.ljust(width)}`"
    mem_p = (sys.get('memory_used', 0) / sys.get('memory_total', 1)) * 100
    parts = ["*🤖 ST4Bot Monitoring Openwrt*\n"]
    parts.append(f"💻 *Sistem*\n"
                 f"  • {ljust_label('Model')}: `{sys.get('model', 'N/A')}`\n"
                 f"  • {ljust_label('Arsitek')}: `{sys.get('arch', 'N/A')}`\n"
                 f"  • {ljust_label('Versi OS')}: `{sys.get('openwrt_version', 'N/A')}`\n"
                 f"  • {ljust_label('Kernel')}: `{sys.get('kernel', 'N/A')}`\n"
                 f"  • {ljust_label('Waktu')}: `{sys.get('device_time', 'N/A')}`\n"
                 f"  • {ljust_label('Uptime')}: `{sys.get('uptime', 'N/A')}`\n"
                 f"  • {ljust_label('Load Avg')}: `{sys.get('load_avg', 'N/A')}`\n"
                 f"  • {ljust_label('Memori')}: `{mem_p:.1f}% ({format_bytes(sys.get('memory_used',0))}/{format_bytes(sys.get('memory_total',0))})`")
    disk = sys.get('disk_usage', {})
    disk_text = f"`{create_bar(disk.get('percent', 0), 10)} {disk.get('percent', 0):.1f}% ({format_bytes(disk.get('used'))}/{format_bytes(disk.get('total'))})`"
    swap_total_val = sys.get('swap_total', 0)
    swap_p = (sys.get('swap_used', 0) / swap_total_val * 100) if swap_total_val > 0 else 0
    swap_text = f"`{create_bar(swap_p, 10)} {swap_p:.1f}% ({format_bytes(sys.get('swap_used'))}/{format_bytes(swap_total_val)})`"
    parts.append(f"\n💾 *Penyimpanan*\n  • {ljust_label('RootFS')}: {disk_text}\n  • {ljust_label('Swap')}: {swap_text}")
    wan_parts = [f"  • *WAN Interface: `{wan['name']}`*\n    `IP       :` `{wan['ip']}`\n    `Gateway  :` `{wan['gateway']}`\n    `Link Speed:` `{wan['speed']}`\n    `Trafik   :` `↓{format_bytes(wan['rx'])} / ↑{format_bytes(wan['tx'])}`" for wan in net.get('wan_interfaces', [])]
    wan_str = "\n".join(wan_parts) if wan_parts else "  • `Tidak ada interface WAN aktif terdeteksi.`"
    parts.append(f"\n🌐 *Jaringan*\n{wan_str}\n"
                 f"  • {ljust_label('LAN IP', 11)}: `{net.get('lan_ip', 'N/A')}`\n"
                 f"  • {ljust_label('DNS', 11)}: `{net.get('dns', 'N/A')}`\n"
                 f"  • {ljust_label('DHCP Aktif', 11)}: `{net.get('dhcp_leases', 0)}`")
    wifi_parts = [f"  • *{w['ssid']}* `({w['mode']}, Ch: {w['channel']})`\n    `Klien    :` `{w['clients']}` | `Bitrate:` `{w['bitrate']}`\n    `Trafik   :` `↓{format_bytes(w['rx'])} / ↑{format_bytes(w['tx'])}`" for w in net.get('wifi_details', [])]
    wifi_str = "\n".join(wifi_parts) if wifi_parts else "  • `Tidak ada data WiFi`"
    parts.append(f"\n\n📶 *Wireless*\n{wifi_str}")
    return "\n".join(parts)

def format_live_dashboard(live_data: dict) -> str:
    header, parts = "--- 🚀 LIVE DASHBOARD ---", []
    cpu_line = f"CPU      : {create_bar(live_data.get('cpu_percent', 0))} {live_data.get('cpu_percent', 0):>5.1f}%"
    ram_line = f"RAM      : {create_bar(live_data.get('ram_percent', 0))} {live_data.get('ram_percent', 0):>5.1f}%"
    disk_line = f"DISK R/W : {create_bar(live_data.get('disk_percent', 0))} {format_bytes(live_data.get('disk_rw_speed', 0), True):>10}"
    parts = [header, cpu_line, ram_line, disk_line]
    for iface_data in live_data.get('interfaces_data', []):
        parts.extend([f"\n--- 📶 Bandwidth ({iface_data['name']}) ---",
                      f"UP       : {create_bar(iface_data.get('net_up_percent', 0))} {format_bytes(iface_data.get('net_up_speed', 0), True):>10}",
                      f"DOWN     : {create_bar(iface_data.get('net_down_percent', 0))} {format_bytes(iface_data.get('net_down_speed', 0), True):>10}"])
    content = '\n'.join(parts)
    return f"```\n{content}\n```"

def format_device_list(devices: List[dict], page: int = 1, per_page: int = 5) -> tuple[str, InlineKeyboardMarkup]:
    if not devices: return "*Tidak ada perangkat yang terhubung.*", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="device_management_show")]])
    start_index, end_index = (page - 1) * per_page, page * per_page
    paginated_devices, text_parts = devices[start_index:end_index], [f"*Daftar Perangkat Terhubung ({len(devices)} total)*\n"]
    keyboard_buttons = []
    blocked_macs = {dev['mac'] for dev in get_blocked_devices()}
    for dev in paginated_devices:
        safe_name = escape_markdown_v1(dev['name'])
        usage_str = f"\n    `Usage:` `↓{format_bytes(dev.get('down', 0))} / ↑{format_bytes(dev.get('up', 0))}`" if 'down' in dev else ""
        text_parts.append(f"  • *{safe_name}*\n    `IP  :` `{dev['ip']}`\n    `MAC :` `{dev['mac']}`{usage_str}")
        if dev['mac'] not in blocked_macs:
            short_name = (dev['name'][:20] + '..') if len(dev['name']) > 22 else dev['name']
            keyboard_buttons.append([InlineKeyboardButton(f"⛔️ Blokir {short_name}", callback_data=f"block_device_{dev['mac']}_{dev['name']}")])
    total_pages = (len(devices) + per_page - 1) // per_page
    text_parts.append(f"\n`Halaman {page}/{total_pages}`")
    nav_buttons = []
    if page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"devices_page_{page-1}"))
    if page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"devices_page_{page+1}"))
    keyboard_buttons.extend([nav_buttons, [InlineKeyboardButton("🔄 Refresh", callback_data="devices_page_1"), InlineKeyboardButton("🔙 Kembali", callback_data="device_management_show")]])
    return "\n".join(text_parts), InlineKeyboardMarkup(keyboard_buttons)

def format_blocked_list(blocked_devices: List[dict]) -> tuple[str, InlineKeyboardMarkup]:
    if not blocked_devices: return "*Tidak ada perangkat yang diblokir.*", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="device_management_show")]])
    text_parts, keyboard_buttons = ["*Daftar Perangkat yang Diblokir*\n"], []
    for dev in blocked_devices:
        safe_name = escape_markdown_v1(dev['name'])
        text_parts.append(f"  • *{safe_name}*\n    `MAC:` `{dev['mac']}`")
        short_name = (dev['name'][:20] + '..') if len(dev['name']) > 22 else dev['name']
        keyboard_buttons.append([InlineKeyboardButton(f"✅ Buka Blokir {short_name}", callback_data=f"unblock_device_{dev['mac']}_{dev['name']}")])
    keyboard_buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="device_management_show")])
    return "\n".join(text_parts), InlineKeyboardMarkup(keyboard_buttons)


# --- BAGIAN 5: KEYBOARD MENU BARU ---

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status Router", callback_data="full_status_refresh")],
        [InlineKeyboardButton("🚀 Live Dashboard", callback_data="live_start")],
        [InlineKeyboardButton("⚙️ Panel Kontrol", callback_data="control_panel_show")],
        [InlineKeyboardButton("🌐 Diagnostik Jaringan", callback_data="diagnostics_menu_show")],
        [InlineKeyboardButton("Developer", url="https://t.me/ST4NGKUDUT")]
    ])

def get_live_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Berhenti Live", callback_data="live_stop")]])

def get_control_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reboot & Restart", callback_data="reboot_menu_show")],
        [InlineKeyboardButton("🔧 Manajemen Layanan", callback_data="services_page_1")],
        [InlineKeyboardButton("🔒 Manajemen Perangkat", callback_data="device_management_show")],
        [InlineKeyboardButton("📡 Kontrol WiFi", callback_data="wifi_control_show")],
        [InlineKeyboardButton("🛡️ Keamanan", callback_data="security_menu_show")],
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="main_menu_show")]
    ])

def get_security_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Audit Keamanan", callback_data="security_audit")],
        [InlineKeyboardButton("🚫 Status Adblock", callback_data="adblock_status")],
        [InlineKeyboardButton("🔙 Kembali ke Panel Kontrol", callback_data="control_panel_show")]
    ])

def get_reboot_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ REBOOT ROUTER ⚠️", callback_data="reboot_confirm_show")],
        [InlineKeyboardButton("🌐 Restart Jaringan", callback_data="action_restart_network")],
        [InlineKeyboardButton("🔥 Restart Firewall", callback_data="action_restart_firewall")],
        [InlineKeyboardButton("🔙 Kembali ke Panel Kontrol", callback_data="control_panel_show")]
    ])

def get_reboot_confirmation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Saya Yakin Ingin Reboot", callback_data="action_reboot_execute")],
        [InlineKeyboardButton("❌ Batal", callback_data="reboot_menu_show")]
    ])

def get_device_management_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📶 Lihat Perangkat Terhubung", callback_data="devices_page_1")],
        [InlineKeyboardButton("⛔️ Lihat Perangkat Diblokir", callback_data="blocked_list_show")],
        [InlineKeyboardButton("🔙 Kembali ke Panel Kontrol", callback_data="control_panel_show")]
    ])

def get_diagnostics_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Jalankan Speedtest", callback_data="action_diagnostic_speedtest")],
        [InlineKeyboardButton("🏓 Lakukan Ping", callback_data="diagnostic_ping_prompt")],
        [InlineKeyboardButton("📝 Lihat Log Sistem", callback_data="action_diagnostic_logread")],
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="main_menu_show")]
    ])

def get_services_keyboard(page: int = 1, per_page: int = 8) -> InlineKeyboardMarkup:
    excluded_services = [s.strip() for s in os.getenv("EXCLUDE_SERVICES", "boot,cron,log,sysfixtime,sysntpd,system").split(',') if s.strip()]
    initd_path = Path("/etc/init.d/")
    all_services = []
    if initd_path.is_dir():
        for service_path in sorted(initd_path.iterdir()):
            if service_path.is_file() and os.access(service_path, os.X_OK) and service_path.name not in excluded_services:
                all_services.append(service_path.name)
    if not all_services:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Tidak ada layanan yang bisa dikelola.", callback_data="no_op")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="control_panel_show")]
        ])
    total_pages = (len(all_services) + per_page - 1) // per_page
    start_index = (page - 1) * per_page
    end_index = start_index + per_page
    paginated_services = all_services[start_index:end_index]
    keyboard_buttons = []
    for service_name in paginated_services:
        keyboard_buttons.append([InlineKeyboardButton(service_name.title(), callback_data=f"service_menu_{service_name}")])
    nav_buttons = []
    if page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"services_page_{page-1}"))
    if page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"services_page_{page+1}"))
    if nav_buttons: keyboard_buttons.append(nav_buttons)
    keyboard_buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="control_panel_show")])
    return InlineKeyboardMarkup(keyboard_buttons)

def get_service_action_keyboard(service_name: str): 
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start", callback_data=f"action_service_{service_name}_start"), InlineKeyboardButton("⏹️ Stop", callback_data=f"action_service_{service_name}_stop"), InlineKeyboardButton("🔄 Restart", callback_data=f"action_service_{service_name}_restart")], 
        [InlineKeyboardButton("🔌 Enable", callback_data=f"action_service_{service_name}_enable"), InlineKeyboardButton("🚫 Disable", callback_data=f"action_service_{service_name}_disable")], 
        [InlineKeyboardButton("ℹ️ Status", callback_data=f"action_service_{service_name}_status")], 
        [InlineKeyboardButton("🔙 Kembali ke Daftar Layanan", callback_data="services_page_1")]
    ])

def get_wifi_control_keyboard():
    buttons = []
    try:
        wifi_status = json.loads(run_cmd(["ubus", "call", "network.wireless", "status"]) or "{}")
        for radio, data in wifi_status.items(): 
            buttons.append([InlineKeyboardButton(f"{radio.title()}: {'ON 🟢' if data.get('up') else 'OFF 🔴'} - Toggle", callback_data=f"action_wifi_{radio}_toggle")])
    except (json.JSONDecodeError, Exception) as e: 
        logger.error(f"Gagal membuat keyboard WiFi: {e}")
    buttons.append([InlineKeyboardButton("🔙 Kembali ke Panel Kontrol", callback_data="control_panel_show")])
    return InlineKeyboardMarkup(buttons)


# --- BAGIAN 6: HANDLER (FUNGSI UTAMA BOT) ---

# HANDLER MENU UTAMA & PUBLIK
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.callback_query.message if update.callback_query else update.message
    try:
        if update.callback_query:
            await update.callback_query.answer()

        if update.callback_query:
            try:
                await message.edit_text("🔄 *Memuat status router...*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard())
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    raise
        else:
            message = await message.reply_text("🔄 *Memuat status router...*", parse_mode=ParseMode.MARKDOWN)

        full_stats = get_full_stats()
        text = format_full_stats(full_stats)
        await message.edit_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        logger.error(f"Error di start_handler: {e}", exc_info=True)
        try:
            await message.edit_text(f"❌ Terjadi kesalahan: `{e}`", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            original_message = update.callback_query.message if update.callback_query else update.message
            await original_message.reply_text(f"❌ Terjadi kesalahan: `{e}`", parse_mode=ParseMode.MARKDOWN)

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await start_handler(update, context)

# HANDLER LIVE DASHBOARD
async def live_update_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        if not (prev_stats := context.bot_data.get(job.name, {})):
            job.schedule_removal()
            return
        live_data, next_stats = get_live_stats(prev_stats)
        context.bot_data[job.name] = next_stats
        await context.bot.edit_message_text(text=format_live_dashboard(live_data), chat_id=job.chat_id, message_id=job.data['message_id'], parse_mode=ParseMode.MARKDOWN, reply_markup=get_live_keyboard())
    except BadRequest as e:
        if "Message to edit not found" in str(e):
            logger.warning(f"Live dashboard message not found for job {job.name}, stopping the job.")
            if job.name in context.bot_data: del context.bot_data[job.name]
            job.schedule_removal()
        elif "Message is not modified" not in str(e): pass
    except Exception as e:
        logger.error(f"Error di live_update_callback: {e}", exc_info=True)
        if job.name in context.bot_data: del context.bot_data[job.name]
        job.schedule_removal()
        try:
            await context.bot.send_message(job.chat_id, "Live dashboard dihentikan karena terjadi error.")
        except Exception as send_e: logger.error(f"Gagal mengirim pesan error live dashboard: {send_e}")

async def live_monitor_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    chat_id, message_id = query.message.chat_id, query.message.message_id
    job_name = f"live_{chat_id}"
    if context.job_queue.get_jobs_by_name(job_name): return
    await query.message.edit_text("🚀 Memulai Live Dashboard...")
    interfaces_str = os.getenv("MONITOR_INTERFACES")
    if interfaces_str: interfaces = [iface.strip() for iface in interfaces_str.split(',')]
    else:
        wan_ifaces = list(set(re.findall(r'dev\s+([^\s]+)', run_cmd(["ip", "route", "show", "default"]))))
        interfaces = wan_ifaces + ["br-lan"] if "br-lan" not in wan_ifaces else wan_ifaces
    cpu_line, cpu_parts = read_file("/proc/stat").splitlines()[0], []
    if cpu_line: cpu_parts = [int(p) for p in cpu_line.split()[1:]]
    disk_stats_raw, (initial_disk_read, initial_disk_write) = read_file("/proc/diskstats"), (0, 0)
    disk_match = re.search(r'\s+(sda|vda|nvme\w+|mmcblk\d+)\s+', disk_stats_raw)
    if disk_match and (parts_match := re.search(rf'\s+{disk_match.group(1)}\s+(.*)', disk_stats_raw)) and len(parts := parts_match.group(1).split()) > 6:
        initial_disk_read, initial_disk_write = int(parts[2]) * 512, int(parts[6]) * 512
    initial_stats = {
        'interfaces': interfaces, 'time': time.time(), 
        'cpu_total': sum(cpu_parts), 'cpu_idle': cpu_parts[3] if len(cpu_parts) > 3 else 0,
        'disk_read': initial_disk_read, 'disk_write': initial_disk_write
    }
    for iface in interfaces:
        initial_stats[f'rx_{iface}'] = int(read_file(f"/sys/class/net/{iface}/statistics/rx_bytes") or 0)
        initial_stats[f'tx_{iface}'] = int(read_file(f"/sys/class/net/{iface}/statistics/tx_bytes") or 0)
    context.bot_data[job_name] = initial_stats
    context.job_queue.run_repeating(live_update_callback, 2, 0, data={'message_id': message_id}, name=job_name, chat_id=chat_id)

async def live_monitor_stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    job_name = f"live_{query.message.chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in jobs: job.schedule_removal()
    if job_name in context.bot_data: del context.bot_data[job.name]
    await start_handler(update, context)

# HANDLER PANEL KONTROL (DIAMANKAN)
@admin_only
async def control_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*⚙️ Panel Kontrol Admin*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_control_panel_keyboard())

@admin_only
async def reboot_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*🔄 Menu Reboot & Restart*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_reboot_menu_keyboard())

@admin_only
async def reboot_confirmation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("⚠️ *ANDA YAKIN?*\n\nPerintah ini akan me-reboot seluruh router.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_reboot_confirmation_keyboard())

@admin_only
async def reboot_execute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("✅ Perintah REBOOT dikirim. Router akan offline sementara.")
    run_cmd(["reboot"])

@admin_only
async def restart_service_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    service = query.data.split('_')[-1]
    await query.message.edit_text(f"✅ Merestart layanan *{service}*...", parse_mode=ParseMode.MARKDOWN)
    run_cmd([f"/etc/init.d/{service}", "restart"])
    await asyncio.sleep(2)
    await query.message.edit_text(f"✅ Layanan *{service}* telah direstart.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_reboot_menu_keyboard())

@admin_only
async def device_management_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*🔒 Manajemen Perangkat*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_device_management_keyboard())

@admin_only
async def device_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    page = int(query.data.split('_')[-1])
    text, keyboard = format_device_list(get_combined_device_list(), page)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

@admin_only
async def block_device_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: _, _, mac, name = query.data.split('_', 3)
    except ValueError: await query.answer("Error: Callback data tidak valid.", show_alert=True); return
    await query.answer(f"Memblokir {name}...")
    rule_name = f"bot_block_{mac.replace(':', '').lower()}"
    run_cmd(["uci", "delete", f"firewall.{rule_name}"])
    run_cmd(["uci", "add", "firewall", "rule"])
    run_cmd(["uci", "set", f"firewall.@rule[-1].name='Block:{name}'"])
    run_cmd(["uci", "set", f"firewall.@rule[-1].src='lan'"])
    run_cmd(["uci", "set", f"firewall.@rule[-1].src_mac='{mac}'"])
    run_cmd(["uci", "set", f"firewall.@rule[-1].target='REJECT'"])
    run_cmd(["uci", "rename", "firewall.@rule[-1]", rule_name])
    run_cmd(["uci", "commit", "firewall"])
    run_cmd(["/etc/init.d/firewall", "restart"])
    await asyncio.sleep(1)
    await device_list_handler(update, context)

@admin_only
async def unblock_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    text, kb = format_blocked_list(get_blocked_devices())
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@admin_only
async def unblock_device_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, mac, name = query.data.split('_', 3)
    rule_name = f"bot_block_{mac.replace(':', '').lower()}"
    await query.answer(f"Membuka blokir {name}...")
    run_cmd(["uci", "delete", f"firewall.{rule_name}"])
    run_cmd(["uci", "commit", "firewall"])
    run_cmd(["/etc/init.d/firewall", "restart"])
    await asyncio.sleep(1)
    await unblock_list_handler(update, context)

@admin_only
async def services_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    try: page = int(query.data.split('_')[-1])
    except (ValueError, IndexError): page = 1
    per_page=8
    excluded_services = [s.strip() for s in os.getenv("EXCLUDE_SERVICES", "").split(',') if s.strip()]
    initd_path = Path("/etc/init.d/")
    total_services = sum(1 for p in initd_path.iterdir() if p.is_file() and os.access(p, os.X_OK) and p.name not in excluded_services)
    total_pages = (total_services + per_page - 1) // per_page
    keyboard = get_services_keyboard(page=page, per_page=per_page)
    text = f"Pilih layanan untuk dikelola (Halaman {page}/{total_pages}):"
    await query.edit_message_text(text, reply_markup=keyboard)

@admin_only
async def service_action_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    service_name = query.data.split('_')[-1]
    await query.edit_message_text(f"Kelola layanan: *{service_name.title()}*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_service_action_keyboard(service_name))

@admin_only
async def service_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    _, _, target, command = query.data.split('_')
    run_cmd([f"/etc/init.d/{target}", command])
    if command == "status":
        status_output = run_cmd([f'/etc/init.d/{target}', 'status']) or 'Tidak ada output.'
        response_text = f"*Status {target}*:\n```\n{status_output}\n```"
    else:
        response_text = f"✅ Perintah `{command}` untuk layanan `{target}` dieksekusi."
    await query.message.edit_text(response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_service_action_keyboard(target))

@admin_only
async def wifi_control_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*📡 Kontrol WiFi*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_wifi_control_keyboard())

@admin_only
async def wifi_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Mengubah status radio...")
    _, _, target, _ = query.data.split('_')
    is_up = json.loads(run_cmd(["ubus", "call", "network.wireless", "status"]) or "{}").get(target, {}).get("up", False)
    run_cmd(["wifi", "down" if is_up else "up", target])
    time.sleep(2)
    await query.message.edit_text("*📡 Kontrol WiFi*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_wifi_control_keyboard())

# HANDLER DIAGNOSTIK (AKSES PUBLIK)
async def diagnostics_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*🌐 Diagnostik Jaringan*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_diagnostics_menu_keyboard())

async def logread_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    log_content = run_cmd(['logread', '-l', '20']) or 'Log kosong.'
    await query.message.edit_text(f"Log Sistem Terakhir:\n```\n{log_content}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=get_diagnostics_menu_keyboard())

async def diagnostic_ping_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("Gunakan perintah `/ping [hostname/ip]`\nContoh: `/ping google.com`", parse_mode=ParseMode.MARKDOWN)

async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Gunakan: `/ping [hostname/ip]`"); return
    host = context.args[0]
    msg = await update.message.reply_text(f"🏓 Menjalankan ping ke *{host}*...", parse_mode=ParseMode.MARKDOWN)
    ping_output = run_cmd(['ping', '-c', '4', host]) or 'Gagal menjalankan ping.'
    await msg.edit_text(f"Hasil Ping ke *{host}*:\n```\n{ping_output}\n```", parse_mode=ParseMode.MARKDOWN)

async def speedtest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    
    msg = await message.reply_text("🚀 Menjalankan *speed test*... (bisa memakan waktu 1-2 menit)", parse_mode=ParseMode.MARKDOWN)
    await msg.edit_text("💨 *Testing Download...*", parse_mode=ParseMode.MARKDOWN)
    output = run_cmd(["speedtest-go", "--json"])
    try:
        data = json.loads(output)
        if not data.get('servers'): raise KeyError("Kunci 'servers' tidak ditemukan.")
        server_info = data['servers'][0]
        ping_ms = float(server_info.get('latency', 0)) / 1_000_000
        down_mbps = float(server_info.get('dl_speed', 0)) * 8 / 1_000_000
        up_mbps = float(server_info.get('ul_speed', 0)) * 8 / 1_000_000
        server_name = server_info.get('name', 'N/A')
        text = (f"*Hasil Speed Test:*\n\n"
                f"  • *Server*: `{server_name}`\n"
                f"  • *Ping*: `{ping_ms:.2f} ms`\n"
                f"  • *Download*: `{down_mbps:.2f} Mbps`\n"
                f"  • *Upload*: `{up_mbps:.2f} Mbps`")
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        await msg.edit_text(f"❌ Gagal mem-parsing hasil.\n*Error:* `{e}`\n\n*Output Mentah:*\n```\n{output or 'Tidak ada output.'}\n```", parse_mode=ParseMode.MARKDOWN)


# --- HANDLER FITUR BARU ---
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*Bantuan ST4Bot Monitoring*\n\n"
        "Bot ini menyediakan antarmuka untuk memonitor dan mengelola router OpenWrt Anda.\n\n"
        "*Perintah Publik:*\n"
        "`/start` - Menampilkan status & menu utama\n"
        "`/help` - Menampilkan pesan bantuan ini\n"
        "`/ping [host]` - Mengirim ping ke host/IP\n"
        "`/check_setup` - Memeriksa dependensi bot\n"
        "`/find_device [nama]` - Mencari perangkat\n\n"
        "*Perintah Admin:*\n"
        "`/wol [MAC]` - Wake-on-LAN\n"
        "`/set_alias [MAC] [Nama]` - Atur nama alias\n"
        "`/del_alias [MAC]` - Hapus nama alias\n"
        "`/aliases` - Lihat semua alias\n"
        "`/schedule_reboot [HH:MM]` - Jadwalkan reboot\n"
        "`/cancel_reboot` - Batalkan reboot terjadwal\n"
        "`/guest_wifi_on [jam]` - Aktifkan WiFi Tamu\n"
        "`/guest_wifi_off` - Matikan WiFi Tamu\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

@admin_only
async def wol_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Gunakan: `/wol [MAC_Address]`"); return
    mac = context.args[0].strip()
    if not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', mac): await update.message.reply_text("Format Alamat MAC tidak valid."); return
    if shutil.which("etherwake"):
        output = run_cmd(["etherwake", "-i", "br-lan", mac])
        await update.message.reply_text(f"✅ Magic Packet dikirim ke `{mac}`.\n`{output or 'Tidak ada output.'}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Perintah `etherwake` tidak ditemukan.")

async def check_setup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = {
        "Bandwidth Monitor": "wrtbwmon", # Diubah ke wrtbwmon
        "Speedtest": "speedtest-go",
        "Wake-on-LAN": "etherwake"
    }
    results = ["*🔎 Hasil Pengecekan Dependensi Bot*\n"]
    for name, cmd in deps.items():
        if shutil.which(cmd) or os.path.exists(f"/etc/init.d/{cmd}"):
            results.append(f"  ✅ *{name}*: Ditemukan")
        else:
            results.append(f"  ❌ *{name}*: Tidak Ditemukan (`{cmd}` tidak ada)")
    results.append("\nFitur yang dependennya tidak ditemukan mungkin tidak akan berfungsi.")
    await update.message.reply_text("\n".join(results), parse_mode=ParseMode.MARKDOWN)

@admin_only
async def set_alias_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Gunakan: `/set_alias [MAC_Address] [Nama Alias]`")
        return
    mac = args[0].upper()
    alias = " ".join(args[1:])
    if not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', mac):
        await update.message.reply_text("Format Alamat MAC tidak valid.")
        return
    DEVICE_ALIASES[mac] = alias
    save_aliases()
    await update.message.reply_text(f"✅ Alias untuk `{mac}` telah diatur menjadi *{alias}*.", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def del_alias_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: `/del_alias [MAC_Address]`")
        return
    mac = context.args[0].upper()
    if mac in DEVICE_ALIASES:
        del DEVICE_ALIASES[mac]
        save_aliases()
        await update.message.reply_text(f"✅ Alias untuk `{mac}` telah dihapus.")
    else:
        await update.message.reply_text(f"❌ Tidak ditemukan alias untuk `{mac}`.")

@admin_only
async def list_aliases_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DEVICE_ALIASES:
        await update.message.reply_text("Tidak ada alias yang diatur.")
        return
    message = ["*Daftar Alias Perangkat:*\n"]
    for mac, alias in DEVICE_ALIASES.items():
        message.append(f"`{mac}`: *{alias}*")
    await update.message.reply_text("\n".join(message), parse_mode=ParseMode.MARKDOWN)

async def find_device_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: `/find_device [Nama]`")
        return
    query = " ".join(context.args).lower()
    all_devices = get_combined_device_list()
    found_devices = [
        dev for dev in all_devices 
        if query in dev['name'].lower()
    ]
    if not found_devices:
        await update.message.reply_text(f"Tidak ada perangkat yang cocok dengan '{query}'.")
        return
    message = [f"*Hasil pencarian untuk '{query}':*\n"]
    for dev in found_devices:
        usage_str = f" | `↓{format_bytes(dev.get('down', 0))} / ↑{format_bytes(dev.get('up', 0))}`" if 'down' in dev else ""
        message.append(f"• *{escape_markdown_v1(dev['name'])}*\n  `{dev['ip']}` | `{dev['mac']}`{usage_str}")
    await update.message.reply_text("\n".join(message), parse_mode=ParseMode.MARKDOWN)

@admin_only
async def security_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("*🛡️ Menu Keamanan*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_security_menu_keyboard())

@admin_only
async def security_audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Menjalankan audit...")
    results = ["*🔎 Hasil Audit Keamanan Dasar:*\n"]
    # Cek SSH
    dropbear_conf = read_file("/etc/config/dropbear")
    if "option PasswordAuth 'on'" in dropbear_conf:
        results.append("  ⚠️ *SSH*: Login dengan password diaktifkan. Disarankan menggunakan key-based auth.")
    else:
        results.append("  ✅ *SSH*: Login dengan password dinonaktifkan.")
    # Cek LuCI
    uhttpd_conf = read_file("/etc/config/uhttpd")
    if "list listen_http '0.0.0.0:" in uhttpd_conf or "list listen_https '0.0.0.0:" in uhttpd_conf:
         results.append("  ❌ *LuCI*: Antarmuka web mungkin bisa diakses dari WAN. Sangat tidak disarankan!")
    else:
        results.append("  ✅ *LuCI*: Antarmuka web hanya bisa diakses dari LAN.")
    await query.message.edit_text("\n".join(results), parse_mode=ParseMode.MARKDOWN, reply_markup=get_security_menu_keyboard())

@admin_only
async def adblock_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not os.path.exists("/etc/init.d/adblock"):
        await query.message.edit_text("❌ Layanan `adblock` tidak ditemukan.", reply_markup=get_security_menu_keyboard()); return
    
    status = run_cmd(["/etc/init.d/adblock", "status"])
    stats_match = re.search(r"(\d+)\s+domains\s+in\s+.*,.*(enabled|disabled)", status)
    if stats_match:
        domains, state = stats_match.groups()
        text = f"*🚫 Status Adblock:*\n`{domains}` domain diblokir.\nStatus saat ini: *{state.upper()}*"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Aktifkan" if state == 'disabled' else "Nonaktifkan", callback_data="adblock_toggle")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="security_menu_show")]
        ])
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await query.message.edit_text(f"*Status Adblock:*\n```\n{status}```", parse_mode=ParseMode.MARKDOWN, reply_markup=get_security_menu_keyboard())

@admin_only
async def adblock_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    status = run_cmd(["/etc/init.d/adblock", "status"])
    state = "enabled" if "enabled" in status else "disabled"
    action = "stop" if state == "enabled" else "start"
    await query.message.edit_text(f"⚙️ Menjalankan perintah `{action}` untuk adblock...", parse_mode=ParseMode.MARKDOWN)
    run_cmd(["/etc/init.d/adblock", action])
    await asyncio.sleep(3)
    await adblock_status_handler(update, context)

async def reboot_job_callback(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=os.getenv("TELEGRAM_ADMIN_ID"), text="⏰ Reboot terjadwal sedang dijalankan sekarang...")
    run_cmd(["reboot"])

@admin_only
async def schedule_reboot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not re.match(r'^\d{2}:\d{2}$', context.args[0]):
        await update.message.reply_text("Gunakan: `/schedule_reboot HH:MM` (format 24 jam)")
        return
    
    current_jobs = context.job_queue.get_jobs_by_name("scheduled_reboot")
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()

    hour, minute = map(int, context.args[0].split(':'))
    context.job_queue.run_daily(reboot_job_callback, time=datetime.strptime(f"{hour}:{minute}", "%H:%M").time(), name="scheduled_reboot")
    await update.message.reply_text(f"✅ Reboot telah dijadwalkan setiap hari pada pukul *{hour:02d}:{minute:02d}*.", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cancel_reboot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_jobs = context.job_queue.get_jobs_by_name("scheduled_reboot")
    if not current_jobs:
        await update.message.reply_text("Tidak ada reboot yang terjadwal.")
        return
    for job in current_jobs:
        job.schedule_removal()
    await update.message.reply_text("✅ Jadwal reboot telah dibatalkan.")

async def guest_wifi_off_callback(context: ContextTypes.DEFAULT_TYPE):
    iface = os.getenv("GUEST_WIFI_IFACE")
    if not iface: return
    run_cmd(["uci", "set", f"wireless.{iface}.disabled='1'"])
    run_cmd(["uci", "commit", "wireless"])
    run_cmd(["wifi", "reload"])
    await context.bot.send_message(chat_id=os.getenv("TELEGRAM_ADMIN_ID"), text="⏰ WiFi Tamu telah dinonaktifkan secara otomatis.")

@admin_only
async def guest_wifi_on_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iface = os.getenv("GUEST_WIFI_IFACE")
    if not iface:
        await update.message.reply_text("❌ `GUEST_WIFI_IFACE` tidak diatur di file .env"); return
    
    hours = 1
    if context.args and context.args[0].isdigit():
        hours = int(context.args[0])
    
    run_cmd(["uci", "set", f"wireless.{iface}.disabled='0'"])
    run_cmd(["uci", "commit", "wireless"])
    run_cmd(["wifi", "reload"])
    
    context.job_queue.run_once(guest_wifi_off_callback, when=timedelta(hours=hours))
    await update.message.reply_text(f"✅ WiFi Tamu telah diaktifkan dan akan mati dalam *{hours} jam*.", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def guest_wifi_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iface = os.getenv("GUEST_WIFI_IFACE")
    if not iface:
        await update.message.reply_text("❌ `GUEST_WIFI_IFACE` tidak diatur di file .env"); return
    
    run_cmd(["uci", "set", f"wireless.{iface}.disabled='1'"])
    run_cmd(["uci", "commit", "wireless"])
    run_cmd(["wifi", "reload"])
    await update.message.reply_text("✅ WiFi Tamu telah dinonaktifkan.")


# --- BAGIAN 7: FUNGSI PERIODIK DAN MAIN ---

async def daily_report_callback(context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    if not admin_id: return
    
    logger.info("Membuat laporan harian...")
    stats = get_full_stats()
    uptime = stats.get("sistem", {}).get("uptime", "N/A")
    
    total_down, total_up = 0, 0
    if os.path.exists("/tmp/usage.db"):
        traffic_data = get_traffic_usage()
        for usage in traffic_data.values():
            total_down += usage.get('down', 0)
            total_up += usage.get('up', 0)
    
    try:
        if KNOWN_DEVICES_FILE.exists():
            known_devices = set(json.loads(KNOWN_DEVICES_FILE.read_text()))
            current_macs = {dev['mac'] for dev in get_dhcp_leases()}
            new_mac_count = len(current_macs - known_devices)
        else: new_mac_count = 0
    except: new_mac_count = "N/A"

    message = [
        f"☀️ *Laporan Harian Router - {datetime.now().strftime('%d %B %Y')}*",
        f"  • *Uptime*: {uptime}",
        f"  • *Total Trafik (sesuai wrtbwmon)*: ↓{format_bytes(total_down)} / ↑{format_bytes(total_up)}",
        f"  • *Perangkat Baru Terdeteksi*: {new_mac_count}"
    ]
    await context.bot.send_message(chat_id=admin_id, text="\n".join(message), parse_mode=ParseMode.MARKDOWN)

async def cpu_alert_callback(context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    if not admin_id: return

    load_avg_str = read_file("/proc/loadavg").split()[0]
    load_avg = float(load_avg_str)
    cpu_cores = os.cpu_count() or 1
    load_percent = (load_avg / cpu_cores) * 100

    alert_threshold = 90
    alert_duration = timedelta(minutes=5)
    
    if load_percent > alert_threshold:
        if 'cpu_high_since' not in context.bot_data:
            context.bot_data['cpu_high_since'] = time.time()
        
        high_since = context.bot_data['cpu_high_since']
        if time.time() - high_since > alert_duration.total_seconds():
            if not context.bot_data.get('cpu_alert_sent', False):
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🚨 *Peringatan Performa*: Beban CPU di atas {alert_threshold}% selama lebih dari 5 menit. (Saat ini: {load_percent:.1f}%)",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.bot_data['cpu_alert_sent'] = True
    else:
        if 'cpu_high_since' in context.bot_data:
            del context.bot_data['cpu_high_since']
        if context.bot_data.get('cpu_alert_sent', False):
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"✅ *Info Performa*: Beban CPU telah kembali normal.",
                parse_mode=ParseMode.MARKDOWN
            )
            del context.bot_data['cpu_alert_sent']

async def check_wan_status(context: ContextTypes.DEFAULT_TYPE):
    if not (admin_id := os.getenv("TELEGRAM_ADMIN_ID")): return
    wan_info = get_wan_interfaces_info()
    current_ip = wan_info[0].get("ip") if wan_info else "N/A"
    if 'last_wan_ip' not in context.bot_data:
        context.bot_data['last_wan_ip'] = current_ip
        return
    last_ip = context.bot_data.get("last_wan_ip")
    if current_ip != last_ip:
        logger.info(f"Perubahan IP WAN terdeteksi! Lama: {last_ip}, Baru: {current_ip}")
        message = "🚨 *Peringatan:* Koneksi WAN terputus!" if current_ip == "N/A" else f"ℹ️ *Info:* Alamat IP WAN berubah.\nLama: `{last_ip}`\nBaru: `{current_ip}`"
        await context.bot.send_message(chat_id=admin_id, text=message, parse_mode=ParseMode.MARKDOWN)
        context.bot_data['last_wan_ip'] = current_ip

async def check_new_devices(context: ContextTypes.DEFAULT_TYPE):
    if not (admin_id := os.getenv("TELEGRAM_ADMIN_ID")): return
    try:
        known_devices = set(json.loads(KNOWN_DEVICES_FILE.read_text())) if KNOWN_DEVICES_FILE.exists() else set()
        current_macs = {dev['mac'] for dev in get_dhcp_leases()}
        if not known_devices and current_macs:
            KNOWN_DEVICES_FILE.write_text(json.dumps(list(current_macs)))
            return
        if new_macs := current_macs - known_devices:
            all_devices = get_combined_device_list()
            for mac in new_macs:
                device_info = next((d for d in all_devices if d['mac'] == mac), None)
                if device_info:
                    message = (f"🕵️ *Notifikasi Keamanan: Perangkat Baru Terdeteksi*\n\n"
                               f"  • *Nama*: `{escape_markdown_v1(device_info['name'])}`\n"
                               f"  • *IP*: `{device_info['ip']}`\n"
                               f"  • *MAC*: `{device_info['mac']}`")
                    await context.bot.send_message(chat_id=admin_id, text=message, parse_mode=ParseMode.MARKDOWN)
            KNOWN_DEVICES_FILE.write_text(json.dumps(list(current_macs)))
    except Exception as e:
        logger.error(f"Gagal memeriksa perangkat baru: {e}")

async def post_init(application: Application) -> None:
    try:
        commands = [
            BotCommand("start", "🚀 Status & Menu Utama"),
            BotCommand("help", "ℹ️ Menampilkan bantuan"),
            BotCommand("ping", "🏓 Ping ke host"),
            BotCommand("check_setup", "🔎 Periksa dependensi bot"),
            BotCommand("find_device", "Cari perangkat"),
            BotCommand("wol", "☕ Wake-on-LAN (Admin)"),
            BotCommand("set_alias", "Atur nama alias perangkat (Admin)"),
            BotCommand("del_alias", "Hapus nama alias (Admin)"),
            BotCommand("aliases", "Lihat semua alias (Admin)"),
            BotCommand("schedule_reboot", "Jadwalkan reboot harian (Admin)"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Perintah bot telah berhasil didaftarkan ke Telegram.")
    except Exception as e:
        logger.error(f"Gagal mendaftarkan perintah bot: {e}", exc_info=True)

def main():
    load_dotenv()
    if not (token := os.getenv("TELEGRAM_BOT_TOKEN")):
        logger.critical("FATAL: TELEGRAM_BOT_TOKEN tidak ditemukan!"); return
    
    load_aliases()
    
    app = ApplicationBuilder().token(token).job_queue(JobQueue()).post_init(post_init).build()

    if (admin_id := os.getenv("TELEGRAM_ADMIN_ID")):
        app.job_queue.run_repeating(check_wan_status, interval=300, first=10)
        app.job_queue.run_repeating(check_new_devices, interval=60, first=20)
        app.job_queue.run_repeating(cpu_alert_callback, interval=60, first=60)
        app.job_queue.run_daily(daily_report_callback, time=datetime.strptime("08:00", "%H:%M").time())

    handlers = [
        # Perintah
        CommandHandler(['start', 'status'], start_handler),
        CommandHandler('help', help_handler),
        CommandHandler('ping', ping_handler),
        CommandHandler('wol', wol_handler),
        CommandHandler('check_setup', check_setup_handler),
        CommandHandler('find_device', find_device_handler),
        CommandHandler('set_alias', set_alias_handler),
        CommandHandler('del_alias', del_alias_handler),
        CommandHandler('aliases', list_aliases_handler),
        CommandHandler('schedule_reboot', schedule_reboot_handler),
        CommandHandler('cancel_reboot', cancel_reboot_handler),
        CommandHandler('guest_wifi_on', guest_wifi_on_handler),
        CommandHandler('guest_wifi_off', guest_wifi_off_handler),

        # Menu Utama & Refresh & Live
        CallbackQueryHandler(main_menu_handler, pattern='^main_menu_show$'),
        CallbackQueryHandler(start_handler, pattern='^full_status_refresh$'),
        CallbackQueryHandler(live_monitor_start_handler, pattern='^live_start$'),
        CallbackQueryHandler(live_monitor_stop_handler, pattern='^live_stop$'),

        # Panel Kontrol (Dilindungi)
        CallbackQueryHandler(control_panel_handler, pattern='^control_panel_show$'),
        CallbackQueryHandler(reboot_menu_handler, pattern='^reboot_menu_show$'),
        CallbackQueryHandler(reboot_confirmation_handler, pattern='^reboot_confirm_show$'),
        CallbackQueryHandler(reboot_execute_handler, pattern='^action_reboot_execute$'),
        CallbackQueryHandler(restart_service_handler, pattern=r'^action_restart_'),
        CallbackQueryHandler(device_management_handler, pattern='^device_management_show$'),
        CallbackQueryHandler(device_list_handler, pattern=r'^devices_page_'),
        CallbackQueryHandler(block_device_handler, pattern=r'^block_device_'),
        CallbackQueryHandler(unblock_list_handler, pattern=r'^blocked_list_show$'),
        CallbackQueryHandler(unblock_device_handler, pattern=r'^unblock_device_'),
        CallbackQueryHandler(services_page_handler, pattern=r'^services_page_'),
        CallbackQueryHandler(service_action_menu_handler, pattern=r'^service_menu_'),
        CallbackQueryHandler(service_action_handler, pattern=r'^action_service_'),
        CallbackQueryHandler(wifi_control_handler, pattern='^wifi_control_show$'),
        CallbackQueryHandler(wifi_toggle_handler, pattern=r'^action_wifi_'),
        CallbackQueryHandler(security_menu_handler, pattern='^security_menu_show$'),
        CallbackQueryHandler(security_audit_handler, pattern='^security_audit$'),
        CallbackQueryHandler(adblock_status_handler, pattern='^adblock_status$'),
        CallbackQueryHandler(adblock_toggle_handler, pattern='^adblock_toggle$'),

        # Diagnostik (Publik)
        CallbackQueryHandler(diagnostics_menu_handler, pattern='^diagnostics_menu_show$'),
        CallbackQueryHandler(speedtest_handler, pattern='^action_diagnostic_speedtest$'),
        CallbackQueryHandler(logread_handler, pattern='^action_diagnostic_logread$'),
        CallbackQueryHandler(diagnostic_ping_prompt_handler, pattern=r'^diagnostic_ping_prompt$'),
    ]
    app.add_handlers(handlers)
    
    logger.info("Bot versi final siap dan memulai polling...")
    app.run_polling()

if __name__ == '__main__':
    main()
