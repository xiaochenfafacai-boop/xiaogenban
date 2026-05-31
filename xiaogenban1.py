import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import re
import threading
from flask import Flask, request, jsonify
import os

# ==================== 系统基础基础配置 ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = "8617895746:AAEwaL8az2XWxS_tY9X14qs2_yrCWRc6Xrs"
WEB_URL = "https://xiaogenban-666ktt.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

# 创始人最高权力UID
FOUNDER_USERS = [8179896441]
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"

# 商业套餐定价
PRICE_1_MONTH = 80
PRICE_2_MONTH = 130
PRICE_3_MONTH = 220

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
    # 偷偷缓存群内成员 @用户名 与 UID 的映射表
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
    except: pass

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
    except: pass
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

# ==================== 权限分级系统 ====================
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
    except: pass

# ==================== 账目数据内核 ====================
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

# ==================== 网页端明细对账看板（像素级还原图三样式） ====================
@flask_app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>实时课堂账单历史明细</title>
        <style>
            *{margin:0;padding:0;box-sizing:border-box;}
            body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#f3f4f9;padding:12px;color:#333;}
            .container{max-width:800px;margin:0 auto;background:#fff;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);overflow:hidden;}
            
            /* 还原顶栏紫色高级渐变 */
            .header-banner {
                background: linear-gradient(135deg, #5b62e7 0%, #8561ea 100%);
                color: #fff;
                padding: 22px 18px;
                position: relative;
            }
            .header-banner h1 { font-size: 19px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
            .header-banner p { font-size: 12px; opacity: 0.85; margin-top: 5px; }
            
            /* 日期选择小挂件 */
            .date-badge {
                position: absolute; right: 15px; bottom: 18px;
                background: rgba(255,255,255,0.23); padding: 5px 10px; border-radius: 8px;
                display: flex; align-items: center; gap: 5px; font-size: 12px; border: 1px solid rgba(255,255,255,0.15);
            }
            .date-badge input { background: transparent; border: none; color: white; outline: none; font-size: 12px; cursor: pointer; font-weight: bold; }
            
            .main-content { padding: 16px; }
            .table-title { font-size: 15px; font-weight: bold; color: #3c42be; margin: 15px 0 10px 0; display: flex; align-items: center; gap: 5px; border-bottom: 1px solid #eef0f6; padding-bottom: 8px; }
            
            /* 数据标准表格布局 */
            .table-wrapper { width: 100%; overflow-x: auto; margin-bottom: 15px; border-radius: 8px; border: 1px solid #edf0f5; }
            table { width: 100%; border-collapse: collapse; background: #fff; min-width: 500px; }
            th, td { padding: 10px 12px; text-align: left; font-size: 13px; border-bottom: 1px solid #edf0f5; }
            th { background: #f8f9fc; color: #6e758b; font-weight: 500; font-size: 12px; }
            td { color: #444; }
            
            /* 还原图三：备注分类统计进度条样式 */
            .cate-box { background: #fff; border: 1px solid #edf0f5; border-radius: 8px; padding: 12px; margin-bottom: 18px; }
            .cate-row { display: flex; justify-content: space-between; align-items: center; font-size: 13px; padding: 6px 0; border-bottom: 1px dashed #f0f2f7; }
            .cate-row:last-child { border-bottom: none; }
            .cate-tag { font-weight: bold; color: #ff9800; display: flex; align-items: center; gap: 4px; }
            .cate-val { font-size: 13px; font-weight: 600; color: #4b52be; }

            /* 还原图三：下方灰白方块卡片矩阵 */
            .grid-container { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 15px; }
            .card { background: #f8f9fc; border-radius: 10px; padding: 12px; text-align: center; border: 1px solid #f0f2f7; }
            .card-label { font-size: 11px; color: #8c93a6; margin-bottom: 5px; display: flex; align-items: center; justify-content: center; gap: 3px; }
            .card-value { font-size: 16px; font-weight: bold; color: #2d3142; }
            
            .no-data { text-align: center; padding: 30px; color: #a0a7b5; font-size: 13px; background: #fafbfe; border-radius: 8px; }
            .loading-shimmer { text-align: center; padding: 50px; color: #623ce4; font-size: 14px; font-weight: 500; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header-banner">
                <h1>📋 实时课堂账单历史明细</h1>
                <p>默认同步实时账单 · 数据每4秒自动更新</p>
                <div class="date-badge">
                    📅 <input type="date" id="targetDate" onchange="dateChanged()">
                </div>
            </div>
            
            <div class="main-content" id="viewShell">
                <div class="loading-shimmer">正在安全对账通道拉取实时流数据...</div>
            </div>
        </div>

        <script>
            let gId = "";
            let selectedDay = "";

            function parseQuery() {
                const urlParams = new URLSearchParams(window.location.search);
                gId = urlParams.get('group_id');
                if(!gId) {
                    document.getElementById('viewShell').innerHTML = `<div class="no-data" style="color:red; font-weight:bold;">❌ 握手失败：未携带合法的对账凭证秘钥</div>`;
                    return false;
                }
                const d = new Date();
                let m = d.getMonth() + 1; let day = d.getDate();
                selectedDay = `${d.getFullYear()}-${m<10?'0'+m:m}-${day<10?'0'+day:day}`;
                document.getElementById('targetDate').value = selectedDay;
                return true;
            }

            function dateChanged() {
                selectedDay = document.getElementById('targetDate').value;
                fetchData();
            }

            async function fetchData() {
                if(!gId) return;
                try {
                    const res = await fetch(`/api/bill?group_id=${gId}&date=${selectedDay}&_cache_burst=${new Date().getTime()}`);
                    const data = await res.json();
                    
                    let html = "";
                    
                    // 1. 入款记录表格
                    html += `<div class="table-title">📥 入款记录 (${data.income_bills.length} 笔)</div>`;
                    if(data.income_bills.length > 0) {
                        html += `<div class="table-wrapper"><table>
                            <thead><tr><th>备注</th><th>时间</th><th>金额(元)</th><th>汇率</th><th>等值数量</th><th>操作人</th></tr></thead><tbody>`;
                        data.income_bills.forEach(b => {
                            html += `<tr><td><b>${b.remark}</b></td><td>${b.time}</td><td>${b.amount}</td><td>${b.exchange_rate}</td><td style="color:#2ecc71;font-weight:bold;">${b.usdt} USDT</td><td>${b.username}</td></tr>`;
                        });
                        html += `</tbody></table></div>`;
                    } else {
                        html += `<div class="no-data">本日暂无任何入款账单流转</div>`;
                    }

                    // 2. 还原图三核心：备注分类统计
                    if(data.summary_by_remark && Object.keys(data.summary_by_remark).length > 0) {
                        html += `<div class="table-title">📊 备注分类统计</div><div class="cate-box">`;
                        for(const [rem, val] of Object.entries(data.summary_by_remark)) {
                            html += `<div class="cate-row">
                                <span class="cate-tag">📝 ${rem}</span>
                                <span class="cate-val">${val.count}笔 | ${val.rmb}元 | ${val.usdt} USDT</span>
                            </div>`;
                        }
                        html += `</div>`;
                    }

                    // 3. 下发记录表格
                    html += `<div class="table-title">📤 下发记录明细 (${data.expense_bills.length} 笔)</div>`;
                    if(data.expense_bills.length > 0) {
                        html += `<div class="table-wrapper"><table>
                            <thead><tr><th>备注</th><th>时间</th><th>下发数量(USDT)</th><th>操作人</th></tr></thead><tbody>`;
                        data.expense_bills.forEach(b => {
                            html += `<tr><td><b>${b.remark}</b></td><td>${b.time}</td><td style="color:#e74c3c;font-weight:bold;">${b.usdt} USDT</td><td>${b.username}</td></tr>`;
                        });
                        html += `</tbody></table></div>`;
                    } else {
                        html += `<div class="no-data">本日暂无任何下发数据流转</div>`;
                    }

                    // 4. 还原图三底部小方块多网格卡片矩阵
                    html += `
                    <div class="grid-container">
                        <div class="card"><div class="card-label">💰 费率</div><div class="card-value">${data.fee_rate}%</div></div>
                        <div class="card"><div class="card-label">💱 汇率</div><div class="card-value" style="color:#4b52be;">${data.exchange_rate}</div></div>
                        <div class="card"><div class="card-label">👤 总入款(元)</div><div class="card-value">${data.total_rmb}</div></div>
                        <div class="card"><div class="card-label">💵 总入款数量</div><div class="card-value" style="color:#2ecc71;">${data.total_usdt} USDT</div></div>
                        <div class="card"><div class="card-label">📤 已下发</div><div class="card-value" style="color:#e74c3c;">${data.expense_usdt} USDT</div></div>
                        <div class="card"><div class="card-label">🏛️ 未下发</div><div class="card-value" style="color:#f39c12;">${data.remaining_usdt} USDT</div></div>
                    </div>`;
                    
                    document.getElementById('viewShell').innerHTML = html;
                } catch(e) {
                    document.getElementById('viewShell').innerHTML = `<div class="no-data" style="color:red;">❌ 数据中继网关拥堵，正在自动重连...</div>`;
                }
            }

            if(parseQuery()) {
                fetchData();
                setInterval(() => {
                    const today = new Date();
                    let m = today.getMonth() + 1; let day = today.getDate();
                    let checkStr = `${today.getFullYear()}-${m<10?'0'+m:m}-${day<10?'0'+day:day}`;
                    if(selectedDay === checkStr) fetchData();
                }, 4000);
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
            if group_id_str.startswith('-'):
                group_id = -int(''.join(filter(str.isdigit, group_id_str)))
            else:
                group_id = int(''.join(filter(str.isdigit, group_id_str)))
        except: group_id = 0

        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        target_date = request.args.get('date', default=now.strftime("%Y-%m-%d"))

        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0

        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0

        income_bills = []
        expense_bills = []
        summary_by_remark = {}

        for row in income:
            remark, username, amount, usdt, ex_rate, ts = row
            rem_key = remark if remark else "无备注"
            
            # 分类合并统计逻辑 (对应图三)
            if rem_key not in summary_by_remark:
                summary_by_remark[rem_key] = {'count': 0, 'rmb': 0, 'usdt': 0}
            summary_by_remark[rem_key]['count'] += 1
            summary_by_remark[rem_key]['rmb'] += amount or 0
            summary_by_remark[rem_key]['usdt'] += usdt or 0

            income_bills.append({
                'remark': remark or '-', 'username': username or '未知', 
                'amount': f"{amount or 0:.0f}", 'usdt': f"{usdt or 0:.2f}", 
                'exchange_rate': f"{ex_rate or rate:.2f}", 'time': ts[11:19] if ts else ''
            })

        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            expense_bills.append({
                'remark': remark or '-', 'username': username or '未知', 
                'usdt': f"{usdt or 0:.2f}", 'time': ts[11:19] if ts else ''
            })

        # 格式化分类统计中的浮点数
        for k in summary_by_remark:
            summary_by_remark[k]['rmb'] = f"{summary_by_remark[k]['rmb']:.0f}"
            summary_by_remark[k]['usdt'] = f"{summary_by_remark[k]['usdt']:.2f}"

        res = jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 
            'income_bills': income_bills, 'expense_bills': expense_bills,
            'summary_by_remark': summary_by_remark
        })
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return res
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

# ==================== 私聊常驻大键盘（100%还原图二） ====================
def get_private_reply_keyboard():
    keyboard = [
        [KeyboardButton("试用"), KeyboardButton("开始")],
        [KeyboardButton("到期时间"), KeyboardButton("详细说明书")],
        [KeyboardButton("自助续费"), KeyboardButton("如何设置权限人")],
        [KeyboardButton("如何设置群内操作人"), KeyboardButton("开启/关闭计算功能")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="请选择下方业务菜单面板")

# ==================== 商业化业务层处理器 ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type == "private":
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)
        welcome = (
            "<b>我是记账机器人</b>\n\n"
            "点击这里把机器人加进群➕\n\n"
            "感谢您把我添加到贵群！下一步设置费率，请发：<code>设置费率 0%</code>"
        )
        await update.message.reply_text(welcome, reply_markup=get_private_reply_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text("📊 智能多群记账核算核心已部署完毕！输入 <code>上课</code> 启动录入。")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    # 创始人特权充值审单流
    if query.data.startswith("v_approve_"):
        parts = query.data.split("_")
        t_uid = int(parts[2])
        m_count = int(parts[3])
        days = m_count * 30
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (t_uid,))
        row = c.fetchone()
        if row:
            try:
                curr = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                base = curr if curr > datetime.now() else datetime.now()
            except: base = datetime.now()
        else: base = datetime.now()
        
        new_expire = base + timedelta(days=days)
        exp_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, '商用买家', ?)", (t_uid, exp_str))
        conn.commit()
        conn.close()
        
        await query.message.edit_caption(f"✅ 审核成功！买家资格已延期至：\n<code>{exp_str}</code>", parse_mode="HTML")
        try: await context.bot.send_message(chat_id=t_uid, text=f"🎉 恭喜！您的自助充值申请已审核通过！\n多群独立主控到期时间更新为：{exp_str}")
        except: pass
    
    elif query.data.startswith("v_reject_"):
        t_uid = int(query.data.split("_")[2])
        await query.message.edit_caption("❌ 账目不符，已驳回此转账截图。")
        try: await context.bot.send_message(chat_id=t_uid, text="⚠️ 您的自助续费凭证未通过审核，请检查真实账目后再次提交。")
        except: pass

# ==================== 文字指令网关核心处理 ====================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name or "记账员"

    if update.effective_user:
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)

    # 1. 拦截私聊文字菜单请求 (对应图二的8个大常驻按钮点击)
    if chat_type == "private":
        if text == "试用":
            await update.message.reply_text("🆓 您当前已开启免费测试资格！可以直接将机器人邀请入群测试录入。")
        elif text == "开始":
            await update.message.reply_text("🚀 系统处于最佳就绪状态。请将本机器人授权加群并设为管理员。")
        elif text == "到期时间":
            if uid in FOUNDER_USERS:
                await update.message.reply_text("👑 ⚖️ <b>创始人至尊永久账户（免续费）</b>", parse_mode="HTML")
                return
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (uid,))
            row = c.fetchone()
            conn.close()
            if row: await update.message.reply_text(f"📅 您的商用买家VIP多群授权截止时间为：\n<code>{row[0]}</code>", parse_mode="HTML")
            else: await update.message.reply_text("⚠️ <b>您目前无任何有效商用授权。请选择 [自助续费] 订购。</b>", parse_mode="HTML")
        elif text == "详细说明书":
            guide = "📚 <b>记账基础指令速查表：</b>\n\n`+1000` - 极速入款\n`备注+5000` - 带备注录入\n`下发500` - 快速下发USDT\n`+0` - 查看今日即时账单"
            await update.message.reply_text(guide, parse_mode="Markdown")
        elif text == "自助续费":
            renew_msg = (
                f"💰 <b>【多群记账系统自动套餐购买中心】</b>\n\n"
                f"🔴 <b>1 个月商用续费：</b> <code>{PRICE_1_MONTH} USDT</code>\n"
                f"🟡 <b>2 个月商用续费：</b> <code>{PRICE_2_MONTH} USDT</code>\n"
                f"🟢 <b>3 个月商用续费：</b> <code>{PRICE_3_MONTH} USDT</code>\n\n"
                f"📌 <b>官方专属收币 TRC-20 地址：</b>\n👉 <code>{TRON_ADDRESS}</code>\n\n"
                f"💡 <i>转账完成后请【直接在这里发送支付成功截图】，系统会自动转交创始人进行秒级审批。</i>"
            )
            await update.message.reply_text(renew_msg, parse_mode="HTML")
        elif text == "如何设置权限人":
            await update.message.reply_text("👑 <b>添加/管理二级主人权限：</b>\n\n回复文字发送：`指派二级主人 12345678` (纯数字UID)\n\n*(每个买家最多支持添加 5 个协助二级主人)*")
        elif text == "如何设置群内操作人":
            await update.message.reply_text("⚙️ <b>设置群内操作记账员命令：</b>\n\n在群内直接发：\n👉 <code>设置操作人 @用户名</code>\n👉 <code>删除操作人 @用户名</code>", parse_mode="HTML")
        elif text == "开启/关闭计算功能":
            await update.message.reply_text("💡 群内发送 <code>上课</code> 开启记账计算，发送 <code>下课</code> 锁定清算本日账目并扎帐。")
        
        # 二级主人绑定文字命令拦截
        elif text.startswith("指派二级主人"):
            if not (uid in FOUNDER_USERS or is_vip_user(uid)): return
            if len(get_dynamic_masters_by_creator(uid)) >= 5 and uid not in FOUNDER_USERS:
                await update.message.reply_text("⚠️ <b>添加失败：您的二级主人添加名额已经达到5人天花板限制！</b>")
                return
            clean_uid = "".join(filter(str.isdigit, text))
            if len(clean_uid) >= 5:
                t_mid = int(clean_uid)
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO dynamic_masters (user_id, username, added_by) VALUES (?, '授权二级主人', ?)", (t_mid, uid))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"✅ <b>指派成功！二级主人 (UID: {t_mid}) 已获得分销系统协同管理特权。</b>", parse_mode="HTML")
            else:
                await update.message.reply_text("❌ 格式不正确。示例：`指派二级主人 8179896441`")
        return

    # 2. 群组内记账常规管理命令
    if text == '上课':
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        await update.message.reply_text("🟢 <b>记账安全通道已开启！请开始录入账单。</b>", parse_mode="HTML")
        return

    if text == '下课':
        if not can_use(gid, uid): return
        if (get_setting(gid, 'is_active') or 0) == 0: return
        update_setting(gid, 'is_active', 0)
        keyboard = [[InlineKeyboardButton("📊 查看完整账单明细 (Web)", url=f"{WEB_URL}?group_id={gid}")]]
        await update.message.reply_text("🔴 <b>下课成功！今日账单已自动封存锁定归档。</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 精准模糊结合配置群组操作人
    if text.startswith('设置操作人'):
        if not (is_master(uid) or is_vip_user(uid)): return
        t_id, show_name = None, None
        match = re.search(r'@(\w+)', text)
        if match:
            t_id, show_name = get_user_id_by_username(match.group(1))
        if not t_id and update.message.reply_to_message:
            t_id = update.message.reply_to_message.from_user.id
            u_obj = update.message.reply_to_message.from_user
            show_name = f"@{u_obj.username}" if u_obj.username else u_obj.first_name
        
        if t_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if t_id not in ops: ops.append(t_id)
            update_setting(gid, 'operators', json.dumps(ops))
            await update.message.reply_text(f"✅ <b>已成功将群成员 {show_name or t_id} 提拔为本群官方操作人。</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ <b>未捕获到该用户的UID。请确保该成员曾在此群里发过言。</b>")
        return

    if text.startswith('删除操作人'):
        if not (is_master(uid) or is_vip_user(uid)): return
        t_id, show_name = None, None
        match = re.search(r'@(\w+)', text)
        if match:
            t_id, _ = get_user_id_by_username(match.group(1))
            show_name = match.group(0)
        if t_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if t_id in ops: ops.remove(t_id)
            update_setting(gid, 'operators', json.dumps(ops))
            await update.message.reply_text(f"❌ <b>已成功撤销 {show_name} 的群组官方记账操作员权限。</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ <b>删除失败，无法在本地指引中反查到该用户名。</b>")
        return

    # 记账数据解析挂钩
    if (get_setting(gid, 'is_active') or 0) == 0 or not can_use(gid, uid): return

    if text == '+0':
        keyboard = [[InlineKeyboardButton("📊 实时对账多维看板 (Web)", url=f"{WEB_URL}?group_id={gid}")]]
        await update.message.reply_text("📋 <b>点击下方查看实时对账：</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 解析下发
    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        add_bill(gid, uid, username, m_exp.group(1).strip(), float(m_exp.group(2)), 'expense')
        await update.message.reply_text(f"✅ 下发成功！点击原链接或输入 <code>+0</code> 刷新看板。", parse_mode="HTML")
        return

    # 解析入款
    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        sign = m_inc.group(2)
        amt = float(m_inc.group(3))
        if sign == '-': amt = -amt
        c_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        add_bill(gid, uid, username, rem, amt, 'income', c_rate)
        await update.message.reply_text(f"📥 入款录入成功！点击原链接或输入 <code>+0</code> 刷新看板。", parse_mode="HTML")
        return

# ==================== 买家上交截图审核网关 ====================
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    uid = update.effective_user.id
    photo_id = update.message.photo[-1].file_id
    
    app_k = [
        [InlineKeyboardButton("✅ 批准 1个月", callback_data=f"v_approve_{uid}_1"), InlineKeyboardButton("✅ 批准 2个月", callback_data=f"v_approve_{uid}_2")],
        [InlineKeyboardButton("✅ 批准 3个月", callback_data=f"v_approve_{uid}_3"), InlineKeyboardButton("❌ 驳回", callback_data=f"v_reject_{uid}")]
    ]
    # 推送至所有创始人手机端
    for f_id in FOUNDER_USERS:
        try:
            await context.bot.send_photo(
                chat_id=f_id, photo=photo_id, 
                caption=f"📸 <b>报告老板，有买家提交转账截图啦！</b>\n\n买家UID: <code>{uid}</code>\n买家用户名: @{update.effective_user.username or '无'}", 
                reply_markup=InlineKeyboardMarkup(app_k), parse_mode="HTML"
            )
        except: pass
    await update.message.reply_text("📥 <b>您的入账转账截图已经秒级提交至后台审核系统，请等待开通提示！</b>", parse_mode="HTML")

def main():
    init_db()
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    print("🤖 100%样式还原多维商用版本已正常发动...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
