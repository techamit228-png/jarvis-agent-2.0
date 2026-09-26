# JARVIS SYSTEM AGENT (fixed/merged) — локальный AI-помощник Windows
# Python 3.10+ / Windows 10-11
#
# Установка зависимостей:
#   pip install requests psutil pyautogui pycaw comtypes SpeechRecognition pyaudio pyttsx3 edge-tts playsound==1.2.2
#
# Запуск Ollama (в отдельном терминале, если ещё не запущен):
#   ollama pull llama3.2:3b
#   ollama serve
#
# Что исправлено по сравнению с твоими v2/v3:
# 1. Модель почти всегда не знала ТЕКУЩУЮ громкость -> просьбы вроде
#    "сделай погромче" ломались или ставили случайный %. Теперь текущая
#    громкость передаётся модели в каждом снимке системы, и добавлен
#    инструмент adjust_volume(delta) для ОТНОСИТЕЛЬНЫХ изменений.
# 2. Список инструментов (tool) теперь жёстко ограничен через enum в
#    JSON-schema — маленькая модель (3B) больше не может придумать
#    несуществующий инструмент.
# 3. Добавлен цикл из нескольких шагов на один запрос пользователя:
#    сначала read-only инструменты (inspect_system/discover_apps), затем
#    одно "изменяющее" действие с подтверждением — как в v2, но с надёжным
#    структурированным JSON из v3.
# 4. Если pycaw не установлен, set_volume/adjust_volume не падают "тихо",
#    а либо честно просят поставить pycaw, либо (для adjust_volume)
#    используют запасной вариант через медиаклавиши.
# 5. discover_apps теперь ищет и в Program Files/AppData, и в PATH, и в
#    ярлыках меню "Пуск" — объединение подходов v2 и v3.

import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import queue
from pathlib import Path

# Печатать сразу, без буферизации — иначе в некоторых консолях/IDE кажется,
# что программа "молчит", хотя она просто ждёт вывода.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

def log(*a, **kw):
    kw.setdefault("flush", True)
    print(*a, **kw)

# Очередь статусов для GUI-окна (Ожидание/Слушаю/Думаю/Говорю).
# Работает и без GUI — если окно не запущено, никто эту очередь не читает,
# она просто не используется.
_STATUS_QUEUE = queue.Queue()

def set_status(text):
    _STATUS_QUEUE.put(text)

try:
    import requests
    import psutil
except ImportError as e:
    print("Не хватает библиотеки:", e)
    print("Установи: pip install requests psutil pyautogui pycaw comtypes SpeechRecognition pyaudio pyttsx3")
    raise SystemExit

try:
    import speech_recognition as sr
except ImportError:
    sr = None

WAKE_WORDS = ["джарвис", "jarvis", "дживс", "джарвиз"]  # варианты, как распознавание может услышать слово

# Голос: ru-RU-DmitryNeural — глубокий мужской нейро-голос (через edge-tts,
# бесплатный сервис Microsoft), звучит гораздо солиднее и "по-джарвисовски",
# чем стандартный робо-голос Windows (pyttsx3). Если интернета нет или
# edge-tts недоступен — тихо откатываемся на pyttsx3.
EDGE_VOICE = os.environ.get("JARVIS_VOICE", "ru-RU-DmitryNeural")

DONE_PHRASES = ["Готово, сэр.", "Выполнено, сэр.", "Сделано, сэр.", "Задача выполнена, сэр."]
FAIL_PHRASES = ["Не получилось, сэр.", "Возникла ошибка, сэр.", "Не удалось выполнить, сэр."]

_TTS_ENGINE = None

def _get_tts():
    """Запасной робо-голос Windows (pyttsx3), если edge-tts не сработал."""
    global _TTS_ENGINE
    if _TTS_ENGINE is None:
        try:
            import pyttsx3
            _TTS_ENGINE = pyttsx3.init()
            _TTS_ENGINE.setProperty("rate", 175)
        except Exception:
            _TTS_ENGINE = False
    return _TTS_ENGINE or None


def _speak_edge_tts(text):
    import asyncio
    import edge_tts
    from playsound import playsound

    path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name

    async def _gen():
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(path)

    asyncio.run(_gen())
    playsound(path)
    try:
        os.remove(path)
    except Exception:
        pass


def speak(text):
    """Печатает и озвучивает ответ. Сначала пробует "голос Джарвиса"
    (edge-tts), при ошибке — обычный голос Windows (pyttsx3)."""
    text = (text or "").strip()
    if not text:
        return
    log("JARVIS:", text)
    set_status("🗣️  Говорю...")
    try:
        _speak_edge_tts(text)
        return
    except Exception:
        pass
    engine = _get_tts()
    if engine:
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception:
            pass

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = os.environ.get("JARVIS_MODEL", "llama3.2:3b")

# ---------------- READ-ONLY: СИСТЕМА ----------------

def get_volume():
    """Текущая громкость и mute-статус. Требует pycaw."""
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        volume = device.EndpointVolume
        return {
            "ok": True,
            "percent": round(volume.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(volume.GetMute()),
        }
    except ImportError:
        return {"ok": False, "error": "pycaw не установлен (pip install pycaw comtypes)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def system_snapshot_light():
    """Маленький и быстрый снимок — это то, что уходит в модель на КАЖДЫЙ запрос.
    Без списка процессов: он тяжёлый и почти всегда не нужен для команд типа
    громкости/запуска приложений."""
    vm = psutil.virtual_memory()
    return {
        "os": platform.platform(),
        "computer": platform.node(),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "ram_available_gb": round(vm.available / 1024**3, 1),
        "volume": get_volume(),
    }


def inspect_system():
    """Полный снимок (с процессами) — доступен модели ТОЛЬКО по явному вызову
    инструмента inspect_system, не подмешивается автоматически в контекст."""
    vm = psutil.virtual_memory()
    return {
        "os": platform.platform(),
        "computer": platform.node(),
        "cpu": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "ram_available_gb": round(vm.available / 1024**3, 1),
        "volume": get_volume(),
        "running_processes": sorted(
            {p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]}
        )[:120],
    }


def _start_menu_dirs():
    dirs = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    return [p for p in dirs if p.exists()]


# Папки, которые никогда не стоит обходить: они огромные, системные, часто
# содержат junction/reparse-точки, из-за которых рекурсивный обход мог
# зацикливаться и идти "бесконечно".
_SKIP_DIR_NAMES = {
    "windowsapps", "temp", "tmp", "cache", "caches", "node_modules",
    "$recycle.bin", "system volume information", "packages",
    ".git", "appdata", "onedrive",
}


def _walk_for_exe(root, name_filter, time_budget=3.0, max_results=40):
    """Ограниченный по времени обход БЕЗ следования по симлинкам/junction-ам
    (os.walk по умолчанию их не разворачивает — в отличие от Path.rglob,
    который может зациклиться на Windows-junction-ах и висеть бесконечно)."""
    results = []
    if not root or not root.exists():
        return results
    start = time.time()
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if time.time() - start > time_budget or len(results) >= max_results:
                break
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIR_NAMES]
            for fn in filenames:
                fnl = fn.lower()
                if fnl.endswith(".exe") and (not name_filter or name_filter in fnl):
                    results.append(str(Path(dirpath) / fn))
                    if len(results) >= max_results:
                        break
    except (OSError, PermissionError):
        pass
    return results


def discover_apps(query=""):
    """Read-only: реальные .exe/ярлыки на этом ПК. query фильтрует по имени.
    Специально ограничено по времени, чтобы никогда не "зависать"."""
    q = (query or "").strip().lower()
    found = []

    # 1) Ярлыки в меню "Пуск" — обычно небольшая папка, но всё равно с лимитом времени
    for base in _start_menu_dirs():
        start = time.time()
        try:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                if time.time() - start > 2.0:
                    break
                for fn in filenames:
                    if fn.lower().endswith(".lnk"):
                        name = Path(fn).stem
                        if not q or q in name.lower():
                            found.append({"name": name, "path": str(Path(dirpath) / fn), "type": "shortcut"})
        except (OSError, PermissionError):
            pass

    # 2) PATH — мгновенно, диск не сканирует
    common_names = ["chrome", "firefox", "msedge", "opera", "steam",
                     "discord", "code", "notepad", "explorer", "vlc", "spotify"]
    names_to_check = [q] if q else common_names
    for name in names_to_check:
        for candidate in (name, name if name.endswith(".exe") else name + ".exe"):
            hit = shutil.which(candidate)
            if hit:
                found.append({"name": Path(hit).stem, "path": hit, "type": "path"})

    # 3) Ограниченный обход Program Files — только если явно задан query,
    #    и только с жёстким бюджетом времени на каждый корень.
    if q:
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ]
        for root in roots:
            for p in _walk_for_exe(root, q, time_budget=3.0, max_results=10):
                found.append({"name": Path(p).stem, "path": p, "type": "exe"})

    seen, unique = set(), []
    for x in found:
        key = x["path"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(x)
    return unique[:60]


def inspect_path(path):
    p = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not p.exists():
        return {"exists": False, "path": str(p)}
    st = p.stat()
    return {
        "exists": True, "path": str(p.resolve()), "is_file": p.is_file(),
        "is_dir": p.is_dir(), "size": st.st_size if p.is_file() else None,
    }

# ---------------- ИЗМЕНЯЮЩИЕ ДЕЙСТВИЯ ----------------

def set_volume(percent):
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        volume = device.EndpointVolume
        pct = max(0, min(100, int(percent)))
        volume.SetMasterVolumeLevelScalar(pct / 100.0, None)
        return {"ok": True, "percent": pct}
    except ImportError:
        return {"ok": False, "error": "Нужен pycaw. Выполни: pip install pycaw comtypes"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def adjust_volume(delta):
    """Относительное изменение громкости, например +10 или -15."""
    info = get_volume()
    if info.get("ok"):
        new_pct = max(0, min(100, info["percent"] + int(delta)))
        return set_volume(new_pct)
    # Запасной вариант без pycaw: медиаклавиши (приблизительно)
    try:
        import pyautogui
        key = "volumeup" if delta > 0 else "volumedown"
        presses = max(1, round(abs(int(delta)) / 2))
        pyautogui.press(key, presses=presses, interval=0.02)
        return {"ok": True, "approx": True, "note": f"Нажал {key} {presses} раз(а); точный % недоступен без pycaw"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_app(path):
    if not path or not Path(path).exists():
        return {"ok": False, "error": "Файл приложения не найден"}
    try:
        if str(path).lower().endswith(".lnk"):
            os.startfile(path)
        else:
            subprocess.Popen([path], shell=False)
        return {"ok": True, "launched": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_url(url):
    import webbrowser
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    webbrowser.open(url)
    return {"ok": True, "url": url}


def press_key(key):
    import pyautogui
    pyautogui.press(key)
    return {"ok": True}


def hotkey(keys):
    import pyautogui
    pyautogui.hotkey(*keys)
    return {"ok": True}


def type_text(text):
    import pyautogui
    pyautogui.write(text, interval=0.01)
    return {"ok": True}


_KNOWN_APPS_CACHE = None

def get_known_apps(force_refresh=False):
    """Базовый список приложений (ярлыки меню "Пуск" + PATH) — без глубокого
    обхода Program Files, поэтому быстро (пара секунд максимум) и без риска
    зависнуть. Кэшируем, чтобы не пересканировать на каждый запрос."""
    global _KNOWN_APPS_CACHE
    if force_refresh or _KNOWN_APPS_CACHE is None:
        log("🔍 Сканирую установленные приложения...")
        _KNOWN_APPS_CACHE = discover_apps()
        log(f"   Готово, найдено {len(_KNOWN_APPS_CACHE)} приложений.")
    return _KNOWN_APPS_CACHE


READ_ONLY = {"inspect_system", "discover_apps", "inspect_path"}
CHANGE = {"set_volume", "adjust_volume", "open_app", "open_url", "key", "hotkey", "type_text"}
ALL_TOOLS = sorted(READ_ONLY | CHANGE)


def execute(tool, args):
    if tool == "inspect_system": return inspect_system()
    if tool == "discover_apps": return discover_apps(args.get("query", ""))
    if tool == "inspect_path": return inspect_path(args.get("path", ""))
    if tool == "set_volume": return set_volume(args.get("percent", 50))
    if tool == "adjust_volume": return adjust_volume(args.get("delta", 0))
    if tool == "open_app": return open_app(args.get("path", ""))
    if tool == "open_url": return open_url(args.get("url", ""))
    if tool == "key": return press_key(args.get("key", ""))
    if tool == "hotkey": return hotkey(args.get("keys", []))
    if tool == "type_text": return type_text(args.get("text", ""))
    return {"ok": False, "error": "Неизвестный инструмент"}

# ---------------- МОДЕЛЬ ----------------

SYSTEM_PROMPT = """Ты JARVIS, локальный помощник Windows.
Ты обязан вернуть РОВНО один JSON-объект и ничего кроме него.

Формат:
{"message":"короткий ответ пользователю","action":null}
или
{"message":"что собираешься сделать","action":{"tool":"название","args":{...}}}

Инструменты (используй ТОЛЬКО эти имена в поле tool):
- inspect_system: read-only, args {} — ОС/CPU/RAM/процессы/ТЕКУЩАЯ громкость и mute-статус (поле volume).
- discover_apps: read-only, args {"query": "часть имени приложения"} — найти реальные .exe/ярлыки.
  ВСЕГДА указывай непустой query (например "steam", "chrome") — без него глубокий поиск в
  Program Files не выполняется, чтобы не тормозить систему.
- inspect_path: read-only, args {"path": "..."}.
- set_volume: изменить громкость на АБСОЛЮТНОЕ значение, args {"percent": 0..100}.
- adjust_volume: изменить громкость ОТНОСИТЕЛЬНО текущей, args {"delta": -100..100}. Используй это для просьб
  вроде "сделай погромче/потише", "прибавь/убавь громкость" — НЕ пытайся угадать абсолютное число сам.
- open_app: запустить найденный (через discover_apps) exe/ярлык, args {"path":"C:\\..."}.
- open_url: открыть URL, args {"url":"https://..."}.
- key: нажать клавишу, args {"key":"..."}.
- hotkey: сочетание клавиш, args {"keys":["ctrl","c"]}.
- type_text: ввести текст, args {"text":"..."}.

Правила:
1. Текущая громкость и mute-статус уже есть в снимке системы (system_snapshot.volume) — не спрашивай их
   отдельным инструментом и не выдумывай значение.
2. Для запуска приложения сначала посмотри known_apps в снимке или вызови discover_apps, если нужного там нет.
   Никогда не выдумывай путь к .exe.
3. Любое изменяющее действие: верни action, программа сама запросит разрешение пользователя — не спрашивай
   разрешение в тексте message.
4. Если запрос нельзя выполнить доступными инструментами — action должен быть null, объясни почему в message.
5. За один ответ — не больше одного действия в поле action.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "action": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": ALL_TOOLS},
                        "args": {"type": "object"},
                    },
                    "required": ["tool", "args"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["message", "action"],
    "additionalProperties": False,
}


def ollama(messages):
    log("🧠 Думаю (жду ответ от Ollama)...")
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0.1},
        }, timeout=180)
    except requests.exceptions.Timeout:
        log("JARVIS: Ollama слишком долго не отвечает (таймаут 180с). "
            "Возможно, модель перегружена или контекст слишком большой.")
        return ""
    except requests.exceptions.ConnectionError:
        log("JARVIS: не могу подключиться к Ollama на http://127.0.0.1:11434. "
            "Убедись, что выполнено 'ollama serve'.")
        return ""
    if not r.ok:
        log(f"JARVIS: Ollama вернул ошибку {r.status_code}: {r.text[:300]}")
        return ""
    return r.json()["message"]["content"]


def parse_json(raw):
    if not raw:
        return None
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        m = re.search(r"\{.*\}", clean, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


_VOICE_RECOGNIZER = None  # выставляется voice_loop(), пока голосовой режим активен
_VOICE_MIC = None

YES_WORDS = ("да", "давай", "подтверждаю", "конечно", "ага", "yes", "sure")
NO_WORDS = ("нет", "не надо", "отмена", "стоп", "no")


def confirm(tool, args):
    log(f"\n⚠️  JARVIS хочет выполнить: {tool} {json.dumps(args, ensure_ascii=False)}")
    if _VOICE_RECOGNIZER is not None and _VOICE_MIC is not None:
        speak("Подтверждаете, сэр?")
        answer = _listen_once(_VOICE_RECOGNIZER, _VOICE_MIC, timeout=6, phrase_time_limit=4)
        answer = (answer or "").lower()
        if any(w in answer for w in YES_WORDS):
            return True
        if any(w in answer for w in NO_WORDS):
            return False
        speak("Не расслышал ответ, отменяю на всякий случай, сэр.")
        return False
    # Резервный вариант, если голосовой режим не запущен (например, при отладке).
    return input("Разрешить? [д/н]: ").strip().lower() in ("д", "да", "y", "yes")


def check_ollama():
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        if not r.ok:
            return False
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(MODEL in m for m in models):
            print(f"Модель {MODEL} не найдена. Выполни: ollama pull {MODEL}")
            return False
        return True
    except Exception:
        print("Ollama не отвечает на http://127.0.0.1:11434 — убедись, что 'ollama serve' запущен.")
        return False


def process_user_turn(user_text, speak_reply=False):
    # Каждый запрос строится с нуля — НЕ накапливаем историю разговора.
    # Это системный агент, а не чат: старая история только раздувала запрос
    # к модели с каждым разом и делала ответ всё медленнее.
    context = {"system_snapshot": system_snapshot_light(), "known_apps": get_known_apps()}
    conv = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Актуальные read-only данные ПК:\n" + json.dumps(context, ensure_ascii=False)},
        {"role": "user", "content": user_text},
    ]

    for step in range(4):  # до 4 шагов рассуждения на один запрос
        set_status("🧠 Думаю...")
        raw = ollama(conv)
        if not raw:
            # ollama() уже вывел причину (таймаут / нет соединения / ошибка HTTP)
            return
        conv.append({"role": "assistant", "content": raw})
        obj = parse_json(raw)
        if obj is None:
            log("JARVIS: не удалось разобрать ответ модели.")
            log("RAW:", raw)
            return
        msg = obj.get("message")
        if msg:
            speak(msg) if speak_reply else log("JARVIS:", msg)
        action = obj.get("action")
        if not action:
            return
        tool, args = action.get("tool"), action.get("args") or {}
        if tool not in ALL_TOOLS:
            log(f"JARVIS: модель выбрала неизвестный инструмент «{tool}».")
            return
        if tool in CHANGE and not confirm(tool, args):
            log("JARVIS: действие отменено.")
            if speak_reply:
                speak("Отменено, сэр.")
            return
        set_status(f"⚙️  Выполняю: {tool}")
        log(f"⚙️  Выполняю: {tool}...")
        result = execute(tool, args)
        log("[РЕЗУЛЬТАТ]", json.dumps(result, ensure_ascii=False))
        if tool in CHANGE and speak_reply:
            speak(random.choice(DONE_PHRASES) if result.get("ok") else random.choice(FAIL_PHRASES))
        if tool in READ_ONLY:
            conv.append({
                "role": "user",
                "content": f"Результат инструмента {tool}:\n{json.dumps(result, ensure_ascii=False)}\n"
                           "Продолжай: вызови следующий нужный инструмент или дай финальный ответ (action:null).",
            })
            continue
        return  # одно изменяющее действие за запрос — и стоп
    log("JARVIS: превышен лимит шагов на этот запрос.")


def _listen_once(recognizer, mic, timeout=None, phrase_time_limit=6):
    """Слушает одну фразу с микрофона и распознаёт её (нужен интернет —
    используется бесплатное Google-распознавание речи)."""
    with mic as source:
        try:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        except sr.WaitTimeoutError:
            return None
    try:
        return recognizer.recognize_google(audio, language="ru-RU")
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        log(f"JARVIS: ошибка распознавания речи (проверь интернет): {e}")
        return None


def voice_loop():
    global _VOICE_RECOGNIZER, _VOICE_MIC
    if sr is None:
        log("Для голосового режима нужны библиотеки. Установи:")
        log("  pip install SpeechRecognition pyaudio")
        set_status("❌ Нет библиотек для голоса")
        return
    try:
        recognizer = sr.Recognizer()
        recognizer.pause_threshold = 0.8
        mic = sr.Microphone()
    except Exception as e:
        log("JARVIS: не удалось открыть микрофон:", e)
        set_status("❌ Микрофон не найден")
        return

    _VOICE_RECOGNIZER, _VOICE_MIC = recognizer, mic  # даёт confirm() возможность спрашивать голосом

    set_status("🎙️  Калибрую микрофон...")
    log("🎙️  Калибрую микрофон под уровень шума (помолчи секунду)...")
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
    log("🎙️  Голосовой режим включён. Скажи «Джарвис» + команду, например:")
    log('     «Джарвис, поставь громкость 70»')
    speak("Голосовой режим включён. Я к вашим услугам, сэр.")

    while True:
        set_status("👂 Жду слово «Джарвис»...")
        log("👂 Жду слово «Джарвис»...")
        text = _listen_once(recognizer, mic, timeout=None, phrase_time_limit=6)
        if not text:
            continue
        low = text.lower()
        if not any(w in low for w in WAKE_WORDS):
            continue

        # Если команда сказана в той же фразе — вырезаем из неё слово-триггер.
        command = low
        for w in WAKE_WORDS:
            command = command.replace(w, " ")
        command = command.strip(" ,.!?")

        if not command:
            speak("Да, слушаю, сэр.")
            set_status("👂 Слушаю команду...")
            command = _listen_once(recognizer, mic, timeout=6, phrase_time_limit=8)
            if not command:
                speak("Не расслышал, повторите, сэр.")
                continue

        clean = command.strip(" .,!?").lower()
        if clean in ("выключись", "заверши работу", "завершить работу", "выход из программы", "shutdown"):
            speak("До свидания, сэр.")
            os._exit(0)

        log(f"🗣️  Распознано: {command}")
        set_status(f"🗣️  {command}")
        try:
            process_user_turn(command, speak_reply=True)
        except Exception as e:
            log("JARVIS: ошибка:", e)
            speak("Произошла ошибка, сэр.")


def text_loop():
    print("\nJARVIS: Текстовый режим. /voice — голосовой режим, /quit — выход.")
    while True:
        try:
            user = input("\nТы: ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not user:
            continue
        low = user.lower()
        if low == "/quit":
            break
        if low == "/system":
            print(json.dumps(inspect_system(), ensure_ascii=False, indent=2))
            continue
        if low == "/volume":
            print(json.dumps(get_volume(), ensure_ascii=False, indent=2))
            continue
        if low == "/apps":
            get_known_apps(force_refresh=True)
            continue
        if low == "/voice":
            voice_loop()
            continue
        try:
            process_user_turn(user)
        except Exception as e:
            print("JARVIS: ошибка:", e)


def run_gui():
    """Небольшое плавающее окно-статус: показывает, что JARVIS сейчас делает
    (ожидание/слушаю/думаю/говорю). tkinter — часть стандартного Python,
    отдельно ставить не нужно."""
    import tkinter as tk

    root = tk.Tk()
    root.title("JARVIS")
    root.geometry("260x260+60+60")
    root.configure(bg="#05060a")
    root.attributes("-topmost", True)
    try:
        root.overrideredirect(True)  # без стандартной рамки/заголовка Windows
    except Exception:
        pass

    canvas = tk.Canvas(root, width=260, height=260, bg="#05060a", highlightthickness=0)
    canvas.pack()

    canvas.create_text(130, 26, text="J A R V I S", fill="#39d8ff", font=("Consolas", 14, "bold"))
    circle = canvas.create_oval(90, 90, 170, 170, outline="#39d8ff", width=3)
    status_text = canvas.create_text(130, 232, text="Запуск...", fill="#7fe6ff", font=("Consolas", 10))

    close_btn = canvas.create_text(244, 14, text="✕", fill="#ff5566", font=("Consolas", 12, "bold"))
    canvas.tag_bind(close_btn, "<Button-1>", lambda e: os._exit(0))

    # Перетаскивание окна мышкой (раз уж убрали стандартную рамку)
    drag = {"x": 0, "y": 0}
    def start_drag(e):
        drag["x"], drag["y"] = e.x, e.y
    def do_drag(e):
        root.geometry(f"+{root.winfo_x() + e.x - drag['x']}+{root.winfo_y() + e.y - drag['y']}")
    canvas.bind("<ButtonPress-1>", start_drag)
    canvas.bind("<B1-Motion>", do_drag)

    pulse = {"i": 0}
    def animate():
        pulse["i"] = (pulse["i"] + 1) % 60
        r = 40 + 8 * abs(30 - pulse["i"]) / 30
        canvas.coords(circle, 130 - r, 130 - r, 130 + r, 130 + r)
        root.after(60, animate)
    animate()

    def poll_status():
        try:
            while True:
                text = _STATUS_QUEUE.get_nowait()
                canvas.itemconfig(status_text, text=text)
        except queue.Empty:
            pass
        root.after(120, poll_status)
    poll_status()

    root.mainloop()


def main():
    print("=" * 64)
    print("JARVIS SYSTEM AGENT — голосовой AI-помощник Windows")
    print("Скажи «Джарвис» + команду, например: «Джарвис, поставь громкость 70».")
    print("Скажи «Джарвис, заверши работу», чтобы выйти.")
    print("=" * 64)

    if not check_ollama():
        return

    get_known_apps()  # первое (быстрое) сканирование — сразу при старте, не во время запроса

    if sr is None:
        log("SpeechRecognition не установлен — работаю в текстовом режиме.")
        text_loop()
        return

    # Голосовой цикл — в фоновом потоке, GUI-окно — в основном (так требует tkinter).
    threading.Thread(target=voice_loop, daemon=True).start()
    try:
        run_gui()
    except Exception as e:
        log("GUI недоступен, работаю без окна:", e)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
