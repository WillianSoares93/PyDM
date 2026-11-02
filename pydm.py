#!/usr/bin/env python3
"""
PyDM - Gerenciador de downloads estilo IDM
- Download segmentado (várias conexões por arquivo)
- Pausar / retomar (inclusive após fechar o programa)
- Fila com limite de downloads simultâneos
- Monitor da área de transferência (copiou um link? ele pergunta se quer baixar)
Requer apenas Python 3.8+ (tkinter já vem instalado na maioria dos sistemas).
"""
import os, re, json, time, threading, subprocess, sys, queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = os.path.join(os.path.expanduser("~"), ".pydm")
DB = os.path.join(APP_DIR, "downloads.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CHUNK = 64 * 1024
MAX_ACTIVE = 3
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads")


def fmt_size(n):
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
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


class Download:
    def __init__(self, url, folder, conns=8, headers=None):
        self.url, self.folder, self.conns = url, folder, conns
        self.headers = headers or {}
        self.name = self.path = self.size = None
        self.resumable = False
        self.segs = []  # cada segmento: [inicio, fim, posicao_atual]
        self.status, self.error = "Na fila", ""
        self.stop = threading.Event()
        self.thread = None
        self.failed = False
        self.speed, self._last = 0.0, (time.time(), 0)

    # ---------- persistência ----------
    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("url", "folder", "conns", "name", "path", "size", "resumable", "segs", "status", "headers")}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d.get("conns", 8), d.get("headers"))
        for k in ("name", "path", "size", "resumable", "segs", "status"):
            setattr(o, k, d.get(k))
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
            if not self.path:
                self.size, self.resumable, self.name = probe(self.url, self.headers)
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


class App:
    def __init__(self, root):
        self.root = root
        root.title("PyDM - Gerenciador de Downloads")
        root.geometry("900x420")
        os.makedirs(APP_DIR, exist_ok=True)
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

        cols = ("nome", "tamanho", "progresso", "velocidade", "eta", "status")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("nome", "Arquivo", 330), ("tamanho", "Tamanho", 90), ("progresso", "Progresso", 90),
                        ("velocidade", "Velocidade", 100), ("eta", "Restante", 80), ("status", "Status", 120)):
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
    def add(self, url, folder, conns, headers=None):
        d = Download(url, folder, conns, headers)
        self.items.append(d)
        self.save()

    def add_dialog(self, url=""):
        win = tk.Toplevel(self.root)
        win.title("Novo download")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="URL:").grid(row=0, column=0, sticky="w")
        e_url = ttk.Entry(f, width=60)
        e_url.grid(row=0, column=1, columnspan=2, pady=3)
        if not url:
            try:
                c = self.root.clipboard_get().strip()
                url = c if c.startswith(("http://", "https://")) else ""
            except tk.TclError:
                pass
        e_url.insert(0, url)
        ttk.Label(f, text="Salvar em:").grid(row=1, column=0, sticky="w")
        folder = tk.StringVar(value=DEFAULT_FOLDER)
        ttk.Entry(f, textvariable=folder, width=50).grid(row=1, column=1, pady=3)
        ttk.Button(f, text="...", width=3,
                   command=lambda: folder.set(filedialog.askdirectory() or folder.get())).grid(row=1, column=2)
        ttk.Label(f, text="Conexões:").grid(row=2, column=0, sticky="w")
        conns = tk.IntVar(value=8)
        ttk.Spinbox(f, from_=1, to=32, textvariable=conns, width=5).grid(row=2, column=1, sticky="w", pady=3)

        def ok():
            u = e_url.get().strip()
            if not u.startswith(("http://", "https://", "ftp://")):
                messagebox.showerror("Erro", "Informe uma URL válida (http/https).", parent=win)
                return
            self.add(u, folder.get(), max(1, min(32, conns.get())))
            win.destroy()

        ttk.Button(f, text="Baixar", command=ok).grid(row=3, column=1, sticky="e", pady=(10, 0))
        e_url.focus()

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
            self.add(j["url"], DEFAULT_FOLDER, 8, j["headers"])
            self.root.deiconify()
            self.root.lift()
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
                   f"{fmt_size(d.speed)}/s" if d.speed else "", eta, status)
            if self.tree.exists(iid):
                self.tree.item(iid, values=row)
            else:
                self.tree.insert("", "end", iid=iid, values=row)
        self.status_bar.config(text=f"{len(active)} ativo(s)  |  Velocidade total: {fmt_size(total_speed)}/s  |  "
                                    f"{len(self.items)} item(ns)  |  Extensão: " + ("ativa" if self.bridge else "porta em uso"))
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
                        self.add_dialog(c)
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
