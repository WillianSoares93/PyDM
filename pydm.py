#!/usr/bin/env python3
"""
PyDM - Gerenciador de downloads estilo IDM
- Download segmentado (várias conexões por arquivo)
- Pausar / retomar (inclusive após fechar o programa)
- Fila com limite de downloads simultâneos
- Monitor da área de transferência (copiou um link? ele pergunta se quer baixar)
- Janela "Informação do arquivo de download" (categoria, pasta, descrição)
  ao capturar um link pelo navegador, pela área de transferência ou por "Adicionar URL"
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
CHUNK = 64 * 1024
MAX_ACTIVE = 3
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
    s = {"categories": copy.deepcopy(DEFAULT_CATEGORIES), "recent": [], "conns": 8}
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
        self.speed, self._last = 0.0, (time.time(), 0)

    # ---------- persistência ----------
    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("url", "folder", "conns", "name", "path", "size", "resumable", "segs", "status",
                 "headers", "probed", "description", "category")}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d.get("conns", 8), d.get("headers"))
        for k in ("name", "path", "size", "resumable", "segs", "status"):
            setattr(o, k, d.get(k))
        # arquivos salvos por versões antigas não têm "probed": se já tinham caminho, já foram consultados
        o.probed = d.get("probed", bool(d.get("path")))
        o.description = d.get("description") or ""
        o.category = d.get("category") or ""
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

    # ---------- controle ----------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
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
            threads = []
            for seg in self.segs:
                if seg[1] < 0 or seg[2] <= seg[1]:
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

    def _worker(self, seg):
        tries = 0
        try:
            with open(self.part, "r+b") as f:
                while not self.stop.is_set() and not self.failed:
                    if seg[1] >= 0 and seg[2] > seg[1]:
                        return
                    try:
                        h = dict(self.headers)
                        if self.resumable:
                            h["Range"] = f"bytes={seg[2]}-{seg[1] if seg[1] >= 0 else ''}"
                        with urllib.request.urlopen(req(self.url, h), timeout=20) as r:
                            if self.resumable and r.status != 206:
                                raise IOError("Servidor ignorou o pedido de Range")
                            f.seek(seg[2])
                            while not self.stop.is_set():
                                buf = r.read(CHUNK)
                                if not buf:
                                    break
                                if seg[1] >= 0:
                                    buf = buf[: seg[1] + 1 - seg[2]]
                                f.write(buf)
                                seg[2] += len(buf)
                                tries = 0
                                if seg[1] >= 0 and seg[2] > seg[1]:
                                    return
                            if self.stop.is_set():
                                return
                            if seg[1] < 0:
                                return  # tamanho desconhecido: EOF = fim
                            raise IOError("Conexão encerrada antes do fim")
                    except Exception as ex:
                        tries += 1
                        if tries > 6:
                            self.error, self.failed = str(ex), True
                            return
                        if not self.resumable:
                            seg[2] = seg[0]
                        time.sleep(min(2 * tries, 10))
        except Exception as ex:
            self.error, self.failed = str(ex), True


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


class App:
    def __init__(self, root):
        self.root = root
        root.title("PyDM - Gerenciador de Downloads")
        root.geometry("980x440")
        os.makedirs(APP_DIR, exist_ok=True)
        self.settings = load_cfg()
        self.items = []
        self.load()

        bar = ttk.Frame(root, padding=6)
        bar.pack(fill="x")
        for txt, cmd in (("➕ Adicionar URL", self.add_dialog), ("▶ Iniciar", self.resume_sel),
                         ("⏸ Pausar", self.pause_sel), ("🗑 Remover", self.remove_sel),
                         ("📂 Abrir pasta", self.open_folder)):
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=2)
        self.clip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Monitorar área de transferência",
                        variable=self.clip_var).pack(side="right")

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
        for d in sel:
            d.cancel()
            self.items.remove(d)
            self.tree.delete(str(id(d)))
        self.save()

    def open_folder(self):
        sel = self.selected()
        self._open(sel[0].folder if sel else DEFAULT_FOLDER)

    def open_file(self):
        for d in self.selected():
            if d.status == "Concluído":
                self._open(d.path)

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
        total_speed = 0
        for d in self.items:
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
