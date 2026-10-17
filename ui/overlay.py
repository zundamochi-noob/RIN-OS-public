import sys
import os
import subprocess
import threading
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout,
    QHBoxLayout, QLabel
)
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, pyqtSignal, QObject
from PyQt6.QtGui import QFont

from core.config import (
    MAIN_PY_PATH, MAIN_PY_PYTHON, RIN_LOG_PATH,
    OLLAMA_URL, MODEL, API_PORT, OVERLAY_PORT,
)

STYLE_INPUT = """
    background-color: rgba(10, 10, 20, 140);
    color: white;
    font-size: 13px;
    font-family: メイリオ;
    border: none;
    border-radius: 0px;
    padding: 4px 10px;
    margin: 0px;
"""

STYLE_TOGGLE = """
    QPushButton {
        background-color: rgba(10, 10, 20, 140);
        color: rgba(153, 204, 255, 200);
        font-size: 11px;
        font-family: メイリオ;
        border: none;
        border-top: 1px solid rgba(153, 204, 255, 80);
        padding: 2px 0px;
    }
    QPushButton:hover {
        background-color: rgba(30, 30, 60, 180);
    }
"""

STYLE_LOG = """
    background-color: rgba(10, 10, 20, 140);
    color: rgba(200, 220, 255, 220);
    font-size: 12px;
    font-family: メイリオ;
    border: none;
    border-top: 1px solid rgba(153, 204, 255, 80);
    padding: 6px;
"""

STYLE_SHORTCUT = """
    QPushButton {
        background-color: rgba(30, 40, 80, 180);
        color: rgba(153, 204, 255, 220);
        font-size: 11px;
        font-family: メイリオ;
        border: 1px solid rgba(153, 204, 255, 100);
        border-radius: 4px;
        padding: 4px 8px;
    }
    QPushButton:hover {
        background-color: rgba(50, 70, 130, 200);
    }
"""

STYLE_LAUNCH = """
    QPushButton {
        background-color: rgba(40, 80, 40, 180);
        color: rgba(180, 255, 180, 230);
        font-size: 12px;
        font-family: メイリオ;
        border: 1px solid rgba(180, 255, 180, 120);
        border-radius: 6px;
        padding: 6px 12px;
    }
    QPushButton:hover {
        background-color: rgba(60, 110, 60, 210);
    }
"""

class RinSignal(QObject):
    response_received = pyqtSignal(str)
    rin_stopped = pyqtSignal()
    voice_toggled = pyqtSignal(bool)

rin_signal = RinSignal()

class Overlay(QWidget):
    COLLAPSED_H = 66
    EXPANDED_H  = 500
    WIDTH       = 297
    MAX_LOG_LINES = 100

    def __init__(self):
        super().__init__()
        self.expanded = False
        self.rin_process = None  # main.pyのsubprocess.Popenインスタンス
        self.setWindowTitle("Overlay")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnBottomHint |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(self.WIDTH, self.COLLAPSED_H)
        self.move(0, 281)
        self._build_ui()
        rin_signal.response_received.connect(self.append_log)
        rin_signal.rin_stopped.connect(self.on_rin_stopped)
        rin_signal.voice_toggled.connect(self._on_voice_toggled)
        self._set_stopped_ui()

    def _build_ui(self):
        # 入力欄
        self.input_box = QLineEdit(self)
        self.input_box.setGeometry(0, 10, self.WIDTH, 36)
        self.input_box.setStyleSheet(STYLE_INPUT)
        self.input_box.setPlaceholderText("凛に話しかける...")
        self.input_box.returnPressed.connect(self.on_enter)

        # 開閉ボタン
        self.toggle_btn = QPushButton("∨ 会話ログ / ショートカット", self)
        self.toggle_btn.setGeometry(0, 46, self.WIDTH, 20)
        self.toggle_btn.setStyleSheet(STYLE_TOGGLE)
        self.toggle_btn.clicked.connect(self.toggle_panel)

        # 停止中のみ表示する起動ボタン（通常時は隠す）
        self.launch_btn = QPushButton("▶ 凛を起動", self)
        self.launch_btn.setGeometry(0, 10, self.WIDTH, 36)
        self.launch_btn.setStyleSheet(STYLE_LAUNCH)
        self.launch_btn.clicked.connect(self.launch_rin)
        self.launch_btn.hide()

        # 折りたたみパネル本体
        self.panel = QWidget(self)
        self.panel.setGeometry(0, 56, self.WIDTH, self.EXPANDED_H - self.COLLAPSED_H)
        self.panel.hide()

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(4)

        # 会話ログ
        self.log_view = QTextEdit(self.panel)
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(STYLE_LOG)
        self.log_view.setFixedHeight(330)
        panel_layout.addWidget(self.log_view)

        # ショートカットボタン1行目
        shortcut_row = QHBoxLayout()
        shortcut_row.setSpacing(4)
        shortcuts = ["今日の予定", "明日の予定", "天気", "時刻"]
        for label in shortcuts:
            btn = QPushButton(label, self.panel)
            btn.setStyleSheet(STYLE_SHORTCUT)
            btn.clicked.connect(lambda _, t=label: self.send_shortcut(t))
            shortcut_row.addWidget(btn)
        panel_layout.addLayout(shortcut_row)

        # ショートカットボタン2行目
        shortcut_row2 = QHBoxLayout()
        shortcut_row2.setSpacing(4)
        shortcut_row2.addStretch()
        self.voice_btn = QPushButton("🔊 音声ON", self.panel)
        self.voice_btn.setStyleSheet(STYLE_SHORTCUT)
        self.voice_btn.clicked.connect(self.toggle_voice)
        shortcut_row2.addWidget(self.voice_btn)
        vram_btn = QPushButton("VRAM解放", self.panel)
        vram_btn.setStyleSheet(STYLE_SHORTCUT)
        vram_btn.clicked.connect(self.release_vram)
        shortcut_row2.addWidget(vram_btn)
        quit_btn = QPushButton("終了", self.panel)
        quit_btn.setStyleSheet(STYLE_SHORTCUT)
        quit_btn.clicked.connect(self.quit_rin)
        shortcut_row2.addWidget(quit_btn)
        panel_layout.addLayout(shortcut_row2)

    def toggle_panel(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.panel.show()
            self.resize(self.WIDTH, self.EXPANDED_H)
            self.toggle_btn.setText("∧ 会話ログ / ショートカット")
        else:
            self.panel.hide()
            self.resize(self.WIDTH, self.COLLAPSED_H)
            self.toggle_btn.setText("∨ 会話ログ / ショートカット")

    def _on_voice_toggled(self, enabled: bool):
        self.voice_btn.setText("🔊 音声ON" if enabled else "🔇 音声OFF")

    def toggle_voice(self):
        def _do():
            try:
                res = httpx.post(f"http://127.0.0.1:{API_PORT}/toggle_voice")
                enabled = res.json().get("voice_enabled", True)
                rin_signal.voice_toggled.emit(enabled)
                label = "音声をONにしました" if enabled else "音声をOFFにしました"
                rin_signal.response_received.emit(f"システム: {label}")
            except Exception:
                rin_signal.response_received.emit("システム: 音声切り替えに失敗しました（凛が起動していますか？）")
        threading.Thread(target=_do, daemon=True).start()

    def release_vram(self):
        def _do():
            try:
                httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": MODEL, "keep_alive": 0})
                rin_signal.response_received.emit("システム: VRAMを解放しました")
            except Exception:
                rin_signal.response_received.emit("システム: VRAM解放に失敗しました")
        threading.Thread(target=_do, daemon=True).start()

    def launch_rin(self):
        """凛を起動ボタン押下 → 別スレッドでOllamaチェック→main.py起動"""
        if self.rin_process is not None and self.rin_process.poll() is None:
            return
        self._set_running_ui()
        self.append_log("システム: 凛を起動しています...")
        threading.Thread(target=self._launch_rin_thread, daemon=True).start()

    def _launch_rin_thread(self):
        """Ollama起動確認 → main.py起動 → 終了監視（別スレッドで実行）"""
        import time
        try:
            httpx.get(OLLAMA_URL, timeout=2.0)
        except Exception:
            rin_signal.response_received.emit("システム: Ollamaを起動しています...")
            subprocess.Popen(
                ["ollama", "serve"],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            # 固定待機ではなく、Ollamaが応答を返すまでポーリング（最大15秒）
            # 環境やタイミングに左右されず、準備ができた瞬間に次へ進める
            for _ in range(30):
                try:
                    httpx.get(OLLAMA_URL, timeout=1.0)
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                rin_signal.response_received.emit("システム: Ollamaの起動確認に失敗しました")

        # main.pyの出力をログファイルにリダイレクトして起動
        # 起動のたびに追記モードで区切り線を入れておくと、どの回のログか見分けやすい
        try:
            os.makedirs(os.path.dirname(RIN_LOG_PATH), exist_ok=True)
            log_file = open(RIN_LOG_PATH, "a", encoding="utf-8")
            log_file.write(f"\n{'=' * 60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 凛を起動\n{'=' * 60}\n")
            log_file.flush()
        except Exception:
            log_file = None

        self.rin_process = subprocess.Popen(
            [MAIN_PY_PYTHON, MAIN_PY_PATH],
            cwd=os.path.dirname(MAIN_PY_PATH),
            stdout=log_file if log_file else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        # プロセス終了を待って通知
        self.rin_process.wait()
        if log_file:
            log_file.close()
        rin_signal.rin_stopped.emit()

    def on_rin_stopped(self):
        """main.pyが終了したことを検知した時のUI切り替え（メインスレッドで実行される）"""
        self._set_stopped_ui()
        self.append_log("システム: 凛は停止しました")

    def _set_running_ui(self):
        """動作中の見た目に切り替える"""
        self.launch_btn.hide()
        self.input_box.show()
        self.toggle_btn.show()

    def _set_stopped_ui(self):
        """停止中の見た目に切り替える（入力欄を隠し、起動ボタンだけ出す）"""
        self.input_box.hide()
        self.toggle_btn.hide()
        if self.expanded:
            self.toggle_panel()
        self.launch_btn.show()

    def quit_rin(self):
        """凛OS本体（main.py）に終了シグナル（q）を送り、VRAMも解放する"""
        def _do():
            try:
                httpx.post(f"http://127.0.0.1:{API_PORT}/input", json={"text": "q"})
                rin_signal.response_received.emit("システム: 終了処理を開始しました")
            except Exception:
                rin_signal.response_received.emit("システム: 終了シグナルの送信に失敗しました")
            try:
                httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": MODEL, "keep_alive": 0})
                rin_signal.response_received.emit("システム: VRAMを解放しました")
            except Exception:
                rin_signal.response_received.emit("システム: VRAM解放に失敗しました")
        threading.Thread(target=_do, daemon=True).start()

    def on_enter(self):
        text = self.input_box.text().strip()
        if not text:
            return
        now = datetime.now().strftime("%H:%M")
        self.append_log(f"[{now}] あなた: {text}")
        threading.Thread(
            target=httpx.post,
            args=(f"http://127.0.0.1:{API_PORT}/input",),
            kwargs={"json": {"text": text}},
            daemon=True,
        ).start()
        self.input_box.clear()

    def send_shortcut(self, text):
        now = datetime.now().strftime("%H:%M")
        msg = "今日の天気を教えて。" if text == "天気" else text
        self.append_log(f"[{now}] あなた: {msg}")
        threading.Thread(
            target=httpx.post,
            args=(f"http://127.0.0.1:{API_PORT}/input",),
            kwargs={"json": {"text": msg}},
            daemon=True,
        ).start()

    def append_log(self, text: str):
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if escaped.startswith("システム:"):
            if any(kw in escaped for kw in ["準備完了"]):
                color = "#4dcc4d"
            elif any(kw in escaped for kw in ["停止", "VRAMを解放", "終了処理"]):
                color = "#cc8844"
            else:
                color = "#4da6cc"
            html = f'<span style="color: {color};">{escaped}</span>'
        else:
            html = f'<span style="color: rgba(200, 220, 255, 220);">{escaped}</span>'

        self.log_view.append(html)
        doc = self.log_view.document()
        if doc.blockCount() > self.MAX_LOG_LINES:
            cursor = self.log_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            excess = doc.blockCount() - self.MAX_LOG_LINES
            for _ in range(excess):
                cursor.select(cursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )


from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class ResponseHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data = json.loads(body)
        text = data.get("text", "")
        rin_signal.response_received.emit(text)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        pass

def start_response_server():
    server = HTTPServer(("127.0.0.1", OVERLAY_PORT), ResponseHandler)
    server.serve_forever()

app = QApplication(sys.argv)
window = Overlay()
window.show()

t = threading.Thread(target=start_response_server, daemon=True)
t.start()

sys.exit(app.exec())
