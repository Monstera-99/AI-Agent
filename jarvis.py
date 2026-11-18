#!/usr/bin/env python3
"""Jarvis - asisten AI desktop yang mengorkestrasi Claude Code + Codex CLI.

Tanpa API key: hanya memanggil CLI resmi yang sudah kamu login-kan sendiri.
Berjalan sebagai jendela aplikasi (Edge/Chrome mode --app), tanpa jendela cmd
kalau dibuka lewat Jarvis.vbs.
"""
import json, os, re, secrets, shutil, socket, subprocess, sys, threading, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "2.3"
BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "jarvis_workspace")
OUT = os.path.join(WORK, "hasil")
APPDIR = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "Jarvis")
os.makedirs(APPDIR, exist_ok=True)
DATA = os.path.join(APPDIR, "data.json")
_OLD = os.path.join(WORK, "data.json")
if not os.path.exists(DATA) and os.path.exists(_OLD):
    try:
        shutil.copyfile(_OLD, DATA)
    except Exception:
        pass
ATT = os.path.join(WORK, "lampiran")
os.makedirs(ATT, exist_ok=True)
TIMEOUT = 900
IS_WIN = sys.platform.startswith("win")
NOWIN = 0x08000000 if IS_WIN else 0
os.makedirs(OUT, exist_ok=True)

CMD = {
    "claude": ["claude", "-p"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "-"],
}
LOCK = threading.RLock()
TOKEN = os.environ.get("JARVIS_TOKEN") or secrets.token_urlsafe(24)
PORT = 0
ST = {"updated": None, "rev": 0, "busy": False, "events": [], "cancel": False, "procs": [],
      "last_ping": 0.0, "bye_at": 0.0}

DEFAULTS = {
    "assistant": "Jarvis",
    "user": "",
    "persona": ("Kamu adalah {assistant}, asisten AI pribadi {user}. "
                "Jawab dalam bahasa Indonesia, ringkas, jelas, dan langsung ke inti."),
    "agent": "claude",
    "skill_team": True,
    "skill_sketchup": True,
    "skill_save": True,
    "update_url": "",
    "auto_update": True,
    "perm": "ketat",
    "voice_tts": False,
    "voice_rate": "1.05",
    "voice_name": "",
    "voice_auto": True,
    "display": "cursor",
    "ontop": True,
}
SETTINGS = dict(DEFAULTS)
HISTORY = []


def pr(*a):
    try:
        print(*a, flush=True)
    except Exception:
        pass


def load():
    try:
        d = json.load(open(DATA, encoding="utf-8"))
        SETTINGS.update({k: v for k, v in d.get("settings", {}).items() if k in DEFAULTS})
        HISTORY[:] = d.get("history", [])[-200:]
    except Exception:
        pass


def save():
    try:
        tmp = DATA + ".tmp"
        json.dump({"settings": SETTINGS, "history": HISTORY[-200:]},
                  open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)
    except Exception as e:
        pr("gagal simpan:", e)


def bump():
    with LOCK:
        ST["rev"] += 1


def ev(key, label, detail="", state="run"):
    with LOCK:
        for e in ST["events"]:
            if e["key"] == key:
                e.update(label=label, detail=detail, state=state)
                break
        else:
            ST["events"].append({"key": key, "label": label, "detail": detail, "state": state})
        ST["rev"] += 1


def which2(name):
    """shutil.which + lokasi umum npm/native-installer yang kadang belum ada di PATH."""
    p = shutil.which(name)
    if p:
        return p
    home = os.path.expanduser("~")
    if IS_WIN:
        cands = [os.path.join(os.environ.get("APPDATA", ""), "npm", name + ".cmd"),
                 os.path.join(home, ".local", "bin", name + ".exe"),
                 os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", name + ".cmd")]
    else:
        cands = [os.path.join(home, ".local", "bin", name), "/usr/local/bin/" + name]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def avail():
    return {k: bool(which2(k)) for k in CMD}


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def kill(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, creationflags=NOWIN)
        else:
            p.kill()
    except Exception:
        pass


PERM_FLAGS = {
    "claude": {"ketat": [], "normal": ["--permission-mode", "acceptEdits"],
               "bebas": ["--permission-mode", "bypassPermissions"]},
    "codex": {"ketat": [], "normal": ["--sandbox", "workspace-write"],
              "bebas": ["--sandbox", "danger-full-access"]},
}
IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMG, MAX_IMG_BYTES = 4, 6 * 1024 * 1024


def perm():
    p = SETTINGS.get("perm", "ketat")
    return p if p in ("ketat", "normal", "bebas") else "ketat"


def build_args(agent, imgs):
    fl = PERM_FLAGS[agent][perm()]
    if agent == "codex":
        a = ["exec"] + fl
        if imgs:
            a += ["-i"] + [os.path.join(ATT, n) for n in imgs]
        return a + ["--skip-git-repo-check", "-"]
    return CMD[agent][1:] + fl


def save_images(items):
    import base64
    names = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i, it in enumerate((items or [])[:MAX_IMG]):
        try:
            mt = str(it.get("mediaType", ""))
            if mt not in IMG_EXT:
                continue
            raw = base64.b64decode(it.get("data", ""), validate=False)
            if not raw or len(raw) > MAX_IMG_BYTES:
                continue
            n = "img_%s_%d.%s" % (stamp, i + 1, IMG_EXT[mt])
            with open(os.path.join(ATT, n), "wb") as f:
                f.write(raw)
            names.append(n)
        except Exception:
            continue
    return names


def run(agent, prompt, imgs=None):
    exe = which2(agent)
    if not exe:
        raise RuntimeError("%s belum terpasang (Pengaturan > Akun)" % agent)
    if ST["cancel"]:
        raise RuntimeError("Dibatalkan")
    p = subprocess.Popen([exe] + build_args(agent, imgs or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=WORK, text=True, encoding="utf-8",
                         errors="replace", creationflags=NOWIN)
    with LOCK:
        ST["procs"].append(p)
    try:
        out, err = p.communicate(prompt, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        kill(p)
        raise RuntimeError("%s terlalu lama (>%d detik)" % (agent, TIMEOUT))
    finally:
        with LOCK:
            if p in ST["procs"]:
                ST["procs"].remove(p)
    if ST["cancel"]:
        raise RuntimeError("Dibatalkan")
    out = ANSI.sub("", out or "").strip()
    if p.returncode != 0 and not out:
        msg = ANSI.sub("", err or "").strip()[-400:] or "kode keluar %s" % p.returncode
        if "login" in msg.lower() or "auth" in msg.lower():
            msg += "\n(Sepertinya belum login: buka Pengaturan > Akun.)"
        raise RuntimeError("%s gagal: %s" % (agent, msg))
    return out


OJ_URL = os.environ.get("JARVIS_OJ_URL", "http://127.0.0.1:8000")


def oj_alive():
    """OpenJarvis (jarvis serve) hanya dianggap ada bila server lokalnya menjawab /health."""
    try:
        import urllib.request
        with urllib.request.urlopen(OJ_URL + "/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def oj_ask(prompt):
    import urllib.request
    body = json.dumps({"model": "default", "messages": [{"role": "user", "content": prompt}],
                       "stream": False}).encode()
    rq = urllib.request.Request(OJ_URL + "/v1/chat/completions", data=body,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (d["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        raise RuntimeError("OpenJarvis tidak menjawab di %s (%s). Jalankan 'jarvis serve' dulu." % (OJ_URL, e))


def strip_fence(t):
    return re.sub(r"^```[\w+-]*\n|\n```\s*$", "", t.strip())


def ready_agents():
    return [k for k in CMD if which2(k) and ACC[k]["logged"] is not False]


def pick(preferred):
    r = ready_agents()
    if preferred in r:
        return preferred
    if r:
        return r[0]
    if any(which2(k) for k in CMD):
        raise RuntimeError("Belum login. Buka Pengaturan > Akun lalu login.")
    raise RuntimeError("Belum ada agen. Buka Pengaturan > Akun lalu pasang & login.")


# ---------------------------------------------------------------- auto-update
UPD = {"state": "idle", "new": "", "msg": "", "checked": 0.0}
UPD_HOSTS = ("raw.githubusercontent.com", "github.com", "github.io", "githubusercontent.com")


def ulog(msg):
    try:
        f = os.path.join(APPDIR, "update.log")
        try:
            if os.path.getsize(f) > 200 * 1024:
                open(f, "w").close()
        except OSError:
            pass
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), VERSION, msg))
    except Exception:
        pass


def announce_update():
    f = os.path.join(APPDIR, "last-version.txt")
    prev = ""
    try:
        prev = open(f, encoding="utf-8").read().strip()
    except Exception:
        pass
    try:
        open(f, "w", encoding="utf-8").write(VERSION)
    except Exception:
        pass
    if prev and prev != VERSION:
        ST["updated"] = {"from": prev, "to": VERSION}
        ulog("berhasil diperbarui %s -> %s" % (prev, VERSION))


def update_url():
    u = (SETTINGS.get("update_url") or "").strip()
    if not u:
        try:
            u = open(os.path.join(BASE, "update_url.txt"), encoding="utf-8").read().strip()
        except Exception:
            u = ""
    return os.path.expandvars(u) if is_local(u) else u


def host_ok(u):
    p = urlparse(u)
    h = (p.hostname or "").lower()
    return p.scheme == "https" and any(h == d or h.endswith("." + d) for d in UPD_HOSTS)


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def is_local(u):
    return not u.lower().startswith("http")


def fetch(u, limit=2_000_000):
    if is_local(u):
        with open(u, "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("berkas terlalu besar")
        return data
    import urllib.request
    rq = urllib.request.Request(u, headers={"User-Agent": "Jarvis-Updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(rq, timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("berkas terlalu besar")
    return data


def check_update(apply=True):
    """Cek version.json; bila lebih baru: unduh, verifikasi sha256 + sintaks, ganti jarvis.py (cadangan .bak)."""
    import hashlib, urllib.parse
    u = update_url()
    if not u:
        UPD.update(state="off", msg="Alamat update belum diisi", checked=time.time())
        return
    UPD.update(state="checking", msg="")
    try:
        loc = is_local(u)
        if not loc and not host_ok(u):
            raise RuntimeError("alamat update harus https dari GitHub atau folder lokal/OneDrive")
        m = json.loads(fetch(u, 100_000).decode("utf-8"))
        ver, sha = str(m["version"]), str(m["sha256"]).lower()
        name = str(m.get("url", "jarvis.py"))
        if loc:
            src = os.path.join(os.path.dirname(u), os.path.basename(name))
        else:
            src = urllib.parse.urljoin(u, name)
            if not host_ok(src):
                raise RuntimeError("alamat berkas update tidak diizinkan")
        UPD["checked"] = time.time()
        if vt(ver) <= vt(VERSION):
            UPD.update(state="uptodate", new="", msg="Sudah versi terbaru (%s)" % VERSION)
            return
        if not apply:
            UPD.update(state="available", new=ver, msg="Versi %s tersedia" % ver)
            return
        data = fetch(src)
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError("checksum tidak cocok, update dibatalkan")
        code = data.decode("utf-8")
        compile(code, "jarvis.py", "exec")
        if 'VERSION = "%s"' % ver not in code:
            raise RuntimeError("versi di berkas tidak sesuai manifest")
        me = os.path.abspath(__file__)
        new = me + ".new"
        with open(new, "wb") as f:
            f.write(data)
        try:
            shutil.copyfile(me, me + ".bak")
        except Exception:
            pass
        os.replace(new, me)
        UPD.update(state="ready", new=ver, msg="Versi %s terpasang. Mulai ulang untuk memakainya." % ver)
    except Exception as e:
        UPD.update(state="err", msg="Update gagal: %s" % str(e)[:120], checked=time.time())
    finally:
        ulog("%s: %s" % (UPD["state"], UPD["msg"]))
        with LOCK:
            ST["rev"] += 1


def update_loop():
    time.sleep(6)
    while True:
        if SETTINGS.get("auto_update") and UPD["state"] != "ready":
            check_update(True)
        if UPD["state"] == "ready" and SETTINGS.get("auto_update"):
            # tunggu Jarvis tidak sedang mengerjakan tugas, lalu mulai ulang otomatis
            while ST["busy"]:
                time.sleep(5)
            UPD["msg"] = "Versi %s terpasang. Memulai ulang…" % UPD["new"]
            with LOCK:
                ST["rev"] += 1
            time.sleep(3)
            restart_app()
            return
        time.sleep(600)


def restart_app():
    env = dict(os.environ, JARVIS_TOKEN=TOKEN, JARVIS_PORT=str(PORT), JARVIS_NO_BROWSER="1",
               JARVIS_RETRY="1")
    exe = sys.executable
    if IS_WIN:
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pw):
            exe = pw
    subprocess.Popen([exe, os.path.abspath(__file__)], env=env, cwd=BASE, creationflags=NOWIN,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Timer(0.8, lambda: os._exit(0)).start()


# ---------------------------------------------------------------- prompt
SKETCHUP = ("Jika diminta membuat/mengubah model SketchUp, tulis kode Ruby API SketchUp yang bisa "
            "langsung dijalankan di Ruby Console: satuan meter (pakai .m), bungkus dengan "
            "model.start_operation/commit_operation, buat grup/komponen bernama jelas, "
            "dan beri komentar singkat per bagian.")


def system_text():
    s = SETTINGS
    t = s["persona"].replace("{assistant}", s["assistant"] or "Jarvis") \
                    .replace("{user}", s["user"] or "penggunanya")
    if s["skill_sketchup"]:
        t += "\n" + SKETCHUP
    return t


def chat_prompt(text, imgs=None):
    lines = [system_text(), "", "Riwayat percakapan:"]
    for m in HISTORY[-9:-1]:
        who = "Pengguna" if m["role"] == "user" else "Asisten"
        lines.append("%s: %s" % (who, m["text"][:3000]))
    if imgs:
        lines += ["", "Pengguna melampirkan gambar (lihat/baca berkas ini):"] + [os.path.join(ATT, n) for n in imgs]
    rule = {"ketat": "Jangan mengubah file apa pun di komputer.",
            "normal": "Kamu boleh membuat atau mengubah file hanya di folder kerja saat ini.",
            "bebas": "Kamu boleh membuat atau mengubah file bila diminta, utamakan folder kerja."}[perm()]
    lines += ["", "Pesan terbaru dari pengguna:", text, "", "Jawab sebagai asisten. " + rule]
    return "\n".join(lines)


# ---------------------------------------------------------------- tim agen
PLAN = """Kamu adalah MASTERMIND. Pecah tujuan ini menjadi 2-5 tugas yang INDEPENDEN,
masing-masing menghasilkan SATU file. Bagi tugas ke agen "claude" atau "codex".
Tulis juga "spec": ringkasan keputusan/ukuran/konvensi bersama agar semua file konsisten.
Balas HANYA JSON: {{"spec":"...","tasks":[{{"id":"t1","agent":"claude","file":"nama.ext","instruction":"..."}}]}}
{extra}
TUJUAN: {goal}"""
WORKER = """SPEC BERSAMA:\n{spec}\n\nTUJUAN UTAMA: {goal}\n\nTUGASMU: {ins}\n
Balas HANYA dengan isi lengkap file '{file}', tanpa penjelasan dan tanpa pagar markdown."""
CRITIC = """Kamu KRITIKUS. Cari bug, ketidakkonsistenan antar file, dan kekurangan nyata.
Beri daftar bernomor, spesifik per nama file, tanpa basa-basi.\nTUJUAN: {goal}\n\n{files}"""
REVISE = """SPEC: {spec}\n\nFile '{file}' milikmu saat ini:\n{old}\n\nKritik dari reviewer:\n{crit}\n
Perbaiki hanya yang relevan dengan file ini. Balas HANYA isi lengkap file baru, tanpa penjelasan."""


def ask_json(agent, prompt):
    for _ in range(2):
        t = run(agent, prompt)
        a, b = t.find("{"), t.rfind("}")
        try:
            return json.loads(t[a:b + 1])
        except Exception:
            prompt += "\n\nPERHATIAN: balas HANYA JSON valid, tanpa teks lain."
    raise RuntimeError("Mastermind tidak menghasilkan JSON valid")


def team(goal):
    av = avail()
    boss = pick("claude")
    critic = pick("codex")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(OUT, "tim_" + stamp)
    os.makedirs(outdir, exist_ok=True)
    extra = SKETCHUP if SETTINGS["skill_sketchup"] else ""
    ev("plan", "Mastermind", boss + " menyusun rencana")
    plan = ask_json(boss, PLAN.format(goal=goal, extra=extra))
    spec, tasks = plan.get("spec", ""), plan["tasks"][:5]
    ev("plan", "Mastermind", "%d tugas" % len(tasks), "ok")
    open(os.path.join(outdir, "SPEC.md"), "w", encoding="utf-8").write(spec)
    for i, t in enumerate(tasks):
        t["agent"] = pick(t.get("agent", "claude"))
        t["file"] = os.path.basename(t.get("file") or "t%d.txt" % (i + 1)) or "t%d.txt" % (i + 1)
        ev("w" + str(i), t["file"], t["agent"] + " menulis")

    def work(a):
        i, t = a
        try:
            txt = strip_fence(run(t["agent"], WORKER.format(
                spec=spec, goal=goal, ins=t["instruction"], file=t["file"])))
            open(os.path.join(outdir, t["file"]), "w", encoding="utf-8").write(txt)
            ev("w" + str(i), t["file"], t["agent"] + " selesai", "ok")
            return txt
        except Exception:
            ev("w" + str(i), t["file"], t["agent"] + " gagal", "err")
            raise

    with ThreadPoolExecutor(len(tasks)) as ex:
        texts = list(ex.map(work, enumerate(tasks)))
    ev("crit", "Kritikus", critic + " meninjau")
    files = "\n\n".join("=== %s ===\n%s" % (t["file"], x[:12000]) for t, x in zip(tasks, texts))
    crit = run(critic, CRITIC.format(goal=goal, files=files))
    open(os.path.join(outdir, "KRITIK.md"), "w", encoding="utf-8").write(crit)
    ev("crit", "Kritikus", "selesai", "ok")

    def revise(a):
        i, (t, old) = a
        ev("w" + str(i), t["file"], t["agent"] + " merevisi")
        new = strip_fence(run(t["agent"], REVISE.format(
            spec=spec, file=t["file"], old=old[:12000], crit=crit[:6000])))
        open(os.path.join(outdir, t["file"]), "w", encoding="utf-8").write(new)
        ev("w" + str(i), t["file"], "direvisi", "ok")

    with ThreadPoolExecutor(len(tasks)) as ex:
        list(ex.map(revise, enumerate(zip(tasks, texts))))
    lines = ["Tim selesai. File hasil:", ""]
    lines += ["- %s (%s)" % (t["file"], t["agent"]) for t in tasks]
    lines += ["", "Folder: " + outdir, "", "Kritik reviewer (%s):" % critic, crit[:2500]]
    return "\n".join(lines)


# ---------------------------------------------------------------- chat job
EXT = {"ruby": "rb", "rb": "rb", "python": "py", "py": "py", "javascript": "js", "js": "js",
       "html": "html", "json": "json", "bash": "sh", "sh": "sh", "powershell": "ps1", "css": "css"}


def save_code(reply):
    saved = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i, (lang, body) in enumerate(re.findall(r"```([\w+-]*)\n(.*?)```", reply, re.S)):
        if len(body.strip().splitlines()) < 4:
            continue
        ext = EXT.get(lang.lower(), lang.lower() or "txt")
        path = os.path.join(OUT, "kode_%s_%d.%s" % (stamp, i + 1, ext))
        open(path, "w", encoding="utf-8").write(body)
        saved.append(path)
    return saved


def job(text, mode, imgs=None):
    reply, tools = "", []
    try:
        if mode == "team":
            reply = team(text + ("".join("\nLampiran: " + os.path.join(ATT, n) for n in (imgs or []))))
        elif mode == "openjarvis":
            ev("agent", "openjarvis", "berpikir")
            reply = oj_ask(chat_prompt(text, imgs))
            ev("agent", "openjarvis", "selesai", "ok")
        else:
            agent = pick(mode if mode in CMD else SETTINGS["agent"])
            ev("agent", agent, "berpikir")
            reply = run(agent, chat_prompt(text, imgs), imgs)
            ev("agent", agent, "selesai", "ok")
            if SETTINGS["skill_save"]:
                saved = save_code(reply)
                if saved:
                    ev("save", "simpan kode", "%d file" % len(saved), "ok")
                    reply += "\n\nKode tersimpan di: %s" % OUT
    except Exception as e:
        reply = "Maaf, gagal: %s" % e
        with LOCK:
            if not any(x["state"] == "err" for x in ST["events"]):
                ST["events"].append({"key": "err", "label": "error", "detail": str(e)[:60],
                                     "state": "err"})
    finally:
        with LOCK:
            tools = [{k: e[k] for k in ("label", "detail", "state")} for e in ST["events"]]
            for t in tools:
                if t["state"] == "run":
                    t["state"] = "err"
            HISTORY.append({"role": "assistant", "text": reply or "(kosong)", "tools": tools})
            ST.update(busy=False, events=[], cancel=False, procs=[])
            ST["rev"] += 1
        save()


def start_job(text, mode, imgs=None):
    with LOCK:
        if ST["busy"]:
            return False
        ST.update(busy=True, events=[], cancel=False)
        HISTORY.append({"role": "user", "text": text, "imgs": imgs or []})
        ST["rev"] += 1
    save()
    threading.Thread(target=job, args=(text, mode, imgs or []), daemon=True).start()
    return True


def cancel_job():
    with LOCK:
        ST["cancel"] = True
        procs = list(ST["procs"])
    for p in procs:
        kill(p)


# ---------------------------------------------------------------- akun & login
# Login dikerjakan CLI resmi (claude auth login / codex login). Jarvis tidak pernah
# membaca, menyimpan, atau mengirim token; ia hanya menjalankan perintahnya dan
# menampilkan keluarannya.
ALLOWED_HOSTS = ("claude.ai", "claude.com", "anthropic.com", "openai.com", "chatgpt.com",
                 "nodejs.org", "python.org")
PKG = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
STATUS_CMD = {"claude": ["auth", "status"], "codex": ["login", "status"]}
ACC = {k: {"installed": False, "logged": None, "detail": ""} for k in CMD}
SESS = {"id": 0, "kind": None, "title": "", "text": "", "running": False, "rc": None, "proc": None}
CHECKING = threading.Lock()
BAD_STATUS = re.compile(r"not logged|not authenticated|authenticated:\s*no|\"loggedIn\"\s*:\s*false"
                        r"|logged out|no active", re.I)


def url_ok(u):
    try:
        p = urlparse(u)
        h = (p.hostname or "").lower()
    except Exception:
        return False
    return p.scheme == "https" and any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS)


def check_accounts():
    if not CHECKING.acquire(False):
        return
    try:
        for k in CMD:
            exe = which2(k)
            a = ACC[k]
            a["installed"] = bool(exe)
            if not exe:
                a.update(logged=False, detail="")
                bump()
                continue
            try:
                r = subprocess.run([exe] + STATUS_CMD[k], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=40, cwd=WORK,
                                   stdin=subprocess.DEVNULL, creationflags=NOWIN)
                out = ANSI.sub("", (r.stdout or "") + "\n" + (r.stderr or "")).strip()
                a["logged"] = (r.returncode == 0) and not BAD_STATUS.search(out)
                a["detail"] = ""
                if a["logged"]:
                    try:
                        d = json.loads(out[out.find("{"):out.rfind("}") + 1])
                        a["detail"] = str(d.get("subscriptionType") or d.get("authMethod") or "")[:20]
                    except Exception:
                        pass
            except Exception:
                a["logged"] = None
            bump()
    finally:
        CHECKING.release()
        bump()


def sess_cmd(kind):
    act, _, k = kind.partition("-")
    if k not in CMD or act not in ("login", "logout", "install", "device"):
        raise RuntimeError("perintah tidak dikenal")
    if act == "install":
        npm = which2("npm")
        if not npm:
            raise RuntimeError("Node.js (npm) belum terpasang")
        return "Memasang " + k, [npm, "install", "-g", PKG[k]]
    exe = which2(k)
    if not exe:
        raise RuntimeError(k + " belum terpasang")
    if act == "login":
        args = ["auth", "login"] if k == "claude" else ["login"]
    elif act == "device":
        if k != "codex":
            raise RuntimeError("tidak tersedia")
        args = ["login", "--device-auth"]
    else:
        args = ["auth", "logout"] if k == "claude" else ["logout"]
    return "%s %s" % ({"login": "Login", "device": "Login kode", "logout": "Keluar"}[act], k), [exe] + args


def pump(p, sid):
    while True:
        b = p.stdout.read(1024)
        if not b:
            break
        t = ANSI.sub("", b.decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
        with LOCK:
            if SESS["id"] != sid:
                break
            SESS["text"] = (SESS["text"] + t)[-8000:]
            ST["rev"] += 1
    rc = p.wait()
    with LOCK:
        if SESS["id"] == sid:
            SESS.update(running=False, rc=rc)
            ST["rev"] += 1
    check_accounts()


def start_sess(kind):
    with LOCK:
        if SESS["running"]:
            return False
    title, cmd = sess_cmd(kind)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         bufsize=0, cwd=WORK, creationflags=NOWIN)
    with LOCK:
        SESS.update(id=SESS["id"] + 1, kind=kind, title=title, text="", running=True, rc=None, proc=p)
        sid = SESS["id"]
        ST["rev"] += 1
    threading.Thread(target=pump, args=(p, sid), daemon=True).start()
    return True


def open_terminal(k):
    """Cadangan: jalankan login di jendela terminal biasa (Windows)."""
    exe = which2(k) if k in CMD else None
    if not exe or not IS_WIN:
        return False
    args = ["auth", "login"] if k == "claude" else ["login"]
    subprocess.Popen([exe] + args, cwd=WORK, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
    return True


def sess_input(text):
    p = SESS.get("proc")
    if p and SESS["running"] and p.stdin:
        try:
            p.stdin.write((text[:600] + "\n").encode("utf-8"))
            p.stdin.flush()
        except Exception:
            pass


def sess_stop():
    p = SESS.get("proc")
    if p and SESS["running"]:
        kill(p)


def sess_clear():
    with LOCK:
        if not SESS["running"]:
            SESS.update(kind=None, text="", rc=None, proc=None)
            ST["rev"] += 1


def sess_view():
    with LOCK:
        urls = []
        for u in re.findall(r"https://[^\s\"'<>)\]]+", SESS["text"]):
            u = u.rstrip(".,;:")
            if u not in urls and url_ok(u):
                urls.append(u)
        d = {k: SESS[k] for k in ("kind", "title", "running", "rc")}
        d["text"] = SESS["text"][-3000:]
        d["urls"] = urls[-3:]
        return d


# ---------------------------------------------------------------- UI
HTML = r"""<!doctype html><html lang="id" translate="no"><meta charset="utf-8">
<meta name="google" content="notranslate"><meta http-equiv="Content-Language" content="id">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis</title>
<style>
:root{color-scheme:dark;
--s0:#131314;--s1:#1e1f20;--s2:#282a2c;--s3:#333537;--fg:#e3e3e3;--mut:#c4c7c5;--mut2:#8e918f;--line:#444746;
--pri:#a8c7fa;--onpri:#062e6f;--pric:#0842a0;--onpric:#d3e3fd;--green:#6dd58c;--amber:#ffcf33;--red:#f2b8b5;--err:#ff8983;
--ease:cubic-bezier(.23,1,.32,1);--ease-io:cubic-bezier(.77,0,.175,1);
--font:"Google Sans","Google Sans Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
--bg:var(--s0);--panel:var(--s1);--card:var(--s1);--blue:var(--pri);--sep:var(--line);--edge:rgba(255,255,255,.08)}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--s0);color:var(--fg);font:15px/1.5 var(--font);overflow:hidden;-webkit-font-smoothing:antialiased}
button,select,input,textarea{font-family:inherit}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
#app{height:100%;display:flex;flex-direction:column;position:relative;background:var(--s0)}
header{display:flex;align-items:center;gap:4px;height:56px;padding:0 8px 0 20px;flex:none}
.dot{width:10px;height:10px;border-radius:50%;background:var(--green);flex:none;margin-right:10px}
.dot.busy{background:var(--amber);animation:p 1s infinite}.dot.off{background:var(--err)}
@keyframes p{50%{opacity:.35}}
header b{font-size:20px;font-weight:500;letter-spacing:-.01em}header small{color:var(--mut2);flex:1;margin-left:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ib{background:none;border:0;color:var(--mut);cursor:pointer;width:40px;height:40px;border-radius:50%;display:grid;place-items:center;flex:none;
transition:background-color .15s,color .15s,transform .16s var(--ease)}
@media (hover:hover){.ib:hover{background:rgba(227,227,227,.08);color:var(--fg)}}
.ib:active{transform:scale(.94);background:rgba(227,227,227,.12)}.ib.on{color:var(--pri);background:rgba(168,199,250,.14)}
#chat{flex:1;overflow-y:auto;padding:4px 20px 12px;display:flex;flex-direction:column;gap:14px}
#chat::-webkit-scrollbar,.sb::-webkit-scrollbar{width:10px}#chat::-webkit-scrollbar-thumb,.sb::-webkit-scrollbar-thumb{background:var(--s3);border-radius:10px;border:3px solid var(--s0)}
#hero{display:flex;flex-direction:column;align-items:flex-start;padding:18px 0 6px;gap:4px}
#hero svg{width:52px;height:52px;margin-bottom:8px;filter:drop-shadow(0 0 18px rgba(100,210,255,.35))}
#hero h1{margin:0;font-size:34px;line-height:1.15;font-weight:500;letter-spacing:-.02em;background:linear-gradient(74deg,#4285f4 0,#9b72cb 45%,#d96570 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
#hero p{margin:2px 0 0;color:var(--mut2);font-size:20px;letter-spacing:-.01em;line-height:1.3}
#hero.small,#sugs.small{display:none}
#sugs{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.sug{background:var(--s1);color:var(--fg);border:0;border-radius:16px;padding:10px 16px;cursor:pointer;font-size:14px;text-align:left;
transition:background-color .15s,transform .16s var(--ease)}
@media (hover:hover){.sug:hover{background:var(--s2)}}.sug:active{transform:scale(.97)}
.m{max-width:100%;word-wrap:break-word;overflow-wrap:anywhere;animation:in .22s var(--ease)}
@keyframes in{from{opacity:0;transform:translateY(6px)}}
.u{align-self:flex-end;max-width:86%;background:var(--pric);color:var(--onpric);padding:10px 16px;border-radius:20px 20px 6px 20px;white-space:pre-wrap}
.a{align-self:stretch;padding:2px 4px}
.a>div+div,.a>div+pre,.a>pre+div{margin-top:10px}
.a code{background:var(--s2);padding:1px 6px;border-radius:6px;font:13px Consolas,"Cascadia Mono",monospace}
.a pre{background:var(--s1);border-radius:16px;padding:14px;margin:10px 0;overflow-x:auto;position:relative}
.a pre code{background:none;padding:0;white-space:pre}
.cp{position:absolute;top:8px;right:8px;font-size:12px;background:var(--s3);color:var(--mut);border:0;border-radius:12px;padding:3px 10px;cursor:pointer}
.imgs{display:flex;gap:6px;flex-wrap:wrap;align-self:flex-end;max-width:86%;justify-content:flex-end}
.imgs img{width:96px;height:96px;object-fit:cover;border-radius:16px;cursor:zoom-in}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-self:flex-start}
.chip{display:flex;align-items:center;gap:8px;border-radius:8px;padding:3px 10px;font-size:13px;box-shadow:inset 0 0 0 1px var(--line);color:var(--mut)}
.chip i{width:8px;height:8px;border-radius:50%;background:var(--amber)}
.chip.ok i{background:var(--green)}.chip.err i{background:var(--err)}.chip.run i{animation:p 1s infinite}
.chip span{color:var(--mut2)}
#strip{display:none;gap:8px;padding:0 20px 8px;flex-wrap:wrap}#strip.on{display:flex}
.att{position:relative;width:56px;height:56px}.att img{width:100%;height:100%;object-fit:cover;border-radius:12px}
.att button{position:absolute;top:-6px;right:-6px;width:22px;height:22px;border-radius:50%;border:0;background:var(--s3);color:var(--fg);cursor:pointer;font-size:14px;line-height:1}
#bar{display:flex;gap:4px;align-items:flex-end;margin:4px 12px 14px;padding:6px 6px 6px 6px;background:var(--s1);border-radius:28px;transition:background-color .18s,box-shadow .18s}
#bar:focus-within{background:var(--s2);box-shadow:0 1px 3px rgba(0,0,0,.35),0 4px 12px rgba(0,0,0,.25)}
#in{flex:1;resize:none;max-height:130px;min-height:40px;background:none;color:var(--fg);border:0;padding:10px 6px;font:inherit;outline:none}
#in::placeholder{color:var(--mut2)}
select,input[type=text],textarea.f{background:var(--s1);color:var(--fg);border:1px solid var(--line);border-radius:12px;padding:10px 12px;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
select:focus,input[type=text]:focus,textarea.f:focus{border-color:var(--pri);box-shadow:0 0 0 1px var(--pri)}
#mode{height:40px;width:84px;padding:0 6px;font-size:13px;border:0;background:var(--s3);border-radius:20px;color:var(--fg);flex:none;cursor:pointer}
#send{width:40px;height:40px;border-radius:50%;border:0;background:var(--pri);color:var(--onpri);cursor:pointer;display:grid;place-items:center;flex:none;transition:transform .16s var(--ease),background-color .15s}
#send:active{transform:scale(.94)}#send.stop{background:var(--err);color:#601410}
#mic.on{background:var(--err);color:#601410;animation:p 1s infinite}
#drop{position:absolute;inset:8px;border:2px dashed var(--pri);border-radius:24px;background:rgba(168,199,250,.1);display:none;place-items:center;z-index:9;color:var(--pri);font-weight:500;pointer-events:none}
#drop.on{display:grid}
#set{position:absolute;inset:0;background:var(--s0);display:none;flex-direction:column;z-index:5}
#set.open{display:flex;animation:in .2s var(--ease)}
.sh{display:flex;justify-content:space-between;align-items:center;padding:18px 20px 4px}
.sh h2{margin:0;font-size:24px;font-weight:500;letter-spacing:-.01em}.sh a{color:var(--pri);cursor:pointer;font-weight:500;padding:8px 12px;border-radius:20px}
.tabs{display:flex;gap:0;margin:4px 8px 0;overflow-x:auto;border-bottom:1px solid var(--line)}
.tabs::-webkit-scrollbar{display:none}
.tabs button{flex:none;background:none;border:0;color:var(--mut2);padding:12px 14px;cursor:pointer;font-size:14px;font-weight:500;position:relative;transition:color .15s}
.tabs button.on{color:var(--pri)}
.tabs button.on:after{content:"";position:absolute;left:10px;right:10px;bottom:-1px;height:3px;border-radius:3px 3px 0 0;background:var(--pri)}
.sb{flex:1;overflow-y:auto;padding:8px 20px 18px}
.sb label{display:block;color:var(--mut);margin:16px 0 6px;font-size:13px;font-weight:500}
.sb select,.sb input[type=text],.sb textarea.f{width:100%}.sb textarea.f{min-height:110px;resize:vertical}
.hint{color:var(--mut2);font-size:13px;margin:8px 0}
.card{background:var(--s1);border-radius:16px;padding:16px;margin:10px 0;display:flex;gap:12px;align-items:flex-start}
.card .t{flex:1}.card b{display:block;font-size:16px;font-weight:500}.card .d{color:var(--mut2);font-size:13.5px;margin-top:4px}
.st{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--mut)}
.st i{width:8px;height:8px;border-radius:50%;background:var(--green)}.st.no i{background:var(--err)}.st.am i{background:var(--amber)}
.sw{position:relative;width:52px;height:32px;flex:none}.sw input{opacity:0;width:0;height:0;position:absolute}
.sw s{position:absolute;inset:0;background:var(--s3);box-shadow:inset 0 0 0 2px var(--mut2);border-radius:32px;cursor:pointer;transition:background-color .2s,box-shadow .2s}
.sw s:before{content:"";position:absolute;width:16px;height:16px;left:8px;top:8px;background:var(--mut2);border-radius:50%;transition:transform .22s var(--ease),background-color .2s,width .2s,height .2s}
.sw input:checked+s{background:var(--pri);box-shadow:none}
.sw input:checked+s:before{width:24px;height:24px;left:4px;top:4px;transform:translateX(20px);background:var(--onpri)}
.seg{display:flex;border-radius:20px;margin:10px 0;box-shadow:inset 0 0 0 1px var(--line);overflow:hidden}
.seg button{flex:1;background:none;border:0;color:var(--fg);padding:10px 4px;cursor:pointer;font-size:14px;font-weight:500;transition:background-color .15s}
.seg button+button{box-shadow:inset 1px 0 0 var(--line)}
.seg button.on{background:var(--pric);color:var(--onpric)}.seg button.on.warn{background:#8c1d18;color:#f9dedc}
.sf{padding:12px 20px 16px;display:flex;justify-content:flex-end}
.btn{background:var(--pri);color:var(--onpri);border:0;border-radius:20px;padding:10px 24px;font-size:14px;font-weight:500;cursor:pointer;transition:background-color .15s,transform .16s var(--ease),box-shadow .15s}
@media (hover:hover){.btn:hover{box-shadow:0 1px 3px rgba(0,0,0,.4)}}
.btn:active{transform:scale(.97)}
.btn.g{background:none;color:var(--pri);box-shadow:inset 0 0 0 1px var(--line)}
@media (hover:hover){.btn.g:hover{background:rgba(168,199,250,.08);box-shadow:inset 0 0 0 1px var(--line)}}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;width:100%}
.card .btn{padding:8px 16px}.btn:disabled{opacity:.4;cursor:default}
#sessText{flex:1;min-width:0}
#sessPre{width:100%;max-height:150px;overflow:auto;background:var(--s0);border-radius:12px;padding:10px;margin:0;font:12px Consolas,monospace;white-space:pre-wrap;word-break:break-all;color:var(--mut)}
#toast{position:absolute;left:50%;bottom:92px;transform:translateX(-50%) translateY(8px);background:#e3e3e3;color:#1f1f1f;box-shadow:0 4px 16px rgba(0,0,0,.4);padding:10px 20px;border-radius:12px;font-size:14px;opacity:0;transition:opacity .2s,transform .22s var(--ease);pointer-events:none;z-index:20;white-space:nowrap}
#toast.on{opacity:1;transform:translateX(-50%)}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;place-items:center;z-index:30;cursor:zoom-out}#lb.on{display:grid}#lb img{max-width:92%;max-height:92%;border-radius:16px}
/* ---- mode pulau melayang (cangkang Electron) ---- */
html.shell,html.shell body{background:transparent}
html.shell #app{position:fixed;left:8px;top:0;width:440px;height:684px;border-radius:28px;overflow:hidden;
box-shadow:0 0 0 1px rgba(255,255,255,.06);transform-origin:50% 0;will-change:transform,clip-path;
transition:clip-path .28s var(--ease),transform .24s var(--ease),opacity .2s;filter:drop-shadow(0 12px 28px rgba(0,0,0,.5))}
html.shell.i-open #app{clip-path:inset(0 0 0 0 round 28px);transform:translateY(8px)}
html.shell.i-pill #app,html.shell.i-hidden #app{clip-path:inset(0 116px 628px 116px round 28px);cursor:pointer}
html.shell.i-pill #app{transform:translateY(8px)}
html.shell.i-hidden #app{transform:translateY(-53px);opacity:.92}
html.shell.i-pill #app>*:not(header),html.shell.i-hidden #app>*:not(header){visibility:hidden}
html.shell:not(.i-open) header .ib{display:none}
html.shell:not(.i-open) header{padding:0 16px;justify-content:center}
html.shell:not(.i-open) header small{flex:none;margin-left:8px}
html.shell.i-open header .ib{animation:in .2s var(--ease) .08s both}
html.shell #set{border-radius:28px}
</style>
<body><div id="app">
<header>
 <span class="dot" id="dot"></span><b id="nm">Jarvis</b><small id="stx">siap</small>
 <button class="ib" id="bSpk" title="Suara balasan (baca keras)"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4zM15.5 8.5a5 5 0 010 7M19 5a9 9 0 010 14"/></svg></button>
 <button class="ib" id="bHide" title="Ciutkan (Esc)" style="display:none"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg></button>
 <button class="ib" id="bNew" title="Percakapan baru"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>
 <button class="ib" id="bDir" title="Buka folder hasil"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg></button>
 <button class="ib" id="bSet" title="Pengaturan"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></button>
</header>
<div id="chat"></div>
<div id="strip"></div>
<div id="bar">
 <button class="ib" id="bAtt" title="Lampirkan gambar"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.1l-9.2 9.2a6 6 0 01-8.5-8.5l9.2-9.2a4 4 0 015.7 5.7l-9.2 9.2a2 2 0 01-2.8-2.8l8.5-8.5"/></svg></button>
 <input type="file" id="attFile" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden>
 <textarea id="in" rows="1" placeholder="Ketik perintah..."></textarea>
 <select id="mode" title="Mode"><option value="auto">Auto</option><option value="claude">Claude</option><option value="codex">Codex</option><option value="openjarvis">Lokal</option><option value="team">Tim</option></select>
 <button id="mic" class="ib" title="Bicara (Ctrl+Space)"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/></svg></button>
 <button id="send" title="Kirim"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
</div>
<div id="set">
 <div class="sh"><h2>Settings</h2><a id="sDone">Selesai</a></div>
 <div class="tabs" id="tabs"><button data-t="akun" class="on">Akun</button><button data-t="ai">AI</button><button data-t="suara">Suara</button><button data-t="tampil" id="tTampil" style="display:none">Tampilan</button><button data-t="skill">Skill</button><button data-t="akses">Akses</button><button data-t="update">Update</button><button data-t="tentang">Tentang</button></div>
 <div class="sb" id="sb"></div>
 <div class="sf"><button class="btn" id="sSave">Simpan</button></div>
</div>
<div id="drop">Lepas gambar di sini</div><div id="lb"><img alt=""></div><div id="toast"></div>
</div>
<script>
const T="__TOKEN__",V0="__VERSION__";
const $=s=>document.querySelector(s);
const api=(p,b)=>fetch(p,{method:b===undefined?"GET":"POST",headers:{"X-Token":T,"Content-Type":"application/json"},
 body:b===undefined?undefined:JSON.stringify(b)}).then(r=>r.json());
let S=null,rev=-1,tab="ai",form=null,stick=true;
const CUBE=`<svg viewBox="0 0 120 124"><ellipse cx="60" cy="114" rx="34" ry="7" fill="#0006"/>
<polygon points="60,10 104,34 60,58 16,34" fill="#aef4ff"/><polygon points="16,34 60,58 60,108 16,84" fill="#35dcff"/>
<polygon points="60,58 104,34 104,84 60,108" fill="#1796d0"/>
<ellipse cx="30" cy="66" rx="4" ry="6" fill="#0b2a3a"/><ellipse cx="46" cy="74" rx="4" ry="6" fill="#0b2a3a"/>
<circle cx="31" cy="64" r="1.4" fill="#fff"/><circle cx="47" cy="72" r="1.4" fill="#fff"/>
<path d="M33 82q7 6 14 1" fill="none" stroke="#0b2a3a" stroke-width="2.4" stroke-linecap="round"/>
<g fill="#6fd0f5" transform="matrix(1,-0.545,0,1,0,0)"><rect x="68" y="100" width="10" height="13" rx="1"/><rect x="86" y="100" width="10" height="13" rx="1"/>
<rect x="68" y="119" width="10" height="13" rx="1"/><rect x="86" y="119" width="10" height="13" rx="1"/></g></svg>`;
function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function md(t){
 const parts=t.split(/(```[\s\S]*?```)/g);
 return parts.map(p=>{
  if(p.startsWith("```")){const b=p.replace(/^```[\w+-]*\n?/,"").replace(/```$/,"");
   return `<pre><button class="cp">salin</button><code>${esc(b)}</code></pre>`}
  p=p.replace(/^\n+|\n+$/g,"");if(!p)return"";
  return "<div>"+esc(p).replace(/`([^`\n]+)`/g,"<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g,"<b>$1</b>")
   .replace(/\n/g,"<br>")+"</div>"}).join("")}
function chips(list){if(!list||!list.length)return"";
 return `<div class="chips">`+list.map(c=>`<div class="chip ${c.state}"><i></i>${esc(c.label)} <span>${esc(c.detail||"")}</span></div>`).join("")+`</div>`}
function render(){
 if(!S)return;
 afterState();
 const name=S.settings.assistant||"Jarvis";$("#nm").textContent=name;document.title=name;
 const av=isReady();
 $("#dot").className="dot"+(S.busy?" busy":(av?"":" off"));
 $("#stx").textContent=S.busy?"bekerja...":(av?"siap":"belum login · buka Pengaturan > Akun");
 if($("#set").className==="open"&&tab==="akun")updateAkun();
 const ch=$("#chat");const near=ch.scrollHeight-ch.scrollTop-ch.clientHeight<80;
 const nmU=S.settings.user?", "+esc(S.settings.user):"";
 let h=S.history.length?"":`<div id="hero">${CUBE}<h1>Halo${nmU}</h1><p>Ada yang bisa kubantu?</p></div><div id="sugs"><button class="sug" data-s="Buatkan kode Ruby SketchUp untuk ">Buat model SketchUp</button><button class="sug" data-s="Jelaskan dan perbaiki kode ini:\n">Jelaskan &amp; perbaiki kode</button><button class="sug" data-s="Bagi pekerjaan ini ke Tim (Claude + Codex): " data-m="team">Kerjakan bersama Tim</button></div>`;
 for(const m of S.history){
  if(m.role==="user"){
   if(m.imgs&&m.imgs.length)h+=`<div class="imgs">`+m.imgs.map(n=>`<img src="/api/img?n=${encodeURIComponent(n)}&t=${T}" data-big="1" alt="">`).join("")+`</div>`;
   if(!(m.imgs&&m.imgs.length&&m.text==="(gambar terlampir)"))h+=`<div class="m u">${esc(m.text)}</div>`}
  else h+=chips(m.tools)+`<div class="m a">${md(m.text)}</div>`}
 if(S.busy)h+=chips(S.events)+`<div class="m a">...</div>`;
 ch.innerHTML=h;if(near||S.busy)ch.scrollTop=ch.scrollHeight;
 const sb=$("#send");sb.className=S.busy?"stop":"";
 sb.innerHTML=S.busy?`<svg width="16" height="16" viewBox="0 0 24 24" fill="#fff"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>`:
 `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`;
}
let auto=false;
function isReady(){return Object.values(S.accounts).some(a=>a.installed&&a.logged!==false)}
async function poll(){try{const s=await api("/api/state");S=s;
 if(s.version&&s.version!==V0){location.reload();return}
 if(!auto&&Object.values(s.accounts).every(a=>a.logged!==null)){auto=true;if(!isReady())openSet()}
 if(s.rev!==rev){rev=s.rev;render()}}catch(e){$("#dot").className="dot off";$("#stx").textContent="terputus"}}
async function send(){
 if(S&&S.busy){await api("/api/cancel",{});return poll()}
 const t=$("#in").value.trim();if(!t&&!atts.length)return;
 stopSpeak();const imgs=atts;atts=[];drawStrip();
 $("#in").value="";fit();
 const r=await api("/api/send",{text:t,mode:$("#mode").value,images:imgs});
 if(!r.ok)toast("Sedang bekerja...");poll()}
function fit(){const e=$("#in");e.style.height="auto";e.style.height=Math.min(e.scrollHeight,130)+"px"}
function toast(m){const e=$("#toast");e.textContent=m;e.className="on";setTimeout(()=>e.className="",2200)}
$("#send").onclick=send;
$("#in").addEventListener("input",fit);
$("#in").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}});
$("#bNew").onclick=async()=>{if(S&&S.busy)return;await api("/api/clear",{});poll()};
$("#bDir").onclick=()=>api("/api/open",{});
$("#chat").addEventListener("click",e=>{if(e.target.classList.contains("cp")){
 navigator.clipboard.writeText(e.target.nextElementSibling.textContent).then(()=>toast("Disalin"))}});

// ---- cangkang Electron (jendela melayang)
const SH=window.jarvisShell||null;let ig=true,shKey="";
const ISL={st:"hidden",t:null};
function island(st){if(!SH||ISL.st===st)return;ISL.st=st;clearTimeout(ISL.t);ISL.t=null;
 const r=document.documentElement;r.classList.remove("i-hidden","i-pill","i-open");r.classList.add("i-"+st);SH.state(st);
 if(st==="open")setTimeout(()=>$("#in").focus(),140)}
if(SH){document.documentElement.classList.add("shell","i-hidden");$("#bHide").style.display="";$("#tTampil").style.display="";
 $("#bHide").onclick=()=>island("pill");
 document.addEventListener("mousemove",e=>{const over=!!e.target.closest("#app,#lb");if(over===ig){ig=!over;SH.ignore(ig)}});
 document.addEventListener("mouseleave",()=>{if(!ig){ig=true;SH.ignore(true)}});
 $("#app").addEventListener("click",e=>{if(ISL.st!=="open"){e.stopPropagation();e.preventDefault();island("open")}},true);
 window.addEventListener("keydown",e=>{if(e.key==="Escape"&&ISL.st==="open"&&$("#set").className!=="open")island("pill")});
 SH.onToggle(()=>island(ISL.st==="open"?"pill":"open"));
 SH.onCursor(p=>{const CX=228,st=ISL.st,busy=S&&S.busy;
  if(st==="hidden"){if(p.y>=0&&p.y<=10&&Math.abs(p.x-CX)<=130)island("pill");return}
  if(st==="pill"){const keep=p.x>=CX-170&&p.x<=CX+170&&p.y>=0&&p.y<=96;
   if(keep){clearTimeout(ISL.t);ISL.t=null}else if(!ISL.t&&!busy)ISL.t=setTimeout(()=>{ISL.t=null;if(ISL.st==="pill")island("hidden")},700);return}
  const out=p.x<0||p.x>456||p.y>708||p.y<0;
  if(out&&!document.hasFocus()){if(!ISL.t)ISL.t=setTimeout(()=>{ISL.t=null;if(ISL.st==="open"&&!document.hasFocus()&&!(S&&S.busy))island("pill")},6000)}
  else{clearTimeout(ISL.t);ISL.t=null}})}
function syncShell(){if(!SH||!S)return;const k=(S.settings.display||"cursor")+"|"+(S.settings.ontop!==false);if(k===shKey)return;shKey=k;
 SH.config({display:S.settings.display||"cursor",ontop:S.settings.ontop!==false})}

// ---- lampiran gambar (tombol, tempel, tarik-lepas)
let atts=[];const MAXA=4;
function drawStrip(){const e=$("#strip");e.className=atts.length?"on":"";
 e.innerHTML=atts.map((a,i)=>`<div class="att"><img src="data:${a.mediaType};base64,${a.data}" alt=""><button data-del="${i}" title="Hapus">×</button></div>`).join("")}
function fileToImg(f){return new Promise((ok,no)=>{
 if(!/^image\/(png|jpeg|webp|gif)$/.test(f.type))return no(new Error("Hanya gambar PNG, JPG, WebP, atau GIF."));
 const u=URL.createObjectURL(f),im=new Image();
 im.onload=()=>{const k=Math.min(1,1600/Math.max(im.width,im.height));const c=document.createElement("canvas");
  c.width=Math.round(im.width*k);c.height=Math.round(im.height*k);c.getContext("2d").drawImage(im,0,0,c.width,c.height);
  URL.revokeObjectURL(u);ok({mediaType:"image/jpeg",data:c.toDataURL("image/jpeg",.88).split(",")[1]})};
 im.onerror=()=>{URL.revokeObjectURL(u);no(new Error("Gambar tidak bisa dibaca."))};im.src=u})}
async function addFiles(list){for(const f of Array.from(list)){
 if(atts.length>=MAXA){toast("Maksimal "+MAXA+" gambar per pesan");break}
 try{atts.push(await fileToImg(f))}catch(e){toast(e.message)}}drawStrip()}
$("#bAtt").onclick=()=>$("#attFile").click();
$("#attFile").onchange=e=>{addFiles(e.target.files);e.target.value=""};
$("#strip").addEventListener("click",e=>{const b=e.target.closest("[data-del]");if(!b)return;atts.splice(+b.dataset.del,1);drawStrip()});
$("#in").addEventListener("paste",e=>{const fs=Array.from(e.clipboardData?e.clipboardData.files:[]).filter(f=>f.type.startsWith("image/"));
 if(fs.length){e.preventDefault();addFiles(fs)}});
let dd=0;
window.addEventListener("dragenter",e=>{if(e.dataTransfer&&Array.from(e.dataTransfer.types).includes("Files")){e.preventDefault();dd++;$("#drop").className="on"}});
window.addEventListener("dragover",e=>e.preventDefault());
window.addEventListener("dragleave",()=>{if(--dd<=0){dd=0;$("#drop").className=""}});
window.addEventListener("drop",e=>{e.preventDefault();dd=0;$("#drop").className="";addFiles(e.dataTransfer.files)});
$("#chat").addEventListener("click",e=>{const g=e.target.closest(".sug");if(g){$("#in").value=g.dataset.s;if(g.dataset.m)$("#mode").value=g.dataset.m;fit();$("#in").focus();return}
 if(e.target.dataset.big){$("#lb img").src=e.target.src;$("#lb").className="on"}});
$("#lb").onclick=()=>$("#lb").className="";

// ---- suara: bicara (Web Speech) & baca keras
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;let rec=null,listening=false;
function micStop(){listening=false;$("#mic").className="";try{rec&&rec.stop()}catch(e){}}
function micStart(){
 if(!SR){toast("Browser ini belum mendukung input suara");return}
 if(listening){micStop();return}
 stopSpeak();rec=new SR();rec.lang="id-ID";rec.interimResults=true;rec.continuous=false;
 let fin="";const base=$("#in").value;
 rec.onresult=e=>{let t="";for(const r of e.results){t+=r[0].transcript;if(r.isFinal)fin=t}
  $("#in").value=(base?base+" ":"")+t;fit()};
 rec.onerror=e=>{micStop();if(e.error==="not-allowed")toast("Izinkan mikrofon untuk Jarvis");else if(e.error!=="no-speech"&&e.error!=="aborted")toast("Suara: "+e.error)};
 rec.onend=()=>{const was=listening;micStop();if(was&&fin&&S&&S.settings.voice_auto&&!S.busy)send()};
 try{rec.start();listening=true;$("#mic").className="on";toast("Mendengarkan…")}catch(e){micStop()}}
$("#mic").onclick=micStart;
window.addEventListener("keydown",e=>{if(e.ctrlKey&&e.code==="Space"){e.preventDefault();micStart()}});
function plain(t){return t.replace(/```[\s\S]*?```/g," (blok kode dilewati) ").replace(/`([^`]*)`/g,"$1").replace(/\*\*([^*]*)\*\*/g,"$1").replace(/[#*_>]/g,"").replace(/https?:\/\/\S+/g,"tautan")}
function pickVoice(){const vs=speechSynthesis.getVoices();const n=S&&S.settings.voice_name;
 return vs.find(v=>v.name===n)||vs.find(v=>/^id/i.test(v.lang))||null}
function speak(t){if(!("speechSynthesis"in window))return;speechSynthesis.cancel();
 const u=new SpeechSynthesisUtterance(plain(t).slice(0,1800));u.lang="id-ID";const v=pickVoice();if(v)u.voice=v;
 u.rate=parseFloat(S.settings.voice_rate)||1.05;speechSynthesis.speak(u)}
function stopSpeak(){try{speechSynthesis.cancel()}catch(e){}}
let lastSeen=-1;
function afterState(){
 syncShell();
 const n=S.history.length;
 if(lastSeen<0)lastSeen=n;
 else if(n>lastSeen){const m=S.history[n-1];lastSeen=n;if(m.role==="assistant"&&S.settings.voice_tts&&!S.busy)speak(m.text);if(m.role==="assistant"&&SH&&ISL.st==="hidden"){island("pill");toast&&0}}
 else lastSeen=n;
 $("#bSpk").className="ib"+(S.settings.voice_tts?" on":"");
 if(S.updated){const u=S.updated;S.updated=null;toast("Jarvis diperbarui ke versi "+u.to);api("/api/updated/ack",{})}}
$("#bSpk").onclick=async()=>{const v=!S.settings.voice_tts;await api("/api/settings",{voice_tts:v});if(!v)stopSpeak();toast(v?"Suara balasan menyala":"Suara balasan mati");rev=-1;poll()};
// settings
const sw=(k,t,d)=>`<div class="card"><div class="t"><b>${t}</b><div class="d">${d}</div></div><label class="sw"><input type="checkbox" data-k="${k}" ${form[k]?"checked":""}><s></s></label></div>`;
function drawSet(){
 let h="";
 if(tab==="akun")h=AKUN_HTML;
 if(tab==="ai"){
  h+=`<label>Agen utama (mode Auto)</label><select data-k="agent"><option value="claude">Claude</option><option value="codex">Codex</option></select>
  <label>Nama asisten</label><input type="text" data-k="assistant" value="${esc(form.assistant)}">
  <label>Nama kamu</label><input type="text" data-k="user" value="${esc(form.user)}">
  <label>Kepribadian</label><textarea class="f" data-k="persona">${esc(form.persona)}</textarea>
  <div class="hint">{assistant} dan {user} diganti otomatis.</div>`}
 if(tab==="tampil"){
  h+=`<label>Penempatan layar</label><select data-k="display" id="dSel"><option value="cursor">Ikuti kursor</option><option value="primary">Layar utama</option><option value="auto">Otomatis (layar yang jarang dipakai)</option></select>
  <div class="hint">Otomatis memindahkan Jarvis ke layar yang paling jarang kamu pakai, dengan jeda supaya tidak bolak-balik. Jarvis tidak pindah saat kamu sedang mengetik atau kursor di atasnya.</div>`+
  sw("ontop","Selalu di atas","Panel tetap terlihat di atas jendela lain dan tidak merebut fokus.")+
  `<div class="card"><div class="t"><b>Mulai bersama Windows</b><div class="d">Jarvis menyala otomatis saat kamu masuk ke Windows.</div></div><label class="sw"><input type="checkbox" id="aSt"><s></s></label></div>
  <div class="hint">Jarvis melayang di tepi atas layar dan muncul saat kursor mengarah ke sana. Klik untuk membuka chat. Pintasan <b>Ctrl+Alt+J</b> membuka dan menutup chat, Esc atau tombol panah atas menciutkannya.</div>`}
 if(tab==="skill"){
  h+=`<div class="hint">Kemampuan ${esc(form.assistant)}. Matikan yang tidak dipakai supaya hemat kuota.</div>`+
  sw("skill_team","Tim Agen","Mode Tim: Claude menyusun rencana, tugas dibagi ke Claude &amp; Codex, lalu Codex mengkritik dan semua direvisi. Memakai sekitar 6-8 panggilan model.")+
  sw("skill_sketchup","SketchUp Ruby","Jawaban kode mengikuti API Ruby SketchUp: satuan meter, start/commit_operation, grup bernama.")+
  sw("skill_save","Simpan kode otomatis","Blok kode panjang di jawaban disimpan ke folder hasil (misalnya .rb).")}
 if(tab==="akses"){
  h+=`<label>Folder kerja</label><div class="card"><div class="t"><div class="d" style="margin:0;word-break:break-all">${esc(S.work)}</div></div></div>
  <button class="btn g" id="oFold">Buka folder hasil</button>
  <label>Tingkat izin agen</label>
  <div class="seg" id="perm"><button data-p="ketat">Ketat</button><button data-p="normal">Normal</button><button data-p="bebas">Bebas</button></div>
  <div class="hint" id="permD"></div>
  <div class="hint" style="margin-top:14px">Jarvis tidak memakai API key dan tidak mengambil token login. Ia hanya menjalankan perintah <code>claude -p</code> dan <code>codex exec</code> milik akunmu.</div>
  <div class="hint">Jarvis tidak diberi kontrol layar. Tingkat izin hanya mengatur apa yang boleh dikerjakan CLI.</div>`}
 if(tab==="suara"){
  h+=sw("voice_tts","Baca balasan keras","Jarvis membacakan jawabannya (suara bawaan Windows/Edge, gratis).")+
  sw("voice_auto","Kirim otomatis setelah bicara","Selesai bicara lewat mikrofon, pesan langsung dikirim.")+
  `<label>Suara</label><select data-k="voice_name" id="vSel"></select>
  <label>Kecepatan</label><select data-k="voice_rate"><option>0.85</option><option>0.95</option><option>1.05</option><option>1.2</option><option>1.35</option></select>
  <div class="row" style="margin-top:12px"><button class="btn g" id="vTest">Coba suara</button></div>
  <div class="hint">Tombol mikrofon atau Ctrl+Space untuk bicara. Pengenalan suara memakai layanan browser (Edge) dan butuh izin mikrofon.</div>`}
 if(tab==="tentang"){
  h+=`<div class="card"><div class="t"><b>Jarvis ${S.version}</b><div class="d">Orkestrator Claude Code + Codex CLI dalam satu aplikasi. Data percakapan tersimpan lokal di folder kerja.</div></div></div>
  <div class="hint">Data dan pengaturan disimpan di <code>${esc(S.appdir||"")}</code>, terpisah dari folder program, jadi update tidak menghapusnya.</div>`}
 if(tab==="update"){
  h+=`<div class="card"><div class="t"><b>Pembaruan otomatis</b><div class="d" id="updMsg"></div></div></div>
  <label>Alamat update (version.json di GitHub, atau jalur folder lokal/OneDrive)</label><input type="text" data-k="update_url" placeholder="https://raw.githubusercontent.com/AKUN/jarvis/main/version.json">
  <label><input type="checkbox" data-k="auto_update"> Pasang update dan mulai ulang otomatis</label>
  <button class="btn g" id="updNow">Cek update sekarang</button> <button class="btn" id="updRestart" style="display:none">Mulai ulang</button>
  <div class="hint">Dicek tiap 10 menit. Versi baru diunduh, diverifikasi checksum SHA-256 dan sintaks, lalu Jarvis mulai ulang sendiri saat tidak sibuk. Cadangan: jarvis.py.bak. Catatan update ada di <code>update.log</code> di folder data.</div>`}
 if(tab==="tentang"){
  h+=`<div class="hint">Pemakaian CLI dengan akun langganan tunduk pada syarat layanan masing-masing penyedia, dan batas kuota langganan tetap berlaku. Aturannya bisa berubah.</div>`}
 $("#sb").innerHTML=h;if(tab==="akun")updateAkun();
 $("#sb").querySelectorAll("[data-k]").forEach(el=>{const k=el.dataset.k;
  if(el.tagName==="SELECT")el.value=form[k];
  el.oninput=el.onchange=()=>{form[k]=el.type==="checkbox"?el.checked:el.value}});
 const o=$("#oFold");if(o)o.onclick=()=>api("/api/open",{});
 if(tab==="tampil"&&SH){
  SH.displays().then(ds=>{const sel=$("#dSel");if(!sel)return;ds.forEach(d=>{const o=document.createElement("option");o.value=d.id;o.textContent=d.label;sel.appendChild(o)});sel.value=form.display||"cursor"});
  SH.autostart().then(v=>{const c=$("#aSt");if(c){c.checked=!!v;c.onchange=()=>SH.autostart(c.checked)}})}
 if(tab==="akses"){
  const ds={ketat:"Ketat: agen hanya membaca dan menjawab. Tidak mengubah file.",normal:"Normal: agen boleh membuat dan mengubah file di folder kerja Jarvis.",bebas:"Bebas: agen boleh bertindak tanpa konfirmasi, termasuk di luar folder kerja. Pakai hanya bila kamu paham risikonya."};
  const paint=()=>{document.querySelectorAll("#perm button").forEach(b=>{b.className=(b.dataset.p===form.perm?"on"+(b.dataset.p==="bebas"?" warn":""):"")});$("#permD").textContent=ds[form.perm]||ds.ketat};
  $("#perm").onclick=e=>{const p=e.target.dataset.p;if(!p)return;
   if(p==="bebas"&&!confirm("Mode Bebas memberi agen izin penuh tanpa konfirmasi. Lanjutkan?"))return;form.perm=p;paint()};paint()}
 if(tab==="suara"){
  const fill=()=>{const vs=speechSynthesis.getVoices();const sel=$("#vSel");if(!sel)return;
   const list=vs.slice().sort((a,b)=>(/^id/i.test(b.lang)-/^id/i.test(a.lang))||a.name.localeCompare(b.name));
   sel.innerHTML=`<option value="">Otomatis (Indonesia)</option>`+list.map(v=>`<option value="${esc(v.name)}">${esc(v.name)} (${v.lang})</option>`).join("");sel.value=form.voice_name||""};
  fill();try{speechSynthesis.onvoiceschanged=fill}catch(e){}
  $("#vTest").onclick=()=>{const keep=S.settings;S.settings=Object.assign({},keep,{voice_name:form.voice_name,voice_rate:form.voice_rate});speak("Halo, aku Jarvis. Beginilah suaraku.");S.settings=keep}}
 if(tab==="update"){const U=S.update||{};$("#updMsg").textContent=U.msg||"Belum dicek";
  $("#updNow").onclick=()=>api("/api/settings",{update_url:form.update_url||"",auto_update:!!form.auto_update}).then(()=>api("/api/update/check",{apply:true}));
  const rb=$("#updRestart");rb.style.display=U.state==="ready"?"inline-block":"none";rb.onclick=doRestart}
}
const AKUN_HTML=`<div class="hint">Masuk dengan akun langgananmu sendiri. Jarvis tidak menyimpan password atau token: login dikerjakan CLI resmi (Claude Code &amp; Codex) di komputer ini.</div>
<div id="accCards"></div>
<div id="sessBox" style="display:none"><div class="card" style="flex-direction:column;gap:8px">
 <div style="display:flex;justify-content:space-between;width:100%"><b id="sessTitle"></b><span class="st" id="sessSt"></span></div>
 <pre id="sessPre"></pre><div class="row" id="sessUrls"></div>
 <div id="sessIn" class="row"><input type="text" id="sessText" placeholder="Tempel kode dari browser bila diminta"><button class="btn g" id="sessSend">Kirim</button></div>
 <div class="row"><button class="btn g" id="sessStop">Hentikan</button><button class="btn g" id="sessClose">Tutup</button><span id="sessTerm"></span></div></div></div>
<div class="row" style="margin-top:8px"><button class="btn g" id="accCheck">Periksa ulang</button></div>
<div class="hint" style="margin-top:12px">Satu orang, satu akun: setiap pengguna login dengan akunnya sendiri. Jangan membagikan akun atau token. Pemakaian lewat CLI tunduk pada syarat layanan Anthropic dan OpenAI serta batas kuota langganan.</div>`;
function updateAkun(){
 const box=$("#accCards");if(!box)return;
 const ss=S.sess,run=ss&&ss.running,dis=run?" disabled":"";
 const card=(k,title,sub)=>{const a=S.accounts[k];let chip,btns="";
  if(!a.installed){chip=`<span class="st no"><i></i>Belum terpasang</span>`;
   btns=S.npm?`<button class="btn" data-act="install-${k}"${dis}>Pasang</button>`:`<button class="btn g" data-url="https://nodejs.org">Pasang Node.js dulu</button><span class="d">Node.js diperlukan untuk memasang CLI.</span>`}
  else if(a.logged===false){chip=`<span class="st am"><i></i>Belum login</span>`;
   btns=`<button class="btn" data-act="login-${k}"${dis}>Login</button>`+(k==="codex"?`<button class="btn g" data-act="device-codex"${dis}>Login dengan kode</button>`:"")}
  else if(a.logged===null){chip=`<span class="st am"><i></i>Memeriksa...</span>`}
  else{chip=`<span class="st"><i></i>Siap${a.detail?" · "+esc(a.detail):""}</span>`;btns=`<button class="btn g" data-act="logout-${k}"${dis}>Keluar / ganti akun</button>`}
  return `<div class="card" style="flex-direction:column;gap:10px"><div style="display:flex;justify-content:space-between;align-items:flex-start;width:100%"><div><b>${title}</b><div class="d">${sub}</div></div>${chip}</div><div class="row">${btns}</div></div>`};
 box.innerHTML=card("claude","Claude","Akun Claude Pro / Max · lewat Claude Code")+card("codex","ChatGPT","Akun ChatGPT Plus / Pro · lewat Codex CLI");
 const sb=$("#sessBox");
 if(ss&&ss.kind){sb.style.display="";$("#sessTitle").textContent=ss.title;
  $("#sessSt").className="st"+(ss.running?" am":(ss.rc===0?"":" no"));
  $("#sessSt").innerHTML=`<i></i>${ss.running?"berjalan":(ss.rc===0?"selesai":"berhenti")}`;
  const pre=$("#sessPre");const bot=pre.scrollHeight-pre.scrollTop-pre.clientHeight<30;pre.textContent=ss.text||"...";if(bot)pre.scrollTop=pre.scrollHeight;
  $("#sessUrls").innerHTML=(ss.running?ss.urls:[]).map((u,i)=>`<button class="btn g" data-url="${u}">${i?"Buka link "+(i+1):"Buka link login"}</button>`).join("");
  $("#sessIn").style.display=ss.running&&/^(login|device)/.test(ss.kind)?"":"none";
  $("#sessStop").style.display=ss.running?"":"none";$("#sessClose").style.display=ss.running?"none":"";
  const m=/^login-(\w+)/.exec(ss.kind||"");
  $("#sessTerm").innerHTML=(!ss.running&&ss.rc!==0&&m)?`<button class="btn g" data-act="term-${m[1]}">Buka di jendela terminal</button>`:"";
 }else sb.style.display="none";
}
$("#sb").addEventListener("click",async e=>{
 const t=e.target.closest("[data-act],[data-url],#sessSend,#sessStop,#sessClose,#accCheck");if(!t||t.disabled)return;
 if(t.dataset.act)await api("/api/sess/start",{kind:t.dataset.act});
 else if(t.dataset.url)await api("/api/openurl",{url:t.dataset.url});
 else if(t.id==="sessSend"){const v=$("#sessText").value;$("#sessText").value="";if(v)await api("/api/sess/input",{text:v})}
 else if(t.id==="sessStop")await api("/api/sess/stop",{});
 else if(t.id==="sessClose")await api("/api/sess/clear",{});
 else if(t.id==="accCheck")await api("/api/acc/check",{});
 poll()});
$("#sb").addEventListener("keydown",e=>{if(e.key==="Enter"&&e.target.id==="sessText"){e.preventDefault();$("#sessSend").click()}});
function doRestart(){api("/api/restart",{}).then(r=>{if(r&&r.ok===false){alert("Jarvis sedang bekerja, coba lagi nanti");return}
 document.body.innerHTML='<div style="padding:40px;color:#9ab;font-family:sans-serif">Memulai ulang Jarvis…</div>';
 const t=setInterval(()=>fetch("/api/state",{headers:{"X-Token":T}}).then(x=>{if(x.ok){clearInterval(t);location.reload()}}).catch(()=>{}),1500)})}
function openSet(){form=Object.assign({},S.settings);tab=isReady()?"ai":"akun";
 document.querySelectorAll("#tabs button").forEach(b=>b.className=b.dataset.t===tab?"on":"");
 drawSet();$("#set").className="open";if(SH)SH.panelOpen(true)}
$("#bSet").onclick=openSet;
$("#sDone").onclick=()=>{$("#set").className="";if(SH)SH.panelOpen(false)};
$("#sSave").onclick=async()=>{await api("/api/settings",form);$("#set").className="";if(SH)SH.panelOpen(false);toast("Tersimpan");rev=-1;poll()};
$("#tabs").onclick=e=>{const t=e.target.dataset.t;if(!t)return;tab=t;
 document.querySelectorAll("#tabs button").forEach(b=>b.className=b.dataset.t===tab?"on":"");drawSet()};
let down=0;
setInterval(()=>api("/api/ping",{}).then(()=>{down=0}).catch(()=>{if(++down===2)waitBack()}),3000);
function waitBack(){const t=setInterval(()=>fetch("/api/state",{headers:{"X-Token":T}}).then(x=>{if(x.ok){clearInterval(t);location.reload()}}).catch(()=>{}),1500)}
window.addEventListener("pagehide",()=>{try{navigator.sendBeacon("/api/bye?t="+T)}catch(e){}});
poll();setInterval(poll,800);$("#in").focus();
</script></html>"""


# ---------------------------------------------------------------- server
def open_folder(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        pr("tidak bisa buka folder:", e)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def ok_host(self):
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def ok_token(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def send(self, body, ctype="application/json", code=200):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self.ok_host():
            return self.send("{}", code=403)
        u = urlparse(self.path)
        if u.path == "/":
            if "t=" + TOKEN not in (u.query or ""):
                return self.send("Buka Jarvis lewat Jarvis.vbs", "text/plain", 403)
            return self.send(HTML.replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION), "text/html")
        if u.path == "/api/img":
            q = dict(x.split("=", 1) for x in (u.query or "").split("&") if "=" in x)
            n = os.path.basename(q.get("n", ""))
            ext = n.rsplit(".", 1)[-1].lower()
            ct = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}.get(ext)
            fp = os.path.join(ATT, n)
            if q.get("t") == TOKEN and ct and n.startswith("img_") and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return
            return self.send("{}", code=404)
        if u.path == "/api/state" and self.ok_token():
            with LOCK:
                d = {"rev": ST["rev"], "busy": ST["busy"], "events": ST["events"],
                     "history": HISTORY, "settings": SETTINGS, "accounts": ACC, "openjarvis": oj_alive(), "update": UPD,
                     "npm": bool(which2("npm")), "sess": sess_view(),
                     "work": OUT, "version": VERSION, "updated": ST["updated"], "appdir": APPDIR}
                return self.send(json.dumps(d, ensure_ascii=False))
        self.send("{}", code=404)

    def do_POST(self):
        if not self.ok_host():
            return self.send("{}", code=403)
        u = urlparse(self.path)
        if u.path == "/api/bye":
            if "t=" + TOKEN in (u.query or ""):
                ST["bye_at"] = time.time() + 4
            return self.send("{}")
        if not self.ok_token():
            return self.send("{}", code=403)
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 40 * 1024 * 1024)
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            d = {}
        r = {"ok": True}
        if u.path == "/api/ping":
            ST["last_ping"] = time.time()
            ST["bye_at"] = 0.0
        elif u.path == "/api/send":
            text = str(d.get("text", "")).strip()[:20000]
            mode = d.get("mode", "auto")
            if mode == "team" and not SETTINGS["skill_team"]:
                mode = "auto"
            imgs = save_images(d.get("images"))
            if not text and imgs:
                text = "(gambar terlampir)"
            r["ok"] = bool(text) and start_job(text, mode, imgs)
        elif u.path == "/api/quit":
            self.send(json.dumps(r))
            save()
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        elif u.path == "/api/updated/ack":
            ST["updated"] = None
            with LOCK:
                ST["rev"] += 1
        elif u.path == "/api/cancel":
            cancel_job()
        elif u.path == "/api/clear":
            with LOCK:
                if not ST["busy"]:
                    HISTORY.clear()
                    ST["rev"] += 1
            save()
        elif u.path == "/api/settings":
            with LOCK:
                for k, v in d.items():
                    if k in DEFAULTS and type(v) == type(DEFAULTS[k]):
                        SETTINGS[k] = v[:4000] if isinstance(v, str) else v
                ST["rev"] += 1
            save()
        elif u.path == "/api/open":
            open_folder(OUT)
        elif u.path == "/api/update/check":
            threading.Thread(target=check_update, args=(bool(d.get("apply", True)),), daemon=True).start()
        elif u.path == "/api/restart":
            if not ST["busy"]:
                restart_app()
            else:
                r["ok"] = False
        elif u.path == "/api/acc/check":
            threading.Thread(target=check_accounts, daemon=True).start()
        elif u.path == "/api/sess/start":
            kind = str(d.get("kind", ""))
            try:
                if kind.startswith("term-"):
                    r["ok"] = open_terminal(kind[5:])
                else:
                    r["ok"] = start_sess(kind)
            except Exception as e:
                r = {"ok": False, "error": str(e)}
                with LOCK:
                    SESS.update(id=SESS["id"] + 1, kind=kind, title="Gagal", text=str(e),
                                running=False, rc=1, proc=None)
                    ST["rev"] += 1
        elif u.path == "/api/sess/input":
            sess_input(str(d.get("text", "")))
        elif u.path == "/api/sess/stop":
            sess_stop()
        elif u.path == "/api/sess/clear":
            sess_clear()
        elif u.path == "/api/openurl":
            url = str(d.get("url", ""))
            r["ok"] = url_ok(url)
            if r["ok"]:
                webbrowser.open(url)
        else:
            return self.send("{}", code=404)
        self.send(json.dumps(r))


class Srv(ThreadingHTTPServer):
    # Di Windows, SO_REUSEADDR membolehkan dua proses memakai port yang sama.
    # Matikan, dan minta akses eksklusif, supaya salinan kedua pindah ke port lain.
    allow_reuse_address = not IS_WIN

    def server_bind(self):
        if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def find_browser():
    pf = [os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
          os.environ.get("LocalAppData", "")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
           r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    for b in pf:
        for r in rel:
            p = os.path.join(b, r)
            if b and os.path.exists(p):
                return p
    for n in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(n)
        if p:
            return p
    return None


def launch_window(url):
    b = find_browser()
    if not b:
        webbrowser.open(url)
        return
    prof = os.path.join(WORK, "window_profile")
    subprocess.Popen([b, "--app=" + url, "--user-data-dir=" + prof, "--window-size=460,800",
                      "--no-first-run", "--no-default-browser-check",
                      "--disable-features=Translate,TranslateUI,msEdgeTranslate,msEdgeSidebar",
                      "--lang=id"], creationflags=NOWIN)


def watchdog():
    start = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        if ST["bye_at"] and now > ST["bye_at"] and not ST["busy"]:
            os._exit(0)
        if not ST["last_ping"] and now - start > 90:
            os._exit(0)
        if ST["last_ping"] and now - ST["last_ping"] > 240 and not ST["busy"]:
            os._exit(0)


def main():
    global PORT
    load()
    srv = None
    base = int(os.environ.get("JARVIS_PORT", 8765))
    if os.environ.get("JARVIS_RETRY"):
        for _ in range(40):
            try:
                srv = Srv(("127.0.0.1", base), H)
                PORT = base
                break
            except OSError:
                time.sleep(0.5)
    else:
        for p in range(base, base + 40):
            try:
                srv = Srv(("127.0.0.1", p), H)
                PORT = p
                break
            except OSError:
                continue
    if not srv:
        sys.exit("Tidak ada port kosong")
    url = "http://127.0.0.1:%d/?t=%s" % (PORT, TOKEN)
    pr("Jarvis berjalan:", url)
    threading.Thread(target=check_accounts, daemon=True).start()
    announce_update()
    threading.Thread(target=update_loop, daemon=True).start()
    if not os.environ.get("JARVIS_NO_BROWSER"):
        threading.Thread(target=launch_window, args=(url,), daemon=True).start()
    if not os.environ.get("JARVIS_NO_WATCHDOG"):
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
