import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import re
import threading
from flask import Flask, request, jsonify
import os

# ==================== 日志与基础配置 ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = "8961723870:AAHK1RoOHnhS9wVWmZ4DMYctZ0OlwtzWpKY"
WEB_URL = "https://xiaogenban-666kty.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

FOUNDER_USERS = [8179896441]
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
PRICE_1_MONTH = 80
PRICE_2_MONTH = 130
PRICE_3_MONTH = 220

TIMEZONES = {
    'china': 'Asia/Shanghai',
    'myanmar': 'Asia/Yangon',
    'thailand': 'Asia/Bangkok',
}

flask_app = Flask(__name__)

# ==================== 数据库引擎 ====================
def get_db_connection():
    conn = sqlite3.connect('bot_data.db', timeout=60.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, operators TEXT DEFAULT '[]', exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 0, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS dynamic_masters
                 (user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_caches
                 (username_lower TEXT PRIMARY KEY, user_id INTEGER, display_name TEXT)''')
    conn.commit()
    conn.close()

def save_user_cache(user_id, username, first_name):
    if not username: return
    username_lower = username.lower()
    display_name = f"@{username}"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_caches (username_lower, user_id, display_name) VALUES (?, ?, ?)",
                  (username_lower, user_id, display_name))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"UID抓取异常: {e}")

def get_user_id_by_username(username_str):
    if not username_str: return None, None
    username_lower = username_str.replace('@', '').strip().lower()
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, display_name FROM user_caches WHERE username_lower = ?", (username_lower,))
        row = c.fetchone()
        conn.close()
        if row: return row[0], row[1]
    except Exception as e:
        logging.error(f"UID反查异常: {e}")
    return None, None

def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

# ==================== 权限与配置系统 ====================
def get_all_masters():
    masters = list(FOUNDER_USERS)
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM dynamic_masters")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            if row[0] not in masters: masters.append(row[0])
    except: pass
    return masters

def is_master(user_id):
    return user_id in get_all_masters()

def get_dynamic_masters_by_creator(creator_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if creator_id in FOUNDER_USERS:
            c.execute("SELECT user_id, username FROM dynamic_masters")
        else:
            c.execute("SELECT user_id, username FROM dynamic_masters WHERE added_by = ?", (creator_id,))
        rows = c.fetchall()
        conn.close()
        return rows
    except: return []

def get_dynamic_masters_count_by_creator(creator_id):
    return len(get_dynamic_masters_by_creator(creator_id))

def is_vip_user(user_id):
    if user_id in FOUNDER_USERS: return True
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return datetime.now() < expire
    except: pass
    return False

def can_use(group_id, user_id):
    if is_master(user_id) or is_vip_user(user_id): return True
    try:
        ops = json.loads(get_setting(group_id, 'operators') or '[]')
        return user_id in ops
    except: return False

def get_setting(group_id, key):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        conn.close()
        if not row: return None
        cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt']
        return dict(zip(cols, row)).get(key)
    except: return None

def update_setting(group_id, key, value):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        if c.fetchone():
            c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        else:
            c.execute("INSERT INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1))
            c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"配置更新失败: {e}")

# ==================== 账目核心读写 ====================
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount

    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO bills 
                 (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str, is_settled)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
              (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str))
    conn.commit()
    conn.close()
    return usdt_amount

def get_class_bills_by_date(group_id, target_date):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id DESC", (group_id, target_date))
    income = c.fetchall()
    c.execute("SELECT remark, username, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id DESC", (group_id, target_date))
    expense = c.fetchall()
    c.execute("SELECT SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income'", (group_id, target_date))
    total_income = c.fetchone()
    c.execute("SELECT SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'", (group_id, target_date))
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense

def settle_today_bills(group_id, target_date):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE bills SET is_settled = 1 WHERE group_id = ? AND date_str = ?", (group_id, target_date))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated

def delete_today_bills(group_id):
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (group_id, today_date))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_last_bill(group_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,))
    last = c.fetchone()
    if last:
        c.execute("DELETE FROM bills WHERE id = ?", (last[0],))
        deleted = 1
    else: deleted = 0
    conn.commit()
    conn.close()
    return deleted

def delete_all_bills(group_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ?", (group_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_user_bills(group_id, name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND (LOWER(username) = ? OR LOWER(remark) = ?)", (group_id, name.lower(), name.lower()))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# ==================== 强效去缓存 Web 前端 ====================
@flask_app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>课时历史账单系统</title>
        <style>
            *{margin:0;padding:0;box-sizing:border-box;}
            body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f0f2f5;padding:15px;}
            .container{max-width:1400px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.08);overflow:hidden;}
            .header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;}
            .header h1{font-size:22px;margin-bottom:4px;}
            .header p{font-size:12px;opacity:0.9;}
            .date-picker-box{background:rgba(255,255,255,0.2);padding:8px 12px;border-radius:8px;color:white;display:flex;align-items:center;gap:8px;}
            .date-picker-box label{font-size:14px;font-weight:bold;}
            .date-picker-box input{border:none;padding:6px 10px;border-radius:6px;font-size:14px;outline:none;}
            .content{padding:20px;}
            .section{margin-bottom:25px;}
            .section-title{font-size:16px;font-weight:600;margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid #667eea;color:#333;}
            .table-responsive{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;}
            table{width:100%;border-collapse:collapse;font-size:14px;white-space:nowrap;}
            th,td{padding:12px 10px;text-align:left;border-bottom:1px solid #eef2f6;}
            th{background:#f8f9fc;font-weight:600;color:#666;}
            .stats-box{background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);border-radius:12px;padding:20px;margin-top:20px;}
            .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;}
            .stat-card{background:white;padding:14px;border-radius:10px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.02);}
            .stat-label{font-size:12px;color:#888;margin-bottom:6px;}
            .stat-value{font-size:20px;font-weight:700;color:#333;}
            .stat-item{display:flex;justify-content:space-between;padding:10px;background:#f8f9fc;border-radius:6px;margin-bottom:6px;font-size:14px;}
            .stat-name{font-weight:500;color:#333;}
            .stat-number{color:#667eea;font-weight:600;}
            .loading{text-align:center;padding:50px 20px;color:#764ba2;font-size:16px;font-weight:500;}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>
                    <h1 id="titleText">📋 实时课堂账单系统</h1>
                    <p id="debugInfo">数据通道已开启智能并发保护</p>
                </div>
                <div class="date-picker-box">
                    <label>📅 选择日期:</label>
                    <input type="date" id="targetDate" onchange="onDateChange()">
                </div>
            </div>
            <div class="content" id="content">
                <div class="loading">正在建立高并发数据对账通道...</div>
            </div>
        </div>

        <script>
            let currentGroupId = "";
            let currentSelectedDate = "";

            function initParam() {
                const match = window.location.href.match(/[?&]group_id=([^&#]*)/);
                if (match && match[1]) {
                    currentGroupId = decodeURIComponent(match[1]).trim();
                    document.getElementById('debugInfo').innerText = "当前关联群组ID: " + currentGroupId;
                } else {
                    document.getElementById('content').innerHTML = '<div class="loading" style="color:red;">❌ 错误：未检测到合法的群组凭证，请通过群内【查看完整账单】按钮重新进入。</div>';
                    return false;
                }

                const today = new Date();
                const yyyy = today.getFullYear();
                let mm = today.getMonth() + 1;
                let dd = today.getDate();
                if (mm < 10) mm = '0' + mm;
                if (dd < 10) dd = '0' + dd;
                currentSelectedDate = `${yyyy}-${mm}-${dd}`;
                document.getElementById('targetDate').value = currentSelectedDate;
                return true;
            }

            function onDateChange() {
                currentSelectedDate = document.getElementById('targetDate').value;
                loadData();
            }

            async function loadData() {
                if (!currentGroupId) return;
                try {
                    const timestamp = new Date().getTime();
                    const targetUrl = `/api/bill?group_id=${encodeURIComponent(currentGroupId)}&date=${currentSelectedDate}&_t=${timestamp}`;

                    const response = await fetch(targetUrl);
                    const data = await response.json();

                    if (data.error) {
                        document.getElementById('content').innerHTML = `<div class="loading" style="color:red;">❌ 系统加载异常: ${data.msg}</div>`;
                        return;
                    }

                    let suffix = data.show_usdt ? ' U' : '';
                    let html = '';

                    // 1. 入款表格部分
                    html += `<div class="section"><div class="section-title">📥 入款明细记录 (${data.income_bills ? data.income_bills.length : 0} 笔)</div><div class="table-responsive">`;
                    if (data.income_bills && data.income_bills.length > 0) {
                        html += `<table><thead><tr><th>备注参数</th><th>实时时间</th><th>金额(元)</th><th>即时汇率</th><th>等值数量</th><th>经办记账员</th></tr></thead><tbody>`;
                        for (const bill of data.income_bills) {
                            html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.amount}</td><td>${bill.exchange_rate}</td><td style="color:green;font-weight:bold;">${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`;
                        }
                        html += `</tbody></table>`;
                    } else {
                        html += `<div style="padding:20px;text-align:center;color:#999;font-size:14px;">本日暂无任何入款账单流转</div>`;
                    }
                    html += `</div></div>`;

                    // 2. 下发表格部分
                    html += `<div class="section"><div class="section-title">📤 下发记录明细 (${data.expense_bills ? data.expense_bills.length : 0} 笔)</div><div class="table-responsive">`;
                    if (data.expense_bills && data.expense_bills.length > 0) {
                        html += `<table><thead><tr><th>下发备注</th><th>实时时间</th><th>下发数量</th><th>经办记账员</th></tr></thead><tbody>`;
                        for (const bill of data.expense_bills) {
                            html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td style="color:red;font-weight:bold;">${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`;
                        }
                        html += `</tbody></table>`;
                    } else {
                        html += `<div style="padding:20px;text-align:center;color:#999;font-size:14px;">本日暂无任何下发数据流转</div>`;
                    }
                    html += `</div></div>`;

                    // 3. 分类备注统计部分
                    if (data.remark_stats && data.remark_stats.length > 0) {
                        html += `<div class="section"><div class="section-title">📊 备注分类汇总统计</div>`;
                        for (const stat of data.remark_stats) {
                            html += `<div class="stat-item"><span class="stat-name">📝 ${stat.remark}</span><span class="stat-number">${stat.count}笔 | ${stat.amount}元 | ${stat.usdt}${suffix}</span></div>`;
                        }
                        html += `</div>`;
                    }

                    // 4. 全局总控网格面板
                    html += `
                    <div class="stats-box">
                        <div class="stats-grid">
                            <div class="stat-card"><div class="stat-label">💰 设定手续费率</div><div class="stat-value" style="color:#764ba2;">${data.fee_rate}%</div></div>
                            <div class="stat-card"><div class="stat-label">💱 当前设定汇率</div><div class="stat-value" style="color:#667eea;">${data.exchange_rate}</div></div>
                            <div class="stat-card"><div class="stat-label">📥 总入款金额</div><div class="stat-value" style="color:#333;">${data.total_rmb} 元</div></div>
                            <div class="stat-card"><div class="stat-label">💵 入款总折合</div><div class="stat-value" style="color:green;">${data.total_usdt}${suffix}</div></div>
                            <div class="stat-card"><div class="stat-label">📤 累计已下发</div><div class="stat-value" style="color:red;">${data.expense_usdt}${suffix}</div></div>
                            <div class="stat-card"><div class="stat-label">📊 结余未下发</div><div class="stat-value" style="color:#ff9800;">${data.remaining_usdt}${suffix}</div></div>
                        </div>
                    </div>`;

                    document.getElementById('content').innerHTML = html;
                } catch (err) {
                    document.getElementById('content').innerHTML = '<div class="loading" style="color:red;">❌ 核心对账通道解析故障，请刷新页面重试。</div>';
                }
            }

            if (initParam()) {
                loadData();
                setInterval(() => {
                    const t = new Date();
                    let m = t.getMonth() + 1;
                    let d = t.getDate();
                    if (m < 10) m = '0' + m;
                    if (d < 10) d = '0' + d;
                    if (currentSelectedDate === `${t.getFullYear()}-${m}-${d}`) {
                        loadData();
                    }
                }, 3000);
            }
        </script>
    </body>
    </html>
    '''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id_str = request.args.get('group_id', default='0').strip()
        try:
            group_id = int(group_id_str)
        except:
            group_id = 0

        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_str = now.strftime("%Y-%m-%d")
        target_date = request.args.get('date', default=today_str)

        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0
        show_usdt = get_setting(group_id, 'show_usdt') or 1

        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0

        income_bills = []
        expense_bills = []
        for row in income:
            remark, username, amount, usdt, ex_rate, ts = row
            time_str = ts[11:16] if (ts and len(ts) > 16) else (ts or '-')
            income_bills.append({'remark': remark or '-', 'username': username or '未知', 'amount': f"{amount or 0:.0f}", 'usdt': f"{usdt or 0:.2f}", 'exchange_rate': f"{ex_rate or rate:.2f}", 'time': time_str})
        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            time_str = ts[11:16] if (ts and len(ts) > 16) else (ts or '-')
            expense_bills.append({'remark': remark or '-', 'username': username or '未知', 'usdt': f"{usdt or 0:.2f}", 'time': time_str})

        remark_stats = []
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT remark, COUNT(*), SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' GROUP BY remark ORDER BY SUM(usdt_amount) DESC", (group_id, target_date))
        for row in c.fetchall():
            remark_stats.append({'remark': row[0] if row[0] else '无备注', 'count': row[1] or 0, 'amount': f"{row[2] or 0:.0f}", 'usdt': f"{row[3] or 0:.2f}"})
        conn.close()

        res = jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'show_usdt': int(show_usdt), 
            'income_bills': income_bills, 'expense_bills': expense_bills, 'remark_stats': remark_stats
        })
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        res.headers["Pragma"] = "no-cache"
        res.headers["Expires"] = "0"
        return res
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

# ==================== 多语言面板处理 ====================
def get_help_text(lang):
    if lang == 'myanmar':
        return """
🤖 *စာရင်းကိုင်ဘော့ အကူအညီ* (Help)
📌 *စာရင်းသွင်းရန် ပုံစံများ：*
`+1000` - Ngwe Win 1000 Kyat
`-1000` - Ngwe Win -1000 Kyat
`MatChet+2000` - 带备注入款
`MatChet-2000` - 带备注减款
`Thut50` / `下发50` - 50 USDT Thut Ranyan
`MatChetThut50` - 带备注下发
`+0` - YaNay SaYinChoke KyiRanyan

📌 *စီမံခန့်ခွဲရေး ကွတ်ကီးများ：*
`အတန်းစ` / `上课` - SaYinKoing Sinit PhwintChin
`အတန်းဆင်း` / `下课` - SaYinPate Pyee ShinLinChin
`ငွေလဲနှုန်း 7.2` / `设置汇率 7.2` - ThatMatRanyan
`အော်ပရေတာခန့်ရန် @username` / `设置操作人 @username` - KhantRanyan 
`အော်ပရေတာစာရင်း` / `查看操作员列表` - KyiRanyan
`ဘာသာစကား` / `改语言` - PyaungRanyan (中文/မြန်မာ)
`အချိန်သတ်မှတ်` / `设置时间` - AChainZone PyaungRanyan
"""
    else:
        return """
🤖 *记账机器人使用指南*
📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元 (扣减款)
`备注+2000` - 带备注入款
`备注-2000` - 带备注减款
`下发50` / `ထုတ်50` - 下发50 USDT
`备注下发50` - 带备注下发50 USDT
`+0` - 查看今日汇总

📌 *管理命令：*
`上课` - 开启记账模式
`下课` - 关闭记账模式并归档
`设置汇率 7.2` - 设置当前常规汇率
`设置操作人 @username` - 授权群成员协助记账
`查看操作员列表` - 查看本群操作人
`改语言` - 切换群内系统语言（中文/缅甸语）
`设置时间 china/myanmar` - 调整本群结算时区

📌 *删除命令：*
`删今天` - 清空今日账单 | `删最后` - 撤销最后一笔
`全部清单` - 清空历史 | `清单+备注` - 删除指定备注账单
"""

def get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang):
    unit = "U"
    if lang == 'myanmar':
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 ငွေဝင်", "📤 ထုတ်ငွေ", "💰 လဲနှုန်း", "📊 စုစုပေါင်း", "📊 ထုတ်ပြီး", "📊 ကျန်ငွေ"
    else:
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 入款", "📤 下发", "💰 汇率", "📊 总入款", "📊 已下发", "📊 未下发"

    message = f"📊 账单汇总 ({today_date})\n\n"
    if income:
        message += f"{income_title}:\n"
        for bill in income[:5]:
            remark, username, amount, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{amount or 0:.0f}/{ex_rate or rate:.1f}={usdt or 0:.1f}{unit}\n"
        message += "\n"
    if expense:
        message += f"{expense_title}:\n"
        for bill in expense[:5]:
            remark, username, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{usdt or 0:.1f}{unit}\n"
        message += "\n"

    message += f"{rate_text}: {rate:.2f}\n"
    message += f"{total_text}: {total_rmb:.0f} | {total_usdt:.1f}{unit}\n"
    message += f"{exp_text}: {expense_usdt:.1f}{unit}\n"
    message += f"{rem_text}: {total_usdt - expense_usdt:.1f}{unit}"
    return message

async def show_full_bill(update: Update, gid):
    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")

    income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
    rate = get_setting(gid, 'exchange_rate') or 7.2
    lang = get_setting(gid, 'language') or 'chinese'
    total_rmb = total_income[0] or 0
    total_usdt = total_income[1] or 0
    expense_usdt = total_expense[0] or 0

    message = get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang)
    keyboard = [
        [InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
        [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
    ]
    if update.message:
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

def get_private_main_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("💰 充值续费套餐", callback_data="menu_renew"),
         InlineKeyboardButton("📅 检查到期时间", callback_data="menu_expire")]
    ]
    if is_vip_user(user_id) or user_id in FOUNDER_USERS:
        keyboard.append([
            InlineKeyboardButton("👑 添加机器人主", callback_data="menu_add_master"),
            InlineKeyboardButton("❌ 取掉机器人主", callback_data="menu_del_master_panel")
        ])
    keyboard.append([
        InlineKeyboardButton("📖 使用指南", callback_data="menu_help"),
        InlineKeyboardButton("🌐 账单网页端", url=WEB_URL)
    ])
    return InlineKeyboardMarkup(keyboard)

def get_renew_text():
    return f"""
💰 <b>【智能记账系统 - 续费套餐】</b>
---
🔴 <b>1 个月：</b> <code>{PRICE_1_MONTH} USDT</code>
🟡 <b>2 个月：</b> <code>{PRICE_2_MONTH} USDT</code>
🟢 <b>3 个月：</b> <code>{PRICE_3_MONTH} USDT</code>

📌 <b>收款地址 (TRC-20)：</b>
👉 <code>{TRON_ADDRESS}</code> <i>(点击自动复制)</i>
转账成功后请发<b>转账截图</b>到本私聊！
"""

# ==================== 核心网关交互 ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type == "private":
        welcome_text = f"👋 您好！智能记账多群分销版后台管理大厅。\n💡 请使用下方面板管理："
        await update.message.reply_text(welcome_text, reply_markup=get_private_main_keyboard(uid), parse_mode="HTML")
    else:
        await update.message.reply_text("📊 记账机器人已就绪！请输入 <code>上课</code> 开启记账。", parse_mode="HTML")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = update.effective_chat.id
    uid = query.from_user.id
    await query.answer()

    if update.effective_chat.type != "private":
        if not can_use(gid, uid): return

    if query.data.startswith("img_approve_"):
        parts = query.data.split("_")
        target_uid = int(parts[2])
        months = int(parts[3])
        days_to_add = months * 30
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (target_uid,))
        row = c.fetchone()
        if row:
            try:
                current_expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                new_expire = (current_expire if current_expire > datetime.now() else datetime.now()) + timedelta(days=days_to_add)
            except: new_expire = datetime.now() + timedelta(days=days_to_add)
        else: new_expire = datetime.now() + timedelta(days=days_to_add)
        expire_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)", (target_uid, "包月买家", expire_str))
        conn.commit()
        conn.close()
        await query.message.edit_caption(f"✅ 审核成功！买家到期时间已更新：<code>{expire_str}</code>", parse_mode="HTML")
        try: await context.bot.send_message(chat_id=target_uid, text=f"🎉 您的转账截图已通过核对！已为您成功解锁多群 VIP 权限，到期时间：{expire_str}")
        except: pass
        return

    elif query.data.startswith("img_reject_"):
        target_uid = int(query.data.split("_")[2])
        await query.message.edit_caption(f"❌ 已拒绝此截图凭证。", parse_mode="HTML")
        try: await context.bot.send_message(chat_id=target_uid, text="⚠️ 您的转账截图对账未通过审核，请检查后重新发送。")
        except: pass
        return

    if query.data.startswith("admin_approve_"):
        target_uid = int(query.data.split("_")[2])
        conn = get_db_connection()
        c = conn.cursor()
        expire_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)", (target_uid, "包月买家", expire_date))
        conn.commit()
        conn.close()
        await query.message.edit_text(f"✅ 已确认到账！已成功激活VIP资格（30天）。", parse_mode="HTML")
        try: await context.bot.send_message(chat_id=target_uid, text="🎉 您的付款已核对成功！多群独立记账已解锁。")
        except: pass
        return

    elif query.data.startswith("admin_reject_"):
        target_uid = int(query.data.split("_")[2])
        await query.message.edit_text(f"❌ 已拒绝开通。", parse_mode="HTML")
        return

    if query.data == 'show_help':
        lang = get_setting(gid, 'language') or 'chinese'
        keyboard = [[InlineKeyboardButton("🔙 返回记账 (Back)", callback_data='back_to_main')]]
        await query.edit_message_text(get_help_text(lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    elif query.data == 'back_to_main':
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_date = now.strftime("%Y-%m-%d")
        income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
        rate = get_setting(gid, 'exchange_rate') or 7.2
        lang = get_setting(gid, 'language') or 'chinese'
        message = get_bill_content(income, expense, total_income[0] or 0, total_income[1] or 0, total_expense[0] or 0, rate, today_date, lang)
        keyboard = [[InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")], [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data == "menu_renew":
        await query.message.reply_text(get_renew_text(), parse_mode="HTML")
    elif query.data == "menu_expire":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        conn.close()
        if uid in FOUNDER_USERS: status_text = "📅 ⚖ 创始人账户（永久有效）"
        elif row: status_text = f"📅 VIP到期时间：<code>{row[0]}</code>"
        else: status_text = "⚠️ 当前尚未开通独立记账多群版VIP资格。"
        await query.message.reply_text(status_text, parse_mode="HTML")
    elif query.data == "menu_add_master":
        if not (uid in FOUNDER_USERS or is_vip_user(uid)): return
        if get_dynamic_masters_count_by_creator(uid) >= 3 and uid not in FOUNDER_USERS:
            await query.message.reply_text("⚠️ 您的二级主人坑位已满（上限 3 个）。")
            return
        context.user_data['waiting_for_master_id'] = True
        await query.message.reply_text("📝 <b>请输入您想添加的【新机器人主人】的 UID（纯数字）：</b>", parse_mode="HTML")
    elif query.data == "menu_del_master_panel":
        if not (uid in FOUNDER_USERS or is_vip_user(uid)): return
        my_masters = get_dynamic_masters_by_creator(uid)
        if not my_masters:
            await query.message.reply_text("📭 您当前还没有添加过二级主人。")
            return
        del_keyboard = [[InlineKeyboardButton(f"❌ 移除: {m_name} ({m_id})", callback_data=f"execute_del_master_{m_id}")] for m_id, m_name in my_masters]
        del_keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back_to_lobby")])
        await query.message.reply_text("⚙️ 请点击下方要删除的主人名单：", reply_markup=InlineKeyboardMarkup(del_keyboard), parse_mode="HTML")
    elif query.data.startswith("execute_del_master_"):
        target_mid = int(query.data.split("_")[3])
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT added_by FROM dynamic_masters WHERE user_id = ?", (target_mid,))
        row = c.fetchone()
        if row and (uid in FOUNDER_USERS or row[0] == uid):
            c.execute("DELETE FROM dynamic_masters WHERE user_id = ?", (target_mid,))
            conn.commit()
            await query.message.reply_text(f"✅ 移除成功！")
        conn.close()
        return
    elif query.data == "menu_back_to_lobby":
        await query.message.edit_text("💡 请使用控制面板管理特权：", reply_markup=get_private_main_keyboard(uid))
    elif query.data == "menu_help":
        await query.message.reply_text(get_help_text('chinese'), parse_mode="Markdown")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    uid = update.effective_user.id
    save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)
    photo_file_id = update.message.photo[-1].file_id
    masters = get_all_masters()
    approve_keyboard = [
        [InlineKeyboardButton("✅ 确认1个月", callback_data=f"img_approve_{uid}_1"), InlineKeyboardButton("✅ 确认2个月", callback_data=f"img_approve_{uid}_2")],
        [InlineKeyboardButton("✅ 确认3个月", callback_data=f"img_approve_{uid}_3"), InlineKeyboardButton("❌ 拒绝", callback_data=f"img_reject_{uid}")]
    ]
    for m_id in masters:
        try: await context.bot.send_photo(chat_id=m_id, photo=photo_file_id, caption=f"📸 <b>【收到转账截图】</b>\n🆔 买家 UID：<code>{uid}</code>", reply_markup=InlineKeyboardMarkup(approve_keyboard), parse_mode="HTML")
        except: pass
    await update.message.reply_text("📥 <b>转账凭证已递交审核！</b>", parse_mode="HTML")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name or "未知"

    if update.effective_user:
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)

    if chat_type == "private":
        if context.user_data.get('waiting_for_master_id'):
            context.user_data['waiting_for_master_id'] = False
            clean_uid = "".join(filter(str.isdigit, text))
            if clean_uid and len(clean_uid) >= 5:
                target_master_id = int(clean_uid)
                try:
                    chat_inf = await context.bot.get_chat(target_master_id)
                    target_name = chat_inf.first_name or "二级机器人主"
                except: target_name = "二级机器人主"
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO dynamic_masters (user_id, username, added_by) VALUES (?, ?, ?)", (target_master_id, target_name, uid))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"🎉 授权成功！已经成功绑定 {target_name}", reply_markup=get_private_main_keyboard(uid), parse_mode="HTML")
            else: await update.message.reply_text("❌ UID 格式错误。", reply_markup=get_private_main_keyboard(uid))
            return
        if len(text) >= 20:
            masters = get_all_masters()
            admin_keyboard = [[InlineKeyboardButton("✅ 确认到账", callback_data=f"admin_approve_{uid}"), InlineKeyboardButton("❌ 拒绝", callback_data=f"admin_reject_{uid}")]]
            for m_id in masters:
                try: await context.bot.send_message(chat_id=m_id, text=f"🔔 付款通知\n🆔 ID: <code>{uid}</code>\n📝 凭证:\n<code>{text}</code>", reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode="HTML")
                except: pass
            await update.message.reply_text("✅ 凭证递交审核成功！")
            return
        return

    if text in ['上课', 'အတန်းစ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        msg = "🟢 记账模式已开启！请发送数据记账。"
        if get_setting(gid, 'language') == 'myanmar': msg = "🟢 စာရင်ကိုင်မုဒ်ကို ဖွင့်လိုက်ပါပြီ。"
        await update.message.reply_text(msg)
        return

    if text in ['下课', 'အတန်းဆင်း']:
        if not can_use(gid, uid): return
        if (get_setting(gid, 'is_active') or 0) == 0: return
        await show_full_bill(update, gid)
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        settle_today_bills(gid, now.strftime("%Y-%m-%d"))
        update_setting(gid, 'is_active', 0)
        msg = "🔴 下课成功！在线账单已归档锁定。"
        if get_setting(gid, 'language') == 'myanmar': msg = "🔴 အတန်းဆင်းခြင်း အောင်မြင်ပါသည်။"
        await update.message.reply_text(msg)
        return

    if text in ['查看操作员列表', 'အော်ပရေတာစာရင်း']:
        if not can_use(gid, uid): return
        ops = json.loads(get_setting(gid, 'operators') or '[]')
        if not ops:
            await update.message.reply_text("📋 暂无操作人")
            return
        message = "📋 操作人列表:\n"
        for oid in ops:
            try:
                member = await context.bot.get_chat_member(gid, oid)
                user_show = f"@{member.user.username}" if member.user.username else member.user.first_name
                message += f"  • {user_show}\n"
            except: message += f"  • 记账员 (ID: {oid})\n"
        await update.message.reply_text(message)
        return

    if text.startswith('设置操作人') or text.startswith('အော်ပရေတာခန့်ရန်'):
        if not (is_master(uid) or is_vip_user(uid)): return
        target_id = None
        user_show = None
        user_match = re.search(r'@(\w+)', text)
        if user_match:
            raw_username = user_match.group(1)
            target_id, cached_name = get_user_id_by_username(raw_username)
            user_show = cached_name if cached_name else f"@{raw_username}"
        if not target_id and update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
            target_user = update.message.reply_to_message.from_user
            user_show = f"@{target_user.username}" if target_user.username else target_user.first_name
            save_user_cache(target_id, target_user.username, target_user.first_name)
        if not target_id:
            num_match = re.search(r'\d{6,}', text)
            if num_match:
                target_id = int(num_match.group(0))
                try:
                    member = await context.bot.get_chat_member(gid, target_id)
                    user_show = f"@{member.user.username}" if member.user.username else member.user.first_name
                except: user_show = f"用户({target_id})"
        if target_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if target_id not in ops:
                ops.append(target_id)
                update_setting(gid, 'operators', json.dumps(ops))
            await update.message.reply_text(f"成功设置 {user_show} 成为记账员")
        else:
            await update.message.reply_text("⚠️ 未抓到用户，请让他发句话或回复他消息发送 `设置操作人`！")
        return

    if text in ['改语言', 'ဘာသာစကား']:
        if not can_use(gid, uid): return
        current = get_setting(gid, 'language') or 'chinese'
        new_lang = 'myanmar' if current == 'chinese' else 'chinese'
        update_setting(gid, 'language', new_lang)
        msg = "✅ 已切换为中文" if new_lang == 'chinese' else "✅ မြန်မာဘာသာသို့ ပြောင်းလဲပြီးပါပြီ"
        await update.message.reply_text(msg)
        return

    if text in ['删今天', 'ယနေ့ဖျက်']:
        if not can_use(gid, uid): return
        delete_today_bills(gid)
        await update.message.reply_text("✅ 已删除今日所有账单")
        return

    if text in ['删最后', 'နောက်ဆုံးဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_last_bill(gid)
        await update.message.reply_text("✅ 已删除最后一笔" if deleted else "📭 暂无账单")
        return

    if text in ['全部清单', 'စာရင်းအားလုံးဖျက်']:
        if not can_use(gid, uid): return
        delete_all_bills(gid)
        await update.message.reply_text("✅ 已清空全量总历史账单")
        return

    m_rate = re.match(r'^(?:设置汇率|ငွေလဲနှုန်း)\s+(\d+(?:\.\d+)?)$', text)
    if m_rate:
        if not can_use(gid, uid): return
        rate = float(m_rate.group(1))
        update_setting(gid, 'exchange_rate', rate)
        await update.message.reply_text(f"✅ 汇率已设为 {rate}")
        return

    m_tz = re.match(r'^(?:设置时间|အချိန်သတ်မှတ်)\s+([a-zA-Z]+)$', text)
    if m_tz:
        if not can_use(gid, uid): return
        tz_name = m_tz.group(1).lower()
        if tz_name in TIMEZONES:
            update_setting(gid, 'timezone', TIMEZONES[tz_name])
            await update.message.reply_text("✅ 时区修改成功")
        return

    m_del_user = re.match(r'^(?:清单\+|မှတ်တမ်းဖျက်\+)(.+)$', text)
    if m_del_user:
        if not can_use(gid, uid): return
        target_name = m_del_user.group(1).strip()
        delete_user_bills(gid, target_name)
        await update.message.reply_text(f"✅ 已清空【{target_name}】的账单")
        return

    if (get_setting(gid, 'is_active') or 0) == 0 or not can_use(gid, uid): return

    if text == '+0':
        await show_full_bill(update, gid)
        return

    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        rem = m_exp.group(1).strip()
        amt = float(m_exp.group(2))
        add_bill(gid, uid, username, rem, amt, 'expense')
        await show_full_bill(update, gid)
        return

    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        sign = m_inc.group(2)
        amt = float(m_inc.group(3))
        if sign == '-': amt = -amt
        custom_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        ex_rate = custom_rate if custom_rate else (get_setting(gid, 'exchange_rate') or 7.2)
        add_bill(gid, uid, username, rem, amt, 'income', ex_rate)
        await show_full_bill(update, gid)
        return

def main():
    init_db()
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    print("🤖 独立子分销版本：净化版无缓存极速网页账单链路全面激活...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
