#!/usr/bin/env python3
"""
PyDM - Gerenciador de downloads estilo IDM
- Download segmentado (várias conexões por arquivo)
- Pausar / retomar (inclusive após fechar o programa)
- Fila com limite de downloads simultâneos
- Monitor da área de transferência (copiou um link? ele pergunta se quer baixar)
- Janela "Informação do arquivo de download" (categoria, pasta, descrição)
  ao capturar um link pelo navegador, pela área de transferência ou por "Adicionar URL"
- Janela de progresso ao iniciar (estado, velocidade, tempo restante e progresso por conexão)
  e janela "Download completo" (Abrir / Abrir com... / Abrir pasta / Fechar)
- Aba "Limitar velocidade" (limite em KB/s, ajustável durante o download)
- Aba "Opções ao concluir" (mensagem, desconectar modem, sair do PyDM, desligar/reiniciar/hibernar/suspender)
- Segmentação dinâmica: quando uma conexão termina, ela "rouba" metade do trecho
  restante da mais lenta, mantendo todas as conexões ocupadas até o fim
- Janela principal no estilo do IDM: menus (Tarefas, Arquivo, Downloads, Visualizar), barra de ferramentas
  com ícones, painel de categorias (Todos / Incompletos / Completos / Tarefas) e lista ordenável com colunas
- Fila principal, agendador (início/fim em horários e dias), limite de velocidade total, janela de Opções,
  download em lote, exportar/importar lista, propriedades, mover/renomear, baixar novamente
- Ao fechar a janela o PyDM continua na bandeja do sistema (botão direito no ícone > Sair para encerrar)
- Instância única: abrir o PyDM de novo só traz a janela existente de volta
Requer Python 3.8+ (tkinter já vem instalado na maioria dos sistemas).
Opcional: "pip install pystray pillow" habilita o ícone da bandeja. Sem eles, fechar a janela
encerra o programa, como antes.
"""

import os, re, json, time, threading, subprocess, sys, queue, copy, math, shutil, fnmatch, hashlib, glob, shlex, uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

APP_DIR = os.path.join(os.path.expanduser("~"), ".pydm")
DB = os.path.join(APP_DIR, "downloads.json")
CFG = os.path.join(APP_DIR, "settings.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CHUNK = 256 * 1024
MIN_SPLIT = 1024 * 1024  # só divide um trecho se ainda faltar pelo menos 1 MB nele
MAX_ACTIVE = 3
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # evita piscar um console ao chamar comandos no Windows
DONE_MODES = ("Desligar", "Hibernar", "Em espera", "Reiniciar")
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")

# Categorias padrão (iguais às do IDM). O usuário pode criar outras pelo botão "+".
DEFAULT_CATEGORIES = {
    "Compactados": {"folder": os.path.join(DEFAULT_FOLDER, "Compressed"),
                    "exts": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso", "cab"]},
    "Documentos": {"folder": os.path.join(DEFAULT_FOLDER, "Documents"),
                   "exts": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf",
                            "odt", "ods", "csv", "epub"]},
    "Música": {"folder": os.path.join(DEFAULT_FOLDER, "Music"),
               "exts": ["mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"]},
    "Programas": {"folder": os.path.join(DEFAULT_FOLDER, "Programs"),
                  "exts": ["exe", "msi", "apk", "dmg", "deb", "rpm", "bat", "msu"]},
    "Vídeo": {"folder": os.path.join(DEFAULT_FOLDER, "Video"),
              "exts": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp"]},
    "Geral": {"folder": DEFAULT_FOLDER, "exts": [], "remember": True},
}


# ---- padrões das novas opções (inspirados no IDM) ----
DEFAULT_CAPTURE_EXTS = ("3GP 7Z AAC ACE AIF APK ARJ ASF AVI BIN BZ2 EXE GZ GZIP IMG ISO LZH M4A M4V MKV MOV "
                        "MP3 MP4 MPA MPE MPEG MPG MSI MSU OGG OGV PDF PLJ PPS PPT QT R0* R1* RA RAR RM RMVB "
                        "SEA SIT SITX TAR TIF TIFF WAV WMA WMV Z ZIP").split()
DEFAULT_EXCLUDE_SITES = ["*.update.microsoft.com", "download.windowsupdate.com", "*.download.windowsupdate.com",
                         "siteseal.thawte.com", "ecom.cimetz.com", "*.voice2page.com"]
# Ordem = ordem de detecção pelo User-Agent (Edge e Opera também têm "Chrome/" no UA, então vêm antes dele)
DEFAULT_BROWSERS = [
    {"name": "Microsoft Edge", "ua": "Edg/", "on": True, "builtin": True},
    {"name": "Opera", "ua": "OPR/", "on": True, "builtin": True},
    {"name": "Mozilla Firefox", "ua": "Firefox/", "on": True, "builtin": True},
    {"name": "Google Chrome", "ua": "Chrome/", "on": True, "builtin": True},
    {"name": "Apple Safari", "ua": "Safari/", "on": True, "builtin": True},
]
DYNAMIC_EXTS = {"PHP", "PHP3", "PHTML", "ASP", "ASPX", "JSP", "JSPX", "CGI", "PL", "DO", "ACTION"}
KEY_CHOICES = ("Nenhuma", "Alt", "Ctrl", "Shift", "Ctrl+Alt", "Ctrl+Shift", "Alt+Shift")
DEFAULT_KEYS = {"prevent": "Alt", "force": "Ctrl+Alt"}
DEFAULT_MENU = {"link": True, "video": True, "audio": True, "image": True, "page": False}
MENU_LABELS = (("link", "Links"), ("video", "Vídeos"), ("audio", "Áudios"), ("image", "Imagens"),
               ("page", "Página (clique em área vazia)"))
DEFAULT_MENU_TITLE = "Baixar com PyDM"
DEFAULT_PROGRESS_UI = {"speed_tab": True, "done_tab": True, "details": True}
DEFAULT_AV = {"enabled": False, "program": "", "args": '-Scan -ScanType 3 -File "{file}"'}
DUP_ACTIONS = (("ask", "Mostrar uma caixa de diálogo e perguntar o que fazer"),
               ("rename", "Adicionar mesmo assim (com outro nome se o arquivo já existir)"),
               ("ignore", "Ignorar o link duplicado"))
BIN_DIR = os.path.join(APP_DIR, "bin")        # yt-dlp, ffmpeg e deno instalados pelo PyDM
MEDIA_DIR = os.path.join(APP_DIR, "media")    # arquivos temporários de cada download de vídeo
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com")
COMPONENT_URLS = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "deno": "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
    "ffmpeg": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
}
# formatos de áudio do painel: codec/qualidade do yt-dlp (-x --audio-format/--audio-quality) e kbps p/ estimar o tamanho
AUDIO_PRESETS = {
    "mp3-320": {"label": "MP3 · 320 kbps", "codec": "mp3", "q": "320K", "ext": "mp3", "kbps": 320},
    "mp3-192": {"label": "MP3 · 192 kbps", "codec": "mp3", "q": "192K", "ext": "mp3", "kbps": 192},
    "mp3-128": {"label": "MP3 · 128 kbps", "codec": "mp3", "q": "128K", "ext": "mp3", "kbps": 128},
    "m4a": {"label": "M4A · AAC", "codec": "m4a", "q": None, "ext": "m4a", "kbps": 0},
    "wav": {"label": "WAV · sem compressão", "codec": "wav", "q": None, "ext": "wav", "kbps": 1411},
    "flac": {"label": "FLAC · sem perdas", "codec": "flac", "q": None, "ext": "flac", "kbps": 0},
    "ogg": {"label": "OGG · Vorbis", "codec": "vorbis", "q": "5", "ext": "ogg", "kbps": 160},
    "opus": {"label": "OPUS", "codec": "opus", "q": None, "ext": "opus", "kbps": 0},
}
DEFAULT_VIDEO = {"enabled": True, "audio_formats": ["mp3-320", "mp3-192", "mp3-128", "m4a", "wav", "flac"],
                 "embed_meta": True, "ytdlp": "", "ffmpeg": "", "video_cat": "Vídeo", "audio_cat": "Música"}
SETTINGS_REF = {"s": None}   # a Bridge (outra thread) lê as configurações por aqui


def fmt_size(n, dec=1):
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.{dec}f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def fmt_time(s):
    if s is None or s < 0 or s > 86400 * 30:
        return "--"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def log(msg):
    """Registro simples em ~/.pydm/pydm.log (ajuda a descobrir por que algo não funcionou)."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "pydm.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


def pt(txt):
    """Formato brasileiro: vírgula como separador decimal."""
    return txt.replace(".", ",")


def fmt_eta(s):
    """Tempo restante no estilo '23 seg.' / '5 min 03 seg.' / '1 h 02 min'."""
    if s is None or s < 0 or s > 86400 * 30:
        return "--"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {sec:02d} seg."
    return f"{sec} seg."


def _untop(win):
    try:
        win.attributes("-topmost", False)
    except tk.TclError:
        pass


def center_front(win, offset=0):
    """Centraliza a janela e a traz para a frente (o foco costuma estar no navegador)."""
    try:
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2 + offset
        y = (win.winfo_screenheight() - win.winfo_reqheight()) // 3 + offset
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(400, lambda: _untop(win))
        win.focus_force()
    except tk.TclError:
        pass


def req(url, headers=None):
    h = {"User-Agent": GLOBAL.get("ua") or UA}
    h.update(headers or {})
    return urllib.request.Request(url, headers=h)


def probe(url, headers=None):
    """Descobre tamanho, suporte a Range e nome do arquivo."""
    with urllib.request.urlopen(req(url, {**(headers or {}), "Range": "bytes=0-0"}), timeout=20) as r:
        cr, cl = r.headers.get("Content-Range"), r.headers.get("Content-Length")
        size, resumable = None, False
        if r.status == 206 and cr:
            try:
                size, resumable = int(cr.split("/")[-1]), True
            except ValueError:
                pass
        elif cl:
            size = int(cl)
        cd = r.headers.get("Content-Disposition", "")
        name = None
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1).strip('" '))
        else:
            m = re.search(r'filename="?([^";]+)"?', cd, re.I)
            if m:
                name = m.group(1)
        if not name:
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(r.geturl()).path))
    name = re.sub(r'[<>:"/\\|?*]', "_", name or "download") or "download"
    return size, resumable, name


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    p, i = os.path.join(folder, name), 1
    while os.path.exists(p) or os.path.exists(p + ".part"):
        p = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return p


# ---------- configurações (categorias, pastas recentes) ----------

def load_cfg():
    s = {"categories": copy.deepcopy(DEFAULT_CATEGORIES), "recent": [], "conns": 8, "show_complete": True,
         "max_active": MAX_ACTIVE, "speed_limit_kbps": 0, "close_to_tray": True, "monitor_clipboard": False,
         "show_cats": True, "columns": list(DEFAULT_COLS), "sort": {"col": None, "desc": False},
         "schedule": copy.deepcopy(DEFAULT_SCHEDULE),
         "clip_auto": False, "browsers": copy.deepcopy(DEFAULT_BROWSERS), "keys": dict(DEFAULT_KEYS),
         "menu": dict(DEFAULT_MENU), "menu_title": DEFAULT_MENU_TITLE,
         "capture_exts": list(DEFAULT_CAPTURE_EXTS), "exclude_sites": list(DEFAULT_EXCLUDE_SITES),
         "exclude_addrs": [], "set_mtime": False, "ignore_mtime": False, "temp_folder": "",
         "show_start": True, "start_queue_only": False, "show_final_later": True, "show_final_batch": True,
         "dup_action": "ask", "manual_ua": UA, "progress_ui": dict(DEFAULT_PROGRESS_UI),
         "antivirus": dict(DEFAULT_AV), "video": copy.deepcopy(DEFAULT_VIDEO)}
    try:
        with open(CFG, encoding="utf-8") as f:
            j = json.load(f)
        cats = {}
        for name, info in (j.get("categories") or {}).items():
            if isinstance(info, dict) and isinstance(info.get("folder"), str) and info["folder"].strip():
                exts = info.get("exts")
                exts = exts if isinstance(exts, list) else []
                cats[str(name)] = {"folder": os.path.normpath(info["folder"]),
                                   "exts": [str(e).lower().lstrip(".") for e in exts],
                                   "remember": bool(info.get("remember"))}
        if cats:
            s["categories"] = cats
        s["recent"] = [p for p in (j.get("recent") or []) if isinstance(p, str)][:10]
        c = j.get("conns")
        if isinstance(c, int) and 1 <= c <= 32:
            s["conns"] = c
        s["show_complete"] = bool(j.get("show_complete", True))
        m = j.get("max_active")
        if isinstance(m, int) and 1 <= m <= 10:
            s["max_active"] = m
        k = j.get("speed_limit_kbps")
        if isinstance(k, int) and 0 <= k <= 10_000_000:
            s["speed_limit_kbps"] = k
        for key in ("close_to_tray", "monitor_clipboard", "show_cats"):
            if key in j:
                s[key] = bool(j[key])
        if isinstance(j.get("columns"), list):
            s["columns"] = [c for c in COL_IDS if c in j["columns"]]
        so = j.get("sort")
        if isinstance(so, dict) and so.get("col") in COL_IDS + ("#0", None):
            s["sort"] = {"col": so.get("col"), "desc": bool(so.get("desc"))}
        sc = j.get("schedule")
        if isinstance(sc, dict):
            for key, default in DEFAULT_SCHEDULE.items():
                if key in sc and type(sc[key]) == type(default):
                    s["schedule"][key] = sc[key]
            s["schedule"]["days"] = [d for d in s["schedule"]["days"] if isinstance(d, int) and 0 <= d <= 6]
        for key in ("clip_auto", "set_mtime", "ignore_mtime", "show_start", "start_queue_only",
                    "show_final_later", "show_final_batch"):
            if key in j:
                s[key] = bool(j[key])
        if isinstance(j.get("temp_folder"), str):
            s["temp_folder"] = j["temp_folder"].strip()
        if j.get("dup_action") in [a for a, _ in DUP_ACTIONS]:
            s["dup_action"] = j["dup_action"]
        for key in ("manual_ua", "menu_title"):
            if isinstance(j.get(key), str) and j[key].strip():
                s[key] = j[key].strip()
        for key in ("capture_exts", "exclude_sites", "exclude_addrs"):
            if isinstance(j.get(key), list):
                s[key] = [str(x).strip() for x in j[key] if str(x).strip()]
        for key, default in (("menu", DEFAULT_MENU), ("progress_ui", DEFAULT_PROGRESS_UI)):
            if isinstance(j.get(key), dict):
                for k in default:
                    if k in j[key]:
                        s[key][k] = bool(j[key][k])
        ks = j.get("keys")
        if isinstance(ks, dict):
            for k in ("prevent", "force"):
                if ks.get(k) in KEY_CHOICES:
                    s["keys"][k] = ks[k]
        av = j.get("antivirus")
        if isinstance(av, dict):
            s["antivirus"]["enabled"] = bool(av.get("enabled"))
            for k in ("program", "args"):
                if isinstance(av.get(k), str):
                    s["antivirus"][k] = av[k]
        br = j.get("browsers")
        if isinstance(br, list):
            out = [{"name": b["name"], "ua": b["ua"].strip(), "on": bool(b.get("on", True)),
                    "builtin": bool(b.get("builtin"))}
                   for b in br if isinstance(b, dict) and isinstance(b.get("name"), str)
                   and isinstance(b.get("ua"), str) and b["ua"].strip()]
            if out:
                s["browsers"] = out
        vd = j.get("video")
        if isinstance(vd, dict):
            for k in ("enabled", "embed_meta"):
                if k in vd:
                    s["video"][k] = bool(vd[k])
            for k in ("ytdlp", "ffmpeg", "video_cat", "audio_cat"):
                if isinstance(vd.get(k), str):
                    s["video"][k] = vd[k].strip()
            if isinstance(vd.get("audio_formats"), list):
                s["video"]["audio_formats"] = [x for x in vd["audio_formats"] if x in AUDIO_PRESETS]
    except Exception:
        pass
    return s


def save_cfg(s):
    tmp = CFG + ".tmp"
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG)  # gravação atômica: nunca deixa o arquivo pela metade
    except Exception:
        pass


def guess_name(url):
    """Nome provisório do arquivo, tirado só da URL (antes de consultar o servidor)."""
    n = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    return re.sub(r'[<>:"/\\|?*]', "_", n) or "download"


def category_for(name, cats):
    """Escolhe a categoria pela extensão do arquivo."""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if ext:
        for c, info in cats.items():
            if ext in info["exts"]:
                return c
    return "Geral" if "Geral" in cats else next(iter(cats))


# ---------- janela principal: colunas, ícones e constantes ----------
GLOBAL = {"kbps": 0, "next": 0.0,   # limite de velocidade TOTAL (todos os downloads somados)
          "ua": UA, "temp": "", "set_mtime": False, "ignore_mtime": False}   # opções que o motor lê
GLOBAL_LOCK = threading.Lock()

# id, título, largura, alinhamento, visível por padrão (a coluna "Nome do arquivo" é sempre visível)
COLS = (
    ("f", "F.", 30, "center", True),
    ("tamanho", "Tamanho", 85, "w", True),
    ("progresso", "Progresso", 90, "w", True),
    ("eta", "Tempo restante", 110, "w", True),
    ("vel", "Taxa de transferência", 150, "w", True),
    ("data", "Data da última tentativa", 185, "w", True),
    ("desc", "Descrição", 160, "w", True),
    ("status", "Status", 110, "w", False),
    ("pasta", "Salvar em", 230, "w", False),
    ("url", "Endereço de download", 280, "w", False),
)
COL_IDS = tuple(c[0] for c in COLS)
DEFAULT_COLS = [c[0] for c in COLS if c[4]]
DAYS = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")
DEFAULT_SCHEDULE = {"enabled": False, "start_on": True, "start": "02:00", "stop_on": False, "stop": "06:00",
                    "days": [0, 1, 2, 3, 4, 5, 6], "quit": False, "shutdown": False,
                    "mode": "Desligar", "force": False}
KIND_BY_CAT = {"Compactados": "archive", "Documentos": "doc", "Música": "music",
               "Programas": "program", "Vídeo": "video"}


def kind_for(name):
    """Tipo do ícone de um arquivo (compactado, documento, música...), pela extensão."""
    return KIND_BY_CAT.get(category_for(name, DEFAULT_CATEGORIES), "doc")


class PixelIcons:
    """Ícones 16x16 desenhados pixel a pixel: não precisam de arquivos de imagem nem do Pillow."""

    def __init__(self, root):
        self.root, self.cache = root, {}

    def get(self, kind):
        if kind not in self.cache:
            self.cache[kind] = self._build(kind)
        return self.cache[kind]

    def _build(self, kind):
        g = [[None] * 16 for _ in range(16)]

        def R(x0, y0, x1, y1, c):
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    g[y][x] = c

        def box(x0, y0, x1, y1, fill, edge):
            R(x0, y0, x1, y1, edge)
            R(x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill)

        def page(fill, edge):
            box(2, 1, 12, 14, fill, edge)
            R(9, 2, 11, 4, "#d6d6d6")   # dobra do canto

        def folder():
            R(1, 3, 6, 4, "#d9a521")
            R(1, 5, 14, 13, "#f4c63d")
            R(1, 5, 14, 5, "#d9a521")
            R(1, 13, 14, 13, "#b8901c")
            R(1, 5, 1, 13, "#b8901c")
            R(14, 5, 14, 13, "#b8901c")

        w = "#ffffff"
        if kind == "archive":
            page("#f7e6a0", "#a8841c")
            for y in range(2, 10):
                g[y][7] = "#6b5412" if y % 2 == 0 else "#c7a63a"
            R(6, 10, 8, 12, "#6b5412")
        elif kind == "music":
            page(w, "#7a7a7a")
            R(8, 5, 8, 10, "#2f6fd6")
            R(8, 5, 10, 6, "#2f6fd6")
            R(5, 9, 7, 11, "#2f6fd6")
        elif kind == "video":
            R(1, 3, 14, 12, "#2b2b2b")
            for y in (4, 6, 8, 10):
                g[y][2] = g[y][13] = w
            R(4, 4, 11, 11, "#5a8fd0")
            R(6, 6, 9, 9, "#cfe3f8")
        elif kind == "program":
            R(1, 2, 14, 13, "#2f6fd6")
            R(2, 5, 13, 12, w)
            R(4, 7, 8, 9, "#9db8e8")
            g[3][12] = g[3][10] = g[3][8] = w
        elif kind == "folder":
            folder()
        elif kind == "folder_dl":
            folder()
            R(8, 8, 14, 14, "#2e9e44")
            R(11, 9, 11, 12, w)
            g[11][10] = g[11][12] = w
        elif kind == "folder_ok":
            folder()
            R(8, 8, 14, 14, "#2f6fd6")
            for x, y in ((9, 11), (10, 12), (11, 11), (12, 10), (13, 9)):
                g[y][x] = w
        elif kind == "queue":
            box(5, 2, 13, 8, "#cfe0f7", "#3a6fb8")
            box(3, 5, 11, 11, "#cfe0f7", "#3a6fb8")
            box(1, 8, 9, 14, "#cfe0f7", "#3a6fb8")
        else:   # "doc"
            page(w, "#7a7a7a")
            for y in (6, 8, 10, 12):
                R(4, y, 10, y, "#b5b5b5")
        img = tk.PhotoImage(master=self.root, width=16, height=16)
        for y in range(16):
            for x in range(16):
                if g[y][x]:
                    img.put(g[y][x], to=(x, y))
        return img


class FileChanged(Exception):
    """O arquivo no servidor mudou (data de modificação diferente) ao retomar um download."""


class Download:
    def __init__(self, url, folder, conns=8, headers=None):
        self.url, self.folder, self.conns = url, folder, conns
        self.headers = headers or {}
        self.name = self.path = self.size = None
        self.resumable = False
        self.probed = False  # já consultou tamanho/Range no servidor?
        self.part_path = None          # arquivo parcial na pasta temporária (None = ao lado do destino)
        self.last_modified = ""         # cabeçalho Last-Modified do servidor
        self.description = self.category = ""
        self.segs = []  # cada segmento: [inicio, fim, posicao_atual]
        self.status, self.error = "Na fila", ""
        self.stop = threading.Event()
        self.thread = None
        self.failed = False
        self.lock = threading.Lock()   # protege a divisão dinâmica de segmentos
        self.seg_state = {}            # id(segmento) -> texto da coluna "Informações"
        self.announce = False          # True = mostrar a janela "Download completo" ao terminar
        self.limit_kbps = 0            # limite de velocidade (KB/s); 0 = sem limite
        self.limit_remember = False    # guardar o limite no arquivo de downloads?
        self.tlock = threading.Lock()
        self._next = 0.0               # instante (monotonic) em que o limitador libera os próximos bytes
        self.show_msg = None           # None = segue a opção global "Avisar ao concluir"
        self.queued = False            # pertence à fila principal?
        self.added = time.time()       # quando foi adicionado à lista
        self.last_try = 0.0            # data da última tentativa de download
        # ações ao concluir (valem só nesta sessão, de propósito: ninguém quer o PC desligando dias depois)
        self.on_done = {"modem": False, "quit": False, "shutdown": False, "mode": "Desligar", "force": False}
        self.speed, self._last = 0.0, (time.time(), 0)
        self.note = ""                  # texto extra do estado (ex.: "Convertendo o áudio...")

    # ---------- persistência ----------
    def to_dict(self):
        j = {k: getattr(self, k) for k in
             ("url", "folder", "conns", "name", "path", "size", "resumable", "segs", "status",
              "headers", "probed", "description", "category", "show_msg", "limit_remember",
              "queued", "added", "last_try", "part_path", "last_modified")}
        # o limite só é guardado se o usuário marcou "Lembrar configurações de velocidade"
        j["limit_kbps"] = self.limit_kbps if self.limit_remember else 0
        return j

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d.get("conns", 8), d.get("headers"))
        for k in ("name", "path", "size", "resumable", "segs", "status"):
            setattr(o, k, d.get(k))
        # arquivos salvos por versões antigas não têm "probed": se já tinham caminho, já foram consultados
        o.probed = d.get("probed", bool(d.get("path")))
        o.description = d.get("description") or ""
        o.category = d.get("category") or ""
        o.show_msg = d.get("show_msg") if isinstance(d.get("show_msg"), bool) else None
        o.limit_remember = bool(d.get("limit_remember"))
        o.queued = bool(d.get("queued"))
        o.part_path = d.get("part_path") if isinstance(d.get("part_path"), str) else None
        o.last_modified = d.get("last_modified") if isinstance(d.get("last_modified"), str) else ""
        for k in ("added", "last_try"):
            try:
                setattr(o, k, float(d.get(k) or 0))
            except (TypeError, ValueError):
                setattr(o, k, 0.0)
        try:
            o.limit_kbps = max(0, int(d.get("limit_kbps") or 0)) if o.limit_remember else 0
        except (TypeError, ValueError):
            o.limit_kbps = 0
        if o.status in ("Baixando", "Na fila"):
            o.status = "Pausado"
        return o

    @property
    def part(self):
        return self.part_path or (self.path + ".part")

    @property
    def downloaded(self):
        t = 0
        for s, e, p in self.segs:
            t += (p - s) if e < 0 else max(0, min(p, e + 1) - s)
        return t

    def seg_rows(self):
        """Linhas da tabela de conexões: (nº, bytes baixados, informação)."""
        rows = []
        for i, seg in enumerate(sorted(list(self.segs), key=lambda x: x[0]), 1):
            s, e, p = seg
            got = (p - s) if e < 0 else max(0, min(p, e + 1) - s)
            if self.status == "Concluído" or (e >= 0 and p > e):
                info = "Concluído"
            elif self.status != "Baixando":
                info = self.status
            else:
                info = self.seg_state.get(id(seg), "Conectando...")
            rows.append((i, got, info))
        return rows

    # ---------- controle ----------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.announce = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.stop.set()

    def cancel(self, delete_file=True):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        if delete_file and self.path and self.status != "Concluído" and os.path.exists(self.part):
            try:
                os.remove(self.part)
            except OSError:
                pass

    def reset(self):
        """Baixar novamente: para o download, descarta o parcial e recomeça do zero."""
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.path and os.path.exists(self.part):
            try:
                os.remove(self.part)
            except OSError:
                pass
        self.segs, self.error, self.failed = [], "", False
        self.part_path, self.last_modified = None, ""   # recalcula com as opções atuais
        self.probed, self.speed = False, 0.0   # consulta o servidor de novo (o arquivo pode ter mudado)
        self.status = "Na fila"

    def _build_segments(self):
        if self.resumable and self.size and self.size > 1_000_000 and self.conns > 1:
            n, step = self.conns, self.size // self.conns
            self.segs = [[i * step, self.size - 1 if i == n - 1 else (i + 1) * step - 1, i * step]
                         for i in range(n)]
        else:
            self.segs = [[0, self.size - 1 if self.size else -1, 0]]

    def _pick_part(self):
        """Define onde fica o arquivo parcial: na pasta temporária das Opções (se houver) ou ao lado do destino.
        Parciais já existentes ao lado do destino (versões antigas) continuam onde estão."""
        if self.part_path:
            os.makedirs(os.path.dirname(self.part_path), exist_ok=True)
            return
        tmp = GLOBAL.get("temp")
        if tmp and not os.path.exists(self.path + ".part"):
            os.makedirs(tmp, exist_ok=True)
            tag = hashlib.sha1(self.path.encode("utf-8", "ignore")).hexdigest()[:10]
            self.part_path = os.path.join(tmp, f"{tag}_{os.path.basename(self.path)}.part")

    def _finalize(self):
        """Move o parcial para o destino (funciona entre discos) e aplica a data do servidor, se pedido."""
        try:
            os.replace(self.part, self.path)
        except OSError:
            shutil.move(self.part, self.path)
        if GLOBAL.get("set_mtime") and self.last_modified:
            try:
                t = parsedate_to_datetime(self.last_modified).timestamp()
                os.utime(self.path, (t, t))
            except Exception:
                pass

    def _run(self):
        try:
            self.stop.clear()
            self.failed, self.error, self.status = False, "", "Baixando"
            self.last_try = time.time()
            os.makedirs(self.folder, exist_ok=True)
            if not self.probed:
                size, resumable, name = probe(self.url, self.headers)
                self.size, self.resumable = size, resumable
                self.name = self.name or name  # se o usuário já escolheu um nome, ele é mantido
                self.probed = True
            if not self.path:
                self.path = unique_path(self.folder, self.name)
            self._pick_part()
            fresh = not os.path.exists(self.part)
            if fresh or not self.segs or not self.resumable:
                self.last_modified = ""
                self._build_segments()
                with open(self.part, "wb") as f:
                    if self.size:
                        f.truncate(self.size)
            self.seg_state = {}
            threads = []
            for seg in list(self.segs):
                if seg[1] < 0 or seg[2] <= seg[1]:
                    self.seg_state[id(seg)] = "Conectando..."
                    t = threading.Thread(target=self._worker, args=(seg,), daemon=True)
                    t.start()
                    threads.append(t)
            for t in threads:
                t.join()
            if self.failed:
                self.status = "Erro"
            elif self.stop.is_set():
                self.status = "Pausado"
            else:
                self._finalize()
                self.last_try = time.time()
                self.status = "Concluído"
        except Exception as ex:
            self.error, self.status = str(ex), "Erro"

    def set_limit(self, kbps, remember=False):
        """Define o limite de velocidade (KB/s, 0 = ilimitado). Vale para todas as conexões juntas
        e pode ser mudado com o download em andamento."""
        self.limit_kbps, self.limit_remember = max(0, int(kbps)), bool(remember)
        with self.tlock:
            self._next = 0.0

    def _read_size(self):
        lims = [k for k in (self.limit_kbps, GLOBAL["kbps"]) if k > 0]
        return CHUNK if not lims else max(2048, min(CHUNK, min(lims) * 1024 // 8))  # blocos pequenos = limite mais suave

    def _throttle(self, n):
        """Freios compartilhados (o do download e o total): cada bloco "reserva" n/limite segundos."""
        now, delay = time.monotonic(), 0.0
        lim = self.limit_kbps * 1024
        if lim > 0:
            with self.tlock:
                self._next = max(self._next, now) + n / lim
                delay = self._next - now
        g = GLOBAL["kbps"] * 1024
        if g > 0:
            with GLOBAL_LOCK:
                GLOBAL["next"] = max(GLOBAL["next"], now) + n / g
                delay = max(delay, GLOBAL["next"] - now)
        while delay > 0 and not self.stop.is_set():
            s = min(delay, 0.2)
            time.sleep(s)
            delay -= s

    def _steal(self):
        """Segmentação dinâmica: pega a metade final do maior trecho ainda não baixado.
        Assim, quando uma conexão termina, ela ajuda as mais lentas em vez de ficar parada."""
        if not self.resumable or not self.size:
            return None
        with self.lock:
            best, rem = None, 0
            for s in self.segs:
                if s[1] >= 0 and s[1] + 1 - s[2] > rem:
                    best, rem = s, s[1] + 1 - s[2]
            if best is None or rem < MIN_SPLIT:
                return None
            mid = best[1] + 1 - rem // 2
            new = [mid, best[1], mid]
            best[1] = mid - 1          # a conexão antiga para sozinha ao chegar aqui
            self.segs.append(new)
            self.seg_state[id(new)] = "Conectando..."
            return new

    def _worker(self, seg):
        try:
            while self._download_seg(seg):
                if self.stop.is_set() or self.failed:
                    return
                seg = self._steal()
                if seg is None:
                    return
        except Exception as ex:
            self.error, self.failed = str(ex), True

    def _download_seg(self, seg):
        """Baixa um segmento. Retorna True se ele foi concluído; False se parou (pausa/erro)."""
        tries = 0
        st = self.seg_state
        with open(self.part, "r+b") as f:
            while not self.stop.is_set() and not self.failed:
                if seg[1] >= 0 and seg[2] > seg[1]:
                    st[id(seg)] = "Concluído"
                    return True
                try:
                    st[id(seg)] = "Conectando..."
                    h = dict(self.headers)
                    if self.resumable:
                        h["Range"] = f"bytes={seg[2]}-{seg[1] if seg[1] >= 0 else ''}"
                    with urllib.request.urlopen(req(self.url, h), timeout=20) as r:
                        lm = r.headers.get("Last-Modified")
                        if lm:
                            if self.last_modified and lm != self.last_modified and not GLOBAL.get("ignore_mtime"):
                                raise FileChanged("O arquivo mudou no servidor desde o início do download. Use "
                                                  "\"Baixar novamente\" ou ative \"Ignorar mudanças de horário "
                                                  "do arquivo\" nas Opções.")
                            self.last_modified = self.last_modified or lm
                        if self.resumable and r.status != 206:
                            raise IOError("Servidor ignorou o pedido de Range")
                        st[id(seg)] = "Recebendo..."
                        f.seek(seg[2])
                        read = getattr(r, "read1", r.read)  # read1 não espera encher o buffer
                        while not self.stop.is_set():
                            buf = read(self._read_size())
                            if not buf:
                                break
                            if seg[1] >= 0:
                                n = seg[1] + 1 - seg[2]   # o fim pode ter encurtado (segmentação dinâmica)
                                if n <= 0:
                                    st[id(seg)] = "Concluído"
                                    return True
                                buf = buf[:n]
                            f.write(buf)
                            f.flush()   # o que está no JSON de progresso já está de fato no disco
                            seg[2] += len(buf)
                            tries = 0
                            self._throttle(len(buf))
                            if seg[1] >= 0 and seg[2] > seg[1]:
                                st[id(seg)] = "Concluído"
                                return True
                        if self.stop.is_set():
                            st[id(seg)] = "Pausado"
                            return False
                        if seg[1] < 0:
                            st[id(seg)] = "Concluído"
                            return True  # tamanho desconhecido: EOF = fim
                        raise IOError("Conexão encerrada antes do fim")
                except FileChanged as ex:
                    self.error, self.failed = str(ex), True
                    st[id(seg)] = "Erro"
                    return False
                except Exception as ex:
                    tries += 1
                    st[id(seg)] = f"Reconectando ({tries})..."
                    if tries > 6:
                        self.error, self.failed = str(ex), True
                        st[id(seg)] = "Erro"
                        return False
                    if not self.resumable:
                        seg[2] = seg[0]
                    time.sleep(min(2 * tries, 10))
        return False


# ---------- ponte com a extensão do navegador ----------
PORT = 9614
INBOX = queue.Queue()
TRAY_EVENTS = queue.Queue()  # eventos do menu da bandeja (vêm de outra thread)


# ====================================================================================
#  Vídeo e áudio de páginas (botão "Baixar este vídeo" da extensão)
#  O PyDM não extrai vídeos sozinho: ele usa o yt-dlp (extrai/baixa) com o ffmpeg (junta e converte)
#  e o Deno (necessário para o YouTube). Os três ficam em ~/.pydm/bin ou no PATH.
# ====================================================================================
class MediaError(Exception):
    pass


def find_tool(name, custom=""):
    """Localiza yt-dlp / ffmpeg / deno: caminho das Opções, depois ~/.pydm/bin, depois o PATH."""
    if custom and os.path.isfile(custom):
        return custom
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    local = os.path.join(BIN_DIR, exe)
    if os.path.isfile(local):
        return local
    return shutil.which(name)


def media_tools(s):
    v = s["video"]
    return {"ytdlp": find_tool("yt-dlp", v["ytdlp"]), "ffmpeg": find_tool("ffmpeg", v["ffmpeg"]),
            "deno": find_tool("deno")}


def media_tools_named(s):
    t = media_tools(s)
    return {"yt-dlp": t["ytdlp"], "deno": t["deno"], "ffmpeg": t["ffmpeg"]}


def valid_media_id(mid):
    """O navegador só escolhe entre opções que o PyDM mesmo ofereceu: nunca passa argumentos soltos ao yt-dlp."""
    return bool(re.fullmatch(r"v\d{3,4}", mid)) or mid in AUDIO_PRESETS or mid == "orig"


def _is_youtube(url):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in YOUTUBE_HOSTS)


def _last_error(text):
    errs = [l[6:].strip() for l in (text or "").splitlines() if l.startswith("ERROR:")]
    msg = re.sub(r"\x1b\[[0-9;]*m", "", errs[-1] if errs else "").strip()
    msg = re.sub(r"^\[[^\]]+\]\s*[\w\-]+:\s*", "", msg)   # tira o prefixo "[youtube] abc123: "
    return msg[:220]


def ytdlp_base(s, url, headers=None):
    """Começo da linha de comando do yt-dlp (comum à análise e ao download)."""
    t = media_tools(s)
    if not t["ytdlp"]:
        raise MediaError("no_engine")
    cmd = [t["ytdlp"], "--ignore-config", "--no-playlist", "--no-warnings", "--no-colors", "--encoding", "utf-8",
           "--socket-timeout", "20", "--retries", "10", "--fragment-retries", "10"]
    if t["deno"]:
        cmd += ["--js-runtimes", "deno:" + t["deno"]]
    if t["ffmpeg"]:
        cmd += ["--ffmpeg-location", t["ffmpeg"]]
    if not _is_youtube(url) and headers:   # no YouTube o yt-dlp usa as próprias identidades
        for key in ("Referer", "Cookie"):
            if headers.get(key):
                cmd += ["--add-header", f"{key}:{headers[key]}"]
        if headers.get("User-Agent"):
            cmd += ["--user-agent", headers["User-Agent"]]
    return cmd


_FMT_CACHE = {}


def media_probe(url, headers, s):
    """Pergunta ao yt-dlp quais formatos o vídeo tem (JSON). Guarda o resultado por 5 minutos."""
    key = (url, bool((headers or {}).get("Cookie")))
    hit = _FMT_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    cmd = ytdlp_base(s, url, headers) + ["-J", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=90, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        raise MediaError("Tempo esgotado ao analisar o vídeo.")
    if r.returncode != 0 or not r.stdout.strip():
        raise MediaError(_last_error(r.stderr) or "Não foi possível analisar este vídeo.")
    try:
        info = json.loads(r.stdout)
    except ValueError:
        raise MediaError("Resposta inesperada do yt-dlp.")
    if len(_FMT_CACHE) > 20:
        _FMT_CACHE.clear()
    _FMT_CACHE[key] = (time.time(), info)
    return info


def _fsize(f, dur):
    n = f.get("filesize") or f.get("filesize_approx")
    if not n and f.get("tbr") and dur:
        n = f["tbr"] * 125 * dur   # tbr é em kbit/s
    return int(n) if n else None


def _res_tag(h):
    return " 8K" if h >= 4320 else " 4K" if h >= 2160 else " HD" if h >= 720 else ""


def media_options(info, s, tools):
    """Transforma o JSON do yt-dlp na lista que o painel mostra: qualidades de vídeo e formatos de áudio."""
    dur = info.get("duration") or 0
    fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not fmts and info.get("url"):   # link direto: o yt-dlp descreve o arquivo no próprio nível
        fmts = [info]
    ffmpeg = bool(tools["ffmpeg"])
    videos = [f for f in fmts if f.get("vcodec") != "none" and f.get("height")]
    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    progressive = [f for f in videos if f.get("acodec") != "none"]   # já traz o som junto
    pool = videos if ffmpeg else progressive
    best_audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0) if audios else None
    asize = _fsize(best_audio, dur) if best_audio else None

    vid_opts = []
    for h in sorted({f["height"] for f in pool}, reverse=True):
        f = max((x for x in pool if x["height"] == h), key=lambda x: x.get("tbr") or 0)
        est = _fsize(f, dur)
        if est and f.get("acodec") == "none" and asize:
            est += asize
        if ffmpeg:
            cont = "MKV" if h > 1080 else "MP4"
        else:
            cont = (f.get("ext") or "mp4").upper()
        vid_opts.append({"id": f"v{h}", "label": f"{cont} · {h}p{_res_tag(h)}", "size": est})

    aud_opts = []
    if ffmpeg:
        for i in s["video"]["audio_formats"]:
            if i not in AUDIO_PRESETS:
                continue
            p = AUDIO_PRESETS[i]
            size = int(p["kbps"] * 125 * dur) if p["kbps"] and dur else (asize if p["codec"] == "m4a" else None)
            aud_opts.append({"id": i, "label": p["label"], "size": size})
    elif best_audio:   # sem ffmpeg não dá para converter: só o áudio como o site entrega
        # o download usa "ba[ext=m4a]/ba": prefere M4A se existir, então o rótulo e o tamanho seguem o mesmo critério
        m4a = [f for f in audios if f.get("ext") == "m4a"]
        pick = max(m4a, key=lambda f: f.get("abr") or f.get("tbr") or 0) if m4a else best_audio
        aud_opts.append({"id": "orig", "label": f"Áudio original ({(pick.get('ext') or 'm4a').upper()})",
                         "size": _fsize(pick, dur)})
    if not vid_opts and not aud_opts:
        raise MediaError("Nenhum formato de vídeo ou áudio encontrado nesta página.")
    return {"title": info.get("title") or "", "duration": dur, "live": bool(info.get("is_live")),
            "video": vid_opts, "audio": aud_opts, "ffmpeg": ffmpeg, "deno": bool(tools["deno"])}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def kill_tree(proc):
    """Encerra o yt-dlp e os programas que ele abriu (ffmpeg)."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=NOWIN, timeout=10)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


POSTPROC_NOTES = {"Merger": "Juntando vídeo e áudio...", "ExtractAudio": "Convertendo o áudio...",
                  "VideoConvertor": "Convertendo o vídeo...", "EmbedThumbnail": "Gravando a capa...",
                  "Metadata": "Gravando informações...", "MoveFiles": "Finalizando..."}


class MediaDownload(Download):
    """Download de vídeo/áudio de uma página: quem extrai, baixa, junta e converte é o yt-dlp.
    Aparece na lista como qualquer download (pausar, retomar, fila, categorias...)."""

    def __init__(self, url, folder, conns=8, headers=None, media=None):
        super().__init__(url, folder, conns, headers)
        self.media = dict(media or {})
        self.media.setdefault("job", uuid.uuid4().hex[:12])
        self.resumable, self.probed = True, True
        self._base = self._cur = 0     # bytes dos fluxos já concluídos / do fluxo atual (vídeo e áudio vêm separados)
        self.note = ""
        self.proc = None

    @property
    def jobdir(self):
        return os.path.join(MEDIA_DIR, self.media["job"])

    @property
    def downloaded(self):
        if self.status == "Concluído":
            return self.size or 0
        n = int(self._base + self._cur)
        return min(n, self.size) if self.size else n

    def to_dict(self):
        j = super().to_dict()
        j["media"] = self.media
        return j

    @classmethod
    def from_dict(cls, d):
        o = super().from_dict(d)
        o.media = dict(d.get("media") or o.media)
        o.media.setdefault("job", uuid.uuid4().hex[:12])
        return o

    # ---------- linha de comando ----------
    def command(self, s):
        m, v = self.media, s["video"]
        cmd = ytdlp_base(s, self.url, self.headers)
        os.makedirs(self.jobdir, exist_ok=True)
        cmd += ["--quiet", "--progress", "--newline", "--no-simulate", "--windows-filenames",
                "--trim-filenames", "150", "-N", "4",
                "-P", "home:" + self.folder, "-P", "temp:" + self.jobdir,
                "--progress-template",
                "download:PYDM|%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s",
                "--progress-template", "postprocess:PYDMPP|%(progress.postprocessor)s|%(progress.status)s",
                "--print", "after_move:PYDMFILE|%(filepath)s"]
        mid, ffm, suffix = m["id"], bool(find_tool("ffmpeg", v["ffmpeg"])), ""
        if mid.startswith("v"):
            h = int(mid[1:])
            suffix = f" [{h}p]"
            if ffm:
                sel = (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]"
                       if h <= 1080 else f"bv*[height<={h}]+ba/b[height<={h}]")
                cmd += ["-f", sel, "--merge-output-format", "mp4" if h <= 1080 else "mkv"]
            else:
                cmd += ["-f", f"b[height<={h}]/b"]
        elif mid == "orig":
            cmd += ["-f", "ba[ext=m4a]/ba"]
        else:
            p = AUDIO_PRESETS[mid]
            cmd += ["-f", "ba/b", "-x", "--audio-format", p["codec"]]
            if p["q"]:
                cmd += ["--audio-quality", p["q"]]
            if v["embed_meta"]:
                cmd += ["--embed-metadata"]
                if p["codec"] in ("mp3", "m4a", "flac", "opus", "vorbis"):
                    cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
        if m.get("overwrite"):
            cmd += ["--force-overwrites"]
        cmd += ["-o", f"%(title)s{suffix}.%(ext)s"]
        lims = [k for k in (self.limit_kbps, GLOBAL["kbps"]) if k > 0]
        if lims:
            cmd += ["--limit-rate", f"{min(lims)}K"]
        return cmd + [self.url]

    # ---------- execução ----------
    def _killer(self, proc):
        while proc.poll() is None:
            if self.stop.wait(0.3):
                kill_tree(proc)
                return

    def _on_line(self, line):
        parts = line.split("|")
        if parts[0] == "PYDM" and len(parts) >= 5:
            dl = _num(parts[2])
            if dl is None:
                return
            if parts[1] == "finished":     # terminou um fluxo (vídeo ou áudio): o próximo começa do zero
                self._base += dl
                self._cur = 0
            else:
                self._cur = dl
        elif parts[0] == "PYDMPP" and len(parts) >= 3:
            self.note = POSTPROC_NOTES.get(parts[1], "Processando...") if parts[2] == "started" else ""

    def _run(self):
        s = SETTINGS_REF["s"]
        started = time.time()
        try:
            self.stop.clear()
            self.failed, self.error, self.status = False, "", "Baixando"
            self.last_try, self.note = started, ""
            self._base = self._cur = 0
            os.makedirs(self.folder, exist_ok=True)
            cmd = self.command(s)
            proc = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                                errors="replace", creationflags=NOWIN, bufsize=1)
            threading.Thread(target=self._killer, args=(proc,), daemon=True).start()
            final, errs = None, []
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line.startswith("PYDMFILE|"):
                    final = line.split("|", 1)[1].strip()
                elif line.startswith("PYDM"):
                    self._on_line(line)
                elif line.startswith("ERROR:"):
                    errs.append(line)
            code = proc.wait()
            if self.stop.is_set():
                self.status = "Pausado"
                return
            if code != 0:
                raise MediaError(_last_error("\n".join(errs)) or f"O yt-dlp terminou com erro (código {code}).")
            if not final:   # não imprimiu o caminho: procura o arquivo mais novo da pasta
                news = [os.path.join(self.folder, n) for n in os.listdir(self.folder)]
                news = [p for p in news if os.path.isfile(p) and os.path.getmtime(p) >= started - 2]
                final = max(news, key=os.path.getmtime) if news else None
            if not final:
                raise MediaError("O arquivo final não foi encontrado.")
            if not os.path.isabs(final):
                final = os.path.join(self.folder, final)
            if not os.path.isfile(final):
                raise MediaError("O arquivo final não foi encontrado.")
            self.path, self.name = final, os.path.basename(final)
            self.folder = os.path.dirname(final)
            self.size = os.path.getsize(final)
            self.last_try = time.time()
            shutil.rmtree(self.jobdir, ignore_errors=True)
            self.media.pop("overwrite", None)
            self.status = "Concluído"
        except MediaError as ex:
            msg = str(ex)
            self.error = ("Faltam componentes (yt-dlp). Veja Opções > Vídeo." if msg == "no_engine" else msg)
            self.failed, self.status = True, "Erro"
        except Exception as ex:
            self.error, self.failed, self.status = f"{type(ex).__name__}: {ex}", True, "Erro"
        finally:
            self.note, self.proc = "", None

    def cancel(self, delete_file=True):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        if delete_file and self.status != "Concluído":
            shutil.rmtree(self.jobdir, ignore_errors=True)

    def reset(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        shutil.rmtree(self.jobdir, ignore_errors=True)
        self._base = self._cur = 0
        self.error, self.failed, self.speed = "", False, 0.0
        self.path, self.status = None, "Na fila"
        self.media["overwrite"] = True   # o yt-dlp não regrava arquivos existentes, a menos que mandem


def detect_browser(ua, browsers):
    """Identifica o navegador pelo User-Agent (primeiro que combinar, na ordem da lista)."""
    ua = (ua or "").lower()
    for b in browsers:
        if b["ua"].lower() in ua:
            return b
    return None


def _match(value, patterns, prefix=False):
    for pat in patterns:
        pat = pat.strip().lower()
        if not pat:
            continue
        if prefix and not any(c in pat for c in "*?"):
            pat += "*"
        if fnmatch.fnmatchcase(value, pat):
            return True
    return False


def capture_decision(s, url, filename="", ua="", mods="", manual=False):
    """Decide se um link enviado pela extensão deve ser capturado pelo PyDM. Retorna (aceito, motivo).
    Se não for aceito, a Bridge responde 409 e a extensão deixa o navegador baixar normalmente."""
    if manual:                       # "Baixar com PyDM" no menu de contexto: sempre baixa
        return True, ""
    b = detect_browser(ua, s["browsers"])
    if b and not b["on"]:
        return False, "navegador desativado"
    keys = s["keys"]
    if mods and keys["prevent"] != "Nenhuma" and mods == keys["prevent"]:
        return False, "tecla de bloqueio"
    if mods and keys["force"] != "Nenhuma" and mods == keys["force"]:
        return True, ""              # tecla de forçar: ignora os filtros abaixo
    p = urllib.parse.urlparse(url)
    if _match((p.hostname or "").lower(), s["exclude_sites"]):
        return False, "site excluído"
    if _match(url.lower(), s["exclude_addrs"], prefix=True):
        return False, "endereço excluído"
    name = filename or urllib.parse.unquote(os.path.basename(p.path))
    ext = os.path.splitext(name)[1].lstrip(".").upper()
    # sem extensão, ou página dinâmica (download.php, get.aspx...): o nome real vem do servidor, então captura
    if ext and ext not in DYNAMIC_EXTS and not any(fnmatch.fnmatchcase(ext, pat.upper()) for pat in s["capture_exts"]):
        return False, "tipo de arquivo"
    return True, ""


WEB_EXTS = {"HTML", "HTM", "XHTML", "SHTML", "PHP", "ASP", "ASPX", "JSP", "CGI"}


def looks_like_download(text, s):
    """True se o texto copiado parece um link de arquivo: URL http(s) única cuja extensão está na lista
    de tipos de arquivos das Opções (ou é uma extensão que não seja de página web)."""
    if not text or re.search(r"\s", text) or not re.match(r"^https?://\S+$", text):
        return False
    ext = os.path.splitext(urllib.parse.unquote(urllib.parse.urlparse(text).path))[1].lstrip(".").upper()
    if not ext:
        return False
    if any(fnmatch.fnmatchcase(ext, pat.upper()) for pat in s["capture_exts"]):
        return True
    return ext not in WEB_EXTS and 2 <= len(ext) <= 5


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _allowed(self):
        o = self.headers.get("Origin")
        host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
        return host_ok and (o is None or o.startswith(("chrome-extension://", "moz-extension://")))

    def _send(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        o = self.headers.get("Origin")
        if o:
            self.send_header("Access-Control-Allow-Origin", o)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200 if self._allowed() else 403)

    def do_GET(self):
        if not self._allowed():
            return self._send(403)
        s = SETTINGS_REF["s"] or {}
        if self.path.split("?")[0] == "/config":   # a extensão monta o menu de contexto com isto
            return self._send(200, {"app": "PyDM", "version": 1,
                                    "title": s.get("menu_title") or DEFAULT_MENU_TITLE,
                                    "menu": s.get("menu") or DEFAULT_MENU,
                                    "video": {"enabled": bool((s.get("video") or DEFAULT_VIDEO)["enabled"])}})
        self._send(200, {"app": "PyDM", "version": 1})

    def _media(self):
        """/media/formats (lista as qualidades do vídeo da página) e /media/download (coloca na fila)."""
        s = SETTINGS_REF["s"]
        try:
            n = int(self.headers.get("Content-Length", 0))
            j = json.loads(self.rfile.read(min(n, 65536)))
            url = str(j["url"]).strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._send(400, {"error": "url"})
            clean = lambda v: re.sub(r"[\r\n]", "", str(v or ""))
            h = {}
            for k, key in (("referrer", "Referer"), ("cookie", "Cookie"), ("userAgent", "User-Agent")):
                if clean(j.get(k)):
                    h[key] = clean(j.get(k))
            mid = str(j.get("id") or "")
            est = int(j.get("est") or 0) or None
            title = clean(j.get("title"))[:150]
        except Exception:
            return self._send(400, {"error": "json"})
        if not s or not s["video"]["enabled"]:
            return self._send(200, {"ok": False, "error": "disabled",
                                    "message": "O download de vídeos está desligado nas Opções do PyDM."})
        if self.path == "/media/download":
            if not valid_media_id(mid):
                return self._send(400, {"error": "id"})
            INBOX.put({"media": {"url": url, "id": mid, "title": title, "est": est, "headers": h}})
            return self._send(200, {"ok": True})
        try:
            out = media_options(media_probe(url, h, s), s, media_tools(s))
            out["ok"] = True
            return self._send(200, out)
        except MediaError as ex:
            if str(ex) == "no_engine":
                TRAY_EVENTS.put(("media_setup",))
                return self._send(200, {"ok": False, "error": "no_engine",
                                        "message": "Faltam componentes. Confirme a instalação na janela do PyDM."})
            return self._send(200, {"ok": False, "error": "extract", "message": str(ex)})
        except Exception as ex:
            log(f"Análise de vídeo: {ex!r}")
            return self._send(200, {"ok": False, "error": "extract", "message": f"{type(ex).__name__}: {ex}"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403)
        if self.path == "/show":   # outra instância do PyDM pedindo para trazer esta janela de volta
            INBOX.put({"show": True})
            return self._send(200, {"ok": True, "app": "PyDM"})
        if self.path in ("/media/formats", "/media/download"):
            return self._media()
        if self.path != "/add":
            return self._send(403)
        try:
            n = int(self.headers.get("Content-Length", 0))
            j = json.loads(self.rfile.read(min(n, 65536)))
            url = str(j["url"]).strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._send(400, {"error": "url"})
            clean = lambda v: re.sub(r"[\r\n]", "", str(v or ""))
            h = {}
            for k, key in (("referrer", "Referer"), ("cookie", "Cookie"), ("userAgent", "User-Agent")):
                if clean(j.get(k)):
                    h[key] = clean(j.get(k))
            s = SETTINGS_REF["s"]
            if s:
                ok, why = capture_decision(s, url, clean(j.get("filename")), clean(j.get("userAgent")),
                                           clean(j.get("mods")), bool(j.get("manual")))
                if not ok:
                    return self._send(409, {"ok": False, "ignored": why})
            INBOX.put({"url": url, "headers": h})
            self._send(200, {"ok": True})
        except Exception:
            self._send(400, {"error": "json"})


def start_bridge():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Bridge)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def already_running():
    """True se já existe um PyDM rodando (e, nesse caso, pede para ele mostrar a janela)."""
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{PORT}/show", data=b"{}", method="POST",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=1.5) as resp:
            return json.loads(resp.read()).get("app") == "PyDM"
    except Exception:
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_cmd():
    """Comando gravado no registro: abre o PyDM direto na bandeja."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = sys.executable
    pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pw):
        exe = pw
    return f'"{exe}" "{os.path.abspath(__file__)}" --tray'


def autostart_get():
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "PyDM")
        return True
    except Exception:
        return False


def autostart_set(on):
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "PyDM", 0, winreg.REG_SZ, autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, "PyDM")
                except FileNotFoundError:
                    pass
        return True
    except Exception as ex:
        log(f"Início automático: {ex!r}")
        return False


def default_av_program():
    """Procura o Windows Defender (MpCmdRun.exe) para usar como verificador padrão."""
    if not sys.platform.startswith("win"):
        return ""
    cands = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe")]
    cands += sorted(glob.glob(os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft",
                                           "Windows Defender", "Platform", "*", "MpCmdRun.exe")), reverse=True)
    return next((c for c in cands if os.path.exists(c)), "")


def make_tray_image():
    """Ícone da bandeja: quadrado azul com uma seta de download (desenhado com o Pillow)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(29, 78, 216, 255))
    dr.polygon([(27, 12), (37, 12), (37, 29), (46, 29), (32, 45), (18, 29), (27, 29)], fill="white")
    dr.rectangle((16, 50, 48, 54), fill="white")
    return im


# ---------- janela "Informação do arquivo de download" ----------
class InfoDialog:
    """Janela exibida antes de iniciar um download: URL, categoria, pasta/nome, descrição.
    Botões: Baixar mais tarde / Iniciar download / Cancelar."""

    def __init__(self, app, url, headers):
        self.app, self.url, self.headers = app, url, headers
        self.cats = app.settings["categories"]  # mesmo dict das configurações
        self.result = {}      # preenchido pela thread de consulta ao servidor
        self.info = None      # (tamanho, aceita_range, nome) quando a consulta termina
        self.cat_touched = False

        self.guess = guess_name(url)
        cat = category_for(self.guess, self.cats)

        win = self.win = tk.Toplevel(app.root)
        win.title("Informação do arquivo de download")
        win.resizable(True, False)
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        f = ttk.Frame(win, padding=(12, 12, 12, 10))
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        self.cat_var = tk.StringVar(value=cat)
        self.path_var = tk.StringVar()
        self.folder_var = tk.StringVar()
        self.remember = tk.BooleanVar(value=bool(self.cats[cat].get("remember")))
        self.desc_var = tk.StringVar()
        self.size_var = tk.StringVar(value="Obtendo...")
        self.path_var.set(os.path.join(self.cats[cat]["folder"], self.guess))

        # URL (somente leitura, mas dá para selecionar e copiar)
        ttk.Label(f, text="URL").grid(row=0, column=0, sticky="w", pady=3)
        self.url_var = tk.StringVar(value=url)  # precisa ficar em self, senão o Tk apaga o texto do campo
        ttk.Entry(f, state="readonly", width=60, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=3)

        # Categoria + botão "+"
        ttk.Label(f, text="Categoria").grid(row=1, column=0, sticky="w", pady=3)
        cf = ttk.Frame(f)
        cf.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        self.cat_box = ttk.Combobox(cf, textvariable=self.cat_var, values=list(self.cats),
                                    state="readonly", width=28)
        self.cat_box.pack(side="left")
        self.cat_box.bind("<<ComboboxSelected>>", self.on_cat)
        ttk.Button(cf, text="+", width=3, command=self.add_cat).pack(side="left", padx=(6, 0))

        # Salvar como + botão "..."
        ttk.Label(f, text="Salvar como...").grid(row=2, column=0, sticky="w", pady=3)
        self.path_box = ttk.Combobox(f, textvariable=self.path_var, width=58)
        self.path_box.grid(row=2, column=1, sticky="ew", pady=3)
        self.path_box.bind("<KeyRelease>", lambda e: self.sync_folder())
        self.path_box.bind("<<ComboboxSelected>>", lambda e: self.sync_folder())
        ttk.Button(f, text="...", width=3, command=self.browse).grid(row=2, column=2, padx=(6, 0), pady=3)

        # Grupo "Relembrar este caminho para a categoria ..."
        self.chk = ttk.Checkbutton(f, variable=self.remember, command=self.sync_folder)
        lf = ttk.LabelFrame(f, labelwidget=self.chk)
        lf.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(6, 3))
        ttk.Entry(lf, textvariable=self.folder_var, state="disabled").pack(fill="x", padx=8, pady=(2, 8))

        # Ícone do arquivo + tamanho (lado direito)
        side = ttk.Frame(f)
        side.grid(row=0, column=3, rowspan=4, sticky="n", padx=(18, 0))
        try:
            bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        except tk.TclError:
            bg = "#f0f0f0"
        self.icon = tk.Canvas(side, width=54, height=66, highlightthickness=0, bd=0, bg=bg)
        self.icon.pack(pady=(4, 0))
        ttk.Label(side, textvariable=self.size_var, anchor="center", width=14).pack(pady=(4, 0))

        # Descrição
        ttk.Label(f, text="Descrição").grid(row=4, column=0, sticky="w", pady=(8, 3))
        ttk.Entry(f, textvariable=self.desc_var).grid(row=4, column=1, columnspan=3, sticky="ew", pady=(8, 3))

        # Botões
        bf = ttk.Frame(f)
        bf.grid(row=5, column=0, columnspan=4, pady=(14, 0))
        for txt, cmd in (("Baixar mais tarde", self.later), ("Iniciar download", self.start),
                         ("Cancelar", self.cancel)):
            ttk.Button(bf, text=txt, width=18, command=cmd).pack(side="left", padx=6)
        win.bind("<Return>", lambda e: self.start())
        win.bind("<Escape>", lambda e: self.cancel())

        self.draw_icon(self.guess)
        self.sync_folder()
        self.refresh_paths()
        self.place_window()

        threading.Thread(target=self._probe_thread, daemon=True).start()
        self.poll()

    # ---------- aparência ----------
    def place_window(self):
        """Centraliza e traz para a frente (o foco costuma estar no navegador)."""
        w = self.win
        try:
            w.update_idletasks()
            x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
            y = (w.winfo_screenheight() - w.winfo_reqheight()) // 3
            w.geometry(f"+{max(x, 0)}+{max(y, 0)}")
            w.deiconify()
            w.lift()
            w.attributes("-topmost", True)
            w.after(400, self._untop)
            w.focus_force()
        except tk.TclError:
            pass

    def _untop(self):
        try:
            self.win.attributes("-topmost", False)
        except tk.TclError:
            pass

    def draw_icon(self, name):
        ext = os.path.splitext(name)[1].lstrip(".")[:4].upper()
        c = self.icon
        c.delete("all")
        c.create_polygon(8, 4, 34, 4, 46, 16, 46, 62, 8, 62, fill="white", outline="#8a8a8a")
        c.create_polygon(34, 4, 34, 16, 46, 16, fill="#e4e4e4", outline="#8a8a8a")
        if ext:
            c.create_text(27, 44, text=ext, fill="#666666", font=("Segoe UI", 8, "bold"))

    # ---------- consulta ao servidor (em segundo plano, sem travar a janela) ----------
    def _probe_thread(self):
        try:
            self.result["res"] = probe(self.url, self.headers)
        except Exception as ex:
            self.result["err"] = str(ex)
        self.result["done"] = True

    def poll(self):
        if not self.win.winfo_exists():
            return
        if not self.result.get("done"):
            self.win.after(150, self.poll)
            return
        res = self.result.get("res")
        if not res:
            self.size_var.set("Desconhecido")
            return
        self.info = res
        size, _resumable, name = res
        self.size_var.set(fmt_size(size, 2).replace(".", ",") if size is not None else "Desconhecido")
        # Se o usuário ainda não mexeu no nome, troca o nome provisório (da URL) pelo real
        # (Content-Disposition / URL final) e reavalia a categoria.
        folder, cur = os.path.split(self.path_var.get())
        if cur == self.guess:
            if (not self.cat_touched and
                    os.path.normpath(folder) == os.path.normpath(self.cats[self.cat_var.get()]["folder"])):
                cat = category_for(name, self.cats)
                self.cat_var.set(cat)
                self.remember.set(bool(self.cats[cat].get("remember")))
                folder = self.cats[cat]["folder"]
            self.path_var.set(os.path.join(folder, name))
        self.draw_icon(os.path.basename(self.path_var.get()))
        self.sync_folder()
        self.refresh_paths()

    # ---------- ações dos campos ----------
    def sync_folder(self):
        """Atualiza o texto do grupo 'Relembrar...' e a pasta mostrada nele."""
        cat = self.cat_var.get()
        self.chk.configure(text=f'Relembrar este caminho para a categoria "{cat}"')
        if self.remember.get():
            folder = os.path.dirname(self.path_var.get())
        else:
            folder = self.cats.get(cat, {}).get("folder", "")
        self.folder_var.set(folder.rstrip("\\/") + os.sep if folder else "")

    def refresh_paths(self):
        """Lista suspensa do 'Salvar como': pasta da categoria + pastas usadas recentemente."""
        name = os.path.basename(self.path_var.get())
        folders = []
        for fo in [self.cats.get(self.cat_var.get(), {}).get("folder")] + list(self.app.settings["recent"]):
            if fo and fo not in folders:
                folders.append(fo)
        self.path_box.configure(values=[os.path.join(fo, name) for fo in folders])

    def on_cat(self, _event=None):
        cat = self.cat_var.get()
        if cat not in self.cats:
            return
        self.cat_touched = True
        self.remember.set(bool(self.cats[cat].get("remember")))
        name = os.path.basename(self.path_var.get())
        self.path_var.set(os.path.join(self.cats[cat]["folder"], name))
        self.sync_folder()
        self.refresh_paths()

    def add_cat(self):
        name = simpledialog.askstring("Nova categoria", "Nome da categoria:", parent=self.win)
        name = (name or "").strip()
        if not name:
            return
        if name not in self.cats:
            folder = filedialog.askdirectory(parent=self.win, title=f'Pasta padrão da categoria "{name}"',
                                             initialdir=DEFAULT_FOLDER)
            if not folder:
                return
            self.cats[name] = {"folder": os.path.normpath(folder), "exts": []}
            save_cfg(self.app.settings)
            self.cat_box.configure(values=list(self.cats))
        self.cat_var.set(name)
        self.on_cat()

    def browse(self):
        cur = self.path_var.get()
        p = filedialog.asksaveasfilename(parent=self.win, title="Salvar como",
                                         initialdir=os.path.dirname(cur) or None,
                                         initialfile=os.path.basename(cur),
                                         confirmoverwrite=False)  # quem pergunta é o PyDM
        if p:
            self.path_var.set(os.path.normpath(p))
            self.sync_folder()
            self.refresh_paths()

    # ---------- botões ----------
    def commit(self, start):
        raw = self.path_var.get().strip().strip('"')
        folder, name = os.path.split(raw)
        cat = self.cat_var.get()
        if not name:
            messagebox.showerror("Erro", 'Informe o nome do arquivo em "Salvar como".', parent=self.win)
            return
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        if not folder:
            folder = self.cats.get(cat, {}).get("folder", DEFAULT_FOLDER)
        path = os.path.join(os.path.abspath(os.path.expanduser(folder)), name)
        ok = self.app.register(self.win, self.url, self.headers, path, cat, self.desc_var.get().strip(),
                               self.info, self.remember.get(), start)
        if ok:
            self.win.destroy()
        return ok

    def start(self):
        self.commit(True)

    def later(self):
        if self.commit(False) and self.app.settings["show_final_later"]:
            messagebox.showinfo("PyDM", "O download foi adicionado à fila principal.\n"
                                        "Use \"Iniciar fila\" quando quiser baixar.", parent=self.app.root)

    def cancel(self):
        self.win.destroy()


def power_action(mode, force=False):
    """Desliga, reinicia, hiberna ou suspende o computador."""
    try:
        if sys.platform.startswith("win"):
            f = ["/f"] if force else []
            cmds = {"Desligar": ["shutdown", "/s", "/t", "0"] + f,
                    "Reiniciar": ["shutdown", "/r", "/t", "0"] + f,
                    "Hibernar": ["shutdown", "/h"],
                    "Em espera": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}
        elif sys.platform == "darwin":
            cmds = {"Desligar": ["osascript", "-e", 'tell app "System Events" to shut down'],
                    "Reiniciar": ["osascript", "-e", 'tell app "System Events" to restart'],
                    "Hibernar": ["pmset", "sleepnow"], "Em espera": ["pmset", "sleepnow"]}
        else:
            cmds = {"Desligar": ["systemctl", "poweroff"], "Reiniciar": ["systemctl", "reboot"],
                    "Hibernar": ["systemctl", "hibernate"], "Em espera": ["systemctl", "suspend"]}
        subprocess.Popen(cmds[mode], creationflags=NOWIN)
    except Exception:
        pass


class ShutdownCountdown:
    """Contagem regressiva antes de desligar/reiniciar, para o usuário poder cancelar."""

    def __init__(self, app, mode, force, secs=30):
        self.app, self.mode, self.force, self.left = app, mode, force, secs
        self.job = None
        win = self.win = tk.Toplevel(app.root)
        win.title("PyDM")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.var = tk.StringVar()
        f = ttk.Frame(win, padding=16)
        f.pack()
        ttk.Label(f, text="Todos os downloads foram concluídos.", font=("Segoe UI", 10, "bold")).pack()
        ttk.Label(f, textvariable=self.var).pack(pady=10)
        ttk.Button(f, text="Cancelar", width=14, command=self.cancel).pack()
        center_front(win)
        self.step()

    def step(self):
        if self.left <= 0:
            self.win.destroy()
            power_action(self.mode, self.force)
            return
        self.var.set(f"Ação: {self.mode.lower()} o computador em {self.left} segundo(s).")
        self.left -= 1
        self.job = self.win.after(1000, self.step)

    def cancel(self):
        if self.job:
            try:
                self.win.after_cancel(self.job)
            except tk.TclError:
                pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class ProgressDialog:
    """Janela de progresso do download (estilo IDM): estado, tamanho, velocidade, tempo restante,
    barra de progresso e, em "detalhes", a posição e o progresso de cada conexão.
    Fechar a janela NÃO cancela o download: ele continua em segundo plano."""

    def __init__(self, app, d):
        self.app, self.d = app, d
        self.job = None
        self.details = True
        win = self.win = tk.Toplevel(app.root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())
        f = ttk.Frame(win, padding=10)
        f.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(f)
        self.nb.pack(fill="x")
        tab1 = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab1, text="Progresso do download")
        box = tk.Frame(tab1, bg="white", bd=1, relief="solid", padx=10, pady=8)
        box.pack(fill="x")
        box.columnconfigure(0, minsize=200)
        box.columnconfigure(1, weight=1)
        self.v = {k: tk.StringVar() for k in ("url", "estado", "tamanho", "baixado", "vel", "eta", "cap")}
        tk.Label(box, textvariable=self.v["url"], bg="white", anchor="w", width=76).grid(
            row=0, column=0, columnspan=2, sticky="w")
        tk.Label(box, text="Estado:", bg="white", anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 10))
        self.estado_lbl = tk.Label(box, textvariable=self.v["estado"], bg="white", fg="#0033cc", anchor="w")
        self.estado_lbl.grid(row=1, column=1, sticky="w", pady=(4, 10))
        for i, (txt, key) in enumerate((("Tamanho", "tamanho"), ("Baixando", "baixado"),
                                        ("Velocidade", "vel"), ("Tempo restante", "eta"),
                                        ("Capacidade de continuar", "cap")), start=2):
            tk.Label(box, text=txt, bg="white", anchor="w").grid(row=i, column=0, sticky="w")
            tk.Label(box, textvariable=self.v[key], bg="white", anchor="w").grid(row=i, column=1, sticky="w")

        ui = app.settings["progress_ui"]
        if ui["speed_tab"]:
            self.build_speed_tab()
        if ui["done_tab"]:
            self.build_done_tab()

        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.pack(fill="x", pady=10)

        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(0, 6))
        self.btn_det = ttk.Button(bf, text="<< Ocultar detalhes", command=self.toggle_details)
        self.btn_det.pack(side="left")
        ttk.Button(bf, text="Cancelar", width=12, command=self.cancel).pack(side="right")
        self.btn_pause = ttk.Button(bf, text="Pausar", width=12, command=self.toggle_pause)
        self.btn_pause.pack(side="right", padx=8)

        self.det = ttk.Frame(f)
        self.det.pack(fill="both", expand=True)
        ttk.Label(self.det, text="Posição de início e progresso do download por conexões").pack()
        self.map = tk.Canvas(self.det, height=14, bg="#e6e6e6", highlightthickness=1,
                             highlightbackground="#9a9a9a")
        self.map.pack(fill="x", pady=(4, 8))
        tf = ttk.Frame(self.det)
        tf.pack(fill="both", expand=True)
        self.tbl = ttk.Treeview(tf, columns=("n", "baixado", "info"), show="headings", height=7)
        for c, t, w in (("n", "N.", 40), ("baixado", "Baixado", 120), ("info", "Informações", 330)):
            self.tbl.heading(c, text=t)
            self.tbl.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tbl.yview)
        self.tbl.configure(yscrollcommand=sb.set)
        self.tbl.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        if not ui["details"]:
            self.toggle_details()
        self.refresh()
        center_front(win, offset=26 * (len(app.progress) % 6))

    # ---------- aba "Limitar velocidade" ----------
    def build_speed_tab(self):
        d = self.d
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Limitar velocidade")
        row = ttk.Frame(t)
        row.pack(fill="x")
        ttk.Label(row, text="Velocidade", width=12).pack(side="left")
        ttk.Label(row, textvariable=self.v["vel"]).pack(side="left")

        self.lim_on = tk.BooleanVar(value=d.limit_kbps > 0)
        self.lim_val = tk.StringVar(value=str(d.limit_kbps or 1024))
        self.lim_rem = tk.BooleanVar(value=d.limit_remember)
        ttk.Checkbutton(t, text="Limitar a velocidade", variable=self.lim_on,
                        command=self.apply_limit).pack(anchor="w", pady=(10, 4))
        ttk.Label(t, text="Velocidade máxima do download:").pack(anchor="w", padx=(18, 0))
        r2 = ttk.Frame(t)
        r2.pack(anchor="w", padx=(18, 0), pady=(2, 10))
        self.lim_spin = ttk.Spinbox(r2, from_=1, to=1000000, increment=10, width=10,
                                    textvariable=self.lim_val, command=self.apply_limit)
        self.lim_spin.pack(side="left")
        self.lim_spin.bind("<FocusOut>", lambda e: self.apply_limit())
        self.lim_spin.bind("<Return>", lambda e: self.apply_limit())
        ttk.Label(r2, text="KBytes/seg").pack(side="left", padx=6)
        ttk.Checkbutton(t, text="Lembrar configurações de velocidade para este arquivo",
                        variable=self.lim_rem, command=self.apply_limit).pack(anchor="w", pady=(0, 6))
        ttk.Button(t, text="Ocultar aba", command=lambda: self.nb.select(0)).pack(anchor="e")
        self.apply_limit(first=True)

    def apply_limit(self, first=False):
        try:
            kb = int(float(self.lim_val.get().replace(",", ".")))
        except ValueError:
            kb = 1024
        kb = max(1, min(1000000, kb))
        self.lim_val.set(str(kb))
        on = self.lim_on.get()
        self.lim_spin.configure(state="normal" if on else "disabled")
        if not first:
            self.d.set_limit(kb if on else 0, self.lim_rem.get())

    # ---------- aba "Opções ao concluir" ----------
    def build_done_tab(self):
        d, od = self.d, self.d.on_done
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Opções ao concluir")
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="Salvar em:").pack(side="left")
        ttk.Label(top, text=d.path or d.folder).pack(side="left", padx=12)

        self.v_msg = tk.BooleanVar(value=d.show_msg if d.show_msg is not None else self.app.notify_var.get())
        self.v_modem = tk.BooleanVar(value=od["modem"])
        self.v_quit = tk.BooleanVar(value=od["quit"])
        self.v_shut = tk.BooleanVar(value=od["shutdown"])
        self.v_force = tk.BooleanVar(value=od["force"])
        self.v_mode = tk.StringVar(value=od["mode"] if od["mode"] in DONE_MODES else DONE_MODES[0])
        self.v_info = tk.StringVar()

        ttk.Checkbutton(t, text="Ver mensagem de download completo", variable=self.v_msg,
                        command=self.save_done).pack(anchor="w", pady=(8, 6))
        ttk.Separator(t).pack(fill="x", pady=2)
        ttk.Label(t, textvariable=self.v_info).pack(anchor="w", pady=(4, 6))
        self.cb_modem = ttk.Checkbutton(t, text="Desconectar o modem quando terminar",
                                        variable=self.v_modem, command=self.save_done)
        self.cb_modem.pack(anchor="w", pady=1)
        self.cb_quit = ttk.Checkbutton(t, text="Sair do PyDM quando terminar",
                                       variable=self.v_quit, command=self.save_done)
        self.cb_quit.pack(anchor="w", pady=1)
        sh = ttk.Frame(t)
        sh.pack(fill="x", pady=1)
        self.cb_shut = ttk.Checkbutton(sh, text="Desligar o computador quando terminar",
                                       variable=self.v_shut, command=self.save_done)
        self.cb_shut.pack(side="left")
        self.combo = ttk.Combobox(sh, textvariable=self.v_mode, values=DONE_MODES, state="readonly", width=12)
        self.combo.pack(side="left", padx=16)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.save_done())
        self.cb_force = ttk.Checkbutton(t, text="Forçar o término dos processos",
                                        variable=self.v_force, command=self.save_done)
        self.cb_force.pack(anchor="w", padx=(22, 0), pady=1)
        ttk.Button(t, text="Ocultar aba", command=lambda: self.nb.select(0)).pack(anchor="e", pady=(6, 0))
        self.refresh_done_state()

    def refresh_done_state(self):
        show = self.v_msg.get()
        self.v_info.set('Configurações indisponíveis quando "Ver mensagem de download completo" está ativado'
                        if show else "Ao concluir, o PyDM executará as ações marcadas abaixo (sem mostrar mensagem)")
        for cb in (self.cb_modem, self.cb_quit, self.cb_shut):
            cb.configure(state="disabled" if show else "normal")
        sh_on = (not show) and self.v_shut.get()
        self.cb_force.configure(state="normal" if sh_on else "disabled")
        self.combo.configure(state="readonly" if sh_on else "disabled")

    def save_done(self):
        d = self.d
        d.show_msg = bool(self.v_msg.get())
        d.on_done.update(modem=bool(self.v_modem.get()), quit=bool(self.v_quit.get()),
                         shutdown=bool(self.v_shut.get()), mode=self.v_mode.get(),
                         force=bool(self.v_force.get()))
        self.refresh_done_state()

    # ---------- botões ----------
    def toggle_details(self):
        if self.details:
            self.det.pack_forget()
            self.btn_det.configure(text="Mostrar detalhes >>")
        else:
            self.det.pack(fill="both", expand=True)
            self.btn_det.configure(text="<< Ocultar detalhes")
        self.details = not self.details

    def toggle_pause(self):
        d = self.d
        if d.status == "Baixando":
            d.pause()
        elif d.status == "Na fila":
            d.status = "Pausado"
        elif d.status in ("Pausado", "Erro"):
            d.status = "Na fila"

    def cancel(self):
        if messagebox.askyesno("Cancelar download",
                               "Cancelar e remover este download da lista?\n"
                               "(O arquivo incompleto será apagado.)", parent=self.win):
            self.app.remove_items([self.d])

    def close(self):
        if self.job:
            try:
                self.win.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        self.app.progress.pop(id(self.d), None)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ---------- atualização periódica ----------
    def draw_map(self):
        c, d = self.map, self.d
        c.delete("all")
        if not d.size:
            return
        w = max(c.winfo_width(), 100)
        for s, e, p in list(d.segs):
            if e < 0:
                continue
            x0 = s * w / d.size
            x1 = (s + max(0, min(p, e + 1) - s)) * w / d.size
            if x1 > x0:
                c.create_rectangle(x0, 1, x1, 15, fill="#1d4ed8", width=0)
            c.create_line(x0, 0, x0, 16, fill="#666666")

    def refresh(self):
        d = self.d
        dl, st = d.downloaded, d.status
        pct = dl * 100 / d.size if d.size else None
        self.win.title((f"{pct:.0f}% " if pct is not None else "") + (d.name or guess_name(d.url)))
        self.v["url"].set(d.url)
        if st == "Baixando":
            estado = d.note or ("Recebendo..." if dl > 0 else "Conectando...")
        elif st == "Na fila":
            estado = "Na fila..."
        elif st == "Erro":
            estado = "Erro: " + (d.error or "falha no download")[:70]
        else:
            estado = st
        self.v["estado"].set(estado)
        self.estado_lbl.configure(fg="#cc0000" if st == "Erro" else "#0033cc")
        self.v["tamanho"].set(pt(fmt_size(d.size, 3)) if d.size else "Desconhecido")
        self.v["baixado"].set(pt(fmt_size(dl, 3)) + (pt(f" ( {pct:.2f} % )") if pct is not None else ""))
        self.v["vel"].set(pt(fmt_size(d.speed, 3)) + "/seg" if d.speed else "")
        self.v["eta"].set(fmt_eta((d.size - dl) / d.speed) if d.size and d.speed > 1 else "--")
        self.v["cap"].set(("Sim" if d.resumable else "Não") if d.probed else "--")
        self.bar["value"] = pct or 0
        self.btn_pause.configure(text="Pausar" if st in ("Baixando", "Na fila") else "Iniciar",
                                 state="disabled" if st == "Concluído" else "normal")
        if self.details:
            self.draw_map()
            for n, got, info in d.seg_rows():
                vals = (n, pt(fmt_size(got, 3)), info)
                iid = str(n - 1)
                if self.tbl.exists(iid):
                    self.tbl.item(iid, values=vals)
                else:
                    self.tbl.insert("", "end", iid=iid, values=vals)
        self.job = self.win.after(300, self.refresh)


class CompleteDialog:
    """Janela "Download completo": Abrir / Abrir com... / Abrir pasta / Fechar."""

    def __init__(self, app, d):
        self.app, self.d = app, d
        win = self.win = tk.Toplevel(app.root)
        win.title("Download completo")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())
        try:
            size = os.path.getsize(d.path)
        except OSError:
            size = d.size or 0
        self.url_var = tk.StringVar(value=d.url)    # precisam ficar em self, senão o Tk apaga o texto
        self.path_var = tk.StringVar(value=d.path)
        self.mute = tk.BooleanVar(value=False)

        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        top = ttk.Frame(f)
        top.pack(fill="x")
        try:
            bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        except tk.TclError:
            bg = "#f0f0f0"
        ic = tk.Canvas(top, width=54, height=66, highlightthickness=0, bd=0, bg=bg)
        ic.pack(side="left")
        ic.create_polygon(8, 4, 34, 4, 46, 16, 46, 62, 8, 62, fill="white", outline="#8a8a8a")
        ic.create_polygon(34, 4, 34, 16, 46, 16, fill="#e4e4e4", outline="#8a8a8a")
        ic.create_oval(26, 42, 50, 66, fill="#2e9e44", outline="white", width=2)
        ic.create_text(38, 54, text="\u2713", fill="white", font=("Segoe UI", 11, "bold"))
        tx = ttk.Frame(top)
        tx.pack(side="left", padx=12)
        ttk.Label(tx, text="Download completo", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(tx, text=f"Baixado {pt(fmt_size(size, 2))} ({size} Bytes)").pack(anchor="w", pady=(4, 0))

        ttk.Label(f, text="Endereço:").pack(anchor="w", pady=(12, 2))
        ttk.Entry(f, state="readonly", textvariable=self.url_var, width=70).pack(fill="x")
        ttk.Label(f, text="O arquivo foi salvo como:").pack(anchor="w", pady=(10, 2))
        ttk.Entry(f, state="readonly", textvariable=self.path_var).pack(fill="x")

        bf = ttk.Frame(f)
        bf.pack(pady=(14, 8))
        for txt, cmd in (("Abrir", self.open), ("Abrir com...", self.open_with),
                         ("Abrir pasta", self.open_folder), ("Fechar", self.close)):
            ttk.Button(bf, text=txt, width=14, command=cmd).pack(side="left", padx=4)
        ttk.Checkbutton(f, text="Não mostrar esta mensagem na próxima vez",
                        variable=self.mute).pack(anchor="w")
        center_front(win)

    def open(self):
        App._open(self.d.path)
        self.close()

    def open_with(self):
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", self.d.path])
            else:
                App._open(self.d.path)
        except Exception:
            pass
        self.close()

    def open_folder(self):
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(self.d.path)}"])
            else:
                App._open(os.path.dirname(self.d.path))
        except Exception:
            pass
        self.close()

    def close(self):
        if self.mute.get():
            self.app.notify_var.set(False)
            self.app.save_notify()
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# ---------- barra de ferramentas (ícones vetoriais desenhados no Canvas) ----------
def _fc(on, color, off="#bdbdbd"):
    return color if on else off


def ico_add(c, cx, cy, on):
    a, b = 4.5, 15
    pts = [(-a, -b), (a, -b), (a, -a), (b, -a), (b, a), (a, a), (a, b), (-a, b), (-a, a), (-b, a), (-b, -a), (-a, -a)]
    c.create_polygon([v for x, y in pts for v in (cx + x, cy + y)],
                     fill=_fc(on, "#3cb371", "#dcdcdc"), outline=_fc(on, "#2a8f58"))


def ico_start(c, cx, cy, on):
    k = _fc(on, "#3a78b5")
    c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, outline=k, width=2)
    c.create_rectangle(cx - 2, cy - 8, cx + 2, cy + 1, fill=k, outline=k)
    c.create_polygon(cx - 7, cy, cx + 7, cy, cx, cy + 8, fill=k, outline=k)


def ico_stop(c, cx, cy, on):
    k = _fc(on, "#c0504d")
    c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, outline=k, width=2)
    c.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5, fill=k, outline=k)


def ico_hand(c, cx, cy, on):
    fill, edge = _fc(on, "#f3c969", "#e8e8e8"), _fc(on, "#a8791a")
    c.create_rectangle(cx - 9, cy - 1, cx + 9, cy + 13, fill=fill, outline=edge)
    for x, top in ((-9, -9), (-4, -13), (1, -14), (6, -11)):
        c.create_rectangle(cx + x, cy + top, cx + x + 4, cy, fill=fill, outline=edge)
    c.create_polygon(cx - 9, cy + 8, cx - 15, cy + 1, cx - 12, cy - 1, cx - 9, cy + 3, fill=fill, outline=edge)


def ico_trash(c, cx, cy, on, check=False):
    k, fill = _fc(on, "#a0306f"), _fc(on, "#f6e6f1", "#f2f2f2")
    c.create_polygon(cx - 9, cy - 5, cx + 9, cy - 5, cx + 7, cy + 14, cx - 7, cy + 14,
                     fill=fill, outline=k, width=2)
    c.create_rectangle(cx - 11, cy - 9, cx + 11, cy - 6, fill=k, outline=k)
    c.create_rectangle(cx - 3, cy - 12, cx + 3, cy - 9, outline=k, width=2)
    if check:
        c.create_line(cx - 4, cy + 3, cx - 1, cy + 7, cx + 5, cy - 1, fill=_fc(on, "#2e9e44"), width=3)
    else:
        for dx in (-3, 0, 3):
            c.create_line(cx + dx, cy - 1, cx + dx, cy + 10, fill=k)


def ico_trash_ok(c, cx, cy, on):
    ico_trash(c, cx, cy, on, check=True)


def ico_gear(c, cx, cy, on):
    k, fill = _fc(on, "#4a62a8"), _fc(on, "#dfe6f7", "#efefef")
    pts, n = [], 8
    for i in range(n):
        a = 2 * math.pi * i / n
        for r, da in ((10.5, -0.26), (14.5, -0.16), (14.5, 0.16), (10.5, 0.26)):
            pts += [cx + r * math.cos(a + da), cy + r * math.sin(a + da)]
    c.create_polygon(pts, fill=fill, outline=k, width=2)
    c.create_oval(cx - 4.5, cy - 4.5, cx + 4.5, cy + 4.5, fill=c.cget("bg"), outline=k, width=2)


def ico_clock(c, cx, cy, on):
    k = _fc(on, "#8a4f9e")
    c.create_oval(cx - 15, cy - 14, cx - 6, cy - 5, fill=k, outline=k)
    c.create_oval(cx + 6, cy - 14, cx + 15, cy - 5, fill=k, outline=k)
    c.create_oval(cx - 13, cy - 10, cx + 13, cy + 16, outline=k, width=2, fill=_fc(on, "#fbf3fd", "#f4f4f4"))
    c.create_line(cx, cy + 3, cx, cy - 5, fill=k, width=2)
    c.create_line(cx, cy + 3, cx + 6, cy + 6, fill=k, width=2)


def ico_queue(c, cx, cy, on, kind):
    k, fill = _fc(on, "#3a6fb8"), _fc(on, "#d9e7fa", "#f0f0f0")
    for d in (0, 4, 8):
        c.create_rectangle(cx - 14 + d, cy - 14 + d, cx + 2 + d, cy - 2 + d, fill=fill, outline=k)
    bx, by = cx + 9, cy + 9
    c.create_oval(bx - 7, by - 7, bx + 7, by + 7, fill=_fc(on, "#2e9e44" if kind == "start" else "#c0504d"),
                  outline="white", width=1)
    if kind == "start":
        c.create_polygon(bx - 4, by - 1, bx + 4, by - 1, bx, by + 4, fill="white", outline="white")
        c.create_rectangle(bx - 1, by - 5, bx + 1, by - 1, fill="white", outline="white")
    else:
        c.create_rectangle(bx - 3, by - 3, bx + 3, by + 3, fill="white", outline="white")


class Tooltip:
    """Dica que aparece ao parar o mouse sobre um widget."""

    def __init__(self, widget, text):
        self.w, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._sched, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _sched(self, _e=None):
        self._hide()
        self.job = self.w.after(700, self._show)

    def _show(self):
        self.job = None
        try:
            x, y = self.w.winfo_rootx() + 10, self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg="#ffffe1", fg="black", relief="solid", bd=1,
                     padx=4, pady=1, font=("Segoe UI", 9)).pack()
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class ToolButton(tk.Canvas):
    """Botão grande da barra de ferramentas: ícone + texto embaixo, com destaque ao passar o mouse.
    Se receber 'menu', mostra uma setinha à direita que abre uma lista de opções."""
    H = 62

    def __init__(self, master, text, draw, command, menu=None, tip=""):
        self._text, self._draw, self._command, self._menu = text, draw, command, menu
        self._iw = 64
        self._bw = self._iw + (16 if menu else 0)
        super().__init__(master, width=self._bw, height=self.H, highlightthickness=0, bd=0,
                         bg=master.winfo_toplevel().cget("bg"))
        self._enabled, self._hover, self._pressed = True, False, False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        if tip:
            Tooltip(self, tip)
        self.redraw()

    def _set(self, **kw):
        if "hover" in kw:
            self._hover = kw["hover"]
        if "pressed" in kw:
            self._pressed = kw["pressed"]
        self.redraw()

    def _release(self, e):
        was = self._pressed
        self._set(pressed=False)
        if not (was and self._enabled and 0 <= e.x < self._bw and 0 <= e.y < self.H):
            return
        if self._menu and e.x > self._iw:
            m = self._menu()
            m.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.H)
            m.grab_release()
        else:
            self._command()

    def set_enabled(self, on):
        if on != self._enabled:
            self._enabled = on
            self.redraw()

    def redraw(self):
        self.delete("all")
        on = self._enabled
        if on and (self._hover or self._pressed):
            self.create_rectangle(0, 0, self._bw - 1, self.H - 1, outline="#99c8f0",
                                  fill="#cce4f7" if self._pressed else "#e5f1fb")
        self._draw(self, self._iw // 2, 23, on)
        self.create_text(self._iw // 2, 50, text=self._text, fill="#000000" if on else "#8a8a8a",
                         font=("Segoe UI", 9))
        if self._menu:
            x, y = self._bw - 9, 27
            self.create_polygon(x - 4, y - 2, x + 4, y - 2, x, y + 3, fill="#333333" if on else "#b0b0b0",
                                outline="")


# ---------- janelas auxiliares ----------
def make_modal(win, parent):
    win.transient(parent)
    try:
        win.wait_visibility()
        win.grab_set()
    except tk.TclError:
        pass


def ask_delete(app, msg, title="Excluir"):
    """Confirmação com a opção de apagar também os arquivos do disco. Retorna (confirmou, apagar_arquivos)."""
    win = tk.Toplevel(app.root)
    win.title(title)
    win.resizable(False, False)
    res, var = {"ok": False}, tk.BooleanVar(value=False)
    f = ttk.Frame(win, padding=14)
    f.pack()
    ttk.Label(f, text=msg, justify="left").pack(anchor="w")
    ttk.Checkbutton(f, text="Excluir também os arquivos do disco", variable=var).pack(anchor="w", pady=(10, 0))
    bf = ttk.Frame(f)
    bf.pack(pady=(14, 0))

    def done(ok):
        res["ok"] = ok
        win.destroy()

    ttk.Button(bf, text="Sim", width=12, command=lambda: done(True)).pack(side="left", padx=6)
    ttk.Button(bf, text="Não", width=12, command=lambda: done(False)).pack(side="left", padx=6)
    win.protocol("WM_DELETE_WINDOW", lambda: done(False))
    win.bind("<Escape>", lambda e: done(False))
    center_front(win)
    make_modal(win, app.root)
    app.root.wait_window(win)
    return res["ok"], var.get()


class BatchDialog:
    """Adicionar vários downloads de uma vez (um endereço por linha)."""

    def __init__(self, app, urls=None):
        self.app = app
        win = self.win = tk.Toplevel(app.root)
        win.title("Adicionar download em lote")
        win.transient(app.root)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Endereços dos arquivos (um por linha):").pack(anchor="w")
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True, pady=(4, 8))
        self.txt = tk.Text(tf, width=84, height=12, wrap="none", undo=True)
        sb = ttk.Scrollbar(tf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        if urls is None:
            try:
                urls = [l.strip() for l in app.root.clipboard_get().splitlines()
                        if l.strip().lower().startswith(("http://", "https://"))]
            except tk.TclError:
                urls = []
        self.txt.insert("1.0", "\n".join(urls))

        self.where = tk.StringVar(value="auto")
        self.folder = tk.StringVar(value=DEFAULT_FOLDER)
        self.start = tk.BooleanVar(value=False)
        ttk.Radiobutton(f, text="Salvar na pasta de cada categoria (automático)", variable=self.where,
                        value="auto").pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=2)
        ttk.Radiobutton(r, text="Salvar em:", variable=self.where, value="custom").pack(side="left")
        ttk.Entry(r, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="...", width=3, command=self.browse).pack(side="left")
        ttk.Checkbutton(f, text="Iniciar os downloads agora (senão, ficam na fila principal)",
                        variable=self.start).pack(anchor="w", pady=(8, 0))
        bf = ttk.Frame(f)
        bf.pack(pady=(12, 0))
        ttk.Button(bf, text="OK", width=12, command=self.ok).pack(side="left", padx=6)
        ttk.Button(bf, text="Cancelar", width=12, command=win.destroy).pack(side="left", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())
        center_front(win)

    def browse(self):
        p = filedialog.askdirectory(parent=self.win, initialdir=self.folder.get() or DEFAULT_FOLDER)
        if p:
            self.folder.set(os.path.normpath(p))
            self.where.set("custom")

    def ok(self):
        lines = [l.strip() for l in self.txt.get("1.0", "end").splitlines() if l.strip()]
        urls = [l for l in lines if l.lower().startswith(("http://", "https://"))]
        if not urls:
            messagebox.showerror("Erro", "Informe pelo menos um endereço http/https válido.", parent=self.win)
            return
        folder = self.folder.get().strip() if self.where.get() == "custom" else None
        n = self.app.add_batch(urls, folder, self.start.get())
        self.win.destroy()
        skipped = len(lines) - len(urls)
        if n == 0:
            messagebox.showinfo("PyDM", "Todos os endereços já estavam na lista.", parent=self.app.root)
        else:
            msg = f"{n} download(s) " + ("iniciado(s)." if self.start.get() else "adicionado(s) à fila principal.")
            if skipped:
                msg += f"\n{skipped} linha(s) sem endereço válido foram ignoradas."
            if self.app.settings["show_final_batch"] or skipped:
                messagebox.showinfo("PyDM", msg, parent=self.app.root)


class PropertiesDialog:
    """Propriedades do download (a descrição pode ser editada)."""

    def __init__(self, app, d):
        self.app, self.d = app, d
        win = self.win = tk.Toplevel(app.root)
        win.title("Propriedades do download")
        win.transient(app.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        def dt(t):
            return time.strftime("%b %d %H:%M:%S %Y", time.localtime(t)) if t else "-"
        dl = d.downloaded
        rows = (("Nome", d.name or guess_name(d.url)), ("Salvar em", d.path or d.folder), ("Endereço", d.url),
                ("Tamanho", f"{pt(fmt_size(d.size, 2))} ({d.size} bytes)" if d.size else "Desconhecido"),
                ("Baixado", pt(fmt_size(dl, 2))), ("Status", d.status + (f": {d.error}" if d.error else "")),
                ("Categoria", d.category or "-"), ("Conexões", str(d.conns)),
                ("Capacidade de continuar", ("Sim" if d.resumable else "Não") if d.probed else "-"),
                ("Na fila principal", "Sim" if d.queued else "Não"),
                ("Adicionado em", dt(d.added)), ("Última tentativa", dt(d.last_try)))
        self.vars = []   # precisam ficar em self, senão o Tk apaga o texto dos campos
        for i, (k, v) in enumerate(rows):
            ttk.Label(f, text=k).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            var = tk.StringVar(value=v)
            self.vars.append(var)
            ttk.Entry(f, textvariable=var, state="readonly", width=70).grid(row=i, column=1, sticky="ew", pady=2)
        ttk.Label(f, text="Descrição").grid(row=len(rows), column=0, sticky="w", pady=(8, 2))
        self.desc = tk.StringVar(value=d.description)
        ttk.Entry(f, textvariable=self.desc).grid(row=len(rows), column=1, sticky="ew", pady=(8, 2))
        bf = ttk.Frame(f)
        bf.grid(row=len(rows) + 1, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(bf, text="OK", width=12, command=self.ok).pack(side="left", padx=6)
        ttk.Button(bf, text="Cancelar", width=12, command=win.destroy).pack(side="left", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())
        center_front(win)

    def ok(self):
        self.d.description = self.desc.get().strip()
        self.app.save()
        self.win.destroy()


class SubDialog:
    """Base das pequenas janelas abertas a partir das Opções: modal, centralizada, com OK/Cancelar."""

    def __init__(self, parent, title, padding=12):
        win = self.win = tk.Toplevel(parent)
        win.title(title)
        win.transient(parent)
        win.resizable(False, False)
        self.f = ttk.Frame(win, padding=padding)
        self.f.pack(fill="both", expand=True)
        win.bind("<Escape>", lambda e: win.destroy())

    def buttons(self):
        bf = ttk.Frame(self.f)
        bf.pack(pady=(12, 0))
        ttk.Button(bf, text="OK", width=12, command=self.ok).pack(side="left", padx=6)
        ttk.Button(bf, text="Cancelar", width=12, command=self.win.destroy).pack(side="left", padx=6)
        center_front(self.win)
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

    def ok(self):
        self.win.destroy()


class AddressListDialog(SubDialog):
    """Lista de endereços (um por linha) que o PyDM não deve baixar automaticamente."""

    def __init__(self, parent, items, on_ok):
        super().__init__(parent, "Endereços que não devem iniciar o download automaticamente")
        self.on_ok = on_ok
        ttk.Label(self.f, justify="left",
                  text="Um endereço por linha. Use * e ? como curinga.\n"
                       "Sem curinga, vale como início do endereço (ex.: https://exemplo.com/privado/).").pack(anchor="w")
        tf = ttk.Frame(self.f)
        tf.pack(fill="both", expand=True, pady=(6, 0))
        self.txt = tk.Text(tf, width=64, height=12, wrap="none", undo=True)
        sb = ttk.Scrollbar(tf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.txt.insert("1.0", "\n".join(items))
        self.buttons()

    def ok(self):
        self.on_ok([l.strip() for l in self.txt.get("1.0", "end").splitlines() if l.strip()])
        self.win.destroy()


class KeysDialog(SubDialog):
    """Teclas que o usuário segura ao clicar num link para impedir ou forçar a captura pelo PyDM."""

    def __init__(self, parent, keys, on_ok):
        super().__init__(parent, "Teclas para impedir/forçar downloads")
        self.on_ok = on_ok
        self.v_prevent = tk.StringVar(value=keys["prevent"])
        self.v_force = tk.StringVar(value=keys["force"])
        ttk.Label(self.f, justify="left",
                  text="Segure a tecla enquanto clica no link no navegador.\n"
                       "Funciona com a extensão do PyDM instalada.").pack(anchor="w", pady=(0, 8))
        for text, var in (("Impedir o PyDM de baixar (o navegador baixa):", self.v_prevent),
                          ("Forçar o PyDM a baixar (ignora tipos de arquivo e sites excluídos):", self.v_force)):
            ttk.Label(self.f, text=text).pack(anchor="w", pady=(6, 2))
            ttk.Combobox(self.f, textvariable=var, values=KEY_CHOICES, state="readonly", width=16).pack(anchor="w")
        self.buttons()

    def ok(self):
        p, fo = self.v_prevent.get(), self.v_force.get()
        if p == fo and p != "Nenhuma":
            messagebox.showerror("Teclas", "Impedir e forçar não podem usar a mesma combinação.", parent=self.win)
            return
        self.on_ok({"prevent": p, "force": fo})
        self.win.destroy()


class ContextMenuDialog(SubDialog):
    """Quais itens 'Baixar com PyDM' aparecem no menu de clique direito do navegador."""

    def __init__(self, parent, menu, title, on_ok):
        super().__init__(parent, "Menu de contexto do navegador")
        self.on_ok = on_ok
        self.v_title = tk.StringVar(value=title)
        self.vars = {k: tk.BooleanVar(value=bool(menu.get(k))) for k, _ in MENU_LABELS}
        r = ttk.Frame(self.f)
        r.pack(fill="x")
        ttk.Label(r, text="Texto do item:").pack(side="left")
        ttk.Entry(r, textvariable=self.v_title, width=30).pack(side="left", padx=8)
        lf = ttk.LabelFrame(self.f, text="Mostrar o item ao clicar com o botão direito em", padding=8)
        lf.pack(fill="x", pady=10)
        for k, label in MENU_LABELS:
            ttk.Checkbutton(lf, text=label, variable=self.vars[k]).pack(anchor="w", pady=1)
        ttk.Label(self.f, justify="left", foreground="#555555",
                  text="A extensão atualiza o menu em até 1 minuto\n(ou ao reiniciar o navegador).").pack(anchor="w")
        self.buttons()

    def ok(self):
        self.on_ok({k: bool(v.get()) for k, v in self.vars.items()}, self.v_title.get().strip() or DEFAULT_MENU_TITLE)
        self.win.destroy()


class ProgressCustomDialog(SubDialog):
    """Personaliza a janela 'Progresso do download'."""

    def __init__(self, parent, ui, on_ok):
        super().__init__(parent, 'Customizar diálogo "Progresso de download"')
        self.on_ok = on_ok
        self.vars = {}
        ttk.Label(self.f, text="A janela de progresso deve mostrar:").pack(anchor="w", pady=(0, 6))
        for k, label in (("speed_tab", 'Aba "Limitar velocidade"'), ("done_tab", 'Aba "Opções ao concluir"'),
                         ("details", "Detalhes das conexões já abertos (barra e tabela de conexões)")):
            self.vars[k] = tk.BooleanVar(value=bool(ui.get(k, True)))
            ttk.Checkbutton(self.f, text=label, variable=self.vars[k]).pack(anchor="w", pady=2)
        ttk.Label(self.f, text="Vale para as próximas janelas de progresso.", foreground="#555555").pack(
            anchor="w", pady=(8, 0))
        self.buttons()

    def ok(self):
        self.on_ok({k: bool(v.get()) for k, v in self.vars.items()})
        self.win.destroy()


class AntivirusDialog(SubDialog):
    """Verificação contra vírus: roda um programa externo no arquivo assim que o download termina."""

    def __init__(self, parent, av, on_ok):
        super().__init__(parent, "Configurar verificação contra vírus")
        self.on_ok = on_ok
        self.v_on = tk.BooleanVar(value=bool(av["enabled"]))
        self.v_prog = tk.StringVar(value=av["program"])
        self.v_args = tk.StringVar(value=av["args"])
        ttk.Checkbutton(self.f, text="Verificar o arquivo contra vírus quando o download terminar",
                        variable=self.v_on).pack(anchor="w")
        ttk.Label(self.f, text="Programa antivírus (linha de comando):").pack(anchor="w", pady=(10, 2))
        r = ttk.Frame(self.f)
        r.pack(fill="x")
        ttk.Entry(r, textvariable=self.v_prog, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="...", width=3, command=self.browse).pack(side="left", padx=(6, 0))
        found = default_av_program()
        ttk.Label(self.f, foreground="#555555",
                  text="Em branco = " + (f"Windows Defender ({found})" if found else "nenhum encontrado")).pack(anchor="w")
        ttk.Label(self.f, text="Parâmetros ({file} = caminho do arquivo baixado):").pack(anchor="w", pady=(10, 2))
        ttk.Entry(self.f, textvariable=self.v_args, width=64).pack(fill="x")
        ttk.Button(self.f, text="Padrão (Windows Defender)",
                   command=lambda: (self.v_prog.set(""), self.v_args.set(DEFAULT_AV["args"]))).pack(anchor="e", pady=(8, 0))
        self.buttons()

    def browse(self):
        p = filedialog.askopenfilename(parent=self.win, title="Programa antivírus",
                                       filetypes=[("Programas", "*.exe"), ("Todos os arquivos", "*.*")])
        if p:
            self.v_prog.set(os.path.normpath(p))

    def ok(self):
        self.on_ok({"enabled": bool(self.v_on.get()), "program": self.v_prog.get().strip(),
                    "args": self.v_args.get().strip() or DEFAULT_AV["args"]})
        self.win.destroy()


class ComponentInstaller:
    """Baixa yt-dlp, Deno e ffmpeg para ~/.pydm/bin (só Windows) mostrando o progresso."""
    LABEL = {"yt-dlp": "yt-dlp (extrai os vídeos dos sites)",
             "deno": "Deno (necessário para o YouTube)",
             "ffmpeg": "ffmpeg (junta vídeo e áudio e converte para MP3/WAV)"}

    def __init__(self, app, names, on_done=None):
        self.app, self.names, self.on_done = app, list(names), on_done
        self.q, self.cancel_flag = queue.Queue(), threading.Event()
        win = self.win = tk.Toplevel(app.root)
        win.title("Instalando componentes de vídeo")
        win.transient(app.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        self.lbl = ttk.Label(f, text="Preparando...", width=64, justify="left")
        self.lbl.pack(anchor="w")
        self.bar = ttk.Progressbar(f, length=440, maximum=100)
        self.bar.pack(fill="x", pady=10)
        ttk.Button(f, text="Cancelar", command=self.cancel).pack()
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        center_front(win)
        self.finished = False
        threading.Thread(target=self._worker, daemon=True).start()
        self.poll()

    def cancel(self):
        self.cancel_flag.set()

    # ---------- thread de download ----------
    def _download(self, url, dest, label):
        tmp = dest + ".part"
        with urllib.request.urlopen(req(url), timeout=30) as r, open(tmp, "wb") as f:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while True:
                if self.cancel_flag.is_set():
                    raise InterruptedError
                b = r.read(256 * 1024)
                if not b:
                    break
                f.write(b)
                got += len(b)
                self.q.put(("prog", label, got, total))
        return tmp

    def _extract(self, zpath, wanted):
        """Tira do zip só os arquivos pedidos (nome final -> sufixo do caminho dentro do zip)."""
        import zipfile
        with zipfile.ZipFile(zpath) as z:
            for out, suffix in wanted.items():
                member = next((n for n in z.namelist() if n.replace("\\", "/").endswith(suffix)), None)
                if not member:
                    raise IOError(f"{suffix} não foi encontrado no pacote baixado.")
                dest = os.path.join(BIN_DIR, out)
                with z.open(member) as src, open(dest + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(dest + ".tmp", dest)

    def _worker(self):
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
            for name in self.names:
                label = self.LABEL[name]
                url = COMPONENT_URLS[name]
                tmp = self._download(url, os.path.join(BIN_DIR, name + ".download"), label)
                self.q.put(("work", label))
                if name == "yt-dlp":
                    os.replace(tmp, os.path.join(BIN_DIR, "yt-dlp.exe"))
                elif name == "deno":
                    self._extract(tmp, {"deno.exe": "deno.exe"})
                    os.remove(tmp)
                else:
                    self._extract(tmp, {"ffmpeg.exe": "/bin/ffmpeg.exe", "ffprobe.exe": "/bin/ffprobe.exe"})
                    os.remove(tmp)
            self.q.put(("done", True, ""))
        except InterruptedError:
            self.q.put(("done", False, "cancelado"))
        except Exception as ex:
            log(f"Instalação de componentes: {ex!r}")
            self.q.put(("done", False, f"{type(ex).__name__}: {ex}"))
        finally:   # sobras de downloads interrompidos
            for n in os.listdir(BIN_DIR) if os.path.isdir(BIN_DIR) else []:
                if n.endswith((".download", ".download.part", ".tmp")):
                    try:
                        os.remove(os.path.join(BIN_DIR, n))
                    except OSError:
                        pass

    # ---------- interface ----------
    def poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "prog":
                    _, label, got, total = ev
                    self.lbl.configure(text=f"Baixando {label}\n{fmt_size(got)}" + (f" de {fmt_size(total)}" if total else ""))
                    self.bar["value"] = got * 100 / total if total else 0
                elif ev[0] == "work":
                    self.lbl.configure(text=f"Instalando {ev[1]}...")
                    self.bar["value"] = 100
                elif ev[0] == "done":
                    self.finished = True
                    self.win.destroy()
                    if ev[1]:
                        messagebox.showinfo("PyDM", "Componentes instalados.\nOs botões de vídeo já podem baixar.",
                                            parent=self.app.root)
                    elif ev[2] != "cancelado":
                        messagebox.showerror("PyDM", f"Não foi possível instalar:\n{ev[2][:300]}", parent=self.app.root)
                    if self.on_done:
                        self.on_done()
                    return
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.win.after(150, self.poll)


class OptionsDialog:
    """Opções (organizadas como no IDM): Geral, Tipos de arquivos, Salvar em, Downloads e Velocidade."""

    def __init__(self, app):
        self.app = app
        s = app.settings
        # cópias de trabalho: só viram configuração de verdade em "Aplicar"/"OK"
        self.cats = copy.deepcopy(s["categories"])
        self.browsers = copy.deepcopy(s["browsers"])
        self.keys = dict(s["keys"])
        self.menu, self.menu_title = dict(s["menu"]), s["menu_title"]
        self.excl_addrs = list(s["exclude_addrs"])
        self.prog_ui = dict(s["progress_ui"])
        self.av = dict(s["antivirus"])
        win = self.win = tk.Toplevel(app.root)
        win.title("Opções")
        win.transient(app.root)
        win.resizable(False, False)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.build_general(nb)
        self.build_types(nb)
        self.build_save(nb)
        self.build_downloads(nb)
        self.build_video(nb)
        self.build_speed(nb)

        bf = ttk.Frame(win)
        bf.pack(pady=10)
        ttk.Button(bf, text="OK", width=12, command=self.ok).pack(side="left", padx=6)
        ttk.Button(bf, text="Cancelar", width=12, command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bf, text="Aplicar", width=12, command=self.apply).pack(side="left", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())
        center_front(win)

    # ================= aba Geral =================
    def build_general(self, nb):
        app, s = self.app, self.app.settings
        g = ttk.Frame(nb, padding=12)
        nb.add(g, text="Geral")
        self.v_conns = tk.IntVar(value=s["conns"])
        self.v_max = tk.IntVar(value=s["max_active"])
        self.v_clip = tk.BooleanVar(value=app.clip_var.get())
        self.v_clip_auto = tk.BooleanVar(value=s["clip_auto"])
        self.v_tray = tk.BooleanVar(value=s["close_to_tray"])
        self.v_auto = tk.BooleanVar(value=autostart_get())
        self.v_auto0 = self.v_auto.get()

        for txt, var, hi in (("Conexões por download:", self.v_conns, 32), ("Downloads simultâneos:", self.v_max, 10)):
            r = ttk.Frame(g)
            r.pack(anchor="w", pady=2)
            ttk.Label(r, text=txt, width=28).pack(side="left")
            ttk.Spinbox(r, from_=1, to=hi, width=5, textvariable=var).pack(side="left")

        lf = ttk.LabelFrame(g, text="Navegador / integração com o sistema", padding=8)
        lf.pack(fill="x", pady=(8, 4))
        r = ttk.Frame(lf)
        r.pack(fill="x")
        self.lbl_bridge = ttk.Label(r, text=self.bridge_text())
        self.lbl_bridge.pack(side="left")
        ttk.Button(r, text="Reiniciar", command=self.restart_bridge).pack(side="right")
        ttk.Checkbutton(lf, text="Iniciar o PyDM automaticamente com o Windows (direto na bandeja)",
                        variable=self.v_auto,
                        state="normal" if sys.platform.startswith("win") else "disabled").pack(anchor="w", pady=(6, 1))
        ttk.Checkbutton(lf, text="Monitorar a área de transferência (perguntar ao copiar um link)",
                        variable=self.v_clip, command=self.clip_state).pack(anchor="w", pady=1)
        self.cb_clip_auto = ttk.Checkbutton(lf, text="Iniciar download automático de URLs da área de transferência",
                                            variable=self.v_clip_auto)
        self.cb_clip_auto.pack(anchor="w", padx=(22, 0), pady=1)
        ttk.Checkbutton(lf, text="Ao fechar a janela, continuar na bandeja do sistema" +
                        ("" if app.tray else "  (indisponível: sem pystray/pillow)"),
                        variable=self.v_tray, state="normal" if app.tray else "disabled").pack(anchor="w", pady=1)
        self.clip_state()

        bf = ttk.LabelFrame(g, text="Capturar downloads nos seguintes navegadores:", padding=6)
        bf.pack(fill="x", pady=4)
        tf = ttk.Frame(bf)
        tf.pack(fill="x")
        self.bt = ttk.Treeview(tf, show="tree", height=4, selectmode="browse")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.bt.yview)
        self.bt.configure(yscrollcommand=sb.set)
        self.bt.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.bt.bind("<Button-1>", lambda e: self.toggle_browser(self.bt.identify_row(e.y)))
        self.bt.bind("<space>", lambda e: self.toggle_browser((self.bt.selection() or [""])[0]))
        self.fill_browsers()
        r = ttk.Frame(bf)
        r.pack(fill="x", pady=(6, 0))
        ttk.Label(r, text="O navegador é identificado pelo User-Agent.", foreground="#555555").pack(side="left")
        ttk.Button(r, text="Remover", command=self.del_browser).pack(side="right", padx=(6, 0))
        ttk.Button(r, text="Adic. navegador...", command=self.add_browser).pack(side="right")

        for txt, cmd, label in (("Usar teclas para impedir/forçar downloads com o PyDM", self.edit_keys, "Teclas..."),
                                ("Personalizar o menu de contexto do PyDM no navegador", self.edit_menu, "Editar...")):
            r = ttk.Frame(g)
            r.pack(fill="x", pady=3)
            ttk.Label(r, text=txt).pack(side="left")
            ttk.Button(r, text=label, width=12, command=cmd).pack(side="right")

    def bridge_text(self):
        return (f"Integração com o navegador ativa (porta {PORT})" if self.app.bridge
                else f"Integração indisponível (porta {PORT} em uso)")

    def restart_bridge(self):
        ok = self.app.restart_bridge()
        self.lbl_bridge.configure(text=self.bridge_text())
        if not ok:
            messagebox.showwarning("PyDM", f"Não foi possível abrir a porta {PORT}.\n"
                                           "Feche o outro programa que a usa e tente de novo.", parent=self.win)

    def clip_state(self):
        self.cb_clip_auto.configure(state="normal" if self.v_clip.get() else "disabled")

    # ---------- navegadores ----------
    def fill_browsers(self):
        self.bt.delete(*self.bt.get_children())
        for i, b in enumerate(self.browsers):
            self.bt.insert("", "end", iid=str(i), text=self.browser_text(b))

    @staticmethod
    def browser_text(b):
        return ("\u2611  " if b["on"] else "\u2610  ") + b["name"]

    def toggle_browser(self, iid):
        if not iid:
            return
        b = self.browsers[int(iid)]
        b["on"] = not b["on"]
        self.bt.item(iid, text=self.browser_text(b))

    def add_browser(self):
        name = simpledialog.askstring("Adicionar navegador", "Nome do navegador:", parent=self.win)
        name = (name or "").strip()
        if not name:
            return
        ua = simpledialog.askstring("Adicionar navegador",
                                    "Trecho do User-Agent que identifica este navegador\n"
                                    "(ex.: Brave e Vivaldi usam o mesmo User-Agent do Chrome e não dá para separá-los):",
                                    parent=self.win)
        ua = (ua or "").strip()
        if not ua:
            return
        self.browsers.insert(0, {"name": name, "ua": ua, "on": True, "builtin": False})  # custom: testado primeiro
        self.fill_browsers()

    def del_browser(self):
        sel = self.bt.selection()
        if not sel:
            return
        if self.browsers[int(sel[0])].get("builtin"):
            messagebox.showinfo("PyDM", "Só os navegadores adicionados por você podem ser removidos.", parent=self.win)
            return
        del self.browsers[int(sel[0])]
        self.fill_browsers()

    def edit_keys(self):
        KeysDialog(self.win, self.keys, lambda k: self.keys.update(k))

    def edit_menu(self):
        def done(menu, title):
            self.menu, self.menu_title = menu, title
        ContextMenuDialog(self.win, self.menu, self.menu_title, done)

    # ================= aba Tipos de arquivos =================
    def build_types(self, nb):
        s = self.app.settings
        t = ttk.Frame(nb, padding=12)
        nb.add(t, text="Tipos de arquivos")
        ttk.Label(t, text="Iniciar download com os seguintes tipos de arquivos:").pack(anchor="w")
        self.txt_exts = tk.Text(t, width=64, height=6, wrap="word")
        self.txt_exts.pack(fill="x", pady=(3, 0))
        self.txt_exts.insert("1.0", " ".join(s["capture_exts"]))
        ttk.Button(t, text="Padrão", width=10,
                   command=lambda: self.set_text(self.txt_exts, DEFAULT_CAPTURE_EXTS)).pack(anchor="e", pady=4)

        ttk.Label(t, text="Não iniciar automaticamente o download dos seguintes sites:").pack(anchor="w", pady=(8, 0))
        self.txt_sites = tk.Text(t, width=64, height=4, wrap="word")
        self.txt_sites.pack(fill="x", pady=(3, 0))
        self.txt_sites.insert("1.0", " ".join(s["exclude_sites"]))
        r = ttk.Frame(t)
        r.pack(fill="x", pady=4)
        ttk.Label(r, text="(separar nomes com espaços; * e ? são curingas)").pack(side="left")
        ttk.Button(r, text="Padrão", width=10,
                   command=lambda: self.set_text(self.txt_sites, DEFAULT_EXCLUDE_SITES)).pack(side="right")

        ttk.Label(t, text="Não iniciar o download automaticamente dos seguintes endereços:").pack(anchor="w", pady=(8, 4))
        ttk.Button(t, text="Editar lista ...", width=18, command=self.edit_addrs).pack(anchor="w")
        ttk.Label(t, foreground="#555555", justify="left",
                  text="Estes filtros valem para os downloads capturados pelo navegador.\n"
                       "\"Baixar com PyDM\" (menu de contexto) e a tecla de forçar sempre baixam.\n"
                       "Links sem extensão ou de página dinâmica (ex.: download.php) são capturados."
                  ).pack(anchor="w", pady=(14, 0))

    @staticmethod
    def set_text(widget, words):
        widget.delete("1.0", "end")
        widget.insert("1.0", " ".join(words))

    @staticmethod
    def words(widget, upper=False):
        out = [w for w in re.split(r"[\s,;]+", widget.get("1.0", "end")) if w]
        return [w.upper() for w in out] if upper else out

    def edit_addrs(self):
        def done(items):
            self.excl_addrs = items
        AddressListDialog(self.win, self.excl_addrs, done)

    # ================= aba Salvar em =================
    def build_save(self, nb):
        s = self.app.settings
        c = ttk.Frame(nb, padding=12)
        nb.add(c, text="Salvar em")
        self.ct = ttk.Treeview(c, columns=("pasta", "exts"), show="tree headings", height=6, selectmode="browse")
        for cid, t, w in (("#0", "Categoria", 110), ("pasta", "Pasta", 250), ("exts", "Extensões", 220)):
            self.ct.heading(cid, text=t)
            self.ct.column(cid, width=w, anchor="w")
        self.ct.pack(fill="x")
        self.ct.bind("<<TreeviewSelect>>", self.load_fields)
        self.cur = None
        self.v_folder, self.v_exts = tk.StringVar(), tk.StringVar()
        self.v_remember = tk.BooleanVar()
        r = ttk.Frame(c)
        r.pack(fill="x", pady=(8, 2))
        ttk.Label(r, text="Pasta:", width=11).pack(side="left")
        ttk.Entry(r, textvariable=self.v_folder).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r, text="...", width=3, command=self.browse).pack(side="left")
        r = ttk.Frame(c)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="Extensões:", width=11).pack(side="left")
        ttk.Entry(r, textvariable=self.v_exts).pack(side="left", fill="x", expand=True, padx=4)
        self.chk_rem = ttk.Checkbutton(c, text="Lembrar a última pasta usada para esta categoria",
                                       variable=self.v_remember)
        self.chk_rem.pack(anchor="w", pady=(4, 0))
        r = ttk.Frame(c)
        r.pack(pady=(8, 0))
        ttk.Button(r, text="Atualizar categoria", command=self.commit_fields).pack(side="left", padx=4)
        ttk.Button(r, text="Nova categoria...", command=self.new_cat).pack(side="left", padx=4)
        ttk.Button(r, text="Excluir categoria", command=self.del_cat).pack(side="left", padx=4)
        self.fill_cats()

        self.v_mtime = tk.BooleanVar(value=s["set_mtime"])
        ttk.Checkbutton(c, text="Definir a data de criação do arquivo, assim como fornecida pelo servidor",
                        variable=self.v_mtime).pack(anchor="w", pady=(12, 4))
        lf = ttk.LabelFrame(c, text="Pasta temporária", padding=8)
        lf.pack(fill="x", pady=4)
        self.v_temp = tk.StringVar(value=s["temp_folder"])
        r = ttk.Frame(lf)
        r.pack(fill="x")
        ttk.Entry(r, textvariable=self.v_temp).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Procurar", command=self.browse_temp).pack(side="left", padx=(6, 0))
        ttk.Label(lf, foreground="#555555", justify="left",
                  text="Os arquivos parciais (.part) ficam aqui enquanto baixam e vão para a pasta de\n"
                       "destino ao concluir. Em branco = o parcial fica na própria pasta de destino.\n"
                       "Vale para os próximos downloads (os em andamento continuam onde estão)."
                  ).pack(anchor="w", pady=(6, 0))

    def browse_temp(self):
        p = filedialog.askdirectory(parent=self.win, initialdir=self.v_temp.get() or APP_DIR)
        if p:
            self.v_temp.set(os.path.normpath(p))

    def fill_cats(self, select=None):
        self.ct.delete(*self.ct.get_children())
        for name, info in self.cats.items():
            self.ct.insert("", "end", iid=name, text=name, values=(info["folder"], " ".join(info["exts"])))
        first = select if select in self.cats else next(iter(self.cats), None)
        if first:
            self.ct.selection_set(first)

    def load_fields(self, _e=None):
        s = self.ct.selection()
        self.cur = s[0] if s else None
        if self.cur in self.cats:
            info = self.cats[self.cur]
            self.v_folder.set(info["folder"])
            self.v_exts.set(" ".join(info["exts"]))
            self.v_remember.set(bool(info.get("remember")))
            self.chk_rem.configure(text=f'Lembrar a última pasta usada para a categoria "{self.cur}"')

    def commit_fields(self):
        if self.cur not in self.cats:
            return
        folder = self.v_folder.get().strip()
        if folder:
            self.cats[self.cur]["folder"] = os.path.normpath(folder)
        self.cats[self.cur]["exts"] = [e.lower().lstrip(".") for e in re.split(r"[,\s;]+", self.v_exts.get()) if e.strip(". ")]
        self.cats[self.cur]["remember"] = bool(self.v_remember.get())
        self.fill_cats(self.cur)

    def browse(self):
        p = filedialog.askdirectory(parent=self.win, initialdir=self.v_folder.get() or DEFAULT_FOLDER)
        if p:
            self.v_folder.set(os.path.normpath(p))

    def new_cat(self):
        name = simpledialog.askstring("Nova categoria", "Nome da categoria:", parent=self.win)
        name = (name or "").strip()
        if not name:
            return
        if name in self.cats:
            messagebox.showerror("Erro", "Já existe uma categoria com esse nome.", parent=self.win)
            return
        self.cats[name] = {"folder": os.path.join(DEFAULT_FOLDER, name), "exts": [], "remember": False}
        self.fill_cats(name)

    def del_cat(self):
        if not self.cur or self.cur == "Geral" or len(self.cats) <= 1:
            messagebox.showinfo("PyDM", 'A categoria "Geral" não pode ser excluída.', parent=self.win)
            return
        del self.cats[self.cur]
        self.cur = None
        self.fill_cats()

    # ================= aba Downloads =================
    def build_downloads(self, nb):
        app, s = self.app, self.app.settings
        d = ttk.Frame(nb, padding=12)
        nb.add(d, text="Downloads")
        self.v_notify = tk.BooleanVar(value=app.notify_var.get())
        self.v_start = tk.BooleanVar(value=s["show_start"])
        self.v_qonly = tk.BooleanVar(value=s["start_queue_only"])
        self.v_later = tk.BooleanVar(value=s["show_final_later"])
        self.v_batch = tk.BooleanVar(value=s["show_final_batch"])
        self.v_imtime = tk.BooleanVar(value=s["ignore_mtime"])
        self.v_dup = tk.StringVar(value=dict(DUP_ACTIONS)[s["dup_action"]])
        self.v_ua = tk.StringVar(value=s["manual_ua"])

        r = ttk.Frame(d)
        r.pack(fill="x")
        ttk.Label(r, text='Customizar diálogo "Progresso de download"').pack(side="left")
        ttk.Button(r, text="Editar...", width=10, command=self.edit_progress).pack(side="right")

        ttk.Checkbutton(d, text='Ver mensagem de início de download (janela "Informação do arquivo")',
                        variable=self.v_start, command=self.dl_state).pack(anchor="w", pady=(10, 2))
        self.cb_qonly = ttk.Checkbutton(d, text="Não começar a baixar, somente adicionar arquivos à fila",
                                        variable=self.v_qonly)
        self.cb_qonly.pack(anchor="w", padx=(22, 0))
        ttk.Checkbutton(d, text="Ver mensagem de download completo", variable=self.v_notify).pack(anchor="w", pady=(8, 0))
        ttk.Label(d, text="Nota: esta configuração não está relacionada com o processo de filas",
                  foreground="#555555").pack(anchor="w", padx=(22, 0))
        ttk.Checkbutton(d, text='Mostrar painel de final se pressionar "Baixar depois"',
                        variable=self.v_later).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(d, text="Mostrar painel de final se adicionar grupo de downloads",
                        variable=self.v_batch).pack(anchor="w", pady=2)
        ttk.Checkbutton(d, text="Ignorar mudanças de horário do arquivo quando continuar um download",
                        variable=self.v_imtime).pack(anchor="w", pady=2)
        ttk.Separator(d).pack(fill="x", pady=8)
        ttk.Label(d, text="Se um link duplicado for adicionado:").pack(anchor="w")
        ttk.Combobox(d, textvariable=self.v_dup, values=[t for _, t in DUP_ACTIONS], state="readonly",
                     width=60).pack(anchor="w", pady=(2, 8))
        ttk.Label(d, text="User-Agent para downloads adicionados manualmente:").pack(anchor="w")
        ttk.Entry(d, textvariable=self.v_ua).pack(fill="x", pady=(2, 8))
        r = ttk.Frame(d)
        r.pack(fill="x")
        ttk.Label(r, text="Configurar verificação contra vírus").pack(side="left")
        ttk.Button(r, text="Editar...", width=10, command=self.edit_av).pack(side="right")
        self.dl_state()

    def dl_state(self):
        # "somente adicionar à fila" só faz sentido quando a janela de informação não é mostrada
        self.cb_qonly.configure(state="disabled" if self.v_start.get() else "normal")

    def edit_progress(self):
        ProgressCustomDialog(self.win, self.prog_ui, lambda ui: self.prog_ui.update(ui))

    def edit_av(self):
        AntivirusDialog(self.win, self.av, lambda av: self.av.update(av))

    # ================= aba Vídeo =================
    def build_video(self, nb):
        v = self.app.settings["video"]
        t = ttk.Frame(nb, padding=12)
        nb.add(t, text="Vídeo")
        self.v_vid = tk.BooleanVar(value=v["enabled"])
        self.v_embed = tk.BooleanVar(value=v["embed_meta"])
        self.v_aud = {i: tk.BooleanVar(value=i in v["audio_formats"]) for i in AUDIO_PRESETS}
        self.v_vcat, self.v_acat = tk.StringVar(value=v["video_cat"]), tk.StringVar(value=v["audio_cat"])
        self.v_ytdlp, self.v_ffmpeg = tk.StringVar(value=v["ytdlp"]), tk.StringVar(value=v["ffmpeg"])

        ttk.Checkbutton(t, text='Mostrar o botão "Baixar este vídeo" sobre os vídeos das páginas (precisa da extensão)',
                        variable=self.v_vid).pack(anchor="w")
        lf = ttk.LabelFrame(t, text="Formatos de áudio oferecidos no painel", padding=8)
        lf.pack(fill="x", pady=8)
        for n, (i, p) in enumerate(AUDIO_PRESETS.items()):
            ttk.Checkbutton(lf, text=p["label"], variable=self.v_aud[i]).grid(row=n // 2, column=n % 2, sticky="w",
                                                                              padx=(0, 28), pady=1)
        ttk.Checkbutton(t, text="Gravar título, artista e capa nos arquivos de áudio",
                        variable=self.v_embed).pack(anchor="w")
        cats = list(self.cats)
        for text, var in (("Salvar vídeos na categoria:", self.v_vcat), ("Salvar áudios na categoria:", self.v_acat)):
            r = ttk.Frame(t)
            r.pack(anchor="w", pady=(6, 0))
            ttk.Label(r, text=text, width=28).pack(side="left")
            ttk.Combobox(r, textvariable=var, values=cats, state="readonly", width=20).pack(side="left")

        cf = ttk.LabelFrame(t, text="Componentes (o PyDM usa estes programas para baixar vídeos de sites)", padding=8)
        cf.pack(fill="x", pady=10)
        self.lbl_comp = ttk.Label(cf, justify="left")
        self.lbl_comp.pack(anchor="w")
        for text, var in (("yt-dlp:", self.v_ytdlp), ("ffmpeg:", self.v_ffmpeg)):
            r = ttk.Frame(cf)
            r.pack(fill="x", pady=(6, 0))
            ttk.Label(r, text=text, width=9).pack(side="left")
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            ttk.Button(r, text="...", width=3, command=lambda v=var: self.pick_tool(v)).pack(side="left", padx=(6, 0))
        ttk.Label(cf, text="Em branco = o PyDM procura sozinho (pasta .pydm\\bin ou PATH).",
                  foreground="#555555").pack(anchor="w", pady=(2, 6))
        r = ttk.Frame(cf)
        r.pack(fill="x")
        ttk.Button(r, text="Instalar / completar componentes", command=self.install_components).pack(side="left")
        ttk.Button(r, text="Atualizar yt-dlp", command=self.update_ytdlp).pack(side="left", padx=8)
        self.refresh_components()

    def tools_now(self):
        return {"yt-dlp": find_tool("yt-dlp", self.v_ytdlp.get().strip()),
                "ffmpeg": find_tool("ffmpeg", self.v_ffmpeg.get().strip()), "deno": find_tool("deno")}

    def refresh_components(self):
        info = {"yt-dlp": "extrai os vídeos", "ffmpeg": "junta vídeo+áudio e converte", "deno": "necessário no YouTube"}
        lines = []
        for name, path in self.tools_now().items():
            lines.append(("\u2714 " if path else "\u2718 ") + f"{name}  ({info[name]}): " + (path or "não encontrado"))
        self.lbl_comp.configure(text="\n".join(lines))

    def pick_tool(self, var):
        p = filedialog.askopenfilename(parent=self.win, title="Escolha o programa",
                                       filetypes=[("Programas", "*.exe"), ("Todos os arquivos", "*.*")])
        if p:
            var.set(os.path.normpath(p))
            self.refresh_components()

    def install_components(self):
        if not sys.platform.startswith("win"):
            messagebox.showinfo("PyDM", "Instale o yt-dlp, o ffmpeg e o Deno pelo gerenciador de pacotes do seu sistema.",
                                parent=self.win)
            return
        missing = [n for n, p in self.tools_now().items() if not p]
        if not missing:
            if not messagebox.askyesno("PyDM", "Todos os componentes já foram encontrados.\n"
                                               "Baixar de novo para atualizar? (cerca de 230 MB)", parent=self.win):
                return
            missing = ["yt-dlp", "deno", "ffmpeg"]
        sizes = {"yt-dlp": "~20 MB", "deno": "~40 MB", "ffmpeg": "~190 MB"}
        if messagebox.askyesno("PyDM", "Baixar agora do GitHub:\n\n" +
                               "\n".join(f"\u2022 {ComponentInstaller.LABEL[n]}  {sizes[n]}" for n in missing),
                               parent=self.win):
            ComponentInstaller(self.app, missing, on_done=self.refresh_components)

    def update_ytdlp(self):
        tool = self.tools_now()["yt-dlp"]
        if not tool:
            messagebox.showinfo("PyDM", "O yt-dlp ainda não está instalado. Use \"Instalar / completar componentes\".",
                                parent=self.win)
            return

        def work():
            try:
                r = subprocess.run([tool, "-U"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=180, stdin=subprocess.DEVNULL, creationflags=NOWIN)
                TRAY_EVENTS.put(("ytdlp_update", r.returncode, (r.stdout or "") + (r.stderr or "")))
            except Exception as ex:
                TRAY_EVENTS.put(("ytdlp_update", None, str(ex)))
        threading.Thread(target=work, daemon=True).start()
        messagebox.showinfo("PyDM", "Verificando atualização do yt-dlp... O resultado aparece em instantes.",
                            parent=self.win)

    # ================= aba Velocidade =================
    def build_speed(self, nb):
        s = self.app.settings
        v = ttk.Frame(nb, padding=14)
        nb.add(v, text="Velocidade")
        self.v_lim_on = tk.BooleanVar(value=s["speed_limit_kbps"] > 0)
        self.v_lim = tk.StringVar(value=str(s["speed_limit_kbps"] or 1024))
        ttk.Checkbutton(v, text="Limitar a velocidade total de todos os downloads",
                        variable=self.v_lim_on, command=self.lim_state).pack(anchor="w")
        r = ttk.Frame(v)
        r.pack(anchor="w", padx=(20, 0), pady=6)
        self.lim_spin = ttk.Spinbox(r, from_=1, to=1000000, increment=50, width=10, textvariable=self.v_lim)
        self.lim_spin.pack(side="left")
        ttk.Label(r, text="KBytes/seg").pack(side="left", padx=6)
        ttk.Label(v, text="O limite é dividido entre os downloads em andamento.\n"
                          "Cada download ainda pode ter seu próprio limite (janela de progresso).",
                  foreground="#555555", justify="left").pack(anchor="w", pady=(10, 0))
        self.lim_state()

    def lim_state(self):
        self.lim_spin.configure(state="normal" if self.v_lim_on.get() else "disabled")

    # ================= aplicar =================
    def apply(self):
        app, s = self.app, self.app.settings

        def num(var, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(str(var.get()).replace(",", ".")))))
            except (ValueError, tk.TclError):
                return default
        s["conns"] = num(self.v_conns, 1, 32, s["conns"])
        s["max_active"] = num(self.v_max, 1, 10, s["max_active"])
        app.conns_var.set(s["conns"])
        if self.v_clip.get() and not app.clip_var.get():
            app.reset_clip_baseline()
        app.clip_var.set(self.v_clip.get())
        s["monitor_clipboard"] = bool(self.v_clip.get())
        s["clip_auto"] = bool(self.v_clip_auto.get())
        app.notify_var.set(self.v_notify.get())
        s["show_complete"] = bool(self.v_notify.get())
        if app.tray:
            s["close_to_tray"] = bool(self.v_tray.get())

        # integração com o navegador
        s["browsers"] = copy.deepcopy(self.browsers)
        s["keys"] = dict(self.keys)
        s["menu"], s["menu_title"] = dict(self.menu), self.menu_title

        # tipos de arquivos
        s["capture_exts"] = self.words(self.txt_exts, upper=True)
        s["exclude_sites"] = self.words(self.txt_sites)
        s["exclude_addrs"] = list(self.excl_addrs)

        # salvar em
        self.commit_fields()
        s["categories"] = copy.deepcopy(self.cats)
        s["set_mtime"] = bool(self.v_mtime.get())
        tmp = self.v_temp.get().strip()
        if tmp:
            try:
                os.makedirs(tmp, exist_ok=True)
                tmp = os.path.normpath(tmp)
            except OSError as ex:
                messagebox.showwarning("Pasta temporária", f"Não foi possível usar a pasta:\n{tmp}\n\n{ex}\n\n"
                                       "Os parciais continuarão na pasta de destino.", parent=self.win)
                tmp = ""
                self.v_temp.set("")
        s["temp_folder"] = tmp

        # downloads
        s["show_start"] = bool(self.v_start.get())
        s["start_queue_only"] = bool(self.v_qonly.get())
        s["show_final_later"] = bool(self.v_later.get())
        s["show_final_batch"] = bool(self.v_batch.get())
        s["ignore_mtime"] = bool(self.v_imtime.get())
        s["dup_action"] = next((k for k, t in DUP_ACTIONS if t == self.v_dup.get()), "ask")
        s["manual_ua"] = self.v_ua.get().strip() or UA
        s["progress_ui"] = dict(self.prog_ui)
        s["antivirus"] = dict(self.av)

        # vídeo
        v = s["video"]
        v["enabled"], v["embed_meta"] = bool(self.v_vid.get()), bool(self.v_embed.get())
        v["audio_formats"] = [i for i in AUDIO_PRESETS if self.v_aud[i].get()]
        v["video_cat"], v["audio_cat"] = self.v_vcat.get(), self.v_acat.get()
        v["ytdlp"], v["ffmpeg"] = self.v_ytdlp.get().strip(), self.v_ffmpeg.get().strip()

        # início automático com o Windows (fica no registro, não no settings.json)
        want = bool(self.v_auto.get())
        if want != self.v_auto0:
            if autostart_set(want) and autostart_get() == want:   # confere lendo de volta do registro
                self.v_auto0 = want
                log("Início automático " + (f"ativado: {autostart_cmd()}" if want else "desativado"))
                if want:
                    messagebox.showinfo("PyDM", "Início automático ativado.\n\nComando registrado no Windows:\n"
                                                f"{autostart_cmd()}\n\nNa próxima vez que você entrar no Windows o "
                                                "PyDM abre na bandeja (perto do relógio; talvez dentro da seta ^).",
                                        parent=self.win)
            else:
                self.v_auto.set(self.v_auto0)
                messagebox.showerror("PyDM", "Não foi possível alterar o início automático com o Windows.\n"
                                             "Detalhes em ~/.pydm/pydm.log.", parent=self.win)

        save_cfg(s)
        app.apply_runtime()
        app.set_global_limit(num(self.v_lim, 1, 1000000, 1024) if self.v_lim_on.get() else 0)
        app.rebuild_categories()

    def ok(self):
        self.apply()
        self.win.destroy()


class SchedulerDialog:
    """Agendador: inicia/para a fila principal em horários definidos e pode sair/desligar ao terminar."""

    def __init__(self, app):
        self.app = app
        sc = app.settings["schedule"]
        win = self.win = tk.Toplevel(app.root)
        win.title("Agendador")
        win.transient(app.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        self.v_en = tk.BooleanVar(value=sc["enabled"])
        ttk.Checkbutton(f, text="Ativar o agendamento da fila principal", variable=self.v_en).pack(anchor="w")

        lf = ttk.LabelFrame(f, text="Horários", padding=10)
        lf.pack(fill="x", pady=(10, 6))
        self.v_start_on, self.v_stop_on = tk.BooleanVar(value=sc["start_on"]), tk.BooleanVar(value=sc["stop_on"])
        self.sh, self.sm = self._hm(sc["start"])
        self.eh, self.em = self._hm(sc["stop"])
        for row, (txt, on, h, m) in enumerate((("Iniciar a fila às", self.v_start_on, self.sh, self.sm),
                                               ("Parar a fila às", self.v_stop_on, self.eh, self.em))):
            ttk.Checkbutton(lf, text=txt, variable=on, width=18).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Spinbox(lf, from_=0, to=23, width=3, wrap=True, format="%02.0f", textvariable=h).grid(row=row, column=1)
            ttk.Label(lf, text=":").grid(row=row, column=2)
            ttk.Spinbox(lf, from_=0, to=59, width=3, wrap=True, format="%02.0f", textvariable=m).grid(row=row, column=3)

        df = ttk.LabelFrame(f, text="Dias da semana", padding=10)
        df.pack(fill="x", pady=6)
        self.v_days = [tk.BooleanVar(value=i in sc["days"]) for i in range(7)]
        for i, name in enumerate(DAYS):
            ttk.Checkbutton(df, text=name, variable=self.v_days[i]).pack(side="left", padx=4)

        af = ttk.LabelFrame(f, text="Quando a fila terminar", padding=10)
        af.pack(fill="x", pady=6)
        self.v_quit, self.v_shut = tk.BooleanVar(value=sc["quit"]), tk.BooleanVar(value=sc["shutdown"])
        self.v_mode = tk.StringVar(value=sc["mode"] if sc["mode"] in DONE_MODES else DONE_MODES[0])
        self.v_force = tk.BooleanVar(value=sc["force"])
        ttk.Checkbutton(af, text="Sair do PyDM", variable=self.v_quit).pack(anchor="w")
        r = ttk.Frame(af)
        r.pack(anchor="w", pady=2)
        ttk.Checkbutton(r, text="Desligar o computador", variable=self.v_shut).pack(side="left")
        ttk.Combobox(r, textvariable=self.v_mode, values=DONE_MODES, state="readonly", width=12).pack(side="left", padx=10)
        ttk.Checkbutton(af, text="Forçar o término dos processos", variable=self.v_force).pack(anchor="w", padx=(22, 0))
        ttk.Label(f, text="O PyDM precisa estar aberto (pode ficar na bandeja) para o agendamento funcionar.",
                  foreground="#555555").pack(anchor="w", pady=(6, 0))

        bf = ttk.Frame(f)
        bf.pack(pady=(12, 0))
        ttk.Button(bf, text="Iniciar fila agora", command=self.run_now).pack(side="left", padx=6)
        ttk.Button(bf, text="OK", width=10, command=self.ok).pack(side="left", padx=6)
        ttk.Button(bf, text="Cancelar", width=10, command=win.destroy).pack(side="left", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())
        center_front(win)

    @staticmethod
    def _hm(txt):
        try:
            h, m = txt.split(":")
            return tk.StringVar(value=f"{int(h):02d}"), tk.StringVar(value=f"{int(m):02d}")
        except ValueError:
            return tk.StringVar(value="00"), tk.StringVar(value="00")

    @staticmethod
    def _t(h, m):
        try:
            return f"{max(0, min(23, int(float(h.get())))):02d}:{max(0, min(59, int(float(m.get())))):02d}"
        except (ValueError, tk.TclError):
            return "00:00"

    def save(self):
        sc = self.app.settings["schedule"]
        sc.update(enabled=bool(self.v_en.get()), start_on=bool(self.v_start_on.get()),
                  start=self._t(self.sh, self.sm), stop_on=bool(self.v_stop_on.get()),
                  stop=self._t(self.eh, self.em), days=[i for i in range(7) if self.v_days[i].get()],
                  quit=bool(self.v_quit.get()), shutdown=bool(self.v_shut.get()),
                  mode=self.v_mode.get(), force=bool(self.v_force.get()))
        save_cfg(self.app.settings)

    def ok(self):
        self.save()
        self.win.destroy()

    def run_now(self):
        self.save()
        self.win.destroy()
        self.app.start_queue(scheduled=True)


class App:
    def __init__(self, root):
        self.root = root
        root.title("PyDM - Gerenciador de Downloads")
        root.geometry("1080x580")
        root.minsize(780, 380)
        os.makedirs(APP_DIR, exist_ok=True)
        self.settings = load_cfg()
        self.items = []
        self.progress = {}   # id(download) -> ProgressDialog aberto
        self.pending_done = []  # ações "ao concluir" aguardando o fim de todos os downloads
        self.sched_arm = False  # True = a fila foi iniciada pelo agendador (executar "ao terminar" no fim)
        self._sched_key = ""
        self._rows = {}      # cache das linhas da lista (só atualiza o que mudou)
        self.filter_key = "all"
        self.load()
        self.icons = PixelIcons(root)
        self.conns_var = tk.IntVar(value=self.settings["conns"])
        self.clip_var = tk.BooleanVar(value=self.settings["monitor_clipboard"])
        self.notify_var = tk.BooleanVar(value=self.settings.get("show_complete", True))
        self.show_cats_var = tk.BooleanVar(value=self.settings["show_cats"])
        self.limit_var = tk.IntVar(value=self.settings["speed_limit_kbps"])
        GLOBAL["kbps"] = self.settings["speed_limit_kbps"]
        SETTINGS_REF["s"] = self.settings   # a Bridge lê as configurações por aqui
        self.apply_runtime()
        ttk.Style().configure("Treeview", rowheight=20)
        self.mi = {}   # itens de menu que mudam de estado (habilitado/desabilitado)
        self.tb = {}   # botões da barra de ferramentas

        self.build_menu()
        self.build_toolbar()
        self.status_bar = ttk.Label(root, text="", anchor="w", padding=4)
        self.status_bar.pack(side="bottom", fill="x")
        self.build_body()
        self.last_clip, self.last_seq = "", None
        self.reset_clip_baseline()   # não perguntar logo na abertura sobre um link que já estava copiado
        self.bridge = start_bridge()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-n>", lambda e: self.add_dialog())
        self.tray, self.tray_notified = None, False
        self._tray_text, self._tray_t = "", 0.0
        self.start_tray()
        self.tick()
        self.watch_clipboard()
        self.poll_tray()

    # ---------- menus ----------
    def build_menu(self):
        mb = tk.Menu(self.root)
        self.root.config(menu=mb)

        def sub(label):
            m = tk.Menu(mb, tearoff=0, postcommand=self.update_menus)
            mb.add_cascade(label=label, menu=m)
            return m

        def item(m, key, label, cmd, accel=None):
            m.add_command(label=label, command=cmd, accelerator=accel)
            if key:
                self.mi[key] = (m, m.index("end"))

        t = sub("Tarefas")
        item(t, None, "Adicionar novo download...", self.add_dialog, "Ctrl+N")
        item(t, None, "Adicionar download em lote...", self.batch_dialog)
        item(t, None, "Adicionar da área de transferência", self.add_from_clipboard, "Ctrl+V")
        t.add_separator()
        item(t, None, "Exportar lista de downloads...", self.export_list)
        item(t, None, "Importar lista de downloads...", self.import_list)
        t.add_separator()
        item(t, None, "Sair", self.request_quit)

        a = sub("Arquivo")
        item(a, "open", "Abrir", self.open_file, "Enter")
        item(a, "openwith", "Abrir com...", self.open_with)
        item(a, "folder", "Abrir pasta", self.open_folder)
        a.add_separator()
        item(a, "move", "Mover/Renomear...", self.move_rename)
        item(a, "copy", "Copiar endereço", self.copy_url)
        item(a, "newurl", "Atualizar endereço de download...", self.update_url)
        a.add_separator()
        item(a, "props", "Propriedades", self.show_properties, "Alt+Enter")

        d = sub("Downloads")
        item(d, "start", "Iniciar download", self.resume_sel)
        item(d, "stop", "Parar download", self.pause_sel)
        item(d, "stopall", "Parar todos os downloads", self.pause_all)
        d.add_separator()
        item(d, "delete", "Excluir", self.delete_sel, "Del")
        item(d, "delcomp", "Excluir concluídos", self.delete_completed)
        item(d, "redown", "Baixar novamente", self.redownload_sel)
        d.add_separator()
        item(d, "qadd", "Adicionar à fila principal", self.queue_add)
        item(d, "qrem", "Remover da fila principal", self.queue_remove)
        item(d, None, "Iniciar fila principal", self.start_queue)
        item(d, None, "Parar fila principal", self.stop_queue)
        d.add_separator()
        item(d, None, "Agendador...", self.scheduler_dialog)
        lm = tk.Menu(d, tearoff=0)
        d.add_cascade(label="Limitar velocidade total", menu=lm)
        for kb, txt in ((0, "Sem limite"), (100, "100 KB/s"), (500, "500 KB/s"), (1024, "1 MB/s"),
                        (2048, "2 MB/s"), (5120, "5 MB/s")):
            lm.add_radiobutton(label=txt, variable=self.limit_var, value=kb,
                               command=lambda kb=kb: self.set_global_limit(kb))
        lm.add_separator()
        lm.add_command(label="Personalizado...", command=self.custom_limit)
        d.add_separator()
        item(d, None, "Opções...", self.options_dialog)

        v = sub("Visualizar")
        v.add_checkbutton(label="Categorias", variable=self.show_cats_var,
                          command=lambda: self.toggle_cats(self.show_cats_var.get()))
        cm = tk.Menu(v, tearoff=0)
        v.add_cascade(label="Colunas", menu=cm)
        self.col_vars = {}
        for cid, title, *_ in COLS:
            self.col_vars[cid] = tk.BooleanVar(value=cid in self.settings["columns"])
            cm.add_checkbutton(label=title, variable=self.col_vars[cid], command=self.on_columns_changed)
        sm = tk.Menu(v, tearoff=0)
        v.add_cascade(label="Classificar por", menu=sm)
        so = self.settings["sort"]
        self.sort_var = tk.StringVar(value=so["col"] or "none")
        self.order_var = tk.StringVar(value="desc" if so["desc"] else "asc")
        for cid, title in (("#0", "Nome do arquivo"),) + tuple((c[0], c[1]) for c in COLS):
            sm.add_radiobutton(label=title, variable=self.sort_var, value=cid, command=self.on_sort_menu)
        sm.add_radiobutton(label="Sem ordenação (ordem de adição)", variable=self.sort_var, value="none",
                           command=self.on_sort_menu)
        sm.add_separator()
        sm.add_radiobutton(label="Ordem crescente", variable=self.order_var, value="asc", command=self.on_sort_menu)
        sm.add_radiobutton(label="Ordem decrescente", variable=self.order_var, value="desc", command=self.on_sort_menu)
        v.add_separator()
        v.add_command(label="Selecionar tudo", command=self.select_all, accelerator="Ctrl+A")

    # ---------- barra de ferramentas ----------
    def build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(4, 2))
        bar.pack(fill="x")

        def btn(key, text, draw, cmd, tip, menu=None):
            b = ToolButton(bar, text, draw, cmd, menu=menu, tip=tip)
            b.pack(side="left", padx=1)
            self.tb[key] = b

        btn("add", "Adicionar", ico_add, self.add_dialog, "Adicionar novo download (Ctrl+N)")
        btn("start", "Iniciar", ico_start, self.resume_sel, "Iniciar ou retomar o download selecionado")
        btn("stop", "Parar", ico_stop, self.pause_sel, "Parar o download selecionado")
        btn("stopall", "Parar...", ico_hand, self.pause_all, "Parar todos os downloads")
        btn("delete", "Excluir", ico_trash, self.delete_sel, "Excluir o download selecionado")
        btn("delcomp", "Excluir...", ico_trash_ok, self.delete_completed, "Excluir os downloads concluídos")
        btn("options", "Opções", ico_gear, self.options_dialog, "Opções do PyDM")
        btn("sched", "Agendar", ico_clock, self.scheduler_dialog, "Agendador da fila principal")
        btn("startq", "Iniciar fila", lambda c, x, y, on: ico_queue(c, x, y, on, "start"), self.start_queue,
            "Iniciar a fila principal", menu=lambda: self.queue_menu(True))
        btn("stopq", "Parar fila", lambda c, x, y, on: ico_queue(c, x, y, on, "stop"), self.stop_queue,
            "Parar a fila principal", menu=lambda: self.queue_menu(False))
        ttk.Separator(self.root).pack(fill="x")

    def queue_menu(self, start):
        m = tk.Menu(self.root, tearoff=0)
        if start:
            m.add_command(label="Fila principal", command=self.start_queue)
            m.add_command(label="Todos os downloads incompletos", command=self.start_all)
        else:
            m.add_command(label="Fila principal", command=self.stop_queue)
            m.add_command(label="Todos os downloads", command=self.pause_all)
        return m

    # ---------- corpo: categorias + lista ----------
    def build_body(self):
        self.pw = ttk.PanedWindow(self.root, orient="horizontal")
        self.pw.pack(fill="both", expand=True)

        self.left = ttk.Frame(self.pw)
        head = ttk.Frame(self.left)
        head.pack(fill="x")
        ttk.Label(head, text="Categorias", padding=(4, 3)).pack(side="left")
        x = tk.Label(head, text="\u2715", cursor="hand2", padx=6)
        x.pack(side="right")
        x.bind("<Button-1>", lambda e: self.toggle_cats(False))
        self.cat_tree = ttk.Treeview(self.left, show="tree", selectmode="browse")
        self.cat_tree.pack(fill="both", expand=True)
        self.cat_tree.bind("<<TreeviewSelect>>", self.on_cat_select)
        self.rebuild_categories()

        self.right = ttk.Frame(self.pw)
        self.tree = ttk.Treeview(self.right, columns=COL_IDS, show="tree headings", selectmode="extended")
        vs = ttk.Scrollbar(self.right, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(self.right, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.column("#0", width=260, minwidth=120, anchor="w")
        for cid, title, w, anc, _vis in COLS:
            self.tree.column(cid, width=w, minwidth=24, anchor=anc, stretch=False)
        self.apply_columns()
        self.apply_headings()
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Return>", lambda e: self.open_file())
        self.tree.bind("<Delete>", lambda e: self.delete_sel())
        self.tree.bind("<Control-a>", lambda e: self.select_all())
        self.tree.bind("<Control-v>", lambda e: self.add_from_clipboard())
        self.tree.bind("<Alt-Return>", lambda e: self.show_properties())
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<<TreeviewSelect>>", self.update_toolbar)

        self._sash_pending = bool(self.settings["show_cats"])
        if self.settings["show_cats"]:
            self.pw.add(self.left, weight=0)
        self.pw.add(self.right, weight=1)
        # a divisória só pode ser posicionada com a janela já visível e com tamanho real; se o PyDM abre
        # escondido na bandeja (--tray) isso só acontece quando a janela é mostrada pela primeira vez
        self.pw.bind("<Configure>", self._place_sash)

    def _place_sash(self, _e=None):
        if not self._sash_pending:
            return
        try:
            if self.pw.winfo_width() < 400 or str(self.left) not in self.pw.panes():
                return
            self.pw.sashpos(0, 200)
            self._sash_pending = False
        except tk.TclError:
            pass

    def toggle_cats(self, show):
        self.settings["show_cats"] = bool(show)
        self.show_cats_var.set(bool(show))
        present = str(self.left) in self.pw.panes()
        if show and not present:
            self.pw.insert(0, self.left)
            self._sash_pending = True
            self.root.after(60, self._place_sash)
        elif not show and present:
            self.pw.forget(self.left)
        save_cfg(self.settings)

    # ---------- painel de categorias ----------
    def rebuild_categories(self):
        t, ic = self.cat_tree, self.icons
        was = {k: self.root.tk.getboolean(t.item(k, "open")) for k in t.get_children()}
        sel = t.selection()[0] if t.selection() else self.filter_key
        t.delete(*t.get_children())
        cats = self.settings["categories"]
        for key, label, kind in (("all", "Todos os downloads", "folder"), ("inc", "Incompletos", "folder_dl"),
                                 ("done", "Completos", "folder_ok")):
            t.insert("", "end", iid=key, text=label, image=ic.get(kind), open=was.get(key, key == "all"))
            for name in cats:
                t.insert(key, "end", iid=f"{key}:{name}", text=name, image=ic.get(KIND_BY_CAT.get(name, "folder")))
        t.insert("", "end", iid="queue", text="Tarefas", image=ic.get("folder"), open=True)
        t.insert("queue", "end", iid="queue:main", text="Fila principal", image=ic.get("queue"))
        if not t.exists(sel):
            sel = "all"
        self.filter_key = sel
        t.selection_set(sel)

    def on_cat_select(self, _e=None):
        s = self.cat_tree.selection()
        if s:
            self.filter_key = s[0]
            self.refresh_list()
            self.update_toolbar()

    def matches(self, d, key):
        group, _, cat = key.partition(":")
        if group == "inc" and d.status == "Concluído":
            return False
        if group == "done" and d.status != "Concluído":
            return False
        if group == "queue":
            return d.queued
        return not cat or d.category == cat

    # ---------- colunas e ordenação ----------
    def apply_columns(self):
        self.tree["displaycolumns"] = [c for c in COL_IDS if c in self.settings["columns"]]

    def apply_headings(self):
        so = self.settings["sort"]
        for cid, title in (("#0", "Nome do arquivo"),) + tuple((c[0], c[1]) for c in COLS):
            mark = (" \u25bc" if so["desc"] else " \u25b2") if so["col"] == cid else ""
            self.tree.heading(cid, text=title + mark, command=lambda c=cid: self.set_sort(c))

    def on_columns_changed(self):
        self.settings["columns"] = [c for c in COL_IDS if self.col_vars[c].get()]
        self.apply_columns()
        save_cfg(self.settings)

    def set_sort(self, col):
        so = self.settings["sort"]
        so["desc"] = (not so["desc"]) if so["col"] == col else False
        so["col"] = col
        self.sort_var.set(col)
        self.order_var.set("desc" if so["desc"] else "asc")
        self.after_sort()

    def on_sort_menu(self):
        col = self.sort_var.get()
        self.settings["sort"] = {"col": None if col == "none" else col, "desc": self.order_var.get() == "desc"}
        self.after_sort()

    def after_sort(self):
        self.apply_headings()
        save_cfg(self.settings)
        self.refresh_list()

    def sort_key(self, col):
        def prog(d):
            if d.status == "Concluído":
                return 100.0
            return d.downloaded * 100 / d.size if d.size else 0.0
        return {
            "#0": lambda d: (d.name or guess_name(d.url)).lower(),
            "f": lambda d: (not d.queued),
            "tamanho": lambda d: d.size or 0,
            "progresso": prog,
            "eta": lambda d: (d.size - d.downloaded) / d.speed if d.size and d.speed > 1 else float("inf"),
            "vel": lambda d: d.speed,
            "data": lambda d: d.last_try,
            "desc": lambda d: d.description.lower(),
            "status": lambda d: d.status,
            "pasta": lambda d: d.folder.lower(),
            "url": lambda d: d.url.lower(),
        }[col]

    # ---------- lista de downloads ----------
    def row_for(self, d):
        dl = d.downloaded
        if d.status == "Concluído":
            prog = "Completo"
        elif d.status == "Erro":
            prog = "Erro"
        elif d.status == "Na fila" and not dl:
            prog = "Na fila"
        elif d.size:
            prog = pt(f"{dl * 100 / d.size:.2f}%")
        else:
            prog = pt(fmt_size(dl, 2)) if dl else ""
        running = d.status == "Baixando"
        vals = {
            "f": "\u2630" if d.queued and d.status != "Concluído" else "",
            "tamanho": pt(fmt_size(d.size, 2)) if d.size else "",
            "progresso": prog,
            "eta": fmt_eta((d.size - dl) / d.speed) if running and d.size and d.speed > 1 else "",
            "vel": pt(fmt_size(d.speed, 2)) + "/s" if running and d.speed else "",
            "data": time.strftime("%b %d %H:%M:%S %Y", time.localtime(d.last_try)) if d.last_try else "",
            "desc": d.description,
            "status": d.status + (f": {d.error[:40]}" if d.status == "Erro" and d.error else
                                  f": {d.note}" if d.status == "Baixando" and d.note else ""),
            "pasta": d.folder,
            "url": d.url,
        }
        return d.name or guess_name(d.url), tuple(vals[c] for c in COL_IDS)

    def refresh_list(self):
        vis = [d for d in self.items if self.matches(d, self.filter_key)]
        so = self.settings["sort"]
        if so["col"]:
            vis.sort(key=self.sort_key(so["col"]), reverse=so["desc"])
        want = [str(id(d)) for d in vis]
        if list(self.tree.get_children()) != want:
            keep = set(want)
            for iid in self.tree.get_children():
                if iid not in keep:
                    self.tree.delete(iid)
                    self._rows.pop(iid, None)
            for i, d in enumerate(vis):
                iid = str(id(d))
                if self.tree.exists(iid):
                    self.tree.move(iid, "", i)
                else:
                    self.tree.insert("", i, iid=iid)
        for d in vis:
            iid = str(id(d))
            name, vals = self.row_for(d)
            if self._rows.get(iid) != (name, vals):
                self._rows[iid] = (name, vals)
                self.tree.item(iid, text=name, values=vals, image=self.icons.get(kind_for(name)))

    # ---------- estado dos botões e menus ----------
    def selected(self):
        ids = self.tree.selection()
        return [d for d in self.items if str(id(d)) in ids]

    def update_toolbar(self, _e=None):
        sel = self.selected()
        running = any(d.status in ("Baixando", "Na fila") for d in self.items)
        en = {
            "start": any(d.status in ("Pausado", "Erro") for d in sel),
            "stop": any(d.status in ("Baixando", "Na fila") for d in sel),
            "stopall": running,
            "delete": bool(sel),
            "delcomp": any(d.status == "Concluído" for d in self.items),
            "startq": any(d.queued and d.status != "Concluído" for d in self.items),
            "stopq": any(d.queued and d.status in ("Baixando", "Na fila") for d in self.items),
        }
        for k, b in self.tb.items():
            b.set_enabled(en.get(k, True))

    def update_menus(self):
        sel = self.selected()
        one = len(sel) == 1
        states = {
            "open": one, "openwith": one and sel[0].status == "Concluído", "folder": one, "move": one,
            "copy": bool(sel), "newurl": one and sel[0].status != "Concluído", "props": one,
            "start": any(d.status in ("Pausado", "Erro") for d in sel),
            "stop": any(d.status in ("Baixando", "Na fila") for d in sel),
            "stopall": any(d.status in ("Baixando", "Na fila") for d in self.items),
            "delete": bool(sel), "delcomp": any(d.status == "Concluído" for d in self.items),
            "redown": bool(sel),
            "qadd": any(not d.queued and d.status != "Concluído" for d in sel),
            "qrem": any(d.queued for d in sel),
        }
        for key, on in states.items():
            m, i = self.mi[key]
            m.entryconfigure(i, state="normal" if on else "disabled")

    def context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        sel = self.selected()
        one = len(sel) == 1
        m = tk.Menu(self.root, tearoff=0)

        def add(label, cmd, on=True):
            m.add_command(label=label, command=cmd, state="normal" if on else "disabled")
        add("Abrir", self.open_file, one)
        add("Abrir pasta", self.open_folder, one)
        m.add_separator()
        add("Iniciar download", self.resume_sel, any(d.status in ("Pausado", "Erro") for d in sel))
        add("Parar download", self.pause_sel, any(d.status in ("Baixando", "Na fila") for d in sel))
        add("Baixar novamente", self.redownload_sel, bool(sel))
        m.add_separator()
        add("Adicionar à fila principal", self.queue_add, any(not d.queued and d.status != "Concluído" for d in sel))
        add("Remover da fila principal", self.queue_remove, any(d.queued for d in sel))
        m.add_separator()
        add("Mover/Renomear...", self.move_rename, one)
        add("Copiar endereço", self.copy_url, bool(sel))
        add("Atualizar endereço de download...", self.update_url, one and sel[0].status != "Concluído")
        m.add_separator()
        add("Excluir", self.delete_sel, bool(sel))
        add("Propriedades", self.show_properties, one)
        m.tk_popup(e.x_root, e.y_root)
        m.grab_release()

    def on_double(self, e):
        if self.tree.identify_region(e.x, e.y) in ("cell", "tree"):
            self.open_file()

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    # ---------- persistência ----------
    def load(self):
        try:
            with open(DB, encoding="utf-8") as f:
                self.items = [(MediaDownload if d.get("media") else Download).from_dict(d) for d in json.load(f)]
        except Exception:
            self.items = []
        for d in self.items:   # itens de versões antigas não tinham categoria
            if not d.category:
                d.category = category_for(d.name or guess_name(d.url), self.settings["categories"])

    def save(self):
        try:
            with open(DB, "w", encoding="utf-8") as f:
                json.dump([d.to_dict() for d in self.items], f)
        except Exception:
            pass

    # ---------- ações ----------
    def add(self, url, folder, conns, headers=None, **info):
        d = Download(url, folder, conns, headers)
        for k, v in info.items():
            setattr(d, k, v)
        self.items.append(d)
        self.save()
        return d

    def free_path(self, folder, name):
        """Como unique_path, mas também evita caminhos já usados por itens da lista."""
        base, ext = os.path.splitext(name)
        p, i = os.path.join(folder, name), 1
        while os.path.exists(p) or os.path.exists(p + ".part") or any(d.path == p for d in self.items):
            p = os.path.join(folder, f"{base} ({i}){ext}")
            i += 1
        return p

    def register(self, parent, url, headers, path, category, description, info, remember, start):
        """Valida o destino escolhido na janela de informação e adiciona o download.
        Retorna True se foi adicionado (a janela pode fechar)."""
        folder, name = os.path.split(path)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as ex:
            messagebox.showerror("Erro", f"Não foi possível usar a pasta:\n{folder}\n\n{ex}", parent=parent)
            return False
        busy = os.path.exists(path + ".part") or any(
            d.path == path and d.status != "Concluído" for d in self.items)
        if busy:  # já existe um download em andamento com esse nome: renomeia sozinho
            path = self.free_path(folder, name)
        elif os.path.exists(path):
            r = messagebox.askyesnocancel(
                "Arquivo já existe",
                f"Já existe um arquivo com este nome:\n{name}\n\n"
                "Sim = sobrescrever\nNão = baixar com outro nome", parent=parent)
            if r is None:
                return False
            if not r:
                path = self.free_path(folder, name)
        size, resumable = (info[0], info[1]) if info else (None, False)
        self.add(url, folder, self.settings["conns"], headers,
                 name=os.path.basename(path), path=path, size=size, resumable=resumable,
                 probed=bool(info), description=description, category=category,
                 status="Na fila" if start else "Pausado", queued=not start)
        cats = self.settings["categories"]
        if remember and category in cats:
            cats[category]["folder"] = folder
        self.settings["recent"] = ([folder] + [x for x in self.settings["recent"] if x != folder])[:10]
        save_cfg(self.settings)
        return True

    def open_info(self, url, headers=None):
        InfoDialog(self, url, headers or {})

    def add_dialog(self, url=""):
        """Pede só a URL; em seguida abre a janela 'Informação do arquivo de download'."""
        win = tk.Toplevel(self.root)
        win.title("Adicionar URL")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Endereço (URL) do arquivo:").grid(row=0, column=0, columnspan=2, sticky="w")
        v = tk.StringVar(value=url)
        if not url:
            try:
                c = self.root.clipboard_get().strip()
                if c.lower().startswith(("http://", "https://")):
                    v.set(c)
            except tk.TclError:
                pass
        e = ttk.Entry(f, textvariable=v, width=70)
        e.grid(row=1, column=0, columnspan=2, pady=4)

        def ok(*_):
            u = v.get().strip()
            if not u.lower().startswith(("http://", "https://")):
                messagebox.showerror("Erro", "Informe uma URL válida (http/https).", parent=win)
                return
            win.destroy()
            if self.check_duplicate(u):
                self.open_info(u)

        ttk.Button(f, text="OK", command=ok).grid(row=2, column=0, sticky="e", pady=(8, 0), padx=4)
        ttk.Button(f, text="Cancelar", command=win.destroy).grid(row=2, column=1, sticky="w",
                                                                  pady=(8, 0), padx=4)
        e.bind("<Return>", ok)
        win.bind("<Escape>", lambda _e: win.destroy())
        e.focus()
        e.select_range(0, "end")

    def add_media(self, m):
        """Coloca na lista um vídeo/áudio escolhido no painel da extensão."""
        s = self.settings
        cats, v = s["categories"], s["video"]
        mid = m["id"]
        audio = not mid.startswith("v")
        cat = v["audio_cat"] if audio else v["video_cat"]
        if cat not in cats:
            cat = category_for("x.mp3" if audio else "x.mp4", cats)
        if audio:
            ext, suffix = (AUDIO_PRESETS[mid]["ext"] if mid in AUDIO_PRESETS else "m4a"), ""
        else:
            h = int(mid[1:])
            ext, suffix = ("mkv" if h > 1080 else "mp4"), f" [{h}p]"
        d = MediaDownload(m["url"], cats[cat]["folder"], 1, m.get("headers"), media={"id": mid, "title": m.get("title", "")})
        d.name = f"{m.get('title') or guess_name(m['url'])}{suffix}.{ext}"
        d.size, d.category, d.status = m.get("est"), cat, "Na fila"
        self.items.append(d)
        self.save()
        log(f"Vídeo adicionado: {mid} {m['url'][:100]}")
        return d

    def offer_components(self):
        """A extensão pediu um vídeo mas falta o yt-dlp: oferece instalar (no máximo a cada 90 s)."""
        if time.time() - getattr(self, "_comp_ask", 0) < 90:
            return
        self._comp_ask = time.time()
        if not sys.platform.startswith("win"):
            messagebox.showinfo("PyDM", "Para baixar vídeos de sites instale o yt-dlp (e o ffmpeg) pelo gerenciador "
                                        "de pacotes do seu sistema.", parent=self.root)
            return
        missing = [n for n, p in media_tools_named(self.settings).items() if not p]
        if missing and messagebox.askyesno(
                "PyDM", "Para baixar vídeos de sites o PyDM precisa instalar alguns componentes do GitHub:\n\n" +
                "\n".join(f"\u2022 {ComponentInstaller.LABEL[n]}" for n in missing) +
                "\n\nBaixar agora? (até ~250 MB, só na primeira vez)", parent=self.root):
            ComponentInstaller(self, missing)

    def apply_runtime(self):
        """Passa para o motor de download as opções que ele consulta durante o download."""
        s = self.settings
        GLOBAL["ua"] = s["manual_ua"]
        GLOBAL["temp"] = s["temp_folder"]
        GLOBAL["set_mtime"] = s["set_mtime"]
        GLOBAL["ignore_mtime"] = s["ignore_mtime"]

    def restart_bridge(self):
        """Reinicia o servidor local que recebe os links da extensão. Retorna True se subiu."""
        try:
            if self.bridge:
                self.bridge.shutdown()
                self.bridge.server_close()
        except Exception:
            pass
        self.bridge = start_bridge()
        return bool(self.bridge)

    def check_duplicate(self, url):
        """Aplica a opção 'Se um link duplicado for adicionado'. True = pode adicionar."""
        dup = next((d for d in self.items if d.url == url), None)
        if not dup:
            return True
        act = self.settings["dup_action"]
        if act == "ignore":
            return False
        if act == "rename":
            return True
        return messagebox.askyesno("Link duplicado",
                                   f"Este endereço já está na lista:\n\n{dup.name or guess_name(url)}\n\n"
                                   "Adicionar mesmo assim?", parent=self.root)

    def add_silent(self, url, headers, start):
        """Adiciona sem mostrar a janela 'Informação do arquivo' (pasta pela categoria, nome pelo servidor)."""
        cats = self.settings["categories"]
        cat = category_for(guess_name(url), cats)
        return self.add(url, cats[cat]["folder"], self.settings["conns"], headers, category=cat,
                        status="Na fila" if start else "Pausado", queued=not start)

    def on_new_link(self, url, headers):
        """Link vindo do navegador: mostra a janela de informação ou adiciona direto, conforme as Opções."""
        if not self.check_duplicate(url):
            return
        if self.settings["show_start"]:
            self.open_info(url, headers)
        else:
            self.add_silent(url, headers, start=not self.settings["start_queue_only"])

    def scan_file(self, d):
        """Verificação contra vírus (programa externo) depois que o download termina."""
        av = self.settings["antivirus"]
        if not av["enabled"] or not d.path or not os.path.exists(d.path):
            return
        prog = av["program"].strip() or default_av_program()
        if not prog:
            log("Antivírus: nenhum programa configurado/encontrado.")
            return
        threading.Thread(target=self._scan_worker, args=(prog, av["args"], d.path, d.name or d.path),
                         daemon=True).start()

    @staticmethod
    def _scan_worker(prog, args, path, name):
        try:
            line = f'"{prog}" ' + args.replace("{file}", path)
            cmd = line if sys.platform.startswith("win") else shlex.split(line)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               stdin=subprocess.DEVNULL, creationflags=NOWIN)
            TRAY_EVENTS.put(("av", name, r.returncode, (r.stdout or "") + (r.stderr or "")))
        except Exception as ex:
            TRAY_EVENTS.put(("av", name, None, str(ex)))

    def show_av_result(self, name, code, out):
        log(f"Antivírus: {name} -> código {code}\n{(out or '')[-800:]}")
        if code is None:
            messagebox.showerror("Verificação contra vírus",
                                 f"Não foi possível verificar \"{name}\":\n{(out or '')[:300]}", parent=self.root)
        elif code != 0:
            messagebox.showwarning("Verificação contra vírus",
                                   f"O verificador retornou o código {code} para:\n{name}\n\n"
                                   "O arquivo pode ser perigoso. Veja os detalhes em ~/.pydm/pydm.log.",
                                   parent=self.root)

    # ---------- iniciar / parar ----------
    def resume_sel(self):
        for d in self.selected():
            if d.status in ("Pausado", "Erro"):
                d.status = "Na fila"

    def pause_sel(self):
        for d in self.selected():
            self._stop(d)

    def pause_all(self):
        for d in self.items:
            self._stop(d)

    @staticmethod
    def _stop(d):
        if d.status == "Baixando":
            d.pause()
        elif d.status == "Na fila":
            d.status = "Pausado"

    def start_all(self):
        for d in self.items:
            if d.status in ("Pausado", "Erro"):
                d.status = "Na fila"

    # ---------- excluir ----------
    def delete_sel(self):
        sel = self.selected()
        if not sel:
            return
        ok, files = ask_delete(self, f"Excluir {len(sel)} download(s) da lista?\n"
                                     "(Arquivos incompletos são sempre apagados.)")
        if ok:
            self.remove_items(sel, files)

    def delete_completed(self):
        done = [d for d in self.items if d.status == "Concluído"]
        if not done:
            return
        ok, files = ask_delete(self, f"Excluir {len(done)} download(s) concluído(s) da lista?",
                               "Excluir concluídos")
        if ok:
            self.remove_items(done, files)

    def remove_items(self, sel, delete_files=False):
        for d in list(sel):
            dlg = self.progress.get(id(d))
            if dlg:
                dlg.close()
            d.cancel()
            if delete_files and d.status == "Concluído" and d.path and os.path.exists(d.path):
                try:
                    os.remove(d.path)
                except OSError:
                    pass
            if d in self.items:
                self.items.remove(d)
            iid = str(id(d))
            if self.tree.exists(iid):
                self.tree.delete(iid)
            self._rows.pop(iid, None)
        self.save()
        self.update_toolbar()

    def redownload_sel(self):
        sel = self.selected()
        if not sel:
            return
        if not messagebox.askyesno("Baixar novamente",
                                   f"Baixar de novo {len(sel)} arquivo(s) desde o início?\n"
                                   "O que já foi baixado será descartado e o arquivo existente "
                                   "será substituído ao concluir.", parent=self.root):
            return
        for d in sel:
            d.reset()

    # ---------- fila principal ----------
    def queue_add(self):
        for d in self.selected():
            if d.status != "Concluído":
                d.queued = True

    def queue_remove(self):
        for d in self.selected():
            d.queued = False

    def start_queue(self, scheduled=False):
        n = 0
        for d in self.items:
            if d.queued and d.status in ("Pausado", "Erro"):
                d.status = "Na fila"
                n += 1
        if scheduled and (n or any(d.queued and d.status in ("Baixando", "Na fila") for d in self.items)):
            self.sched_arm = True   # ao terminar a fila, executa o que foi marcado no agendador
        elif scheduled:
            log("Agendador: fila principal vazia, nada para iniciar.")

    def stop_queue(self):
        self.sched_arm = False
        for d in self.items:
            if d.queued:
                self._stop(d)

    def check_schedule(self):
        """Chamado a cada tick: dispara início/parada da fila no minuto marcado (uma vez por minuto)."""
        sc = self.settings["schedule"]
        if not sc["enabled"]:
            return
        now = datetime.now()
        key = now.strftime("%Y-%m-%d %H:%M")
        if key == self._sched_key:
            return
        self._sched_key = key
        if now.weekday() not in sc["days"]:
            return
        hm = now.strftime("%H:%M")
        if sc["start_on"] and hm == sc["start"]:
            self.start_queue(scheduled=True)
        if sc["stop_on"] and hm == sc["stop"]:
            self.stop_queue()

    def sched_finish(self):
        """Fila iniciada pelo agendador terminou: aplica "Quando a fila terminar"."""
        sc = self.settings["schedule"]
        if sc["quit"] or sc["shutdown"]:
            self.pending_done.append({"modem": False, "quit": sc["quit"], "shutdown": sc["shutdown"],
                                      "mode": sc["mode"], "force": sc["force"]})

    # ---------- limite de velocidade total ----------
    def set_global_limit(self, kb):
        kb = max(0, int(kb))
        self.settings["speed_limit_kbps"] = kb
        GLOBAL["kbps"] = kb
        with GLOBAL_LOCK:
            GLOBAL["next"] = 0.0
        self.limit_var.set(kb)
        save_cfg(self.settings)

    def custom_limit(self):
        kb = simpledialog.askinteger("Limitar velocidade total", "Velocidade máxima total (KBytes/seg):",
                                     parent=self.root, minvalue=1, maxvalue=1000000,
                                     initialvalue=self.settings["speed_limit_kbps"] or 1024)
        if kb:
            self.set_global_limit(kb)

    # ---------- adicionar ----------
    def add_batch(self, urls, folder, start):
        """Adiciona vários endereços de uma vez. Retorna quantos entraram (ignora os já existentes)."""
        cats, n = self.settings["categories"], 0
        seen = {d.url for d in self.items if d.status != "Concluído"}
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            cat = category_for(guess_name(u), cats)
            self.add(u, folder or cats[cat]["folder"], self.settings["conns"], {}, category=cat,
                     status="Na fila" if start else "Pausado", queued=not start)
            n += 1
        return n

    def batch_dialog(self, urls=None):
        BatchDialog(self, urls)

    def add_from_clipboard(self):
        try:
            c = self.root.clipboard_get()
        except tk.TclError:
            c = ""
        urls = [l.strip() for l in c.splitlines() if l.strip().lower().startswith(("http://", "https://"))]
        if not urls:
            messagebox.showinfo("PyDM", "Não há nenhum endereço (http/https) na área de transferência.",
                                parent=self.root)
        elif len(urls) == 1:
            if self.check_duplicate(urls[0]):
                self.open_info(urls[0])
        else:
            self.batch_dialog(urls)
        return "break"

    def export_list(self):
        items = [d for d in self.items if not isinstance(d, MediaDownload)]   # vídeos do painel dependem do formato escolhido
        skipped = len(self.items) - len(items)
        if not items:
            messagebox.showinfo("PyDM", "Não há downloads para exportar" +
                                (" (os vídeos do painel não entram na exportação)." if skipped else "."), parent=self.root)
            return
        p = filedialog.asksaveasfilename(parent=self.root, title="Exportar lista de downloads",
                                         defaultextension=".json", initialfile="pydm-downloads.json",
                                         filetypes=[("Lista do PyDM (.json)", "*.json"), ("Só os endereços (.txt)", "*.txt")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                if p.lower().endswith(".txt"):
                    f.write("\n".join(d.url for d in items) + "\n")
                else:   # os cookies/headers de sessão não são exportados, de propósito
                    json.dump([{"url": d.url, "name": d.name, "folder": d.folder, "category": d.category,
                                "description": d.description, "size": d.size, "status": d.status}
                               for d in items], f, ensure_ascii=False, indent=2)
        except OSError as ex:
            messagebox.showerror("Erro", f"Não foi possível salvar o arquivo:\n{ex}", parent=self.root)
            return
        if skipped:
            messagebox.showinfo("PyDM", f"{skipped} vídeo(s) baixados pelo painel da extensão não foram exportados.",
                                parent=self.root)

    def import_list(self):
        p = filedialog.askopenfilename(parent=self.root, title="Importar lista de downloads",
                                       filetypes=[("Listas (.json, .txt)", "*.json *.txt"), ("Todos os arquivos", "*.*")])
        if not p:
            return
        urls = []
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            try:
                data = json.loads(raw)
                urls = [(x.get("url") if isinstance(x, dict) else x) for x in data] if isinstance(data, list) else []
            except ValueError:
                urls = raw.splitlines()
        except OSError as ex:
            messagebox.showerror("Erro", f"Não foi possível ler o arquivo:\n{ex}", parent=self.root)
            return
        urls = [u.strip() for u in urls if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))]
        if not urls:
            messagebox.showinfo("PyDM", "Nenhum endereço (http/https) encontrado no arquivo.", parent=self.root)
        else:
            self.batch_dialog(urls)

    # ---------- janelas ----------
    def options_dialog(self):
        OptionsDialog(self)

    def scheduler_dialog(self):
        SchedulerDialog(self)

    def show_properties(self):
        sel = self.selected()
        if sel:
            PropertiesDialog(self, sel[0])

    # ---------- arquivo: mover, copiar endereço, atualizar endereço ----------
    def move_rename(self):
        sel = self.selected()
        if not sel:
            return
        d = sel[0]
        if d.status == "Baixando":
            messagebox.showinfo("PyDM", "Pare o download antes de mover ou renomear o arquivo.", parent=self.root)
            return
        cur = d.path or os.path.join(d.folder, d.name or guess_name(d.url))
        new = filedialog.asksaveasfilename(parent=self.root, title="Mover/Renomear", initialdir=os.path.dirname(cur),
                                           initialfile=os.path.basename(cur))
        if not new or os.path.normpath(new) == os.path.normpath(cur):
            return
        new = os.path.normpath(new)
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            if d.path:
                if d.status == "Concluído":
                    src, dst = d.path, new
                elif d.part_path:          # parcial na pasta temporária: só muda o destino final
                    src = dst = None
                else:
                    src, dst = d.part, new + ".part"
                if src and os.path.exists(src):
                    shutil.move(src, dst)
        except OSError as ex:
            messagebox.showerror("Erro", f"Não foi possível mover o arquivo:\n{ex}", parent=self.root)
            return
        d.path, d.folder, d.name = new, os.path.dirname(new), os.path.basename(new)
        self.save()

    def copy_url(self):
        urls = "\n".join(d.url for d in self.selected())
        if urls:
            self.root.clipboard_clear()
            self.root.clipboard_append(urls)

    def update_url(self):
        sel = self.selected()
        if not sel:
            return
        d = sel[0]
        if d.status == "Baixando":
            messagebox.showinfo("PyDM", "Pare o download antes de atualizar o endereço.", parent=self.root)
            return
        u = simpledialog.askstring("Atualizar endereço de download",
                                   "Novo endereço (URL) do arquivo.\nO download continua de onde parou.",
                                   initialvalue=d.url, parent=self.root)
        u = (u or "").strip()
        if not u:
            return
        if not u.lower().startswith(("http://", "https://")):
            messagebox.showerror("Erro", "Informe uma URL válida (http/https).", parent=self.root)
            return
        d.url = u
        if d.status == "Erro":
            d.status, d.error = "Pausado", ""
        self.save()

    def open_progress(self, d):
        dlg = self.progress.get(id(d))
        if dlg:
            try:
                if dlg.win.winfo_exists():
                    dlg.win.deiconify()
                    dlg.win.lift()
                    return
            except tk.TclError:
                pass
        self.progress[id(d)] = ProgressDialog(self, d)

    def finished(self, d):
        try:
            self.scan_file(d)
        except Exception as ex:
            log(f"Antivírus: {ex!r}")
        dlg = self.progress.get(id(d))
        if dlg:
            dlg.close()
        show = d.show_msg if d.show_msg is not None else self.notify_var.get()
        if show:
            CompleteDialog(self, d)
        elif d.on_done["modem"] or d.on_done["quit"] or d.on_done["shutdown"]:
            self.pending_done.append(dict(d.on_done))

    def run_done_actions(self, acts):
        """Executa as ações "ao concluir". Retorna True se o PyDM foi encerrado."""
        if any(a["modem"] for a in acts) and sys.platform.startswith("win"):
            try:
                subprocess.Popen(["rasdial", "/disconnect"], creationflags=NOWIN)
            except Exception:
                pass
        sd = next((a for a in acts if a["shutdown"]), None)
        if sd:
            ShutdownCountdown(self, sd["mode"], sd["force"])
            return False
        if any(a["quit"] for a in acts):
            self.quit_app()
            return True
        return False

    def save_notify(self):
        self.settings["show_complete"] = bool(self.notify_var.get())
        save_cfg(self.settings)

    def save_conns(self, _e=None):
        try:
            n = int(self.conns_var.get())
        except (tk.TclError, ValueError):
            n = self.settings["conns"]
        n = max(1, min(32, n))
        self.conns_var.set(n)
        self.settings["conns"] = n   # vale para os próximos downloads
        save_cfg(self.settings)

    def open_file(self):
        """Enter/duplo clique: abre o arquivo concluído; nos demais, abre a janela de progresso."""
        for d in self.selected():
            if d.status == "Concluído":
                self._open(d.path)
            else:
                self.open_progress(d)

    def open_with(self):
        sel = self.selected()
        if not sel or not sel[0].path:
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", sel[0].path])
            else:
                self._open(sel[0].path)
        except Exception:
            pass

    def open_folder(self):
        sel = self.selected()
        if not sel:
            self._open(DEFAULT_FOLDER)
            return
        d = sel[0]
        if d.status == "Concluído" and d.path and os.path.exists(d.path) and sys.platform.startswith("win"):
            try:   # abre a pasta já com o arquivo selecionado
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(d.path)}"])
                return
            except Exception:
                pass
        self._open(d.folder)

    @staticmethod
    def _open(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # ---------- loop da interface ----------
    def tick(self):
        now = time.time()
        while not INBOX.empty():
            j = INBOX.get()
            if j.get("show"):   # segunda instância pediu para mostrar a janela
                self.show_window()
                continue
            if j.get("media"):   # botão "Baixar este vídeo" da extensão
                try:
                    self.add_media(j["media"])
                except Exception as ex:
                    log(f"Vídeo: {ex!r}")
                continue
            try:
                self.on_new_link(j["url"], j["headers"])
            except Exception:
                # se a janela falhar por qualquer motivo, não perde o download: adiciona com os padrões
                self.add(j["url"], DEFAULT_FOLDER, self.settings["conns"], j["headers"])
        active = [d for d in self.items if d.status == "Baixando"]
        for d in self.items:
            if d.status == "Na fila" and len(active) < self.settings["max_active"]:
                d.start()
                active.append(d)
                try:
                    self.open_progress(d)   # janela de progresso ao iniciar
                except Exception:
                    pass   # uma falha na janela nunca deve derrubar o laço principal
        total_speed = 0
        for d in self.items:
            if d.status == "Concluído":
                d.queued = False   # download concluído sai da fila
                if d.announce:
                    d.announce = False
                    try:
                        self.finished(d)    # janela "Download completo" / ações ao concluir
                    except Exception:
                        pass
            dl = d.downloaded
            if d.status == "Baixando":
                t0, b0 = d._last
                if now - t0 >= 0.5:
                    inst = (dl - b0) / (now - t0)
                    d.speed = inst if d.speed == 0 else d.speed * 0.6 + inst * 0.4
                    d._last = (now, dl)
                total_speed += d.speed
            else:
                d.speed, d._last = 0, (now, dl)
        try:
            self.refresh_list()
            self.update_toolbar()
        except tk.TclError:
            pass
        try:
            self.check_schedule()
        except Exception as ex:
            log(f"Agendador: {ex!r}")
        busy = any(x.status in ("Baixando", "Na fila") for x in self.items)
        if self.sched_arm and not busy:
            self.sched_arm = False
            self.sched_finish()
        self.status_bar.config(text=f"{len(active)} ativo(s) | Velocidade total: {pt(fmt_size(total_speed))}/s | "
                                    f"{len(self.items)} item(ns) | Extensão: " +
                                    ("ativa" if self.bridge else "porta em uso") +
                                    f" | Bandeja: {'ativa' if self.tray else 'indisponível'}")
        self.update_tray_title(len(active), total_speed)
        if self.pending_done and not busy:
            acts, self.pending_done = self.pending_done, []
            if self.run_done_actions(acts):
                return   # o PyDM foi encerrado
        if int(now) % 3 == 0:
            self.save()
        self.root.after(500, self.tick)

    @staticmethod
    def clip_seq():
        """Número de sequência da área de transferência (Windows): muda a cada cópia, mesmo do mesmo texto."""
        if sys.platform.startswith("win"):
            try:
                import ctypes
                return ctypes.windll.user32.GetClipboardSequenceNumber()
            except Exception:
                pass
        return None

    def reset_clip_baseline(self):
        """Considera o que já está copiado como 'visto' (para não perguntar sobre um link antigo)."""
        self.last_seq = self.clip_seq()
        try:
            self.last_clip = self.root.clipboard_get().strip()
        except tk.TclError:
            self.last_clip = ""

    def check_clipboard(self):
        seq = self.clip_seq()
        try:
            c = self.root.clipboard_get().strip()
        except tk.TclError:
            c = ""
        changed = (seq != self.last_seq) if seq is not None else (c != self.last_clip)
        self.last_seq, self.last_clip = seq, c
        if not changed or not looks_like_download(c, self.settings):
            return
        log(f"Área de transferência: link detectado {c[:120]}")
        if self.settings["clip_auto"]:   # início automático, sem perguntar
            if self.check_duplicate(c):
                self.add_silent(c, {}, True)
        elif messagebox.askyesno("PyDM", f"Baixar este link?\n\n{c[:100]}"):
            if self.check_duplicate(c):
                self.open_info(c)

    def watch_clipboard(self):
        try:
            if self.clip_var.get():
                self.check_clipboard()
        except Exception as ex:   # um erro aqui não pode matar o monitor
            import traceback
            log(f"Área de transferência: {ex!r}\n{traceback.format_exc()}")
        finally:
            self.root.after(1000, self.watch_clipboard)

    # ---------- bandeja do sistema ----------
    def start_tray(self):
        """Cria o ícone da bandeja. Se não der (falta pystray/Pillow, sistema sem suporte...),
        self.tray fica None, o motivo vai para self.tray_error e para o pydm.log."""
        self.tray, self.tray_error, self.tray_missing = None, "", False
        if sys.platform == "darwin":   # no macOS o pystray briga com o laço de eventos do Tk
            self.tray_error = "a bandeja não é suportada no macOS"
            return
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Abrir PyDM", lambda icon, item: TRAY_EVENTS.put("show"), default=True),
                pystray.MenuItem("Sair", lambda icon, item: TRAY_EVENTS.put("quit")))
            self.tray = pystray.Icon("PyDM", make_tray_image(), "PyDM", menu)
            self.tray.run_detached()
        except Exception as ex:
            import traceback
            self.tray = None
            self.tray_missing = isinstance(ex, ImportError)
            name = getattr(ex, "name", None)
            self.tray_error = (f"falta a biblioteca '{name}'" if self.tray_missing and name
                               else f"{type(ex).__name__}: {ex}")
            log("Bandeja indisponível: " + self.tray_error + "\n" + traceback.format_exc())

    def poll_tray(self):
        """O menu da bandeja roda em outra thread; o Tk só pode ser tocado por esta (a principal)."""
        while True:
            try:
                ev = TRAY_EVENTS.get_nowait()
            except queue.Empty:
                break
            try:
                if isinstance(ev, tuple) and ev[0] == "pip":
                    self.finish_install(ev[1], ev[2])
                elif isinstance(ev, tuple) and ev[0] == "av":
                    self.show_av_result(ev[1], ev[2], ev[3])
                elif isinstance(ev, tuple) and ev[0] == "media_setup":
                    self.offer_components()
                elif isinstance(ev, tuple) and ev[0] == "ytdlp_update":
                    out = (ev[2] or "").strip()[-600:] or "(sem saída)"
                    messagebox.showinfo("yt-dlp", out, parent=self.root)
                elif ev == "show":
                    self.show_window()
                elif ev == "quit":
                    self.request_quit()
            except Exception:
                pass
        try:
            self.root.after(150, self.poll_tray)
        except tk.TclError:
            pass

    def update_tray_title(self, n, speed):
        if not self.tray or time.time() - self._tray_t < 2:
            return
        self._tray_t = time.time()
        txt = f"PyDM - {n} ativo(s) - {fmt_size(speed)}/s" if n else "PyDM"
        if txt != self._tray_text:
            self._tray_text = txt
            try:
                self.tray.title = txt
            except Exception:
                pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.tray_notified:   # avisa uma única vez por sessão
            self.tray_notified = True
            try:
                self.tray.notify("O PyDM continua em execução na bandeja do sistema.\n"
                                 "Clique com o botão direito no ícone e escolha Sair para fechar.", "PyDM")
            except Exception:
                pass

    def on_close(self):
        """Botão X da janela: esconde na bandeja. Sem bandeja, explica o motivo e deixa o usuário escolher."""
        if self.tray and self.settings.get("close_to_tray", True):
            self.hide_to_tray()
        elif self.tray:
            self.request_quit()
        else:
            self.close_without_tray()

    def close_without_tray(self):
        frozen = getattr(sys, "frozen", False)   # True quando roda como .exe (PyInstaller)
        head = ("Não foi possível manter o PyDM na bandeja do sistema.\n\n"
                f"Motivo: {self.tray_error or 'desconhecido'}\n"
                f"(detalhes em {os.path.join(APP_DIR, 'pydm.log')})\n\n")
        if self.tray_missing and not frozen:
            r = messagebox.askyesnocancel(
                "Bandeja indisponível", head +
                "Deseja instalar agora as bibliotecas necessárias (pystray e pillow)?\n\n"
                "Sim = instalar e usar a bandeja\nNão = fechar o PyDM\nCancelar = voltar ao programa",
                parent=self.root)
            if r is True:
                self.install_tray_libs()
            elif r is False:
                self.quit_app()
            return
        extra = ("Este executável foi gerado sem o pystray/pillow. Gere-o de novo com o build.bat "
                 "atualizado (ou o build.yml do GitHub Actions), que já inclui as duas bibliotecas.\n\n"
                 if frozen and self.tray_missing else "")
        r = messagebox.askyesnocancel(
            "Bandeja indisponível", head + extra +
            "Sim = minimizar para a barra de tarefas (os downloads continuam)\n"
            "Não = fechar o PyDM\nCancelar = voltar ao programa", parent=self.root)
        if r is True:
            self.root.iconify()
        elif r is False:
            self.quit_app()

    def install_tray_libs(self):
        """Roda 'pip install pystray pillow' em segundo plano (só quando executado pelo Python)."""
        win = self._install_win = tk.Toplevel(self.root)
        win.title("Instalando")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(win, text="Instalando pystray e pillow...\nIsso leva alguns segundos.",
                  padding=16).pack()
        pb = ttk.Progressbar(win, mode="indeterminate", length=260)
        pb.pack(padx=16, pady=(0, 16))
        pb.start(12)
        center_front(win)

        def work():
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "pillow"],
                                   capture_output=True, text=True, timeout=300,
                                   stdin=subprocess.DEVNULL, creationflags=NOWIN)
                ok, out = r.returncode == 0, (r.stdout or "") + (r.stderr or "")
            except Exception as ex:
                ok, out = False, str(ex)
            TRAY_EVENTS.put(("pip", ok, out))   # a interface é atualizada pela thread principal
        threading.Thread(target=work, daemon=True).start()

    def finish_install(self, ok, out):
        try:
            self._install_win.destroy()
        except Exception:
            pass
        log("pip install pystray pillow -> " + ("ok" if ok else "FALHOU") + "\n" + out[-1500:])
        if ok:
            import importlib
            importlib.invalidate_caches()
            self.start_tray()
            if self.tray:
                self.hide_to_tray()
                return
            messagebox.showwarning("Bandeja indisponível",
                                   "As bibliotecas foram instaladas, mas a bandeja não iniciou:\n"
                                   f"{self.tray_error}\n\nFeche e abra o PyDM de novo e tente outra vez.",
                                   parent=self.root)
        else:
            messagebox.showerror("Falha na instalação",
                                 "Não consegui instalar automaticamente. Instale pelo terminal:\n\n"
                                 "    py -m pip install pystray pillow\n\n" + out[-400:], parent=self.root)

    def request_quit(self):
        """"Sair" do menu da bandeja: pede confirmação se houver downloads em andamento."""
        n = sum(1 for d in self.items if d.status in ("Baixando", "Na fila"))
        if n:
            self.show_window()
            if not messagebox.askyesno("Sair do PyDM",
                                       f"Há {n} download(s) em andamento.\n"
                                       "Se sair agora, eles serão pausados (você pode retomar depois).\n\n"
                                       "Sair mesmo assim?", parent=self.root):
                return
        self.quit_app()

    def quit_app(self):
        """Encerra de verdade: pausa os downloads, salva a lista e remove o ícone da bandeja."""
        if self.tray:   # remove o ícone sem travar a interface (espera no máximo 2 s)
            t = threading.Thread(target=self.tray.stop, daemon=True)
            t.start()
            t.join(2)
        for d in self.items:
            if d.status == "Baixando":
                d.pause()
        for d in self.items:
            if d.thread and d.thread.is_alive():
                d.thread.join(timeout=3)
        self.save()
        self.root.destroy()


if __name__ == "__main__":
    if already_running():   # já existe um PyDM (provavelmente na bandeja): ele mostra a janela
        sys.exit(0)
    root = tk.Tk()

    def _tk_error(exc, val, tb):   # sem console (pythonw) os erros de botões/menus ficariam invisíveis
        import traceback
        log("Erro na interface:\n" + "".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = _tk_error
    log(f"PyDM iniciado (argumentos: {sys.argv[1:]}, executável: {sys.executable})")
    app = App(root)
    if "--tray" in sys.argv:   # iniciado com o Windows: fica escondido na bandeja
        if app.tray:
            root.withdraw()

            def _hello():
                try:
                    app.tray.notify("O PyDM foi iniciado e está na bandeja.", "PyDM")
                except Exception as ex:
                    log(f"Aviso da bandeja: {ex!r}")
            root.after(3000, _hello)
        else:
            root.iconify()
    root.mainloop()
    os._exit(0)   # a lista já foi salva em quit_app; garante que nenhuma thread da bandeja segure o processo
