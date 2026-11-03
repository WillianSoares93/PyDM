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
Requer apenas Python 3.8+ (tkinter já vem instalado na maioria dos sistemas).
"""

import os, re, json, time, threading, subprocess, sys, queue, copy
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
    "Geral": {"folder": DEFAULT_FOLDER, "exts": []},
}


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
    h = {"User-Agent": UA}
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
    s = {"categories": copy.deepcopy(DEFAULT_CATEGORIES), "recent": [], "conns": 8, "show_complete": True}
    try:
        with open(CFG, encoding="utf-8") as f:
            j = json.load(f)
        cats = {}
        for name, info in (j.get("categories") or {}).items():
            if isinstance(info, dict) and isinstance(info.get("folder"), str) and info["folder"].strip():
                exts = info.get("exts")
                exts = exts if isinstance(exts, list) else []
                cats[str(name)] = {"folder": os.path.normpath(info["folder"]),
                                   "exts": [str(e).lower().lstrip(".") for e in exts]}
        if cats:
            s["categories"] = cats
        s["recent"] = [p for p in (j.get("recent") or []) if isinstance(p, str)][:10]
        c = j.get("conns")
        if isinstance(c, int) and 1 <= c <= 32:
            s["conns"] = c
        s["show_complete"] = bool(j.get("show_complete", True))
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


class Download:
    def __init__(self, url, folder, conns=8, headers=None):
        self.url, self.folder, self.conns = url, folder, conns
        self.headers = headers or {}
        self.name = self.path = self.size = None
        self.resumable = False
        self.probed = False  # já consultou tamanho/Range no servidor?
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
        # ações ao concluir (valem só nesta sessão, de propósito: ninguém quer o PC desligando dias depois)
        self.on_done = {"modem": False, "quit": False, "shutdown": False, "mode": "Desligar", "force": False}
        self.speed, self._last = 0.0, (time.time(), 0)

    # ---------- persistência ----------
    def to_dict(self):
        j = {k: getattr(self, k) for k in
             ("url", "folder", "conns", "name", "path", "size", "resumable", "segs", "status",
              "headers", "probed", "description", "category", "show_msg", "limit_remember")}
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
        try:
            o.limit_kbps = max(0, int(d.get("limit_kbps") or 0)) if o.limit_remember else 0
        except (TypeError, ValueError):
            o.limit_kbps = 0
        if o.status in ("Baixando", "Na fila"):
            o.status = "Pausado"
        return o

    @property
    def part(self):
        return self.path + ".part"

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

    def _build_segments(self):
        if self.resumable and self.size and self.size > 1_000_000 and self.conns > 1:
            n, step = self.conns, self.size // self.conns
            self.segs = [[i * step, self.size - 1 if i == n - 1 else (i + 1) * step - 1, i * step]
                         for i in range(n)]
        else:
            self.segs = [[0, self.size - 1 if self.size else -1, 0]]

    def _run(self):
        try:
            self.stop.clear()
            self.failed, self.error, self.status = False, "", "Baixando"
            os.makedirs(self.folder, exist_ok=True)
            if not self.probed:
                size, resumable, name = probe(self.url, self.headers)
                self.size, self.resumable = size, resumable
                self.name = self.name or name  # se o usuário já escolheu um nome, ele é mantido
                self.probed = True
            if not self.path:
                self.path = unique_path(self.folder, self.name)
            fresh = not os.path.exists(self.part)
            if fresh or not self.segs or not self.resumable:
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
                os.replace(self.part, self.path)
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
        lim = self.limit_kbps * 1024
        return CHUNK if lim <= 0 else max(2048, min(CHUNK, lim // 8))  # blocos pequenos = limite mais suave

    def _throttle(self, n):
        """Freio compartilhado: cada bloco lido "reserva" n/limite segundos na linha do tempo comum."""
        lim = self.limit_kbps * 1024
        if lim <= 0:
            return
        with self.tlock:
            now = time.monotonic()
            self._next = max(self._next, now) + n / lim
            delay = self._next - now
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
        self._send(200, {"app": "PyDM", "version": 1})

    def do_POST(self):
        if not self._allowed() or self.path != "/add":
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
        self.remember = tk.BooleanVar(value=False)
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
        if self.app.register(self.win, self.url, self.headers, path, cat, self.desc_var.get().strip(),
                             self.info, self.remember.get(), start):
            self.win.destroy()

    def start(self):
        self.commit(True)

    def later(self):
        self.commit(False)

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

        self.build_speed_tab()
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
            estado = "Recebendo..." if dl > 0 else "Conectando..."
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


class App:
    def __init__(self, root):
        self.root = root
        root.title("PyDM - Gerenciador de Downloads")
        root.geometry("1100x440")
        os.makedirs(APP_DIR, exist_ok=True)
        self.settings = load_cfg()
        self.items = []
        self.progress = {}   # id(download) -> ProgressDialog aberto
        self.pending_done = []  # ações "ao concluir" aguardando o fim de todos os downloads
        self.load()

        bar = ttk.Frame(root, padding=6)
        bar.pack(fill="x")
        for txt, cmd in (("➕ Adicionar URL", self.add_dialog), ("▶ Iniciar", self.resume_sel),
                         ("⏸ Pausar", self.pause_sel), ("🗑 Remover", self.remove_sel),
                         ("📂 Abrir pasta", self.open_folder)):
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=2)
        self.conns_var = tk.IntVar(value=self.settings["conns"])
        ttk.Label(bar, text="Conexões:").pack(side="left", padx=(14, 2))
        sp = ttk.Spinbox(bar, from_=1, to=32, width=4, textvariable=self.conns_var, command=self.save_conns)
        sp.pack(side="left")
        sp.bind("<FocusOut>", self.save_conns)
        sp.bind("<Return>", self.save_conns)
        self.clip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Monitorar área de transferência",
                        variable=self.clip_var).pack(side="right")
        self.notify_var = tk.BooleanVar(value=self.settings.get("show_complete", True))
        ttk.Checkbutton(bar, text="Avisar ao concluir", variable=self.notify_var,
                        command=self.save_notify).pack(side="right", padx=(0, 10))

        cols = ("nome", "tamanho", "progresso", "velocidade", "eta", "status", "descricao")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("nome", "Arquivo", 290), ("tamanho", "Tamanho", 90), ("progresso", "Progresso", 90),
                        ("velocidade", "Velocidade", 100), ("eta", "Restante", 80), ("status", "Status", 120),
                        ("descricao", "Descrição", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", lambda e: self.open_file())
        self.status_bar = ttk.Label(root, text="", anchor="w", padding=4)
        self.status_bar.pack(fill="x")
        self.last_clip = ""
        self.bridge = start_bridge()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()
        self.watch_clipboard()

    # ---------- persistência ----------
    def load(self):
        try:
            with open(DB, encoding="utf-8") as f:
                self.items = [Download.from_dict(d) for d in json.load(f)]
        except Exception:
            self.items = []

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
                 status="Na fila" if start else "Pausado")
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
            self.open_info(u)

        ttk.Button(f, text="OK", command=ok).grid(row=2, column=0, sticky="e", pady=(8, 0), padx=4)
        ttk.Button(f, text="Cancelar", command=win.destroy).grid(row=2, column=1, sticky="w",
                                                                  pady=(8, 0), padx=4)
        e.bind("<Return>", ok)
        win.bind("<Escape>", lambda _e: win.destroy())
        e.focus()
        e.select_range(0, "end")

    def selected(self):
        ids = self.tree.selection()
        return [d for d in self.items if str(id(d)) in ids]

    def resume_sel(self):
        for d in self.selected():
            if d.status in ("Pausado", "Erro"):
                d.status = "Na fila"

    def pause_sel(self):
        for d in self.selected():
            if d.status == "Baixando":
                d.pause()
            elif d.status == "Na fila":
                d.status = "Pausado"

    def remove_sel(self):
        sel = self.selected()
        if not sel:
            return
        if not messagebox.askyesno("Remover", f"Remover {len(sel)} item(ns) da lista?\n"
                                              "(Arquivos incompletos serão apagados; os concluídos são mantidos.)"):
            return
        self.remove_items(sel)

    def remove_items(self, sel):
        for d in sel:
            dlg = self.progress.get(id(d))
            if dlg:
                dlg.close()
            d.cancel()
            if d in self.items:
                self.items.remove(d)
            if self.tree.exists(str(id(d))):
                self.tree.delete(str(id(d)))
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
            self.on_close()
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

    def open_folder(self):
        sel = self.selected()
        self._open(sel[0].folder if sel else DEFAULT_FOLDER)

    def open_file(self):
        """Duplo clique: abre o arquivo concluído; nos demais, abre a janela de progresso."""
        for d in self.selected():
            if d.status == "Concluído":
                self._open(d.path)
            else:
                self.open_progress(d)

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
            try:
                self.open_info(j["url"], j["headers"])
            except Exception:
                # se a janela falhar por qualquer motivo, não perde o download: adiciona com os padrões
                self.add(j["url"], DEFAULT_FOLDER, self.settings["conns"], j["headers"])
        active = [d for d in self.items if d.status == "Baixando"]
        for d in self.items:
            if d.status == "Na fila" and len(active) < MAX_ACTIVE:
                d.start()
                active.append(d)
                try:
                    self.open_progress(d)   # janela de progresso ao iniciar
                except Exception:
                    pass   # uma falha na janela nunca deve derrubar o laço principal
        total_speed = 0
        for d in self.items:
            if d.status == "Concluído" and d.announce:
                d.announce = False
                try:
                    self.finished(d)    # janela "Download completo" / ações ao concluir
                except Exception:
                    pass
            iid = str(id(d))
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
            pct = f"{dl * 100 / d.size:.1f}%" if d.size else ("100%" if d.status == "Concluído" else "--")
            eta = fmt_time((d.size - dl) / d.speed) if d.size and d.speed > 1 else "--"
            status = d.status + (f": {d.error[:40]}" if d.status == "Erro" and d.error else "")
            row = (d.name or d.url, fmt_size(d.size), pct,
                   f"{fmt_size(d.speed)}/s" if d.speed else "", eta, status, d.description)
            if self.tree.exists(iid):
                self.tree.item(iid, values=row)
            else:
                self.tree.insert("", "end", iid=iid, values=row)
        self.status_bar.config(text=f"{len(active)} ativo(s) | Velocidade total: {fmt_size(total_speed)}/s | "
                                    f"{len(self.items)} item(ns) | Extensão: " +
                                    ("ativa" if self.bridge else "porta em uso"))
        if self.pending_done and not any(x.status in ("Baixando", "Na fila") for x in self.items):
            acts, self.pending_done = self.pending_done, []
            if self.run_done_actions(acts):
                return   # o PyDM foi encerrado
        if int(now) % 3 == 0:
            self.save()
        self.root.after(500, self.tick)

    def watch_clipboard(self):
        if self.clip_var.get():
            try:
                c = self.root.clipboard_get().strip()
            except tk.TclError:
                c = ""
            if c != self.last_clip:
                self.last_clip = c
                if re.match(r"^https?://\S+\.\w{2,5}(\?\S*)?$", c) and " " not in c:
                    if messagebox.askyesno("PyDM", f"Baixar este link?\n\n{c[:100]}"):
                        self.open_info(c)
        self.root.after(1000, self.watch_clipboard)

    def on_close(self):
        for d in self.items:
            if d.status == "Baixando":
                d.pause()
        for d in self.items:
            if d.thread and d.thread.is_alive():
                d.thread.join(timeout=3)
        self.save()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
