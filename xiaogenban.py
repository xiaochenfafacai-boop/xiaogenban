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

TOKEN = "8961723870:AAFuh1A5IesPrk8U8xLsl68O5teE-Hr5-sg"
WEB_URL = "https://xiaogenban-888gh.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

# 创始超级管理员（分销控制端ID）
FOUNDER_USERS = [8179896441]

# 销售收款与三档阶梯价格配置
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

# ==================== 数据库安全连接引擎 (核心加固) ====================
def get_db_connection():
    """
    高并发企业级连接器：
    1. 增加 60 秒无响应排队等待，彻底杜绝 Database Locked 锁表异常
    2. 开启 WAL (Write-Ahead Logging) 模式，允许多人同时记账、同时高频刷新网页不卡顿
    """
    conn = sqlite3.connect('bot_data.db', timeout=60.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # 群组设置表
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, operators TEXT DEFAULT '[]', exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 0, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1)''')
    # 账单明细表
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    # 购买了特权的包月买家表
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    # 动态绑定的多群代理二级主人表
    c.execute('''CREATE TABLE IF NOT EXISTS dynamic_masters
                 (user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)''')
    # 偷偷自动抓取的全群 UID 映射表
    c.execute('''CREATE TABLE IF NOT EXISTS user_caches
                 (username_lower TEXT PRIMARY KEY, user_id INTEGER, display_name TEXT)''')
    conn.commit()
    conn.close()

# ==================== 成员 UID 自动拦截与反查缓存 ====================
def save_user_cache(user_id, username, first_name):
    """只要有人冒泡，立刻强行记忆其UID，确保 @ 授权随时可用"""
    if not username:
        return
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
        logging.error(f"UID安全抓取异常: {e}")

def get_user_id_by_username(username_str):
    """通过缓存表反查用户UID"""
    if not username_str:
        return None, None
    username_lower = username_str.replace('@', '').strip().lower()
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, display_name FROM user_caches WHERE username_lower = ?", (username_lower,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        logging.error(f"UID反查异常: {e}")
    return None, None

# ==================== 核心时间计算时区 ====================
def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

# ==================== 商业权限判定系统 ====================
def get_all_masters():
    """获取包含创办人与授权新主人的总大老板名单"""
    masters = list(FOUNDER_USERS)
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM dynamic_masters")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            if row[0] not in masters: 
                masters.append(row[0])
    except: pass
    return masters

def is_master(user_id):
    return user_id in get_all_masters()

def get_dynamic_masters_count():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM dynamic_masters")
        count = c.fetchone()[0]
        conn.close()
        return count
    except: return 0

def is_vip_user(user_id):
    """判定买家特权是否在有效期内"""
    if is_master(user_id): return True
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
    """鉴权：只有老板、包月买家、以及被授权的群操作人允许触发记账逻辑"""
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

# ==================== 账目数据安全读写 ====================
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

# ==================== Web 网页端与安全数据前端 API ====================
@flask_app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>课时历史账单系统</title><style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f0f2f5;padding:20px;}.container{max-width:1400px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);overflow:hidden;}.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:24px 30px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;}.header-text{flex:1;}.header h1{font-size:28px;margin-bottom:8px;}.date-picker-box{background:rgba(255,255,255,0.2);padding:10px 15px;border-radius:8px;color:white;}.date-picker-box label{font-size:14px;margin-right:8px;font-weight:bold;}.date-picker-box input{border:none;padding:6px 10px;border-radius:4px;font-size:14px;outline:none;}.content{padding:24px 30px;}.section{margin-bottom:32px;}.section-title{font-size:18px;font-weight:600;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #667eea;}table{width:100%;border-collapse:collapse;font-size:14px;}th,td{padding:12px 10px;text-align:left;border-bottom:1px solid #eef2f6;}th{background:#f8f9fc;font-weight:600;}.stats-box{background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);border-radius:12px;padding:24px;margin-top:20px;}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;}.stat-card{background:white;padding:16px;border-radius:12px;text-align:center;}.stat-label{font-size:12px;color:#888;margin-bottom:8px;}.stat-value{font-size:24px;font-weight:700;color:#333;}.stat-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef2f6;}.stat-name{font-weight:500;color:#333;}.stat-number{color:#667eea;font-weight:600;}.loading{text-align:center;padding:50px;color:#888;}</style></head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-text">
                    <h1>📋 实时课堂账单历史明细</h1>
                    <p id="dateInfo">数据已开启毫秒级并发防锁死保护</p>
                </div>
                <div class="date-picker-box">
                    <label>📅 选择账单日期:</label>
                    <input type="date" id="targetDate" onchange="onDateChange()">
                </div>
            </div>
            <div class="content" id="content"><div class="loading">正在同步实时账单...</div></div>
        </div>
        <script>
            let GROUP_ID = null; let currentSelectedDate = "";
            const today = new Date(); const yyyy = today.getFullYear(); let mm = today.getMonth() + 1; let dd = today.getDate();
            if (mm < 10) mm = '0' + mm; if (dd < 10) dd = '0' + dd;
            currentSelectedDate = `${yyyy}-${mm}-${dd}`; document.getElementById('targetDate').value = currentSelectedDate;

            function getGroupID() { 
                const urlParams = new URLSearchParams(window.location.search); GROUP_ID = urlParams.get('group_id'); 
                if (!GROUP_ID) { document.getElementById('content').innerHTML = '<div class="loading">❌ 请通过机器人的 "查看完整账单" 按钮访问</div>'; return false; } 
                return true; 
            }
            function onDateChange() { currentSelectedDate = document.getElementById('targetDate').value; loadData(); }

            async function loadData() { 
                if (!GROUP_ID) return;
                try { 
                    const response = await fetch(`/api/bill?group_id=${GROUP_ID}&date=${currentSelectedDate}`); 
                    const data = await response.json(); 
                    if (data.error || (!data.income_bills.length && !data.expense_bills.length)) { 
                        document.getElementById('content').innerHTML = `<div class="loading">📅 ${currentSelectedDate} 暂无账单数据记录</div>`; return; 
                    }
                    let suffix = data.show_usdt ? ' USDT' : ''; let html = '';
                    if (data.income_bills && data.income_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📥 入款记录 (${data.income_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>金额(元)</th><th>汇率</th><th>等值数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.income_bills) { html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.amount}</td><td>${bill.exchange_rate}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; } 
                        html += `</tbody></table></div>`; 
                    }
                    if (data.expense_bills && data.expense_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📤 下发记录 (${data.expense_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>下发数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.expense_bills) { html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; } 
                        html += `</tbody></table></div>`; 
                    }
                    if (data.remark_stats && data.remark_stats.length > 0) { 
                        html += `<div class="section"><div class="section-title">📊 备注分类统计</div>`; 
                        for (const stat of data.remark_stats) { html += `<div class="stat-item"><span class="stat-name">📝 ${stat.remark}</span><span class="stat-number">${stat.count}笔 | ${stat.amount}元 | ${stat.usdt}${suffix}</span></div>`; } 
                        html += `</div>`; 
                    }
                    html += `<div class="stats-box"><div class="stats-grid"><div class="stat-card"><div class="stat-label">💰 费率</div><div class="stat-value">${data.fee_rate}%</div></div><div class="stat-card"><div class="stat-label">💱 汇率</div><div class="stat-value">${data.exchange_rate}</div></div><div class="stat-card"><div class="stat-label">📥 总入款(元)</div><div class="stat-value">${data.total_rmb}</div></div><div class="stat-card"><div class="stat-label">💵 总入款数量</div><div class="stat-value">${data.total_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📤 已下发</div><div class="stat-value">${data.expense_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📊 未下发</div><div class="stat-value">${data.remaining_usdt}${suffix}</div></div></div></div>`;
                    document.getElementById('content').innerHTML = html;
                } catch (err) { document.getElementById('content').innerHTML = '<div class="loading">❌ 数据解析错误或网络异常</div>'; }
            }
            // 💡 网页轮询间隔提速至 2.5 秒，保证账目一出，网页立刻刷出
            if (getGroupID()) { loadData(); setInterval(() => { const t = new Date(); let m = t.getMonth() + 1; let d = t.getDate(); if (m < 10) m = '0' + m; if (d < 10) d = '0' + d; if (currentSelectedDate === `${t.getFullYear()}-${m}-${d}`) { loadData(); } }, 2500); }
        </script>
    </body>
    </html>
    '''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id = request.args.get('group_id', type=int, default=0)
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
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            income_bills.append({'remark': remark or '-', 'username': username or '未知', 'amount': f"{amount or 0:.0f}", 'usdt': f"{usdt or 0:.2f}", 'exchange_rate': f"{ex_rate or rate:.2f}", 'time': time_str})
        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            expense_bills.append({'remark': remark or '-', 'username': username or '未知', 'usdt': f"{usdt or 0:.2f}", 'time': time_str})

        remark_stats = []
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT remark, COUNT(*), SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' GROUP BY remark ORDER BY SUM(usdt_amount) DESC", (group_id, target_date))
        for row in c.fetchall():
            remark_stats.append({'remark': row[0] if row[0] else '无备注', 'count': row[1] or 0, 'amount': f"{row[2] or 0:.0f}", 'usdt': f"{row[3] or 0:.2f}"})
        conn.close()
        
        return jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'show_usdt': int(show_usdt), 
            'income_bills': income_bills, 'expense_bills': expense_bills, 'remark_stats': remark_stats
        })
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

# ==================== 智能面板多语言生成包 ====================
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

# ==================== 私聊销售控制台键盘 ====================
def get_private_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 充值续费套餐", callback_data="menu_renew"),
         InlineKeyboardButton("📅 检查到期时间", callback_data="menu_expire")],
        [InlineKeyboardButton("👑 添加新机器人主人", callback_data="menu_add_master"),
         InlineKeyboardButton("📖 机器人使用指南", callback_data="menu_help")],
        [InlineKeyboardButton("🌐 访问账单网页端", url=WEB_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_renew_text():
    return f"""
💰 <b>【智能记账系统 - 三档特惠续费套餐】</b>
---
📊 <b>当前最新特惠价格：</b>
🔴 <b>1 个月 (30天)：</b> <code>{PRICE_1_MONTH} USDT</code>
🟡 <b>2 个月 (60天)：</b> <code>{PRICE_2_MONTH} USDT</code>
🟢 <b>3 个月 (90天)：</b> <code>{PRICE_3_MONTH} USDT</code>

🌟 <b>特权包干：</b> 购买后，您名下在<b>【无数个群组】</b>拉入此机器人均可自动解锁，不受限制！

📌 <b>自主转账与截图核对流程：</b>
1️⃣ 请向下方 <b>TRX/波场</b> 官方收币地址转账对应套餐金额：
👉 <code>{TRON_ADDRESS}</code> <i>(点击可自动复制)</i>

2️⃣ 转账成功后，<b>请直接将您的【转账成功截图】发送到当前私聊对话框中！</b>
3️⃣ 机器人会自动将截图提交给创始主人审核，审核通过后秒开特权！
"""

# ==================== 核心网关分流交互处理器 ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        welcome_text = (
            f"👋 您好，<b>{update.effective_user.first_name}</b>！欢迎使用智能记账多群分销版后台管理大厅。\n\n"
            f"💡 请使用下方的高级控制面板管理您的记账特权、绑定新主人或查看账单："
        )
        await update.message.reply_text(welcome_text, reply_markup=get_private_main_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text("📊 记账机器人已在群组就绪！包月买家请输入 <code>上课</code> 开启记账。私聊我可进入充值大厅。", parse_mode="HTML")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = update.effective_chat.id
    uid = query.from_user.id
    await query.answer()

    if update.effective_chat.type != "private":
        if not can_use(gid, uid):
            await query.answer("❌ 您没有权限点击此机器人的操作按钮", show_alert=True)
            return

    # 快捷发图自动续费判定
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
                if current_expire > datetime.now():
                    new_expire = current_expire + timedelta(days=days_to_add)
                else:
                    new_expire = datetime.now() + timedelta(days=days_to_add)
            except:
                new_expire = datetime.now() + timedelta(days=days_to_add)
        else:
            new_expire = datetime.now() + timedelta(days=days_to_add)
            
        expire_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)", (target_uid, "包月买家", expire_str))
        conn.commit()
        conn.close()
        
        await query.message.edit_caption(f"✅ <b>图片审核成功！</b>\n已为买家 (ID: <code>{target_uid}</code>) 快捷开通 <b>{months} 个月 ({days_to_add}天)</b> VIP特权。\n新到期时间：<code>{expire_str}</code>", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=target_uid, text=f"🎉 <b>您的转账截图已审核通过！</b>\n已为您成功解锁/续费 <b>{months} 个月</b> 独立多群无限记账 VIP 权限！")
        except: pass
        return

    elif query.data.startswith("img_reject_"):
        target_uid = int(query.data.split("_")[2])
        await query.message.edit_caption(f"❌ <b>已拒绝此截图凭证。</b>\n已通知该买家截图未通过核对。", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=target_uid, text="⚠️ <b>您的转账截图对账未通过审核。</b>\n请确保发送的是本次交易成功的真实截图，或联系创始人进行人工复核。")
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
        await query.message.edit_text(f"✅ <b>已确认到账！</b>\n已成功为买家 (ID: <code>{target_uid}</code>) 激活多群独立记账VIP资格（30天）。", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=target_uid, text="🎉 <b>您的付款已核对成功！</b>\n系统已为您全面解锁多群无限制建群、无限记账 VIP 权限！")
        except: pass
        return

    elif query.data.startswith("admin_reject_"):
        target_uid = int(query.data.split("_")[2])
        await query.message.edit_text(f"❌ <b>已拒绝开通。</b>\n已通知买家 (ID: <code>{target_uid}</code>) 账单未核对成功。", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=target_uid, text="⚠️ <b>通知：您的付款对账未通过审核。</b>\n请检查对账信息是否正确。")
        except: pass
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
        if uid in FOUNDER_USERS: status_text = "📅 <b>特权状态：</b> ⚖️ 创始人账户（永久有效）"
        elif row: status_text = f"📅 <b>特权到期时间：</b> <code>{row[0]}</code>\n💡 有效期内可在任意群内正常上课记账。"
        else: status_text = "⚠️ <b>特权状态：</b> 您当前尚未开通多群包月VIP资格，请点击充值续费。"
        await query.message.reply_text(status_text, parse_mode="HTML")
    elif query.data == "menu_add_master":
        if not (is_master(uid) or is_vip_user(uid)):
            await query.message.reply_text("❌ 抱歉，您当前还没有购买本机器人，无权添加新的机器人主人。")
            return
        if get_dynamic_masters_count() >= 3:
            await query.message.reply_text("⚠️ <b>系统提示：授权失败</b>\n本机器人当前最多只能添加 3 个二级机器人主人账号。", parse_mode="HTML")
            return
        context.user_data['waiting_for_master_id'] = True
        guide_text = "📝 <b>请输入您想添加的【新机器人主人】的 UID（纯数字）：</b>"
        await query.message.reply_text(guide_text, parse_mode="HTML")
    elif query.data == "menu_help":
        await query.message.reply_text(get_help_text('chinese'), parse_mode="Markdown")

# ==================== 图片/截图拦截与自动处理 ====================
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    uid = update.effective_user.id
    username = update.effective_user.first_name or "未知"
    
    if update.effective_user:
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)
        
    if chat_type != "private": return

    photo_file_id = update.message.photo[-1].file_id
    masters = get_all_masters()
    
    approve_keyboard = [
        [InlineKeyboardButton("✅ 确认1个月(80)", callback_data=f"img_approve_{uid}_1"), InlineKeyboardButton("✅ 确认2个月(130)", callback_data=f"img_approve_{uid}_2")],
        [InlineKeyboardButton("✅ 确认3个月(220)", callback_data=f"img_approve_{uid}_3"), InlineKeyboardButton("❌ 拒绝凭证", callback_data=f"img_reject_{uid}")]
    ]
    notification_caption = f"📸 <b>【收到买家转账成功图】</b>\n\n👤 <b>买家用户：</b> {username}\n🆔 <b>买家 UID：</b> <code>{uid}</code>"
    for m_id in masters:
        try: await context.bot.send_photo(chat_id=m_id, photo=photo_file_id, caption=notification_caption, reply_markup=InlineKeyboardMarkup(approve_keyboard), parse_mode="HTML")
        except: pass
    await update.message.reply_text("📥 <b>收到您的转账成功截图凭证！</b>\n系统已将其提交给创始主人进行多重风控核对，请耐心等待！", parse_mode="HTML")

# ==================== 核心消息拦截与记账运算处理器 ====================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name or "未知"

    if update.effective_user:
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)

    # 1. 私聊管理后台逻辑
    if chat_type == "private":
        if context.user_data.get('waiting_for_master_id'):
            context.user_data['waiting_for_master_id'] = False
            if get_dynamic_masters_count() >= 3:
                await update.message.reply_text("⚠️ 主人坑位已满（上限 3 个）。", parse_mode="HTML")
                return
            clean_uid = "".join(filter(str.isdigit, text))
            if clean_uid and len(clean_uid) >= 5:
                target_master_id = int(clean_uid)
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO dynamic_masters (user_id, username, added_by) VALUES (?, ?, ?)", (target_master_id, "新绑定的主人", uid))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"🎉 <b>授权成功！</b>\n已将 <code>{target_master_id}</code> 设置为机器人新主人！", parse_mode="HTML")
            else: await update.message.reply_text("❌ UID 格式不正确。")
            return
        if len(text) >= 20:
            masters = get_all_masters()
            admin_keyboard = [[InlineKeyboardButton("✅ 确认到账", callback_data=f"admin_approve_{uid}"), InlineKeyboardButton("❌ 拒绝", callback_data=f"admin_reject_{uid}")]]
            notification = f"🔔 <b>买家付款文本对账通知</b>\n\n👤 买家: {username} (ID: <code>{uid}</code>)\n📝 凭证:\n<code>{text}</code>"
            for m_id in masters:
                try: await context.bot.send_message(chat_id=m_id, text=notification, reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode="HTML")
                except: pass
            await update.message.reply_text("✅ 文本信息已递交审核！")
            return
        await update.message.reply_text("💡 请使用控制面板管理您的特权：", reply_markup=get_private_main_keyboard(), parse_mode="HTML")
        return

    # 2. 群组指令：上课
    if text in ['上课', 'အတန်းစ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        msg = "🟢 记账模式已开启！请发送数据记账。"
        if get_setting(gid, 'language') == 'myanmar': msg = "🟢 စာရင်ကိုင်မုဒ်ကို ဖွင့်လိုက်ပါပြီ。"
        await update.message.reply_text(msg)
        return

    # 3. 群组指令：下课
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

    # 4. 群组指令：查看操作员列表
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
            except: message += f"  • 记账员\n"
        await update.message.reply_text(message)
        return

    # 5. 群组指令：设置操作人 (免回复高智商抓取)
    if text.startswith('设置操作人') or text.startswith('အော်ပရေတာခန့်ရန်'):
        if not (is_master(uid) or is_vip_user(uid)):
            await update.message.reply_text("❌ 只有已激活VIP的群主有权限设定本群操作人。")
            return
        
        target_id = None
        user_show = None
        
        # 优先从消息中的艾特提取
        user_match = re.search(r'@(\w+)', text)
        if user_match:
            raw_username = user_match.group(1)
            target_id, cached_name = get_user_id_by_username(raw_username)
            user_show = cached_name if cached_name else f"@{raw_username}"

        # 其次从回复消息中提取
        if not target_id and update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
            target_user = update.message.reply_to_message.from_user
            user_show = f"@{target_user.username}" if target_user.username else target_user.first_name
            save_user_cache(target_id, target_user.username, target_user.first_name)

        # 最后尝试匹配纯数字UID
        if not target_id:
            num_match = re.search(r'\d{6,}', text)
            if num_match:
                target_id = int(num_match.group(0))
                try:
                    member = await context.bot.get_chat_member(gid, target_id)
                    user_show = f"@{member.user.username}" if member.user.username else member.user.first_name
                except: user_show = "指定操作员"

        if target_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if target_id not in ops:
                ops.append(target_id)
                update_setting(gid, 'operators', json.dumps(ops))
            await update.message.reply_text(f"成功设置 {user_show} 成为记账员")
        else:
            await update.message.reply_text("⚠️ 该用户还未在群里发过言，机器人暂时没能抓到他的UID。请让他发个言，或者直接回复他任意一条消息发送 `设置操作人` 即可成功绑定！")
        return

    # 6. 群组指令：语言切换
    if text in ['改语言', 'ဘာသာစကား']:
        if not can_use(gid, uid): return
        current = get_setting(gid, 'language') or 'chinese'
        new_lang = 'myanmar' if current == 'chinese' else 'chinese'
        update_setting(gid, 'language', new_lang)
        msg = "✅ 已切换为中文" if new_lang == 'chinese' else "✅ မြန်မာဘာသာသို့ ပြောင်းလဲပြီးပါပြီ"
        await update.message.reply_text(msg)
        return

    # 7. 群组指令：删除命令系列
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

    # 8. 群组指令：汇率及时区微调
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

    # ==================== 流水账目实时计算核心引擎 ====================
    if (get_setting(gid, 'is_active') or 0) == 0 or not can_use(gid, uid): return

    if text == '+0':
        await show_full_bill(update, gid)
        return

    # 下发记账 (支持: 备注下发50 / ထုတ်50)
    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        rem = m_exp.group(1).strip()
        amt = float(m_exp.group(2))
        add_bill(gid, uid, username, rem, amt, 'expense')
        await show_full_bill(update, gid)
        return

    # 入款/扣减记账 (支持: 备注+1000 / 备注-500 / 备注+2000/7.3)
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

# ==================== 异步服务启动器 ====================
def main():
    init_db()
    # 启动 Flask 网页监听线程
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    
    # 启动 Telegram 机器人主线程
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    print("🤖 交付级商业分销版智能记账系统已全面加固启动...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
