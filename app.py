import os
import sys
import re
import time
import json
import sqlite3
import smtplib
import ssl
import subprocess
import threading
import webbrowser
import socket
import struct
import ctypes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, send_file

# Configura codificação segura do console para evitar UnicodeEncodeError em Windows (cp1252)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Suporte à bandeja do sistema (System Tray / Relógio)
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# Diretório base (compatível com PyInstaller .exe)
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = get_app_dir()
DB_PATH = os.path.join(APP_DIR, 'monitor.db')

def get_logo_path():
    """Retorna o caminho absoluto do arquivo logo.png caso exista na raiz ou no bundle PyInstaller."""
    base_dirs = [
        APP_DIR,
        getattr(sys, '_MEIPASS', APP_DIR),
        os.getcwd()
    ]
    for bdir in base_dirs:
        lp = os.path.join(bdir, 'logo.png')
        if os.path.exists(lp):
            return lp
    return None

# Inicializa Flask apontando para pasta templates
template_folder = os.path.join(getattr(sys, '_MEIPASS', APP_DIR), 'templates')
app = Flask(__name__, template_folder=template_folder)

# --- BANCO DE DADOS (SQLite) ---
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn

def init_db():
    conn = get_db()
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    cursor = conn.cursor()

    # Tabela de dispositivos
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ip TEXT NOT NULL UNIQUE,
            location TEXT,
            room TEXT,
            mac TEXT,
            model TEXT,
            is_active INTEGER DEFAULT 1,
            last_status TEXT DEFAULT 'unknown',
            last_latency REAL DEFAULT 0,
            last_check TEXT,
            offline_since TEXT,
            created_at TEXT
        )
    ''')

    # Garante colunas caso o banco já exista (Migrações automáticas)
    for col in ['mac', 'model', 'location', 'resolved_ip', 'room', 'hostname', 'mode']:
        try:
            cursor.execute(f'ALTER TABLE devices ADD COLUMN {col} TEXT')
        except Exception:
            pass
    try:
        cursor.execute("UPDATE devices SET mode = 'dynamic' WHERE mode IS NULL OR mode = ''")
    except Exception:
        pass

    # Tabela de histórico de pings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ping_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER,
            timestamp TEXT,
            is_online INTEGER,
            latency_ms REAL,
            FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_dev_time ON ping_logs (device_id, timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_dev_id_desc ON ping_logs (device_id, id DESC)')

    # Tabela de configurações
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Configurações padrão
    defaults = {
        'ping_interval': '15',
        'email_enabled': '0',
        'smtp_host': 'smtp.office365.com',
        'smtp_port': '587',
        'smtp_user': '',
        'smtp_password': '',
        'smtp_from': '',
        'smtp_to': '',
        'smtp_tls': '1',
        'alert_offline': '1',
        'alert_recovery': '1'
    }

    for k, v in defaults.items():
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

    # Adiciona dispositivos de exemplo se estiver vazio
    cursor.execute('SELECT COUNT(*) as cnt FROM devices')
    if cursor.fetchone()['cnt'] == 0:
        cursor.execute('''
            INSERT INTO devices (name, ip, location, created_at)
            VALUES 
            ('Sala de Reunião 1 (Exemplo)', '8.8.8.8', 'Andar 1', datetime('now')),
            ('Gateway Principal (Exemplo)', '1.1.1.1', 'NOC', datetime('now'))
        ''')

    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    conn.commit()
    conn.close()

# --- PING ENGINE NATIVO (ICMP & HOSTNAMES/DNS) ---
def ping_ip(ip, timeout_ms=1000):
    """Executa ping nativo no Windows forçando IPv4 (-4) e aceitando IP ou Hostname/DNS."""
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    target = str(ip).strip()
    cmd = ["ping", "-4", "-n", "1", "-w", str(timeout_ms), target]

    resolved_ip = None
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=(timeout_ms / 1000.0) + 1.2,
            creationflags=creation_flags
        )
        out = proc.stdout or ""

        # Tenta extrair o IP resolvido caso o usuário tenha passado um hostname
        ip_match = re.search(r'\[([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\]', out)
        if ip_match:
            resolved_ip = ip_match.group(1)

        # Verifica TTL (indicação universal de resposta válida)
        if proc.returncode == 0 and ("TTL=" in out or "ttl=" in out):
            m = re.search(r'(?:time|tempo)\s*[=<]\s*(\d+)\s*ms', out, re.IGNORECASE)
            latency = float(m.group(1)) if m else 1.0
            return True, latency, resolved_ip
        return False, 0.0, resolved_ip
    except Exception:
        return False, 0.0, None

def get_email_signature_html(context_text="Notificação gerada automaticamente pelo sistema de monitoramento"):
    """Gera o rodapé HTML com assinatura institucional e logo embutido via CID."""
    has_logo = get_logo_path() is not None
    logo_td = ""
    if has_logo:
        logo_td = """
        <td style="vertical-align: middle; width: 130px; padding-right: 14px;">
            <div style="background: #ffffff; padding: 4px 8px; border-radius: 6px; display: inline-block;">
                <img src="cid:company_logo" alt="Logo" height="34" style="height: 34px; max-width: 120px; display: block; border: 0; outline: none;" />
            </div>
        </td>
        """
    return f"""
    <div style="border-top: 1px solid #1e293b; padding-top: 16px; margin-top: 24px;">
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: collapse;">
            <tr>
                {logo_td}
                <td style="vertical-align: middle; font-size: 11px; color: #64748b; line-height: 1.5;">
                    <strong style="color: #cbd5e1; font-size: 12px; letter-spacing: 0.3px;">Monitor</strong> &mdash; Videoconferência &amp; Infraestrutura<br>
                    <span>{context_text}</span>
                </td>
            </tr>
        </table>
    </div>
    """

# --- DISPARO DE E-MAIL (SMTP) ---
def dispatch_smtp(cfg, subject, html_content, custom_recipients=None):
    """
    Executa a conexão, autenticação e envio SMTP com tratamento detalhado de erros.
    Retorna uma tupla (sucesso: bool, mensagem_detalhada: str).
    """
    smtp_host = (cfg.get('smtp_host') or '').strip()
    try:
        smtp_port = int(cfg.get('smtp_port', '587') or 587)
    except (ValueError, TypeError):
        smtp_port = 587

    smtp_user = (cfg.get('smtp_user') or '').strip()
    smtp_pass = (cfg.get('smtp_password') or '').strip()
    smtp_from = (cfg.get('smtp_from') or '').strip() or smtp_user
    use_tls = str(cfg.get('smtp_tls', '1')) == '1'

    if not smtp_host:
        return False, "O endereço do servidor SMTP não foi informado."

    if custom_recipients:
        recipients = [r.strip() for r in custom_recipients if r and r.strip()]
    else:
        smtp_to_raw = (cfg.get('smtp_to') or '').strip()
        recipients = [r.strip() for r in smtp_to_raw.split(',') if r.strip()]

    if not recipients:
        return False, "Nenhum endereço de destinatário válido informado para envio."

    try:
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From'] = f"Monitor <{smtp_from}>" if smtp_from else "Monitor"
        msg['To'] = ", ".join(recipients)

        msg_alt = MIMEMultipart('alternative')
        msg_alt.attach(MIMEText(html_content, 'html', 'utf-8'))
        msg.attach(msg_alt)

        # Anexa o logotipo inline caso o arquivo exista
        logo_path = get_logo_path()
        if logo_path and os.path.exists(logo_path):
            try:
                with open(logo_path, 'rb') as f_img:
                    logo_img = MIMEImage(f_img.read(), _subtype='png')
                    logo_img.add_header('Content-ID', '<company_logo>')
                    logo_img.add_header('Content-Disposition', 'inline', filename='logo.png')
                    msg.attach(logo_img)
            except Exception as e_img:
                print(f"[!] Erro ao anexar imagem do logo ao e-mail: {e_img}")

        # Conexão SMTP com timeout seguro de 12 segundos
        if smtp_port == 465:
            try:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
            except Exception:
                ctx = ssl._create_unverified_context()
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=12)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)

        server.ehlo()
        if use_tls and smtp_port != 465:
            try:
                server.starttls()
            except Exception:
                ctx = ssl._create_unverified_context()
                server.starttls(context=ctx)
            server.ehlo()

        if smtp_user and smtp_pass:
            # Remove espaços comuns em senhas de app (ex: 'abcd efgh ijkl mnop' -> 'abcdefghijklmnop')
            clean_pass = smtp_pass.replace(' ', '') if 'gmail' in smtp_host.lower() else smtp_pass
            server.login(smtp_user, clean_pass)

        server.sendmail(smtp_from or smtp_user, recipients, msg.as_string())
        try:
            server.quit()
        except Exception:
            pass
        return True, f"E-mail enviado com sucesso para: {', '.join(recipients)}"
    except smtplib.SMTPServerDisconnected as e:
        if 'gmail' in smtp_host.lower():
            return False, "Conexão encerrada pelo Gmail (Connection unexpectedly closed). O Google NÃO aceita a sua senha normal da conta por segurança. Você precisa ativar a 'Verificação em 2 etapas' na sua Conta Google e criar uma 'Senha de App' (16 letras) em: myaccount.google.com/apppasswords."
        return False, f"O servidor SMTP encerrou a conexão inesperadamente ({str(e)}). Verifique se o servidor requer autenticação especial, Senha de Aplicativo ou se a porta e TLS estão corretos."
    except smtplib.SMTPAuthenticationError as e:
        code = getattr(e, 'smtp_code', 535)
        return False, f"Falha de Autenticação (código {code}): Usuário ou senha incorretos. (Dica: no Microsoft 365 ou Gmail, utilize uma Senha de Aplicativo ou verifique a permissão SMTP AUTH da conta)."
    except smtplib.SMTPSenderRefused:
        return False, f"Remetente Recusado ({smtp_from}): O servidor rejeitou este remetente. Verifique se a conta autenticada tem permissão de envio como '{smtp_from}'."
    except smtplib.SMTPRecipientsRefused:
        return False, f"Destinatários Recusados: O servidor SMTP rejeitou um ou mais destinatários ({', '.join(recipients)})."
    except (socket.timeout, TimeoutError):
        return False, f"Tempo limite esgotado (Timeout) ao conectar em {smtp_host}:{smtp_port}. Verifique conexão com a internet, regras de firewall ou se a porta está correta."
    except socket.gaierror:
        return False, f"Não foi possível resolver o endereço '{smtp_host}'. Verifique o nome do host do servidor SMTP."
    except ConnectionRefusedError:
        return False, f"Conexão recusada pelo servidor {smtp_host} na porta {smtp_port}. Verifique se o serviço SMTP está ativo nesta porta."
    except ssl.SSLError as e:
        return False, f"Erro de criptografia SSL/TLS: {str(e)}. Experimente alternar a opção TLS ou mudar a porta."
    except Exception as e:
        return False, f"Erro ao enviar e-mail: {str(e)}"

def send_email_alert(subject, html_content):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT key, value FROM settings')
    cfg = {row['key']: row['value'] for row in c.fetchall()}
    conn.close()

    if cfg.get('email_enabled') != '1':
        return False, "Envio de e-mail desativado nas configurações."

    return dispatch_smtp(cfg, subject, html_content)

# --- WORKER DE MONITORAMENTO EM BACKGROUND ---
def perform_ping_sweep():
    """Executa varredura paralela de pings em todos os dispositivos ativos, com auto-healing DHCP seguro."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM devices WHERE is_active = 1')
        devices = [dict(r) for r in cursor.fetchall()]
        conn.close()

        if not devices:
            return

        # Dispara pings em paralelo de forma escalável (até 60 workers concorrentes)
        workers = min(60, max(15, len(devices)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda d: (d, ping_ip(d['ip'])), devices))

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        now_time_only = datetime.now().strftime('%H:%M:%S')

        # Tabela ARP do sistema para auto-aprendizado e auto-healing por MAC (DHCP)
        arp_table = get_arp_table()

        log_records = []
        update_records = []
        email_alerts = []

        for dev, (is_online, latency, resolved_ip) in results:
            dev_id = dev['id']
            dev_ip = dev['ip']
            dev_mac = (dev.get('mac') or '').replace('-', ':').upper().strip()
            dev_hostname = (dev.get('hostname') or '').strip()
            dev_mode = (dev.get('mode') or 'dynamic').strip().lower()
            prev_status = dev.get('last_status') or 'unknown'

            # 1. AUTO-APRENDIZADO (Se online): aprende MAC nativo via SendARP e Hostname reverso se faltarem
            if is_online:
                check_ip = resolved_ip or dev_ip
                if not dev_mac and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', str(check_ip)):
                    learned_mac = get_mac_from_ip(check_ip)
                    if learned_mac:
                        dev_mac = learned_mac
                        conn_mac = get_db()
                        conn_mac.execute('UPDATE devices SET mac = ? WHERE id = ?', (learned_mac, dev_id))
                        conn_mac.commit()
                        conn_mac.close()

                if not dev_hostname and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', str(check_ip)):
                    try:
                        host, _, _ = socket.gethostbyaddr(check_ip)
                        if host:
                            dev_hostname = host
                            conn_h = get_db()
                            conn_h.execute('UPDATE devices SET hostname = ? WHERE id = ?', (host, dev_id))
                            conn_h.commit()
                            conn_h.close()
                    except Exception:
                        pass

            # 2. AUTO-HEALING DHCP MULTI-TIER: Se o ping falhou e o dispositivo está em modo DINÂMICO
            if not is_online and dev_mode == 'dynamic':
                healed = False

                # TIER A: Resolução por Hostname / DNS Dinâmico
                if dev_hostname:
                    try:
                        cand_ip = socket.gethostbyname(dev_hostname)
                        if cand_ip and cand_ip != dev_ip:
                            cand_ok, cand_lat, _ = ping_ip(cand_ip, timeout_ms=800)
                            if cand_ok:
                                cand_mac = get_mac_from_ip(cand_ip)
                                if not dev_mac or not cand_mac or cand_mac == dev_mac:
                                    print(f"[DHCP HOSTNAME AUTO-HEAL] Dispositivo '{dev['name']}' ({dev_hostname}) migrou: {dev_ip} -> {cand_ip}")
                                    is_online = True
                                    latency = cand_lat
                                    resolved_ip = cand_ip
                                    dev_ip = cand_ip
                                    healed = True
                                    conn_heal = get_db()
                                    try:
                                        conn_heal.execute('UPDATE devices SET ip = ?, resolved_ip = ? WHERE id = ?', (cand_ip, cand_ip, dev_id))
                                        conn_heal.commit()
                                    except sqlite3.IntegrityError:
                                        conn_heal.execute('UPDATE devices SET resolved_ip = ? WHERE id = ?', (cand_ip, dev_id))
                                        conn_heal.commit()
                                    finally:
                                        conn_heal.close()
                    except Exception:
                        pass

                # TIER B: Localização por MAC Address via SendARP / ARP Table / Subnet Scan
                if not healed and dev_mac:
                    cand_ip = arp_table.get(dev_mac)
                    if not cand_ip:
                        cand_ip = find_device_ip_by_mac(dev_mac)
                    if cand_ip and cand_ip != dev_ip:
                        cand_ok, cand_lat, _ = ping_ip(cand_ip, timeout_ms=800)
                        if cand_ok:
                            confirmed_mac = get_mac_sendarp(cand_ip) or get_mac_from_ip(cand_ip)
                            if confirmed_mac == dev_mac:
                                print(f"[DHCP MAC AUTO-HEAL] Dispositivo '{dev['name']}' ({dev_mac}) encontrado no novo IP: {dev_ip} -> {cand_ip}")
                                is_online = True
                                latency = cand_lat
                                resolved_ip = cand_ip
                                dev_ip = cand_ip
                                conn_heal = get_db()
                                try:
                                    conn_heal.execute('UPDATE devices SET ip = ?, resolved_ip = ? WHERE id = ?', (cand_ip, cand_ip, dev_id))
                                    conn_heal.commit()
                                except sqlite3.IntegrityError:
                                    conn_heal.execute('UPDATE devices SET resolved_ip = ? WHERE id = ?', (cand_ip, dev_id))
                                    conn_heal.commit()
                                finally:
                                    conn_heal.close()

            current_status = 'online' if is_online else 'offline'
            log_records.append((dev_id, now_str, 1 if is_online else 0, latency))

            offline_since = dev.get('offline_since')
            if not is_online and prev_status != 'offline':
                offline_since = now_str
            elif is_online:
                offline_since = None

            update_records.append((current_status, latency, now_time_only, offline_since, resolved_ip, dev_id))

            # Eventos de Alerta por E-mail
            if prev_status == 'online' and current_status == 'offline':
                if get_setting('alert_offline', '1') == '1':
                    subject = f"🔴 [ALERTA] Dispositivo Offline: {dev['name']} ({dev['ip']})"
                    body = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; padding: 24px; background: #0b0f19; color: #f8fafc; border-radius: 10px; max-width: 580px; margin: 0 auto; border: 1px solid #1e293b;">
                        <div style="border-bottom: 1px solid #1e293b; padding-bottom: 14px; margin-bottom: 16px;">
                            <h2 style="color: #ef4444; margin: 0; font-size: 20px;">⚠️ Dispositivo Inacessível (OFFLINE)</h2>
                            <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">O equipamento abaixo parou de responder às solicitações de PING.</div>
                        </div>
                        <table style="width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px;">
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Nome da Sala:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{dev['name']}</td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Endereço IP:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{dev['ip']}</td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Localização:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{dev['location'] or 'Não informada'}</td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Horário da Queda:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #ef4444;"><b>{now_str}</b></td></tr>
                        </table>
                        {get_email_signature_html("Alerta de Incidente emitido em tempo real pelo Monitor")}
                    </div>
                    """
                    email_alerts.append((subject, body))

            elif prev_status == 'offline' and current_status == 'online':
                if get_setting('alert_recovery', '1') == '1':
                    subject = f"🟢 [RECUPERADO] Dispositivo Online: {dev['name']} ({dev['ip']})"
                    body = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; padding: 24px; background: #0b0f19; color: #f8fafc; border-radius: 10px; max-width: 580px; margin: 0 auto; border: 1px solid #1e293b;">
                        <div style="border-bottom: 1px solid #1e293b; padding-bottom: 14px; margin-bottom: 16px;">
                            <h2 style="color: #10b981; margin: 0; font-size: 20px;">✅ Comunicação Restaurada (ONLINE)</h2>
                            <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">O equipamento voltou a responder normalmente na rede.</div>
                        </div>
                        <table style="width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px;">
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Nome da Sala:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{dev['name']}</td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Endereço IP:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{dev['ip']}</td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Latência Atual:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #10b981;"><b>{latency} ms</b></td></tr>
                            <tr><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;"><b>Horário de Retorno:</b></td><td style="padding: 8px; border-bottom: 1px solid #1e293b; color: #fff;">{now_str}</td></tr>
                        </table>
                        {get_email_signature_html("Notificação de recuperação de serviço emitida pelo Monitor")}
                    </div>
                    """
                    email_alerts.append((subject, body))

        # Gravação em lote ultrarrápida (Batch Transaction)
        conn = get_db()
        c = conn.cursor()
        if log_records:
            c.executemany(
                'INSERT INTO ping_logs (device_id, timestamp, is_online, latency_ms) VALUES (?, ?, ?, ?)',
                log_records
            )
        if update_records:
            c.executemany('''
                UPDATE devices 
                SET last_status = ?, last_latency = ?, last_check = ?, offline_since = ?, resolved_ip = COALESCE(?, resolved_ip)
                WHERE id = ?
            ''', update_records)

        conn.commit()
        conn.close()

        # Dispara alertas de e-mail em threads separadas
        for subj, bdy in email_alerts:
            threading.Thread(target=send_email_alert, args=(subj, bdy), daemon=True).start()

    except Exception as e_sweep:
        print(f"Erro no ciclo de ping sweep: {e_sweep}")

def monitoring_loop():
    last_cleanup = time.time()
    while True:
        try:
            interval = max(5, int(get_setting('ping_interval', '15')))
        except Exception:
            interval = 15

        try:
            perform_ping_sweep()

            # Limpeza periódica inteligente (roda apenas uma vez por hora)
            if time.time() - last_cleanup > 3600:
                conn = get_db()
                conn.execute("DELETE FROM ping_logs WHERE timestamp < datetime('now', '-7 days')")
                conn.commit()
                conn.close()
                last_cleanup = time.time()

        except Exception as e:
            print(f"Erro no loop de monitoramento: {e}")

        time.sleep(interval)



# --- ROTAS DA API / FRONTEND ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/logo.png')
def serve_logo():
    """Serve o arquivo logo.png da raiz do aplicativo."""
    logo_path = get_logo_path()
    if logo_path and os.path.exists(logo_path):
        return send_file(logo_path, mimetype='image/png', max_age=0)
    return ('', 404)

@app.route('/icon.ico')
@app.route('/favicon.ico')
def serve_icon():
    """Serve o ícone icon.ico da raiz do aplicativo."""
    base_dirs = [
        APP_DIR,
        getattr(sys, '_MEIPASS', APP_DIR),
        os.getcwd()
    ]
    for bdir in base_dirs:
        ico_path = os.path.join(bdir, 'icon.ico')
        if os.path.exists(ico_path):
            return send_file(ico_path, mimetype='image/x-icon', max_age=3600)
    return ('', 404)

@app.route('/api/devices', methods=['GET'])
def get_devices():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM devices WHERE is_active = 1 ORDER BY id ASC')
    devices = [dict(row) for row in c.fetchall()]

    if not devices:
        conn.close()
        return jsonify([])

    # 1. Calcula uptime 24h em UMA única query agrupada (O(1))
    twenty_four_hours_ago = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''
        SELECT 
            device_id,
            COUNT(*) as total,
            SUM(CASE WHEN is_online = 1 THEN 1 ELSE 0 END) as online_count
        FROM ping_logs 
        WHERE timestamp >= ?
        GROUP BY device_id
    ''', (twenty_four_hours_ago,))
    stats_map = {row['device_id']: row for row in c.fetchall()}

    # 2. Busca histórico recente (últimos 35 pings) de todos os dispositivos em UMA única query particionada
    c.execute('''
        SELECT id, device_id, is_online, latency_ms, timestamp
        FROM (
            SELECT id, device_id, is_online, latency_ms, timestamp,
                   ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id DESC) as rn
            FROM ping_logs
        )
        WHERE rn <= 35
        ORDER BY device_id, id ASC
    ''')
    history_map = {}
    for row in c.fetchall():
        dev_id = row['device_id']
        if dev_id not in history_map:
            history_map[dev_id] = []
        history_map[dev_id].append({
            'timestamp': row['timestamp'],
            'is_online': row['is_online'],
            'latency_ms': row['latency_ms']
        })

    conn.close()

    for d in devices:
        stat = stats_map.get(d['id'])
        if stat and stat['total']:
            total = stat['total']
            online = stat['online_count'] or 0
            d['uptime_24h'] = round((online / total * 100), 1)
        else:
            d['uptime_24h'] = 100.0

        d['history'] = history_map.get(d['id'], [])

    return jsonify(devices)

# --- FUNÇÕES DE REDE E TABELA ARP (DHCP AUTO-HEALING) ---
def get_mac_sendarp(ip_str):
    """
    Envia uma requisição ARP pura de Camada 2 diretamente via API do Kernel do Windows (SendARP).
    Retorna o MAC no formato XX:XX:XX:XX:XX:XX ou None se inacessível.
    """
    ip_str = str(ip_str).strip()
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
        return None
    try:
        ip_num = struct.unpack('>I', socket.inet_aton(ip_str))[0]
        mac_buf = (ctypes.c_byte * 6)()
        mac_len = ctypes.c_ulong(6)
        res = ctypes.windll.iphlpapi.SendARP(
            ctypes.c_ulong(socket.htonl(ip_num)),
            0,
            ctypes.byref(mac_buf),
            ctypes.byref(mac_len)
        )
        if res == 0:
            mac_str = ':'.join(f'{b & 0xff:02X}' for b in mac_buf)
            if mac_str not in ('00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'):
                return mac_str
    except Exception:
        pass
    return None

def get_arp_table():
    """Retorna um mapeamento {MAC: IP} e {IP: MAC} a partir da tabela ARP do sistema operacional."""
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        p = subprocess.run(['arp', '-a'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creation_flags)
        out = p.stdout or ""
        entries = re.findall(
            r'([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\s+([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})',
            out
        )
        arp_map = {}
        for ip, mac in entries:
            clean_mac = mac.replace('-', ':').upper()
            arp_map[clean_mac] = ip
            arp_map[ip] = clean_mac
        return arp_map
    except Exception:
        return {}

def get_mac_from_ip(ip):
    """Obtém o MAC Address do IP, priorizando a API SendARP nativa de alta velocidade."""
    mac = get_mac_sendarp(ip)
    if mac:
        return mac
    ip_str = str(ip).strip()
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
        return None
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        p = subprocess.run(
            ['arp', '-a', ip_str],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags,
            timeout=1.5
        )
        m = re.search(r'([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})', p.stdout or '')
        if m:
            clean = m.group(1).replace('-', ':').upper()
            if clean not in ('00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'):
                return clean
    except Exception:
        pass
    return None

def detect_device_network(target):
    """
    Sondagem inteligente em 3 camadas:
    1. Resolução de IP/Hostname
    2. Teste de conectividade (ICMP Ping + TCP Probe nas portas 80, 443, 5060)
    3. Captura do MAC Address via SendARP nativo e identificação de fabricante
    """
    target = str(target).strip()
    result = {
        'ip': target,
        'resolved_ip': target,
        'mac': '',
        'hostname': '',
        'vendor': '',
        'model': '',
        'is_online': False,
        'latency': 0.0,
        'l2_detected': False,
        'message': ''
    }

    resolved_ip = target
    # Se o usuário digitou um Hostname, resolve para IP
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', target):
        try:
            resolved_ip = socket.gethostbyname(target)
            result['resolved_ip'] = resolved_ip
            result['hostname'] = target
        except Exception:
            pass
    else:
        # Se digitou IP, tenta resolver o Hostname reverso
        try:
            host, _, _ = socket.gethostbyaddr(target)
            result['hostname'] = host
        except Exception:
            pass

    # 1. Teste de conectividade: Ping ICMP
    ok, lat, ping_resolved = ping_ip(target, timeout_ms=800)
    if ping_resolved:
        result['resolved_ip'] = ping_resolved
        resolved_ip = ping_resolved

    if ok:
        result['is_online'] = True
        result['latency'] = lat
    else:
        # 2. Se ICMP falhou (firewall bloqueando ping), testa portas TCP comuns de videoconferência
        for port in [443, 80, 5060, 8080]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.6)
                t_start = time.time()
                res = s.connect_ex((resolved_ip, port))
                t_end = time.time()
                s.close()
                if res == 0:
                    result['is_online'] = True
                    result['latency'] = round((t_end - t_start) * 1000, 1)
                    break
            except Exception:
                pass

    # 3. Captura o MAC Address físico via SendARP (Camada 2)
    clean_mac = get_mac_from_ip(resolved_ip)
    if clean_mac:
        result['mac'] = clean_mac
        result['l2_detected'] = True
        prefix = clean_mac[:8].upper()
        if any(prefix.startswith(x) for x in ['00:E0:DB', '00:04:F2', '64:16:7F', '00:25:45', '48:25:67', '80:7A:BF']):
            result['vendor'] = 'Poly / HP'
        elif any(prefix.startswith(x) for x in ['00:08:E1', '00:1B:D4', '00:1D:A2', '00:24:97', '70:69:79']):
            result['vendor'] = 'Cisco'
        elif any(prefix.startswith(x) for x in ['00:15:65', '80:5E:C0', '24:9A:D8']):
            result['vendor'] = 'Yealink'
        elif any(prefix.startswith(x) for x in ['00:04:20', '00:1F:20']):
            result['vendor'] = 'Logitech'
        elif prefix.startswith('00:10:7F'):
            result['vendor'] = 'Crestron'
        elif prefix.startswith('00:1B:24'):
            result['vendor'] = 'Lifesize'

    # 4. Tenta banner web se portas 80/443 responderem
    for proto, port in [('http', 80), ('https', 443)]:
        try:
            import urllib.request
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(f"{proto}://{resolved_ip}:{port}/", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=1.0, context=ctx) as resp:
                server = dict(resp.headers).get('Server', '')
                if 'poly' in server.lower() or 'realpresence' in server.lower():
                    result['vendor'] = result['vendor'] or 'Poly'
                    result['model'] = server
        except Exception:
            pass

    return result

def dhcp_watchdog_loop():
    """Worker em segundo plano: verifica a cada 60s se algum dispositivo dinâmico mudou de IP na rede."""
    time.sleep(15)
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id, name, ip, mac, hostname, mode FROM devices WHERE is_active = 1 AND mode = 'dynamic'")
            dynamic_devices = [dict(r) for r in c.fetchall()]
            conn.close()

            if dynamic_devices:
                arp_map = get_arp_table()
                for d in dynamic_devices:
                    d_mac = (d.get('mac') or '').replace('-', ':').upper().strip()
                    current_ip = d['ip'].strip()
                    if not d_mac:
                        continue
                    cand_ip = arp_map.get(d_mac)
                    if cand_ip and cand_ip != current_ip:
                        # Confirma via SendARP direto
                        if get_mac_sendarp(cand_ip) == d_mac:
                            ok, lat, _ = ping_ip(cand_ip, timeout_ms=600)
                            if ok:
                                print(f"[DHCP WATCHDOG AUTO-SYNC] '{d['name']}' sincronizado: {current_ip} -> {cand_ip}")
                                conn_w = get_db()
                                try:
                                    conn_w.execute("UPDATE devices SET ip = ?, resolved_ip = ? WHERE id = ?", (cand_ip, cand_ip, d['id']))
                                    conn_w.commit()
                                finally:
                                    conn_w.close()
        except Exception:
            pass
        time.sleep(60)

def get_local_subnet():
    """Identifica a sub-rede local da máquina (ex: 192.168.0.)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split('.')
        return f"{parts[0]}.{parts[1]}.{parts[2]}."
    except Exception:
        pass

    try:
        # Fallback para máquinas em redes fechadas/intranets
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and not local_ip.startswith('127.'):
            parts = local_ip.split('.')
            return f"{parts[0]}.{parts[1]}.{parts[2]}."
    except Exception:
        pass

    return "192.168.1."

def scan_network_subnet(subnet_prefix, start=1, end=254):
    """Varre a sub-rede em paralelo para descobrir IPs ativos com ARP/Hostname."""
    ips_to_scan = [f"{subnet_prefix}{i}" for i in range(start, end + 1)]
    active_devices = []

    def check_single_ip(target_ip):
        ok, lat, _ = ping_ip(target_ip, timeout_ms=500)
        if ok:
            info = detect_device_network(target_ip)
            info['latency'] = lat
            return info
        return None

    with ThreadPoolExecutor(max_workers=60) as executor:
        results = executor.map(check_single_ip, ips_to_scan)
        for res in results:
            if res:
                active_devices.append(res)

    active_devices.sort(key=lambda x: int(x['ip'].split('.')[-1]) if x['ip'].split('.')[-1].isdigit() else 0)
    return active_devices

def find_device_ip_by_mac(mac, subnet=None):
    """Localiza na rede local o novo endereço IP correspondente a um determinado MAC Address."""
    if not mac:
        return None
    target_mac = mac.replace('-', ':').upper().strip()

    # 1. Consulta o cache ARP do Windows (resposta instantânea em ~10ms)
    arp_table = get_arp_table()
    if target_mac in arp_table:
        candidate_ip = arp_table[target_mac]
        ok, _, _ = ping_ip(candidate_ip, timeout_ms=600)
        if ok:
            return candidate_ip

    # 2. Se não estiver no cache ARP, faz varredura rápida na sub-rede para popular a tabela ARP
    if not subnet:
        subnet = get_local_subnet()
    if not subnet.endswith('.'):
        subnet += '.'

    found_devices = scan_network_subnet(subnet, 1, 254)
    for d in found_devices:
        d_mac = (d.get('mac') or '').replace('-', ':').upper().strip()
        if d_mac == target_mac:
            return d['ip']

    # 3. Re-checa tabela ARP após a varredura
    arp_table = get_arp_table()
    if target_mac in arp_table:
        candidate_ip = arp_table[target_mac]
        ok, _, _ = ping_ip(candidate_ip, timeout_ms=600)
        if ok:
            return candidate_ip

    return None

@app.route('/api/network/subnet', methods=['GET'])
def api_get_subnet():
    return jsonify({'subnet': get_local_subnet()})

@app.route('/api/network/scan', methods=['POST'])
def api_scan_network():
    data = request.json or {}
    subnet = data.get('subnet') or get_local_subnet()
    if not subnet.endswith('.'):
        subnet += '.'
    start = int(data.get('start', 1))
    end = int(data.get('end', 254))

    found = scan_network_subnet(subnet, start, end)

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT ip, resolved_ip FROM devices WHERE is_active = 1')
    rows = c.fetchall()
    existing_ips = set()
    for row in rows:
        if row['ip']:
            existing_ips.add(row['ip'].strip().lower())
        if row['resolved_ip']:
            existing_ips.add(row['resolved_ip'].strip().lower())
    conn.close()

    for item in found:
        item['already_added'] = item['ip'].strip().lower() in existing_ips

    return jsonify({'subnet': subnet, 'count': len(found), 'devices': found})

@app.route('/api/devices/batch', methods=['POST'])
def api_batch_add():
    items = request.json or []
    if not isinstance(items, list):
        return jsonify({'error': 'Esperava uma lista de dispositivos.'}), 400

    conn = get_db()
    c = conn.cursor()
    added = 0
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    now_time_only = datetime.now().strftime('%H:%M:%S')

    for d in items:
        ip = d.get('ip', '').strip()
        if not ip:
            continue
        name = d.get('name', '').strip() or f"Dispositivo {ip}"
        mac = (d.get('mac') or '').replace('-', ':').upper().strip()
        model = d.get('model', '').strip() or d.get('vendor', '')
        location = d.get('location', '').strip() or 'Rede Local'
        room = d.get('room', '').strip() or location
        try:
            latency = float(d.get('latency', 1.0) or 1.0)
        except Exception:
            latency = 1.0

        hostname = d.get('hostname', '').strip()
        mode = d.get('mode', 'dynamic').strip().lower()
        if mode not in ('dynamic', 'manual'):
            mode = 'dynamic'

        try:
            c.execute('SELECT id FROM devices WHERE ip = ?', (ip,))
            existing = c.fetchone()
            if existing:
                dev_id = existing['id']
                c.execute('''
                    UPDATE devices 
                    SET name = ?, room = ?, location = ?, 
                        mac = COALESCE(NULLIF(?, ''), mac), 
                        model = COALESCE(NULLIF(?, ''), model), 
                        hostname = COALESCE(NULLIF(?, ''), hostname),
                        last_status = 'online', last_latency = ?, last_check = ?, offline_since = NULL
                    WHERE id = ?
                ''', (name, room, location, mac, model, hostname, latency, now_time_only, dev_id))
            else:
                c.execute('''
                    INSERT INTO devices (name, ip, room, location, mac, model, hostname, mode, is_active, last_status, last_latency, last_check, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'online', ?, ?, ?)
                ''', (name, ip, room, location, mac, model, hostname, mode, latency, now_time_only, now_str))
                dev_id = c.lastrowid
                added += 1

            if dev_id:
                c.execute('''
                    INSERT INTO ping_logs (device_id, timestamp, is_online, latency_ms)
                    VALUES (?, ?, 1, ?)
                ''', (dev_id, now_str, latency))
        except Exception as e:
            print(f"Aviso ao adicionar dispositivo em lote: {e}")

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'added': added})

@app.route('/api/devices/check-now', methods=['POST'])
def api_check_now():
    """Força checagem imediata em tempo real de todos os dispositivos e retorna dados atualizados."""
    perform_ping_sweep()
    return get_devices()

@app.route('/api/devices/detect', methods=['GET'])
def api_detect_device():
    ip = request.args.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'IP não informado.'}), 400
    info = detect_device_network(ip)
    return jsonify(info)

@app.route('/api/devices', methods=['POST'])
def add_device():
    data = request.json or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    room = data.get('room', '').strip()
    location = data.get('location', '').strip()
    mac = data.get('mac', '').strip()
    model = data.get('model', '').strip()
    hostname = data.get('hostname', '').strip()
    mode = data.get('mode', 'dynamic').strip().lower()
    if mode not in ('dynamic', 'manual'):
        mode = 'dynamic'

    if not name or not ip:
        return jsonify({'error': 'Nome e IP são obrigatórios.'}), 400

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO devices (name, ip, room, location, mac, model, hostname, mode, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (name, ip, room, location, mac, model, hostname, mode, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        dev_id = c.lastrowid
        conn.close()
        return jsonify({'success': True, 'id': dev_id})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Já existe um dispositivo com este endereço IP.'}), 400

@app.route('/api/devices/<int:dev_id>', methods=['PUT'])
def update_device(dev_id):
    data = request.json or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    room = data.get('room', '').strip()
    location = data.get('location', '').strip()
    mac = data.get('mac', '').strip()
    model = data.get('model', '').strip()
    hostname = data.get('hostname', '').strip()
    mode = data.get('mode', 'dynamic').strip().lower()
    if mode not in ('dynamic', 'manual'):
        mode = 'dynamic'

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            'UPDATE devices SET name = ?, ip = ?, room = ?, location = ?, mac = ?, model = ?, hostname = ?, mode = ? WHERE id = ?',
            (name, ip, room, location, mac, model, hostname, mode, dev_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Já existe outro dispositivo com este endereço IP.'}), 400

@app.route('/api/devices/<int:dev_id>', methods=['DELETE'])
def delete_device(dev_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM devices WHERE id = ?', (dev_id,))
    c.execute('DELETE FROM ping_logs WHERE device_id = ?', (dev_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/devices/<int:dev_id>/locate', methods=['POST'])
def api_locate_device(dev_id):
    """Localiza o dispositivo na rede usando seu MAC Address quando o IP muda via DHCP."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM devices WHERE id = ?', (dev_id,))
    dev = c.fetchone()
    if not dev:
        conn.close()
        return jsonify({'error': 'Dispositivo não encontrado.'}), 404

    mac = dev['mac']
    old_ip = dev['ip']

    if not mac:
        learned_mac = get_mac_from_ip(old_ip)
        if learned_mac:
            mac = learned_mac
            c.execute('UPDATE devices SET mac = ? WHERE id = ?', (learned_mac, dev_id))
            conn.commit()
        else:
            conn.close()
            return jsonify({'error': 'Dispositivo não possui MAC Address cadastrado para rastreamento.'}), 400

    new_ip = find_device_ip_by_mac(mac)
    if new_ip:
        now_time_only = datetime.now().strftime('%H:%M:%S')
        ok, lat, _ = ping_ip(new_ip)
        c.execute('SELECT id, name FROM devices WHERE ip = ? AND id != ?', (new_ip, dev_id))
        conflict = c.fetchone()
        if conflict:
            c.execute('''
                UPDATE devices 
                SET resolved_ip = ?, last_status = 'online', last_latency = ?, last_check = ?, offline_since = NULL
                WHERE id = ?
            ''', (new_ip, lat, now_time_only, dev_id))
        else:
            c.execute('''
                UPDATE devices 
                SET ip = ?, last_status = 'online', last_latency = ?, last_check = ?, offline_since = NULL
                WHERE id = ?
            ''', (new_ip, lat, now_time_only, dev_id))
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': f"Equipamento localizado e sincronizado com sucesso!",
            'old_ip': old_ip,
            'new_ip': new_ip,
            'mac': mac,
            'latency': lat
        })
    else:
        conn.close()
        return jsonify({
            'success': False,
            'message': f"O equipamento com MAC {mac} não foi encontrado na sub-rede. Verifique se está ligado."
        }), 404

@app.route('/api/devices/<int:dev_id>/history', methods=['GET'])
def get_device_history(dev_id):
    """Retorna os últimos 40 registros para montar o gráfico de linha do tempo."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT timestamp, is_online, latency_ms 
        FROM ping_logs 
        WHERE device_id = ? 
        ORDER BY id DESC LIMIT 40
    ''', (dev_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    rows.reverse() # Cronológico
    return jsonify(rows)

@app.route('/api/settings', methods=['GET'])
def get_settings():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT key, value FROM settings')
    settings = {row['key']: row['value'] for row in c.fetchall()}
    conn.close()

    # Oculta senha para segurança na visualização
    if 'smtp_password' in settings and settings['smtp_password']:
        settings['has_password'] = True
        settings['smtp_password'] = '********'
    else:
        settings['has_password'] = False

    return jsonify(settings)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()

    for k, v in data.items():
        if k == 'smtp_password' and v == '********':
            continue # Mantém senha existente
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (k, str(v)))

    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/settings/test-email', methods=['POST'])
def test_email():
    data = request.json or {}
    
    # Obtém configurações salvas do banco de dados como base
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT key, value FROM settings')
    db_cfg = {row['key']: row['value'] for row in c.fetchall()}
    conn.close()

    # Mescla configurações enviadas no teste com as do banco (sem alterar o banco!)
    cfg = dict(db_cfg)
    for k in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_from', 'smtp_to', 'smtp_tls']:
        if k in data and data[k] is not None:
            cfg[k] = str(data[k]).strip()

    # Trata senha: se enviada e não for máscara '********', usa a enviada; senão mantém a do banco
    if 'smtp_password' in data and data['smtp_password'] and data['smtp_password'] != '********':
        cfg['smtp_password'] = str(data['smtp_password'])

    # Destinatário do teste: específico ou lista configurada
    test_target = (data.get('test_recipient') or '').strip()
    if test_target:
        recipients = [test_target]
    else:
        smtp_to = cfg.get('smtp_to', '').strip()
        recipients = [r.strip() for r in smtp_to.split(',') if r.strip()]

    if not recipients:
        return jsonify({'error': 'Nenhum destinatário informado. Digite um e-mail para receber o teste ou preencha a lista de destinatários.'}), 400

    smtp_host = cfg.get('smtp_host', 'Não informado')
    smtp_port = cfg.get('smtp_port', '587')
    smtp_user = cfg.get('smtp_user') or 'Sem autenticação'
    use_tls_text = "Ativado (STARTTLS)" if str(cfg.get('smtp_tls', '1')) == '1' else "Desativado"
    now_str = datetime.now().strftime('%d/%m/%Y às %H:%M:%S')

    subject = "🔔 [TESTE DE SUCESSO] Monitor - Videoconferência & Infra"
    body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f19; padding: 28px; color: #f8fafc; border-radius: 12px; max-width: 580px; margin: 0 auto; border: 1px solid #1e293b;">
        <div style="border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 18px;">
            <div style="display: inline-block; background: #2563eb; color: #fff; font-size: 11px; font-weight: 800; padding: 4px 10px; border-radius: 6px; letter-spacing: 0.5px; margin-bottom: 8px;">MONITOR</div>
            <h2 style="color: #60a5fa; margin: 0; font-size: 20px;">✅ Teste de Configuração de E-mail</h2>
        </div>
        
        <p style="font-size: 14px; line-height: 1.5; color: #cbd5e1; margin-bottom: 18px;">
            Excelente notícia! A comunicação com o seu servidor SMTP foi realizada com sucesso e as credenciais foram autenticadas corretamente.
        </p>

        <div style="background: rgba(30, 41, 59, 0.7); border: 1px solid #334155; border-radius: 8px; padding: 14px 18px; margin-bottom: 18px;">
            <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; margin-bottom: 10px; letter-spacing: 0.5px;">Parâmetros Validados no Teste:</div>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                <tr><td style="padding: 5px 0; color: #94a3b8;">Servidor SMTP:</td><td style="padding: 5px 0; color: #f8fafc; font-weight: 600;">{smtp_host}:{smtp_port}</td></tr>
                <tr><td style="padding: 5px 0; color: #94a3b8;">Criptografia:</td><td style="padding: 5px 0; color: #f8fafc; font-weight: 600;">{use_tls_text}</td></tr>
                <tr><td style="padding: 5px 0; color: #94a3b8;">Conta / Usuário:</td><td style="padding: 5px 0; color: #f8fafc; font-weight: 600;">{smtp_user}</td></tr>
                <tr><td style="padding: 5px 0; color: #94a3b8;">Data e Horário:</td><td style="padding: 5px 0; color: #f8fafc; font-weight: 600;">{now_str}</td></tr>
            </table>
        </div>

        <p style="font-size: 13px; line-height: 1.5; color: #94a3b8; margin-bottom: 20px;">
            A partir de agora, ao ativar a opção <b>"Ativar Notificações por E-mail"</b>, os alertas serão disparados automaticamente para sua equipe quando qualquer sala ou equipamento de videoconferência sofrer queda.
        </p>
        
        {get_email_signature_html("Disparo de validação emitido com sucesso pelo Monitor")}
    </div>
    """

    ok, msg = dispatch_smtp(cfg, subject, body, custom_recipients=recipients)
    if ok:
        return jsonify({
            'success': True,
            'message': msg,
            'recipients': recipients,
            'host': smtp_host,
            'port': smtp_port
        })
    return jsonify({'error': msg}), 400

# --- BANDEJA DO SISTEMA (SYSTEM TRAY) ---
def get_tray_image():
    """Carrega o ícone icon.ico ou desenha um ícone vetorial com Pillow."""
    icon_path = os.path.join(getattr(sys, '_MEIPASS', APP_DIR), 'icon.ico')
    if not os.path.exists(icon_path):
        icon_path = os.path.join(APP_DIR, 'icon.ico')

    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path)
        except Exception:
            pass

    # Ícone fallback dinâmico
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(15, 23, 42, 255), outline=(59, 130, 246, 255), width=3)
    draw.rectangle([14, 16, 50, 40], outline=(248, 250, 252, 255), width=2)
    draw.line([32, 40, 32, 47], fill=(248, 250, 252, 255), width=2)
    draw.line([24, 47, 40, 47], fill=(248, 250, 252, 255), width=2)
    draw.ellipse([42, 8, 56, 22], fill=(16, 185, 129, 255), outline=(15, 23, 42, 255), width=2)
    return img

def setup_system_tray():
    """Inicia o ícone na bandeja do Windows (junto ao relógio) com menu interativo."""
    if not HAS_TRAY:
        return

    def on_open(icon=None, item=None):
        webbrowser.open('http://127.0.0.1:5000')

    def on_exit(icon=None, item=None):
        if icon:
            icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("🌐 Abrir Painel Web", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🟢 Monitoramento Ativo (Porta 5000)", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Encerrar Monitor", on_exit)
    )

    image = get_tray_image()
    tray_icon = pystray.Icon("Monitor", image, "Monitor - Videoconferência", menu)

    # Notificação inicial amigável
    def notify_start():
        time.sleep(2.0)
        try:
            tray_icon.notify("O monitor está ativo e rodando na bandeja do relógio.", "Monitor Iniciado")
        except Exception:
            pass

    threading.Thread(target=notify_start, daemon=True).start()
    tray_icon.run()

# --- INICIALIZAÇÃO DA APLICAÇÃO ---
if __name__ == '__main__':
    init_db()

    # Inicia a thread de monitoramento em segundo plano
    monitor_thread = threading.Thread(target=monitoring_loop, daemon=True)
    monitor_thread.start()

    # Inicia a thread de DHCP Watchdog (Auto-Healing em segundo plano)
    dhcp_thread = threading.Thread(target=dhcp_watchdog_loop, daemon=True)
    dhcp_thread.start()

    # Abre o navegador automaticamente após 1.5 segundo
    def open_browser():
        time.sleep(1.5)
        webbrowser.open('http://127.0.0.1:5000')

    threading.Thread(target=open_browser, daemon=True).start()

    print("=" * 60)
    print(" [*] MONITOR - VIDEOCONFERENCIA & EQUIPAMENTOS INICIADO")
    print(" Painel Web: http://127.0.0.1:5000")
    print("=" * 60)

    if HAS_TRAY:
        # Inicia o servidor Flask com suporte multithread
        flask_thread = threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True),
            daemon=True
        )
        flask_thread.start()
        # Inicia a bandeja do sistema
        try:
            setup_system_tray()
        except Exception as e:
            pass
        # Mantém a thread principal e o servidor Flask sempre ativos
        while True:
            time.sleep(1)
    else:
        # Modo console tradicional caso pystray não esteja presente
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
