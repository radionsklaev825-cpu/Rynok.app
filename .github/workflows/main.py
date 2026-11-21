"""Запускает сервер из amzn_phone.py внутри приложения и показывает его в WebView."""
import os
import threading
from http.server import ThreadingHTTPServer

from kivy.app import App
from kivy.uix.widget import Widget
from jnius import autoclass
from android.runnable import run_on_ui_thread

import amzn_phone

WebView = autoclass("android.webkit.WebView")
WebViewClient = autoclass("android.webkit.WebViewClient")
Intent = autoclass("android.content.Intent")
Uri = autoclass("android.net.Uri")
Activity = autoclass("org.kivy.android.PythonActivity").mActivity
_view = None


def open_external(url):
    """Открывает ссылку в браузере телефона (Chrome и т.п.)."""
    Activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))


def start_server():
    for p in range(amzn_phone.PORT, amzn_phone.PORT + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), amzn_phone.Handler)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return p
    raise RuntimeError("Нет свободного порта")


@run_on_ui_thread
def open_webview(url):
    global _view
    _view = WebView(Activity)
    s = _view.getSettings()
    s.setJavaScriptEnabled(True)
    s.setDomStorageEnabled(True)
    _view.setWebViewClient(WebViewClient())
    Activity.setContentView(_view)
    _view.loadUrl(url)


class StockApp(App):
    def build(self):
        # Профиль (имя и ключ) хранится в памяти приложения и переживает перезапуск.
        amzn_phone.PROFILE_PATH = os.path.join(self.user_data_dir, "profile.json")
        amzn_phone.OPEN_URL = open_external
        port = start_server()
        open_webview(f"http://127.0.0.1:{port}")
        return Widget()

    def on_pause(self):
        return True


StockApp().run()
