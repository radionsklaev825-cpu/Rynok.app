"""
Stock Analyzer для телефона (Android + Pydroid 3) - свечи в реальном времени.

Только стандартная библиотека Python - ничего устанавливать не нужно.
Скрипт запускает мини-сервер на телефоне и открывает страницу в браузере.
График рисует браузер (касания: палец - двигать, щипок - масштаб),
данные и расчёты делает Python.

Таймфреймы: 1 мин, 5 мин, 10 мин, 1 день. Режим Live обновляет текущую свечу
и добавляет новые автоматически.

Запуск: открыть файл в Pydroid 3 и нажать ▶.
Если браузер не открылся сам - откройте http://127.0.0.1:8000 вручную.
Pydroid 3 при этом должен оставаться запущенным (можно свернуть).

Это учебный технический анализ, а не финансовая рекомендация.
"""
import calendar
import json
import math
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

PORT = 8000
PERIODS = {"3mo", "6mo", "1y", "2y", "5y"}
BARS = {"3mo": 63, "6mo": 126, "1y": 252, "2y": 504, "5y": 1260}

# Таймфреймы внутри дня: (интервал Twelve Data, сколько свечей, интервал Yahoo, диапазон Yahoo).
# 10 минут строятся из 5-минутных свечей.
INTRA = {"1m": ("1min", 780, "1m", "5d"),
         "5m": ("5min", 780, "5m", "1mo"),
         "10m": ("5min", 1560, "5m", "1mo")}
TFS = {"1m", "5m", "10m", "1d"}

# Ключ Twelve Data каждый пользователь вводит в самом приложении (поле "Ключ").
# В коде ключей нет. Без ключа данные берутся только из Yahoo.

CACHE = {}

# Профиль (имя + ключ) хранится в файле на телефоне, на сервер никуда не уходит.
# Приложение (main.py) подставляет сюда папку своих данных.
PROFILE_PATH = os.path.join(os.path.expanduser("~"), ".stock_analyzer_profile.json")
PROFILE_LOCK = threading.Lock()
KEY_URL = "https://twelvedata.com/register"
OPEN_URL = lambda url: webbrowser.open(url)  # main.py заменяет на системный Intent
CACHE_TTL = 20  # секунд: повторные запросы не тратят лимит ключа

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")


# =====================================================================
#                      ДАННЫЕ И ИНДИКАТОРЫ
# =====================================================================
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


class TickerNotFound(ValueError):
    pass


def _ssl_contexts():
    """Сначала обычная проверка сертификатов, затем набор из certifi (если есть)."""
    ctxs = [None]
    try:
        import ssl
        import certifi
        ctxs.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    return ctxs


def _http_json(urls, headers):
    """GET -> JSON. Если сервер ответил JSON-ошибкой, вернёт её тело."""
    errors = []
    for url in urls:
        for ctx in _ssl_contexts():
            try:
                with urlopen(Request(url, headers=headers), timeout=20,
                             context=ctx) as r:
                    return json.load(r)
            except HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    if isinstance(body, dict):
                        return body
                except Exception:
                    pass
                errors.append(f"HTTP {e.code}")
                break  # сервер ответил - смена сертификатов не поможет
            except Exception as e:
                errors.append(f"{type(e).__name__}: {str(e)[:90]}")
    raise ConnectionError("; ".join(dict.fromkeys(errors)))


def fetch_yahoo(ticker, period, tf):
    headers = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
               "Accept-Language": "en-US,en;q=0.9"}
    if tf == "1d":
        interval, rng = "1d", period
    else:
        interval, rng = INTRA[tf][2], INTRA[tf][3]
    urls = [f"https://{h}/v8/finance/chart/{quote(ticker)}"
            f"?range={rng}&interval={interval}&includePrePost=false" for h in HOSTS]
    try:
        data = _http_json(urls, headers)
    except ConnectionError as e:
        raise ValueError(f"Yahoo: {e}")
    chart = data.get("chart") or {}
    result = chart.get("result")
    if not result:
        if "not found" in str(chart.get("error")).lower():
            raise TickerNotFound(f"Тикер {ticker} не найден.")
        raise ValueError("Yahoo: ответ без данных")
    r0 = result[0]
    off = (r0.get("meta") or {}).get("gmtoffset", 0) or 0
    q = r0["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(r0.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        v = q["volume"][i]
        if None in (o, h, l, c):
            continue
        key = (time.strftime("%Y-%m-%d", time.gmtime(t + off)) if tf == "1d"
               else int(t + off))  # время биржи, записанное как "UTC"
        rows.append((key, o, h, l, c, v or 0))
    return rows


def fetch_twelvedata(ticker, period, tf, key):
    if tf == "1d":
        interval, size = "1day", BARS[period]
    else:
        interval, size = INTRA[tf][0], INTRA[tf][1]
    url = ("https://api.twelvedata.com/time_series?symbol=" + quote(ticker)
           + f"&interval={interval}&outputsize={size}&apikey=" + quote(key))
    try:
        data = _http_json([url], {"User-Agent": UA})
    except ConnectionError as e:
        raise ValueError(f"Twelve Data: {e}")
    if data.get("status") == "error" or "values" not in data:
        msg = str(data.get("message", "нет данных"))[:160]
        low = msg.lower()
        if "symbol" in low and "missing or invalid" in low:
            raise TickerNotFound(f"Тикер {ticker} не найден.")
        raise ValueError("Twelve Data: " + msg)
    rows = []
    for d in data["values"]:
        try:
            o, h, l, c = (float(d[k]) for k in ("open", "high", "low", "close"))
            dt = str(d["datetime"])
            key = dt[:10] if tf == "1d" else calendar.timegm(
                time.strptime(dt[:19], "%Y-%m-%d %H:%M:%S"))  # время биржи как "UTC"
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((key, o, h, l, c, float(d.get("volume") or 0)))
    rows.sort(key=lambda r: r[0])
    return rows


def aggregate(rows, sec=600):
    """Склейка свечей в более крупные (5 мин -> 10 мин)."""
    out, cur = [], None
    for t, o, h, l, c, v in rows:
        b = t // sec * sec
        if cur is not None and cur[0] == b:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
        else:
            cur = [b, o, h, l, c, v]
            out.append(cur)
    return [tuple(x) for x in out]


def fetch(ticker, period, tf, key=""):
    """Источники по очереди: с ключом - Twelve Data, затем Yahoo; без ключа - Yahoo."""
    sources = [fetch_yahoo]
    if key:
        sources.insert(0, lambda t, p, f: fetch_twelvedata(t, p, f, key))
    errors = []
    for fn in sources:
        try:
            rows = fn(ticker, period, tf)
            return aggregate(rows) if tf == "10m" else rows
        except TickerNotFound:
            raise
        except Exception as e:
            errors.append(str(e))
    msg = "Не удалось получить данные.\n" + "\n".join(errors)
    if not key:
        msg += ("\nYahoo блокирует запросы с этого телефона. Решение: бесплатный ключ "
                "на twelvedata.com (Sign up -> Dashboard -> API key), вставьте его "
                "в поле \"Ключ\" сверху и нажмите Сохранить.")
    raise ValueError(msg)


def sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(v, span):
    a = 2 / (span + 1)
    out, prev = [], None
    for x in v:
        prev = x if prev is None else a * x + (1 - a) * prev
        out.append(prev)
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    a = 1 / n
    ag = al = None
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if ag is None:
            ag, al = g, l
        else:
            ag = a * g + (1 - a) * ag
            al = a * l + (1 - a) * al
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def bollinger(c, n=20, k=2.0):
    mid = sma(c, n)
    up, lo = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in w) / (n - 1))
        up[i], lo[i] = mid[i] + k * sd, mid[i] - k * sd
    return up, lo


def evaluate(close, s20, s50, r, macd, sig, bb_up, bb_lo):
    """Каждый индикатор голосует +1 (рост) / -1 (падение) / 0 (нейтрально)."""
    c = close[-1]
    votes = [
        ["SMA20 выше SMA50 (краткосрочный тренд)", 1 if s20[-1] > s50[-1] else -1],
        ["Цена выше SMA50", 1 if c > s50[-1] else -1],
    ]
    rv = r[-1]
    if rv < 30:
        votes.append([f"RSI={rv:.0f} - перепроданность", 1])
    elif rv > 70:
        votes.append([f"RSI={rv:.0f} - перекупленность", -1])
    else:
        votes.append([f"RSI={rv:.0f} - нейтральная зона", 0])
    votes.append(["MACD выше сигнальной линии", 1 if macd[-1] > sig[-1] else -1])
    if c < bb_lo[-1]:
        votes.append(["Цена ниже нижней полосы Боллинджера", 1])
    elif c > bb_up[-1]:
        votes.append(["Цена выше верхней полосы Боллинджера", -1])
    else:
        votes.append(["Цена внутри полос Боллинджера", 0])

    score = sum(v for _, v in votes)
    if score >= 3:
        verdict = "СИЛЬНО ВОСХОДЯЩИЙ сигнал"
    elif score >= 1:
        verdict = "умеренно восходящий сигнал"
    elif score <= -3:
        verdict = "СИЛЬНО НИСХОДЯЩИЙ сигнал"
    elif score <= -1:
        verdict = "умеренно нисходящий сигнал"
    else:
        verdict = "нейтрально / боковик"
    return votes, score, verdict


def trend(close, window=30, days=5):
    """Линейная регрессия по последним `window` закрытиям."""
    y = close[-window:]
    n = len(y)
    mx, my = (n - 1) / 2, sum(y) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    slope = sum((i - mx) * (y[i] - my) for i in range(n)) / sxx
    intercept = my - slope * mx
    return slope, intercept + slope * (n - 1 + days)


def build(ticker, period, tf="1d", key=""):
    rows = fetch(ticker, period, tf, key)
    if len(rows) < 60:
        raise ValueError("Слишком мало данных, выберите другой период или таймфрейм.")
    intraday = tf != "1d"
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    v = [r[5] for r in rows]

    s20, s50 = sma(c, 20), sma(c, 50)
    rs = rsi(c)
    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sig = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig)]
    bb_up, bb_lo = bollinger(c)

    votes, score, verdict = evaluate(c, s20, s50, rs, macd, sig, bb_up, bb_lo)
    slope, fc = trend(c)

    def rd(arr, k=4):
        return [None if x is None else round(x, k) for x in arr]

    date = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(t[-1])) if intraday
            else t[-1])
    return {
        "ticker": ticker, "tf": tf, "intraday": intraday, "date": date,
        "last": round(c[-1], 2), "change": round(c[-1] - c[-2], 2),
        "pct": round((c[-1] - c[-2]) / c[-2] * 100, 2),
        "verdict": verdict, "score": score, "votes": votes,
        "slope": round(slope, 2), "forecast": round(fc, 2),
        "t": t, "o": rd(o), "h": rd(h), "l": rd(l), "c": rd(c), "v": v,
        "sma20": rd(s20), "sma50": rd(s50), "bbUp": rd(bb_up), "bbLow": rd(bb_lo),
        "rsi": rd(rs, 2), "macd": rd(macd), "signal": rd(sig), "hist": rd(hist),
    }


def load_profile():
    with PROFILE_LOCK:
        try:
            with open(PROFILE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}


def save_profile(d):
    with PROFILE_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(PROFILE_PATH)), exist_ok=True)
        tmp = PROFILE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, PROFILE_PATH)


def delete_profile():
    with PROFILE_LOCK:
        try:
            os.remove(PROFILE_PATH)
        except OSError:
            pass
    CACHE.clear()


def public_profile(d):
    """Что можно показать странице: ключ целиком не отдаём."""
    k = d.get("key") or ""
    return {"name": d.get("name", ""), "hasKey": bool(k),
            "keyTail": k[-4:] if k else "", "done": bool(d.get("done"))}


def update_profile(data):
    d = load_profile()
    if "name" in data:
        name = str(data["name"]).strip()
        if not 1 <= len(name) <= 30:
            raise ValueError("Имя: от 1 до 30 символов.")
        d["name"] = name
    if "key" in data:
        k = str(data["key"]).strip()
        if k and not re.fullmatch(r"[A-Za-z0-9]{8,64}", k):
            raise ValueError("Некорректный ключ: только буквы и цифры, без пробелов.")
        d["key"] = k
    if data.get("done"):
        if not d.get("name"):
            raise ValueError("Сначала введите имя.")
        d["done"] = True
    save_profile(d)
    CACHE.clear()
    return public_profile(d)


def build_cached(ticker, period, tf, apikey=""):
    key = (ticker, period, tf, apikey)
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    payload = build(ticker, period, tf, apikey)
    if len(CACHE) > 40:
        CACHE.clear()
    CACHE[key] = (now, payload)
    return payload


# =====================================================================
#                        СТРАНИЦА (HTML + JS)
# =====================================================================
PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Stock Analyzer</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root { --bg:#0b1220; --card:#131c2e; --border:#1f2a40; --text:#e2e8f0;
          --muted:#94a3b8; --accent:#3b82f6; --up:#22c55e; --down:#ef4444; --warn:#eab308; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
         padding: 12px 12px 28px; max-width: 900px; margin-inline:auto; }
  h1 { font-size: 20px; margin: 4px 0 12px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }
  input, select, button { font-size:16px; border-radius:10px; border:1px solid var(--border);
         background:var(--card); color:var(--text); padding:10px 12px; }
  input { width: 110px; text-transform: uppercase; font-weight:700; }
  select:disabled { opacity:.45; }
  button.primary { background:var(--accent); border-color:var(--accent); font-weight:700; }
  button:disabled { opacity:.5; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px;
          padding:14px 16px; margin-bottom:10px; }
  .lbl { font-size:11px; letter-spacing:.06em; color:var(--muted); font-weight:700; }
  .price { font-size:32px; font-weight:800; margin:2px 0; }
  .verdict { font-size:18px; font-weight:800; margin:6px 0 2px; }
  .muted { color:var(--muted); font-size:13px; }
  .sig { display:flex; gap:8px; margin:6px 0; font-size:14px; }
  .dot { font-size:12px; line-height:20px; }
  .up { color:var(--up); } .down { color:var(--down); } .warn { color:var(--warn); }
  .chartwrap { position:relative; background:var(--card); border:1px solid var(--border);
               border-radius:14px; padding:6px; margin-bottom:10px; overflow:hidden; }
  #legend { position:absolute; z-index:5; top:10px; left:12px; right:70px; font-size:11px;
            line-height:1.5; pointer-events:none; }
  .tag { font-size:10px; color:var(--muted); margin:2px 6px; }
  label.chk { display:inline-flex; gap:6px; align-items:center; font-size:14px; color:var(--text);
              padding:6px 4px; }
  label.chk input { width:18px; height:18px; padding:0; }
  .err { color:var(--down); margin:8px 0; font-size:14px; white-space:pre-wrap; }
  .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--up);
           margin-right:6px; animation: pl 1.4s infinite; }
  @keyframes pl { 0%,100% { opacity:1 } 50% { opacity:.25 } }
  #auth { display:none; position:fixed; inset:0; z-index:100; background:var(--bg);
          overflow:auto; padding:20px; }
  #auth .box { max-width:420px; margin:6vh auto 0; }
  #auth h2 { margin:6px 0 8px; font-size:24px; }
  #auth input { width:100%; text-transform:none; font-weight:400; margin:10px 0; padding:12px; }
  #auth button { width:100%; margin-bottom:8px; padding:12px; }
  #auth ol { padding-left:20px; margin:8px 0; line-height:1.55; font-size:14px; }
  #pbar { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
  #pbar .muted { flex:1; }
  #pbar button { padding:6px 10px; font-size:13px; }
</style>
</head>
<body>
  <div id="auth"><div class="box">
    <div id="step1">
      <div class="lbl">ШАГ 1 ИЗ 2</div>
      <h2>Регистрация</h2>
      <div class="muted">Введите имя, и приложение запомнит вас на этом телефоне. Данные хранятся только на устройстве и никуда не отправляются.</div>
      <input id="uname" maxlength="30" placeholder="Ваше имя" autocomplete="off">
      <button id="next1" class="primary">Далее</button>
    </div>
    <div id="step2" style="display:none">
      <div class="lbl">ШАГ 2 ИЗ 2</div>
      <h2>Ключ Twelve Data</h2>
      <div class="muted">Ключ нужен для стабильной загрузки котировок. Он будет сохранён на вашем телефоне.</div>
      <input id="ukey" placeholder="Вставьте API-ключ" autocomplete="off" autocapitalize="off">
      <button id="help">❓ Где взять ключ?</button>
      <div id="helpbox" class="card" style="display:none">
        <div class="lbl">КАК ПОЛУЧИТЬ БЕСПЛАТНЫЙ КЛЮЧ</div>
        <ol>
          <li>Нажмите кнопку ниже и откройте twelvedata.com.</li>
          <li>Зарегистрируйтесь (Sign up) и подтвердите почту.</li>
          <li>Войдите и откройте Dashboard → API keys.</li>
          <li>Скопируйте ключ, вернитесь в приложение и вставьте его в поле выше.</li>
        </ol>
        <button id="openkey" class="primary">Открыть twelvedata.com</button>
      </div>
      <button id="save2" class="primary">Сохранить и начать</button>
      <button id="skip">Пропустить (только Yahoo)</button>
      <button id="back2" style="display:none">Назад</button>
    </div>
    <div id="autherr" class="err"></div>
  </div></div>

  <h1>📈 Stock Analyzer</h1>
  <div id="pbar"><span id="who" class="muted"></span>
    <button id="chkey">🔑 Ключ</button><button id="logout">Выйти</button></div>
  <div class="row">
    <input id="ticker" value="AMZN" maxlength="12" autocapitalize="characters" autocomplete="off">
    <select id="tf">
      <option value="1m">1 мин</option><option value="5m" selected>5 мин</option>
      <option value="10m">10 мин</option><option value="1d">1 день</option>
    </select>
    <select id="period" disabled>
      <option value="3mo">3 мес</option><option value="6mo">6 мес</option>
      <option value="1y" selected>1 год</option><option value="2y">2 года</option>
      <option value="5y">5 лет</option>
    </select>
    <button id="go" class="primary">Анализировать</button>
    <label class="chk"><input type="checkbox" id="live" checked> Live</label>
  </div>
  <div id="liveinfo" class="muted" style="margin-bottom:6px"></div>
  <div id="err" class="err"></div>

  <div class="card">
    <div id="name" class="muted">-</div>
    <div id="price" class="price">$ -</div>
    <div id="chg" style="font-weight:700"></div>
  </div>

  <div class="card">
    <div class="lbl">ПОДСКАЗКА</div>
    <div id="verdict" class="verdict">-</div>
    <div id="score" class="muted"></div>
  </div>

  <div class="row" style="margin-bottom:4px">
    <label class="chk"><input type="checkbox" id="cSma20" checked> SMA 20</label>
    <label class="chk"><input type="checkbox" id="cSma50" checked> SMA 50</label>
    <label class="chk"><input type="checkbox" id="cBb" checked> Bollinger</label>
    <button id="reset" style="margin-left:auto;padding:6px 10px;font-size:14px">⟲ К последним</button>
  </div>

  <div class="chartwrap">
    <div id="legend"></div>
    <div id="main"></div>
    <div class="tag">RSI 14</div>
    <div id="rsi"></div>
    <div class="tag">MACD 12 26 9</div>
    <div id="macd"></div>
  </div>
  <div class="muted" style="margin:0 2px 10px">Палец - двигать график, щипок - масштаб. Время на графике - биржевое (Нью-Йорк).</div>

  <div class="card">
    <div class="lbl" style="margin-bottom:6px">СИГНАЛЫ ИНДИКАТОРОВ</div>
    <div id="signals"></div>
  </div>
  <div class="card">
    <div id="trendLbl" class="lbl">ТРЕНД</div>
    <div id="trend" style="margin-top:4px;font-size:15px"></div>
  </div>
  <div class="muted">Это учебный технический анализ, а не финансовая рекомендация.</div>

<script>
const $ = id => document.getElementById(id);

const C = {card:'#131c2e', border:'#1f2a40', text:'#e2e8f0', muted:'#94a3b8',
           up:'#22c55e', down:'#ef4444', sma20:'#60a5fa', sma50:'#fbbf24', bb:'#a5b4fc', rsi:'#c084fc'};
const UPV = 'rgba(34,197,94,.45)', DNV = 'rgba(239,68,68,.45)';
const UPH = 'rgba(34,197,94,.6)', DNH = 'rgba(239,68,68,.6)';
const VISIBLE_BARS = 90;
const KEYS = ['t','o','h','l','c','v','sma20','sma50','bbUp','bbLow','rsi','macd','signal','hist'];
let charts = [], S = {}, N = 0, idxByTime = {}, DATA = null;
let CUR = null, timer = null, idle = 0, lastSig = '', reqId = 0;

function mk(el, height, extra) {
  const chart = LightweightCharts.createChart(el, Object.assign({
    width: el.clientWidth, height: height,
    layout: { background: { type: 'solid', color: C.card }, textColor: C.muted, fontSize: 11 },
    grid: { vertLines: { color: C.border }, horzLines: { color: C.border } },
    rightPriceScale: { borderColor: C.border },
    timeScale: { borderColor: C.border, rightOffset: 4 },
    crosshair: { mode: 0 },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
  }, extra || {}));
  charts.push({ chart: chart, el: el });
  return chart;
}

function line(chart, color, width, opts) {
  return chart.addLineSeries(Object.assign({
    color: color, lineWidth: width, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false }, opts || {}));
}

function fmtTime(t) {
  if (typeof t === 'object' && t) {
    const p = n => String(n).padStart(2, '0');
    return t.year + '-' + p(t.month) + '-' + p(t.day);
  }
  return t;
}

function fmtDT(t) {
  return typeof t === 'number' ? new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') : t;
}

function setLegend(i) {
  const d = DATA, prev = i > 0 ? d.c[i - 1] : d.o[i], chg = d.c[i] - prev;
  const cls = chg >= 0 ? 'up' : 'down';
  $('legend').innerHTML = fmtDT(d.t[i]) + '<br>O ' + d.o[i].toFixed(2) + '  H ' + d.h[i].toFixed(2) +
    '  L ' + d.l[i].toFixed(2) + '  C ' + d.c[i].toFixed(2) +
    ' <span class="' + cls + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '</span>';
}

function resetView() {
  const r = { from: Math.max(0, N - VISIBLE_BARS) - 0.5, to: N + 4 };
  charts.forEach(o => o.chart.timeScale().setVisibleLogicalRange(r));
}

const wsp = (t, v) => v === null ? { time: t } : { time: t, value: v };
const histPt = (t, v) => v === null ? { time: t } :
  { time: t, value: v, color: v >= 0 ? UPH : DNH };

function render(d) {
  charts.forEach(o => o.chart.remove());
  charts = []; S = {}; DATA = d; N = d.t.length;
  idxByTime = {}; d.t.forEach((t, i) => idxByTime[t] = i);
  const T = d.t, intra = !!d.intraday;
  const ser = arr => arr.map((v, i) => wsp(T[i], v));
  const hiddenAxis = { timeScale: { borderColor: C.border, rightOffset: 4, visible: false } };

  // ---- основной график ----
  const main = mk($('main'), 360, hiddenAxis);
  S.candles = main.addCandlestickSeries({
    upColor: C.up, downColor: C.down, borderVisible: false,
    wickUpColor: C.up, wickDownColor: C.down });
  S.candles.setData(T.map((t, i) => ({ time: t, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] })));
  main.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });

  S.vol = main.addHistogramSeries({
    priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  S.vol.setData(T.map((t, i) => ({ time: t, value: d.v[i], color: d.c[i] >= d.o[i] ? UPV : DNV })));

  S.sma20 = line(main, C.sma20, 2); S.sma20.setData(ser(d.sma20));
  S.sma50 = line(main, C.sma50, 2); S.sma50.setData(ser(d.sma50));
  S.bb1 = line(main, 'rgba(165,180,252,.55)', 1); S.bb1.setData(ser(d.bbUp));
  S.bb2 = line(main, 'rgba(165,180,252,.55)', 1); S.bb2.setData(ser(d.bbLow));
  toggles();

  // ---- RSI ----
  const rsiC = mk($('rsi'), 110, hiddenAxis);
  S.rsi = line(rsiC, C.rsi, 2, { autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
  S.rsi.setData(ser(d.rsi));
  S.rsi.createPriceLine({ price: 70, color: 'rgba(239,68,68,.7)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  S.rsi.createPriceLine({ price: 30, color: 'rgba(34,197,94,.7)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });

  // ---- MACD (внизу - ось времени) ----
  const macdC = mk($('macd'), 130, { timeScale: { borderColor: C.border, rightOffset: 4,
                                                   timeVisible: intra, secondsVisible: false } });
  S.hist = macdC.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  S.hist.setData(d.hist.map((v, i) => histPt(T[i], v)));
  S.macdL = line(macdC, C.sma20, 1.5); S.macdL.setData(ser(d.macd));
  S.sigL = line(macdC, C.sma50, 1.5); S.sigL.setData(ser(d.signal));

  // ---- синхронизация прокрутки и масштаба ----
  let busy = false;
  charts.forEach(src => {
    src.chart.timeScale().subscribeVisibleLogicalRangeChange(r => {
      if (busy || !r) return;
      busy = true;
      charts.forEach(o => { if (o !== src) o.chart.timeScale().setVisibleLogicalRange(r); });
      busy = false;
    });
  });

  // ---- строка данных свечи ----
  main.subscribeCrosshairMove(p => {
    if (!p || !p.time) { setLegend(N - 1); return; }
    const i = idxByTime[fmtTime(p.time)];
    setLegend(i === undefined ? N - 1 : i);
  });
  setLegend(N - 1);
  resetView();
}

// Живое обновление: меняем последнюю свечу и добавляем новые, не сбивая положение графика
function pushPoint(d, i) {
  const t = d.t[i];
  S.candles.update({ time: t, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] });
  S.vol.update({ time: t, value: d.v[i], color: d.c[i] >= d.o[i] ? UPV : DNV });
  S.sma20.update(wsp(t, d.sma20[i]));
  S.sma50.update(wsp(t, d.sma50[i]));
  S.bb1.update(wsp(t, d.bbUp[i]));
  S.bb2.update(wsp(t, d.bbLow[i]));
  S.rsi.update(wsp(t, d.rsi[i]));
  S.hist.update(histPt(t, d.hist[i]));
  S.macdL.update(wsp(t, d.macd[i]));
  S.sigL.update(wsp(t, d.signal[i]));
}

function applyUpdate(d) {
  const lastT = DATA.t[DATA.t.length - 1];
  const j = d.t.indexOf(lastT);
  if (j < 0) { render(d); return; }
  for (let i = j; i < d.t.length; i++) {
    if (i === j) { const last = DATA.t.length - 1; KEYS.forEach(k => { DATA[k][last] = d[k][i]; }); }
    else { KEYS.forEach(k => DATA[k].push(d[k][i])); }
    pushPoint(d, i);
  }
  N = DATA.t.length;
  idxByTime = {}; DATA.t.forEach((t, i) => idxByTime[t] = i);
  setLegend(N - 1);
}

function toggles() {
  if (!S.sma20) return;
  S.sma20.applyOptions({ visible: $('cSma20').checked });
  S.sma50.applyOptions({ visible: $('cSma50').checked });
  S.bb1.applyOptions({ visible: $('cBb').checked });
  S.bb2.applyOptions({ visible: $('cBb').checked });
}

function fillCards(d) {
  const up = d.change >= 0;
  $('name').textContent = d.ticker + '  •  ' + d.date + (d.intraday ? '  (время биржи)' : '');
  $('price').textContent = '$' + d.last.toFixed(2);
  const chg = $('chg');
  chg.textContent = (up ? '▲ ' : '▼ ') + (up ? '+' : '') + d.change.toFixed(2) +
    '  (' + (d.pct >= 0 ? '+' : '') + d.pct.toFixed(2) + '%)' + (d.intraday ? '  к пред. свече' : '');
  chg.className = up ? 'up' : 'down';

  const v = $('verdict');
  const icon = d.score >= 1 ? '▲ ' : d.score <= -1 ? '▼ ' : '► ';
  v.textContent = icon + d.verdict;
  v.className = 'verdict ' + (d.score >= 1 ? 'up' : d.score <= -1 ? 'down' : 'warn');
  $('score').textContent = 'Итоговый балл: ' + (d.score > 0 ? '+' : '') + d.score + ' из 5';

  const box = $('signals'); box.innerHTML = '';
  d.votes.forEach(([name, val]) => {
    const row = document.createElement('div'); row.className = 'sig';
    const dot = document.createElement('span');
    dot.className = 'dot ' + (val > 0 ? 'up' : val < 0 ? 'down' : 'muted'); dot.textContent = '●';
    const txt = document.createElement('span'); txt.textContent = name;
    row.appendChild(dot); row.appendChild(txt); box.appendChild(row);
  });
  $('trendLbl').textContent = 'ТРЕНД (30 СВЕЧЕЙ)';
  $('trend').innerHTML = '<span class="' + (d.slope >= 0 ? 'up' : 'down') + '">' +
    (d.slope >= 0 ? '+' : '') + d.slope.toFixed(2) + ' $/свеча</span><br>' +
    'Оценка через 5 свечей: $' + d.forecast.toFixed(2);
}

// ---------- загрузка и авто-обновление ----------
function nextDelay() {
  const base = $('tf').value === '1d' ? 300000 : 60000;
  return idle >= 5 ? Math.max(base, 300000) : base;
}

function showLive(d) {
  const el = $('liveinfo');
  if (!$('live').checked) { el.textContent = 'Авто-обновление выключено'; return; }
  const sec = nextDelay() / 1000;
  const slow = idle >= 5 ? '  •  новых данных нет (рынок, похоже, закрыт) - обновляю реже' : '';
  el.innerHTML = '<span class="pulse"></span>Live  •  обновлено ' + new Date().toLocaleTimeString() +
    '  •  следующее через ' + sec + ' с' + slow;
}

function trackIdle(d) {
  const n = d.t.length - 1;
  const sig = d.t[n] + '|' + d.c[n] + '|' + d.v[n];
  if (sig === lastSig) idle++; else { idle = 0; lastSig = sig; }
}

async function load(silent) {
  const t = $('ticker').value.trim().toUpperCase();
  if (!t) return;
  const tf = $('tf').value, period = $('period').value;
  const my = ++reqId;
  if (!silent) { $('go').disabled = true; $('err').textContent = 'Загружаю ' + t + '...'; }
  try {
    const r = await fetch('/api?ticker=' + encodeURIComponent(t) + '&period=' + period + '&tf=' + tf);
    const d = await r.json();
    if (my !== reqId) return;
    if (d.error) {
      $('err').textContent = d.error;
    } else {
      $('err').textContent = '';
      fillCards(d);
      const same = CUR && CUR.t === t && CUR.tf === tf && CUR.period === period && charts.length > 0;
      if (silent && same) { applyUpdate(d); }
      else { render(d); CUR = { t: t, tf: tf, period: period }; idle = 0; lastSig = ''; }
      trackIdle(d);
      showLive(d);
    }
  } catch (e) {
    if (my === reqId) $('err').textContent = 'Ошибка: ' + e;
  }
  if (!silent && my === reqId) $('go').disabled = false;
}

function schedule() {
  clearTimeout(timer);
  if ($('live').checked) timer = setTimeout(tick, nextDelay());
}
async function tick() {
  if (!document.hidden) await load(true);
  schedule();
}
async function manual() {
  clearTimeout(timer);
  await load(false);
  schedule();
}

$('go').onclick = manual;
$('tf').onchange = () => { $('period').disabled = $('tf').value !== '1d'; manual(); };
$('period').onchange = manual;
$('live').onchange = () => { schedule(); showLive(); };
$('reset').onclick = () => { if (N) resetView(); };
$('ticker').addEventListener('keydown', e => { if (e.key === 'Enter') manual(); });
['cSma20', 'cSma50', 'cBb'].forEach(id => $(id).onchange = toggles);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && $('live').checked) { clearTimeout(timer); tick(); }
});
window.addEventListener('resize', () => charts.forEach(o => o.chart.applyOptions({ width: o.el.clientWidth })));

const api = (path, body) => fetch(path, {
  method: body === undefined ? 'GET' : 'POST',
  headers: { 'X-App': '1', 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body)
}).then(r => r.json());
let PROF = {}, STARTED = false, logoutArmed = false;

function showAuth(step) {
  clearTimeout(timer);
  $('auth').style.display = 'block';
  $('step1').style.display = step === 1 ? 'block' : 'none';
  $('step2').style.display = step === 2 ? 'block' : 'none';
  $('helpbox').style.display = 'none';
  $('autherr').textContent = '';
  $('ukey').value = '';
  $('back2').style.display = PROF.done ? 'block' : 'none';
  $('skip').style.display = PROF.done ? 'none' : 'block';
}
function startApp() {
  $('auth').style.display = 'none';
  $('who').textContent = '👤 ' + PROF.name + (PROF.hasKey ? ' · ключ ...' + PROF.keyTail : ' · без ключа');
  STARTED = true;
  manual();
}
async function saveProfile(data, nextFn) {
  try {
    const r = await api('/profile', data);
    if (r.error) { $('autherr').textContent = r.error; return; }
    PROF = r; nextFn();
  } catch (e) { $('autherr').textContent = 'Ошибка: ' + e; }
}
$('next1').onclick = () => {
  const n = $('uname').value.trim();
  if (!n) { $('autherr').textContent = 'Введите имя.'; return; }
  saveProfile({ name: n }, () => showAuth(2));
};
$('uname').addEventListener('keydown', e => { if (e.key === 'Enter') $('next1').click(); });
$('help').onclick = () => { $('helpbox').style.display = $('helpbox').style.display === 'none' ? 'block' : 'none'; };
$('openkey').onclick = () => { api('/open', { url: 'https://twelvedata.com/register' }).catch(() => {}); };
$('save2').onclick = () => {
  const k = $('ukey').value.trim();
  if (!k) { $('autherr').textContent = 'Вставьте ключ или нажмите "Пропустить".'; return; }
  saveProfile({ key: k, done: true }, startApp);
};
$('skip').onclick = () => saveProfile({ done: true }, startApp);
$('back2').onclick = () => startApp();
$('chkey').onclick = () => showAuth(2);
$('logout').onclick = async () => {
  if (!logoutArmed) {
    logoutArmed = true; $('logout').textContent = 'Точно выйти?';
    setTimeout(() => { logoutArmed = false; $('logout').textContent = 'Выйти'; }, 3000);
    return;
  }
  logoutArmed = false; $('logout').textContent = 'Выйти';
  try { await api('/logout', {}); } catch (e) {}
  PROF = {}; showAuth(1);
};
async function boot() {
  try { PROF = await api('/profile'); } catch (e) { PROF = {}; }
  if (!PROF.name) showAuth(1);
  else if (!PROF.done) showAuth(2);
  else startApp();
}
boot();
</script>
</body>
</html>
"""


# =====================================================================
#                              СЕРВЕР
# =====================================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/profile":
            if self.headers.get("X-App") != "1":
                return self._send(403, b"forbidden", "text/plain")
            self._json(public_profile(load_profile()))
        elif u.path == "/api":
            qs = parse_qs(u.query)
            ticker = qs.get("ticker", ["AMZN"])[0].strip().upper()
            period = qs.get("period", ["1y"])[0]
            tf = qs.get("tf", ["1d"])[0]
            try:
                if not re.fullmatch(r"[A-Z0-9.\-=^]{1,12}", ticker):
                    raise ValueError("Некорректный тикер.")
                if period not in PERIODS:
                    raise ValueError("Некорректный период.")
                if tf not in TFS:
                    raise ValueError("Некорректный таймфрейм.")
                apikey = load_profile().get("key", "")
                payload = build_cached(ticker, period, tf, apikey)
            except Exception as e:
                payload = {"error": str(e)}
            body = json.dumps(payload).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self):
        # Заголовок X-App не отправить с чужой веб-страницы без разрешения сервера.
        if self.headers.get("X-App") != "1":
            return self._send(403, b"forbidden", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Неверный запрос.")
            path = urlparse(self.path).path
            if path == "/profile":
                self._json(update_profile(data))
            elif path == "/logout":
                delete_profile()
                self._json({})
            elif path == "/open":
                if data.get("url") != KEY_URL:
                    raise ValueError("Ссылка не разрешена.")
                OPEN_URL(KEY_URL)
                self._json({"ok": True})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)})

    def log_message(self, *args):  # тишина в консоли
        pass


def main():
    server, port = None, PORT
    for p in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("Не нашёл свободный порт. Перезапустите Pydroid 3.")
        return
    url = f"http://127.0.0.1:{port}"
    print("Stock Analyzer запущен.")
    print("Откройте в браузере:", url)
    print("Чтобы остановить - нажмите ■ в Pydroid 3.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
