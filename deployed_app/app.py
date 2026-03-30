import asyncio
import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def guest_name() -> str:
    return f"Anon-{str(uuid.uuid4())[:4]}"


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return guest_name()
    name = re.sub(r"[^a-zA-Z0-9 _.-]", "", name)[:24].strip()
    return name or guest_name()


def clean_room(room: str) -> str:
    room = (room or "").strip().lower().replace(" ", "-")
    room = re.sub(r"[^a-z0-9_-]", "", room)[:24]
    return room or "world"


def clean_text(text: str, limit: int = 500) -> str:
    return (text or "").strip()[:limit]


@dataclass
class User:
    id: str
    name: str
    room: str
    connected_at: str = field(default_factory=now_iso)
    typing: bool = False


@dataclass
class Message:
    id: str
    user_id: str
    user_name: str
    room: str
    text: str = ""
    image_data: str = ""
    image_name: str = ""
    image_type: str = ""
    timestamp: str = field(default_factory=now_iso)
    kind: str = "message"  # message | photo | system


@dataclass
class ChatRoom:
    name: str
    users: Dict[str, User] = field(default_factory=dict)
    history: List[Message] = field(default_factory=list)

    def add_user(self, user: User) -> None:
        self.users[user.id] = user

    def remove_user(self, user_id: str) -> None:
        self.users.pop(user_id, None)

    def add_message(self, message: Message, max_history: int = 80) -> None:
        self.history.append(message)
        if len(self.history) > max_history:
            self.history = self.history[-max_history:]


class ChatManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, ChatRoom] = {}
        self.connections: Dict[WebSocket, User] = {}
        self.lock = asyncio.Lock()
        for room in ("world", "friends", "travel", "memes"):
            self.rooms[room] = ChatRoom(room)

    def get_or_create_room(self, room_name: str) -> ChatRoom:
        room_name = clean_room(room_name)
        if room_name not in self.rooms:
            self.rooms[room_name] = ChatRoom(room_name)
        return self.rooms[room_name]

    async def send_json(self, websocket: WebSocket, payload: dict) -> None:
        await websocket.send_text(json.dumps(payload))

    async def broadcast_room(self, room_name: str, payload: dict, exclude: Optional[WebSocket] = None) -> None:
        dead: List[WebSocket] = []
        for ws, user in list(self.connections.items()):
            if user.room == room_name and ws is not exclude:
                try:
                    await self.send_json(ws, payload)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.connections.pop(ws, None)

    def snapshot(self, room_name: str, current_user: User) -> dict:
        room = self.get_or_create_room(room_name)
        return {
            "type": "state",
            "me": {"id": current_user.id, "name": current_user.name, "room": current_user.room},
            "rooms": [{"name": r.name, "users": len(r.users)} for r in sorted(self.rooms.values(), key=lambda x: x.name)],
            "users": [{"id": u.id, "name": u.name, "typing": u.typing} for u in room.users.values()],
            "messages": [self._serialize_message(m) for m in room.history],
        }

    def _serialize_message(self, m: Message) -> dict:
        return {
            "id": m.id,
            "user_id": m.user_id,
            "user_name": m.user_name,
            "room": m.room,
            "text": m.text,
            "image_data": m.image_data,
            "image_name": m.image_name,
            "image_type": m.image_type,
            "timestamp": m.timestamp,
            "kind": m.kind,
        }

    async def register(self, websocket: WebSocket, user_name: str, room_name: str) -> User:
        async with self.lock:
            user = User(id=str(uuid.uuid4())[:8], name=clean_name(user_name), room=clean_room(room_name))
            self.get_or_create_room(user.room).add_user(user)
            self.connections[websocket] = user
            return user

    async def unregister(self, websocket: WebSocket) -> Optional[User]:
        async with self.lock:
            user = self.connections.pop(websocket, None)
            if not user:
                return None
            room = self.rooms.get(user.room)
            if room:
                room.remove_user(user.id)
            return user

    async def switch_room(self, websocket: WebSocket, new_room: str) -> Optional[User]:
        async with self.lock:
            user = self.connections.get(websocket)
            if not user:
                return None
            new_room = clean_room(new_room)
            if user.room == new_room:
                return user
            old_room = self.rooms.get(user.room)
            if old_room:
                old_room.remove_user(user.id)
            user.room = new_room
            user.typing = False
            self.get_or_create_room(new_room).add_user(user)
            return user

    async def set_typing(self, websocket: WebSocket, value: bool) -> Optional[User]:
        async with self.lock:
            user = self.connections.get(websocket)
            if not user:
                return None
            user.typing = value
            return user

    async def rename(self, websocket: WebSocket, new_name: str) -> Optional[User]:
        async with self.lock:
            user = self.connections.get(websocket)
            if not user:
                return None
            user.name = clean_name(new_name)
            return user

    async def post_message(self, websocket: WebSocket, text: str) -> Optional[Message]:
        async with self.lock:
            user = self.connections.get(websocket)
            if not user:
                return None
            text = clean_text(text)
            if not text:
                return None
            msg = Message(
                id=str(uuid.uuid4())[:10],
                user_id=user.id,
                user_name=user.name,
                room=user.room,
                text=text,
                kind="message",
            )
            self.get_or_create_room(user.room).add_message(msg)
            return msg

    async def post_photo(self, websocket: WebSocket, data_url: str, file_name: str = "photo") -> Optional[Message]:
        async with self.lock:
            user = self.connections.get(websocket)
            if not user:
                return None
            if not data_url.startswith("data:image/"):
                return None
            # basic size safety: roughly cap to about 2MB of encoded content
            if len(data_url) > 2_800_000:
                return None
            msg = Message(
                id=str(uuid.uuid4())[:10],
                user_id=user.id,
                user_name=user.name,
                room=user.room,
                image_data=data_url,
                image_name=clean_text(file_name, 80),
                image_type="photo",
                kind="photo",
            )
            self.get_or_create_room(user.room).add_message(msg)
            return msg

    async def system_message(self, room_name: str, text: str) -> Message:
        msg = Message(
            id=str(uuid.uuid4())[:10],
            user_id="system",
            user_name="System",
            room=room_name,
            text=clean_text(text, 250),
            kind="system",
        )
        self.get_or_create_room(room_name).add_message(msg)
        return msg


manager = ChatManager()
app = FastAPI(title="Anonymous Photo Chat")

PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Anonymous Photo Chat</title>
  <style>
    :root {
      --bg: #09111f;
      --panel: rgba(14, 21, 39, 0.9);
      --panel2: rgba(21, 30, 53, 0.95);
      --border: rgba(255,255,255,0.08);
      --text: #eef3ff;
      --muted: #8fa1c5;
      --accent: #7b8cff;
      --accent2: #42d7c8;
      --danger: #ff6b7a;
      --bubble-in: rgba(255,255,255,0.06);
      --bubble-out: linear-gradient(135deg, #7b8cff, #42d7c8);
      --radius: 22px;
      --shadow: 0 24px 70px rgba(0,0,0,.38);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(123,140,255,0.18), transparent 28%),
        radial-gradient(circle at bottom right, rgba(66,215,200,0.14), transparent 26%),
        var(--bg);
      overflow: hidden;
    }
    .app {
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 18px;
      height: 100vh;
      padding: 18px;
    }
    .sidebar, .chat {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      overflow: hidden;
    }
    .sidebar { display: flex; flex-direction: column; min-width: 0; }
    .brand {
      padding: 22px;
      border-bottom: 1px solid var(--border);
    }
    .brand h1 { margin: 0; font-size: 1.25rem; }
    .brand p { margin: 8px 0 0; color: var(--muted); line-height: 1.45; font-size: 0.92rem; }
    .section { padding: 18px 22px; border-bottom: 1px solid var(--border); }
    .label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.14em; color: var(--muted); margin-bottom: 10px; }
    .pill-row { display: flex; flex-wrap: wrap; gap: 10px; }
    .pill {
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      transition: .2s ease;
      font-size: 0.92rem;
      user-select: none;
    }
    .pill:hover, .pill.active { background: rgba(123,140,255,0.14); border-color: rgba(123,140,255,0.5); transform: translateY(-1px); }
    .users { display: grid; gap: 10px; max-height: 40vh; overflow: auto; padding-right: 4px; }
    .user-chip {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 12px 14px; border-radius: 14px; background: rgba(255,255,255,0.04); border: 1px solid var(--border);
    }
    .user-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .dot { width: 10px; height: 10px; border-radius: 999px; background: var(--accent2); box-shadow: 0 0 0 5px rgba(66,215,200,.12); flex: 0 0 auto; }
    .muted { color: var(--muted); }
    .small { font-size: 0.88rem; }
    .chat { display: grid; grid-template-rows: auto 1fr auto; min-width: 0; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 20px 22px; border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(255,255,255,0.04), transparent);
    }
    .title h2 { margin: 0; font-size: 1.1rem; }
    .title p { margin: 6px 0 0; color: var(--muted); font-size: 0.92rem; }
    .status {
      display: inline-flex; align-items: center; gap: 8px; padding: 10px 14px;
      border-radius: 999px; background: rgba(255,255,255,0.05); border: 1px solid var(--border);
      white-space: nowrap;
    }
    .messages { padding: 20px; overflow: auto; display: grid; gap: 12px; align-content: start; }
    .message-row { display: flex; gap: 10px; align-items: flex-end; }
    .message-row.me { justify-content: flex-end; }
    .bubble {
      max-width: min(720px, 86%);
      border-radius: 20px;
      padding: 12px 14px;
      border: 1px solid rgba(255,255,255,0.08);
      background: var(--bubble-in);
      overflow: hidden;
      word-break: break-word;
    }
    .me .bubble { background: var(--bubble-out); border-color: transparent; color: #fff; }
    .system { justify-content: center; }
    .system .bubble { background: rgba(255,255,255,0.03); color: var(--muted); font-style: italic; }
    .meta { display: flex; gap: 8px; font-size: 0.8rem; color: rgba(255,255,255,0.72); margin-bottom: 8px; align-items: center; }
    .me .meta { color: rgba(255,255,255,0.9); }
    .name { font-weight: 700; }
    .photo {
      display: block; width: 100%; max-width: 520px; border-radius: 14px; margin-top: 10px;
      border: 1px solid rgba(255,255,255,0.10); background: rgba(0,0,0,0.1);
    }
    .photo-link { font-size: 0.86rem; opacity: .85; margin-top: 8px; }
    .typing { min-height: 24px; padding: 0 20px 10px; color: var(--muted); font-size: 0.9rem; }
    .composer {
      padding: 16px 18px 18px; border-top: 1px solid var(--border); background: rgba(255,255,255,0.02);
    }
    .preview { display: none; gap: 10px; align-items: center; margin-bottom: 12px; }
    .preview img { width: 72px; height: 72px; object-fit: cover; border-radius: 16px; border: 1px solid var(--border); }
    .composer-grid {
      display: grid;
      grid-template-columns: 1fr auto auto auto;
      gap: 10px;
    }
    input[type="text"] {
      width: 100%; border: 1px solid var(--border); background: var(--panel2); color: var(--text);
      border-radius: 14px; padding: 12px 14px; outline: none;
    }
    input[type="file"] { display: none; }
    .btn {
      border: 0; border-radius: 14px; padding: 12px 16px; cursor: pointer; font-weight: 700;
      background: var(--bubble-out); color: #fff;
    }
    .btn.secondary { background: rgba(255,255,255,0.07); border: 1px solid var(--border); color: var(--text); }
    .btn.danger { background: rgba(255,107,122,0.16); border: 1px solid rgba(255,107,122,0.35); color: #ffd7dc; }
    .composer-row2 { margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .name-box { flex: 1; min-width: 220px; }
    .footer-note { color: var(--muted); font-size: 0.82rem; }
    .badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); border: 1px solid var(--border); font-size: 0.82rem; }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; height: auto; min-height: 100vh; overflow: auto; }
      body { overflow: auto; }
      .chat { min-height: 70vh; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <h1>Anonymous Photo Chat</h1>
        <p>Chat like friends and share photos instantly with people around the world — no login, no account, no setup.</p>
      </div>

      <div class="section">
        <div class="label">Your identity</div>
        <div class="input-row">
          <input id="nameInput" type="text" placeholder="Anonymous name" maxlength="24" />
          <button class="btn secondary" id="renameBtn">Set</button>
        </div>
        <div class="footer-note" style="margin-top:10px;">You can stay anonymous by leaving the name as it is.</div>
      </div>

      <div class="section">
        <div class="label">Rooms</div>
        <div class="pill-row" id="roomPills"></div>
      </div>

      <div class="section" style="border-bottom:0; flex:1; min-height:0; display:flex; flex-direction:column;">
        <div class="label">Online in this room</div>
        <div class="users" id="usersList"></div>
      </div>
    </aside>

    <main class="chat">
      <div class="topbar">
        <div class="title">
          <h2 id="roomTitle">world</h2>
          <p>Public, anonymous, and real-time</p>
        </div>
        <div class="status"><span class="dot"></span><span id="statusText">connecting…</span></div>
      </div>

      <div class="messages" id="messages"></div>
      <div class="typing" id="typingLine"></div>

      <div class="composer">
        <div class="preview" id="previewBox">
          <img id="previewImg" alt="preview" />
          <div>
            <div style="font-weight:700;">Photo ready to send</div>
            <div class="muted small" id="previewName"></div>
          </div>
        </div>

        <div class="composer-grid">
          <input id="messageInput" type="text" placeholder="Say hello or send a photo…" maxlength="500" />
          <label class="btn secondary" for="photoInput">Photo</label>
          <input id="photoInput" type="file" accept="image/*" />
          <button class="btn" id="sendBtn">Send</button>
        </div>

        <div class="composer-row2">
          <button class="btn danger" id="clearPhotoBtn" style="display:none;">Remove photo</button>
          <span class="badge">Photos stay in memory only</span>
          <span class="badge">No account needed</span>
        </div>
      </div>
    </main>
  </div>

<script>
  const rooms = ["world", "friends", "travel", "memes"];
  let socket;
  let me = { id: "", name: "", room: "world" };
  let selectedRoom = "world";
  let selectedPhoto = null;
  let typingTimer = null;

  const els = {
    roomPills: document.getElementById('roomPills'),
    usersList: document.getElementById('usersList'),
    messages: document.getElementById('messages'),
    typingLine: document.getElementById('typingLine'),
    roomTitle: document.getElementById('roomTitle'),
    statusText: document.getElementById('statusText'),
    messageInput: document.getElementById('messageInput'),
    photoInput: document.getElementById('photoInput'),
    sendBtn: document.getElementById('sendBtn'),
    renameBtn: document.getElementById('renameBtn'),
    nameInput: document.getElementById('nameInput'),
    previewBox: document.getElementById('previewBox'),
    previewImg: document.getElementById('previewImg'),
    previewName: document.getElementById('previewName'),
    clearPhotoBtn: document.getElementById('clearPhotoBtn'),
  };

  function randId() { return Math.random().toString(36).slice(2, 8); }
  function guestName() { return `Anon-${randId()}`; }
  function roomLabel(room) { return room.replace(/-/g, ' '); }

  function makeRoomPills(stateRooms = []) {
    els.roomPills.innerHTML = '';
    const list = stateRooms.length ? stateRooms : rooms.map(n => ({name:n, users:0}));
    list.forEach(r => {
      const btn = document.createElement('button');
      btn.className = 'pill' + (r.name === selectedRoom ? ' active' : '');
      btn.textContent = `${roomLabel(r.name)}${typeof r.users === 'number' ? ` · ${r.users}` : ''}`;
      btn.onclick = () => switchRoom(r.name);
      els.roomPills.appendChild(btn);
    });
  }

  function addMessage(m) {
    const row = document.createElement('div');
    row.className = 'message-row';
    if (m.kind === 'system') row.classList.add('system');
    if (m.user_id === me.id && m.kind !== 'system') row.classList.add('me');

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    if (m.kind !== 'system') {
      const meta = document.createElement('div');
      meta.className = 'meta';
      const who = document.createElement('span');
      who.className = 'name';
      who.textContent = m.user_name || 'Anonymous';
      const time = document.createElement('span');
      time.textContent = m.timestamp || '';
      meta.appendChild(who);
      meta.appendChild(time);
      bubble.appendChild(meta);
    }

    if (m.text) {
      const text = document.createElement('div');
      text.textContent = m.text;
      bubble.appendChild(text);
    }

    if (m.kind === 'photo' && m.image_data) {
      const img = document.createElement('img');
      img.className = 'photo';
      img.src = m.image_data;
      img.alt = m.image_name || 'photo';
      bubble.appendChild(img);
      if (m.image_name) {
        const cap = document.createElement('div');
        cap.className = 'photo-link muted';
        cap.textContent = m.image_name;
        bubble.appendChild(cap);
      }
    }

    if (m.kind === 'system') {
      bubble.textContent = m.text;
    }

    row.appendChild(bubble);
    els.messages.appendChild(row);
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function setUsers(users) {
    els.usersList.innerHTML = '';
    if (!users.length) {
      const empty = document.createElement('div');
      empty.className = 'muted small';
      empty.textContent = 'No one is online here right now.';
      els.usersList.appendChild(empty);
      return;
    }
    users.forEach(u => {
      const card = document.createElement('div');
      card.className = 'user-chip';
      const left = document.createElement('div');
      left.className = 'user-left';
      const dot = document.createElement('span');
      dot.className = 'dot';
      const name = document.createElement('div');
      name.textContent = u.name + (u.typing ? ' · typing…' : '');
      left.appendChild(dot);
      left.appendChild(name);
      card.appendChild(left);
      card.appendChild(document.createElement('span'));
      els.usersList.appendChild(card);
    });
  }

  function renderState(state) {
    me = state.me;
    selectedRoom = me.room;
    els.roomTitle.textContent = roomLabel(me.room);
    els.nameInput.value = me.name;
    makeRoomPills(state.rooms);
    setUsers(state.users);
    els.messages.innerHTML = '';
    state.messages.forEach(addMessage);
    els.statusText.textContent = 'connected';
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${proto}://${location.host}/ws`);

    socket.onopen = () => {
      els.statusText.textContent = 'connected';
      socket.send(JSON.stringify({ type: 'join', name: els.nameInput.value || guestName(), room: selectedRoom }));
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'state') renderState(data);
      if (data.type === 'message' || data.type === 'photo' || data.type === 'system') addMessage(data);
      if (data.type === 'users') setUsers(data.users);
      if (data.type === 'rooms') makeRoomPills(data.rooms);
      if (data.type === 'typing') {
        if (data.user_id !== me.id) {
          els.typingLine.textContent = data.typing ? `${data.user_name} is typing…` : '';
        }
      }
      if (data.type === 'rename') {
        me.name = data.name;
        els.nameInput.value = data.name;
      }
      if (data.type === 'room_changed') {
        me.room = data.room;
        selectedRoom = data.room;
        els.roomTitle.textContent = roomLabel(data.room);
      }
    };

    socket.onclose = () => {
      els.statusText.textContent = 'reconnecting…';
      setTimeout(connect, 900);
    };

    socket.onerror = () => {
      els.statusText.textContent = 'connection issue';
    };
  }

  function sendTyping(value) {
    if (socket && socket.readyState === 1) {
      socket.send(JSON.stringify({ type: 'typing', typing: value }));
    }
  }

  function sendMessage() {
    const text = els.messageInput.value.trim();
    const hasPhoto = !!selectedPhoto;
    if (!text && !hasPhoto) return;

    if (hasPhoto) {
      socket.send(JSON.stringify({
        type: 'photo',
        image_data: selectedPhoto.dataUrl,
        image_name: selectedPhoto.name
      }));
      clearPhoto();
    }

    if (text) {
      socket.send(JSON.stringify({ type: 'message', text }));
    }

    els.messageInput.value = '';
    sendTyping(false);
  }

  function clearPhoto() {
    selectedPhoto = null;
    els.photoInput.value = '';
    els.previewBox.style.display = 'none';
    els.clearPhotoBtn.style.display = 'none';
  }

  function switchRoom(room) {
    selectedRoom = room;
    socket.send(JSON.stringify({ type: 'room', room }));
    els.messages.innerHTML = '';
    els.typingLine.textContent = '';
    makeRoomPills(rooms.map(name => ({name, users: 0})));
    const active = [...document.querySelectorAll('.pill')].find(p => p.textContent.startsWith(roomLabel(room)));
    document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
    if (active) active.classList.add('active');
  }

  els.sendBtn.onclick = sendMessage;
  els.renameBtn.onclick = () => {
    const name = els.nameInput.value.trim() || guestName();
    socket.send(JSON.stringify({ type: 'rename', name }));
  };

  els.clearPhotoBtn.onclick = clearPhoto;
  els.photoInput.onchange = () => {
    const file = els.photoInput.files && els.photoInput.files[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      alert('Please choose an image file.');
      return;
    }
    if (file.size > 3 * 1024 * 1024) {
      alert('Please keep the image under 3 MB for smooth sharing.');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      selectedPhoto = { dataUrl: reader.result, name: file.name };
      els.previewImg.src = reader.result;
      els.previewName.textContent = file.name;
      els.previewBox.style.display = 'flex';
      els.clearPhotoBtn.style.display = 'inline-flex';
    };
    reader.readAsDataURL(file);
  };

  els.messageInput.addEventListener('input', () => {
    sendTyping(true);
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => sendTyping(false), 900);
  });
  els.messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendMessage();
  });
  els.nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') els.renameBtn.click();
  });

  makeRoomPills(rooms.map(name => ({name, users:0})));
  els.nameInput.value = guestName();
  connect();
</script>
</body>
</html>
"""


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user: Optional[User] = None
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "join":
                if user is None:
                    user = await manager.register(websocket, data.get("name", ""), data.get("room", "world"))
                    await manager.send_json(websocket, manager.snapshot(user.room, user))
                    await manager.broadcast_room(user.room, {"type": "rooms", "rooms": [{"name": r.name, "users": len(r.users)} for r in sorted(manager.rooms.values(), key=lambda x: x.name)]})
                    await manager.broadcast_room(user.room, {"type": "users", "users": [{"id": u.id, "name": u.name, "typing": u.typing} for u in manager.get_or_create_room(user.room).users.values()]})
                    sys = await manager.system_message(user.room, f"{user.name} joined the room.")
                    await manager.broadcast_room(user.room, {"type": "system", **manager._serialize_message(sys)})
                else:
                    await manager.send_json(websocket, manager.snapshot(user.room, user))

            elif msg_type == "message" and user:
                msg = await manager.post_message(websocket, data.get("text", ""))
                if msg:
                    payload = {"type": "message", **manager._serialize_message(msg)}
                    await manager.broadcast_room(user.room, payload)

            elif msg_type == "photo" and user:
                img = data.get("image_data", "")
                name = data.get("image_name", "photo")
                msg = await manager.post_photo(websocket, img, name)
                if msg:
                    payload = {"type": "photo", **manager._serialize_message(msg)}
                    await manager.broadcast_room(user.room, payload)

            elif msg_type == "typing" and user:
                user = await manager.set_typing(websocket, bool(data.get("typing"))) or user
                await manager.broadcast_room(user.room, {"type": "typing", "user_id": user.id, "user_name": user.name, "typing": user.typing}, exclude=websocket)

            elif msg_type == "rename" and user:
                user = await manager.rename(websocket, data.get("name", "")) or user
                await manager.send_json(websocket, {"type": "rename", "name": user.name})
                await manager.broadcast_room(user.room, {"type": "users", "users": [{"id": u.id, "name": u.name, "typing": u.typing} for u in manager.get_or_create_room(user.room).users.values()]})

            elif msg_type == "room" and user:
                old_room = user.room
                old_users = [{"id": u.id, "name": u.name, "typing": u.typing} for u in manager.get_or_create_room(old_room).users.values()]
                user = await manager.switch_room(websocket, data.get("room", "world")) or user
                await manager.send_json(websocket, manager.snapshot(user.room, user))
                await manager.broadcast_room(old_room, {"type": "users", "users": old_users})
                await manager.broadcast_room(user.room, {"type": "users", "users": [{"id": u.id, "name": u.name, "typing": u.typing} for u in manager.get_or_create_room(user.room).users.values()]})
                await manager.broadcast_room(user.room, {"type": "rooms", "rooms": [{"name": r.name, "users": len(r.users)} for r in sorted(manager.rooms.values(), key=lambda x: x.name)]})
                sys = await manager.system_message(user.room, f"{user.name} entered the room.")
                await manager.broadcast_room(user.room, {"type": "system", **manager._serialize_message(sys)})

    except WebSocketDisconnect:
        if user:
            room_name = user.room
            removed = await manager.unregister(websocket)
            if removed:
                await manager.broadcast_room(room_name, {"type": "users", "users": [{"id": u.id, "name": u.name, "typing": u.typing} for u in manager.get_or_create_room(room_name).users.values()]})
                sys = await manager.system_message(room_name, f"{removed.name} left the room.")
                await manager.broadcast_room(room_name, {"type": "system", **manager._serialize_message(sys)})


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
