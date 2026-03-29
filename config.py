import os, json

SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "qito_super_secret_admin_key")
USERS_DB = "/root/qito_master/users_db.json"
NODES_LIST = "/root/qito_master/nodes_list.txt"
CONFIG_FILE = "/root/qito_master/config.json"
ADMIN_PASS = os.environ.get("PANEL_ADMIN_LEGACY_PASS", "admin123")
MASTER_API_KEY = os.environ.get("PANEL_MASTER_API_KEY", "My_Super_Secret_VPN_Key_2026")

def load_config():
    config = {
        "interval": 12,
        "bot_token": "",
        "admin_ids": [],
        "mod_ids": [],
        "auth_username": "admin",
        "auth_password_hash": "",
        "auth_2fa_enabled": True,
        "auth_telegram_bot_token": "",
        "auth_telegram_admin_id": "",
        "auth_otp_ttl_seconds": 300,
        "api_key_clients": [],
        "disabled_nodes": [],
        "backup_bot_enabled": False,
        "backup_bot_token": "",
        "backup_bot_admin_id": "",
        "backup_bot_interval_hours": 1,
        "backup_bot_interval_minutes": 60,
        "backup_bot_last_sent_ts": 0,
        "backup_bot_last_update_id": 0
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
                config.update(loaded)
                if not isinstance(config.get('admin_ids'), list): config['admin_ids'] = []
                if not isinstance(config.get('mod_ids'), list): config['mod_ids'] = []
                if not isinstance(config.get('disabled_nodes'), list): config['disabled_nodes'] = []
        except: pass
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: 
        json.dump(config, f)
