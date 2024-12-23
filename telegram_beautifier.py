from flask import Flask, render_template, request, abort
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import requests
import logging
import json
import os

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("telegram_bot.log"),
        logging.StreamHandler()
    ]
)

# 加载配置
with open("config.json", "r") as config_file:
    config = json.load(config_file)

BOT_TOKEN = config.get("bot_token", "")
SECURE_TOKEN = config.get("secure_token", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FORWARD_TO_ID = config.get("forward_to_id", "")
WELCOME_MESSAGE = config.get("welcome_message", "")
COOLDOWN_HOURS = config.get("cooldown_hours", 24)

# 初始化 Flask 应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messages.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 初始化数据库和 SocketIO
db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# 数据库模型定义
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.String(50), nullable=False, index=True)
    sender_name = db.Column(db.String(50), nullable=False)
    text = db.Column(db.String(2000), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    media_type = db.Column(db.String(20))
    media_url = db.Column(db.String(500))

class UserAutoReply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False, unique=True)
    last_auto_reply = db.Column(db.DateTime, nullable=True)

# Jinja2 过滤器
def format_datetime(value):
    shanghai_time = value + timedelta(hours=8)
    return shanghai_time.strftime('%Y-%m-%d %H:%M:%S')

app.jinja_env.filters['dateformat'] = format_datetime

def create_tables():
    with app.app_context():
        db.create_all()

def should_send_auto_reply(user_id):
    """检查是否应该发送自动回复"""
    user_record = UserAutoReply.query.filter_by(user_id=user_id).first()
    
    if not user_record:
        # 用户第一次发消息
        return True
    
    if user_record.last_auto_reply is None:
        return True
    
    # 检查是否超过冷却时间
    cooldown_time = user_record.last_auto_reply + timedelta(hours=COOLDOWN_HOURS)
    return datetime.utcnow() > cooldown_time

def update_auto_reply_record(user_id):
    """更新用户的自动回复记录"""
    user_record = UserAutoReply.query.filter_by(user_id=user_id).first()
    
    if not user_record:
        user_record = UserAutoReply(user_id=user_id)
    
    user_record.last_auto_reply = datetime.utcnow()
    db.session.add(user_record)
    db.session.commit()

def handle_auto_reply(chat_id):
    """处理自动回复"""
    if str(chat_id) != FORWARD_TO_ID and should_send_auto_reply(str(chat_id)):
        try:
            send_message_to_telegram(chat_id, WELCOME_MESSAGE)
            update_auto_reply_record(str(chat_id))
            
            # 保存自动回复消息到数据库
            new_message = Message(
                sender_id=BOT_TOKEN,
                sender_name="Bot",
                text=WELCOME_MESSAGE,
                timestamp=datetime.utcnow()
            )
            db.session.add(new_message)
            db.session.commit()
            
            # 广播自动回复消息到前端
            socketio.emit('new_message', {
                'sender_id': BOT_TOKEN,
                'sender_name': "Bot",
                'text': WELCOME_MESSAGE,
                'timestamp': format_datetime(new_message.timestamp)
            })
            
            logging.info(f"Sent auto-reply to user {chat_id}")
        except Exception as e:
            logging.error(f"Error sending auto-reply: {str(e)}")

@app.route('/')
def home():
    """用户访问主页，需要 secure_token 验证"""
    token = request.args.get('secure_token')
    if token != SECURE_TOKEN:
        abort(403, description="Forbidden: Invalid secure_token.")
    messages = Message.query.order_by(Message.timestamp.desc()).limit(5000).all()
    messages.reverse()
    return render_template('display.html', messages=messages, secure_token=token)

@app.route(f"/{BOT_TOKEN}/webhook", methods=['POST'])
def telegram_webhook():
    """处理 Telegram Webhook 请求"""
    data = request.get_json()
    logging.debug(f"Webhook received data: {data}")

    if 'message' in data:
        message = data['message']
        chat_id = message['chat']['id']
        sender_name = message['chat'].get('first_name', 'Unknown')

        # 处理客服的回复
        if str(chat_id) == FORWARD_TO_ID and 'reply_to_message' in message:
            handle_customer_service_reply(message)
            return {"status": "ok"}

        # 处理普通用户消息
        if message.get('chat', {}).get('type') == 'private':
            if 'text' in message:
                handle_text_message(chat_id, sender_name, message['text'])
                # 处理自动回复
                handle_auto_reply(chat_id)
            elif 'photo' in message:
                photo = message['photo'][-1]
                file_id = photo['file_id']
                caption = message.get('caption', '')
                handle_photo_message(chat_id, sender_name, file_id, caption)
                # 处理自动回复
                handle_auto_reply(chat_id)

    return {"status": "ok"}

@socketio.on('send_message')
def handle_send_message(data):
    """处理 WebSocket 消息"""
    logging.debug(f"Received data for sending message: {data}")
    token = data.get('secure_token')
    if token != SECURE_TOKEN:
        logging.error("Invalid secure token received")
        emit('error', {'error': 'Invalid secure_token'}, broadcast=False)
        return

    chat_id = data.get('sender_id')
    text = data.get('text')

    if not chat_id or not text:
        logging.warning("Invalid data received for sending message")
        emit('error', {'error': 'Missing required fields'}, broadcast=False)
        return

    try:
        # 发送消息到 Telegram
        send_message_to_telegram(chat_id, text)

        # 保存消息到数据库
        new_message = Message(
            sender_id=chat_id,
            sender_name="You",
            text=text,
            timestamp=datetime.utcnow()
        )
        db.session.add(new_message)
        db.session.commit()

        # 广播消息到前端
        emit('new_message', {
            'sender_id': chat_id,
            'sender_name': "You",
            'text': text,
            'timestamp': format_datetime(new_message.timestamp)
        }, broadcast=True)

        logging.info(f"Message sent successfully to user {chat_id}")
    except Exception as e:
        logging.error(f"Error sending message: {str(e)}")
        emit('error', {'error': 'Failed to send message'}, broadcast=False)

def handle_customer_service_reply(message):
    """处理客服的回复"""
    logging.debug(f"Handling customer service reply: {message}")
    reply_to_message = message.get('reply_to_message')
    if not reply_to_message:
        logging.error("No reply_to_message found")
        return

    # 获取原始消息中的用户ID
    original_text = reply_to_message.get('caption', '') or reply_to_message.get('text', '')
    if 'ID:' not in original_text:
        logging.error("No user ID found in original message")
        return

    try:
        target_user_id = original_text.split('ID:')[1].split(')')[0].strip()
        logging.info(f"Identified target user ID: {target_user_id}")

        # 处理图片消息
        if 'photo' in message:
            photo = message['photo'][-1]
            file_id = photo['file_id']
            caption = message.get('caption', '')
            
            # 获取图片URL
            file_info = get_telegram_file_info(file_id)
            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['file_path']}"
            
            # 发送图片到用户
            send_photo_to_telegram(target_user_id, file_id, caption)
            
            # 保存到数据库
            new_message = Message(
                sender_id=FORWARD_TO_ID,
                sender_name="You",
                text=f"[图片] {file_url} {caption}",
                timestamp=datetime.utcnow(),
                media_type='photo',
                media_url=file_url
            )
            db.session.add(new_message)
            db.session.commit()
            
            # 广播到前端
            socketio.emit('new_message', {
                'sender_id': FORWARD_TO_ID,
                'sender_name': "You",
                'text': f"[图片] {file_url} {caption}",
                'timestamp': format_datetime(new_message.timestamp)
            })
        
        # 处理文本消息
        elif 'text' in message:
            text = message['text']
            send_message_to_telegram(target_user_id, text)
            
            new_message = Message(
                sender_id=FORWARD_TO_ID,
                sender_name="You",
                text=text,
                timestamp=datetime.utcnow()
            )
            db.session.add(new_message)
            db.session.commit()
            
            socketio.emit('new_message', {
                'sender_id': FORWARD_TO_ID,
                'sender_name': "You",
                'text': text,
                'timestamp': format_datetime(new_message.timestamp)
            })
        
        logging.info(f"Successfully handled customer service reply to user {target_user_id}")
    except Exception as e:
        logging.error(f"Error handling customer service reply: {str(e)}")

def handle_text_message(chat_id, sender_name, text):
    """处理用户发送的文本消息"""
    new_message = Message(
        sender_id=str(chat_id),
        sender_name=sender_name,
        text=text,
        timestamp=datetime.utcnow()
    )
    db.session.add(new_message)
    db.session.commit()

    forward_text = f"用户 {sender_name} (ID: {chat_id}) 发来消息:\n{text}"
    send_message_to_telegram(FORWARD_TO_ID, forward_text)

    socketio.emit('new_message', {
        'sender_id': chat_id,
        'sender_name': sender_name,
        'text': text,
        'timestamp': format_datetime(new_message.timestamp)
    })

def handle_photo_message(chat_id, sender_name, file_id, caption):
    """处理用户发送的图片消息"""
    try:
        file_info = get_telegram_file_info(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['file_path']}"

        # 转发图片到客服
        forward_caption = f"用户 {sender_name} (ID: {chat_id}) 发来图片: {caption}"
        send_photo_to_telegram(FORWARD_TO_ID, file_id, forward_caption)

        # 保存到数据库
        new_message = Message(
            sender_id=str(chat_id),
            sender_name=sender_name,
            text=f"[图片] {file_url} {caption}",
            timestamp=datetime.utcnow(),
            media_type='photo',
            media_url=file_url
        )
        db.session.add(new_message)
        db.session.commit()

        # 广播到前端
        socketio.emit('new_message', {
            'sender_id': chat_id,
            'sender_name': sender_name,
            'text': f"[图片] {file_url} {caption}",
            'timestamp': format_datetime(new_message.timestamp)
        })

        logging.info(f"Successfully handled photo message from user {chat_id}")
    except Exception as e:
        logging.error(f"Error handling photo message: {str(e)}")

def get_telegram_file_info(file_id):
    """获取 Telegram 文件信息"""
    url = f"{TELEGRAM_API_URL}/getFile"
    payload = {'file_id': file_id}
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()['result']
    except Exception as e:
        logging.error(f"Error getting file info: {str(e)}")
        raise

def send_message_to_telegram(chat_id, text):
    """发送消息到 Telegram"""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error sending message to Telegram: {str(e)}")
        raise

def send_photo_to_telegram(chat_id, file_id, caption):
    """发送图片到 Telegram"""
    url = f"{TELEGRAM_API_URL}/sendPhoto"
    payload = {
        'chat_id': chat_id,
        'photo': file_id,
        'caption': caption,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error sending photo to Telegram: {str(e)}")
        raise

if __name__ == '__main__':
    create_tables()
    import eventlet
    import eventlet.wsgi
    socketio.run(app, host='0.0.0.0', port=15000, debug=True)