"""Bulut hatırlatıcısının yerel uçtan uca testi: `wrangler dev` + sahte Notion/Telegram sunucusu.

Gerçek Notion'a, Telegram'a ya da Cloudflare'a çıkmaz. Kullanım (claude-telefon klasöründen):
    .venv\\Scripts\\python.exe bulut\\test\\uctan_uca.py
Senaryolar: vadesi gelen tek seferlik ve tekrarlayan kayıtlar gider; gecikme payı içindekiler, brifing, çok eski ve
servisin sahiplendiği kayıt gitmez; Notion yazılamazsa aynı zaman ikinci kez gitmez; Telegram hatasında sahiplenme
bırakılır ve sonraki taramada gider; /sahiplen yetki ve anahtar denetimi; bulut mesajında hb: düğmeleri; nabız:
hiç nabız yokken uyarı yok, susunca bir kez uyarı, nabız gelince bir kez "yeniden açık", temiz kapanıştan sonra uyarı yok,
sessiz saatte (SESSIZ_SAAT) nabız mesajları bildirimsiz.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BULUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRANGLER = os.path.join(BULUT, "node_modules", "wrangler", "bin", "wrangler.js")
SAHTE_PORT, WORKER_PORT = 8799, 8788
DS = "11111111-2222-4333-8444-555555555555"
SERVIS_JETON = "test-servis-jetonu"
TZ = dt.timezone(dt.timedelta(hours=3))
NABIZ_ESIK_SN = 6   # testte "sustu" eşiği (gerçekte 15 dk)

SATIRLAR, GIDEN, YAMALAR = {}, [], []
DURUM = {"tg_hata": 1}   # "TG_HATA" konulu mesaj ilk denemede 502 alır


def satir(pid, konu, dk_once, durum="Bekliyor", tur="Hatırlatma", tekrar=None):
    z = (dt.datetime.now(TZ) - dt.timedelta(minutes=dk_once)).replace(second=0, microsecond=0)
    SATIRLAR[pid] = {"id": f"{pid[:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:]}", "properties": {
        "Konu": {"type": "title", "title": [{"plain_text": konu}]},
        "Not": {"type": "rich_text", "rich_text": []},
        "Zaman": {"type": "date", "date": {"start": z.isoformat(timespec="milliseconds")}},
        "Durum": {"type": "select", "select": {"name": durum}},
        "Tür": {"type": "select", "select": {"name": tur}},
        "Tekrar": {"type": "select", "select": {"name": tekrar} if tekrar else None}}}
    return z


class Sahte(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # workerd bağlantıları yeniden kullanıyor

    def log_message(self, *a):
        pass

    def _cevap(self, kod, govde):
        b = json.dumps(govde).encode()
        self.send_response(kod)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _govde(self):
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            veri = b""
            while True:
                boy = int(self.rfile.readline().strip().split(b";")[0], 16)
                if not boy:
                    self.rfile.readline()
                    break
                veri += self.rfile.read(boy)
                self.rfile.readline()
        else:
            veri = self.rfile.read(int(self.headers.get("content-length") or 0))
        return json.loads(veri or b"{}")

    def do_POST(self):
        g = self._govde()
        if self.path == f"/notion/data_sources/{DS}/query":
            assert self.headers["authorization"] == "Bearer sahte-notion"
            assert g["filter"] == {"property": "Durum", "select": {"equals": "Bekliyor"}}
            sonuc = [s for s in SATIRLAR.values() if s["properties"]["Durum"]["select"]["name"] == "Bekliyor"]
            return self._cevap(200, {"results": sonuc, "has_more": False})
        if self.path == "/tg/botsahte-tg/sendMessage":
            if "TG_HATA" in g["text"] and DURUM["tg_hata"]:
                DURUM["tg_hata"] -= 1
                return self._cevap(502, {"ok": False, "description": "Bad Gateway"})
            GIDEN.append(g)
            return self._cevap(200, {"ok": True, "result": {"message_id": len(GIDEN)}})
        self._cevap(404, {})

    def do_PATCH(self):
        g = self._govde()
        pid = self.path.rsplit("/", 1)[-1].replace("-", "")
        YAMALAR.append((pid, g))
        if SATIRLAR[pid]["properties"]["Konu"]["title"][0]["plain_text"].startswith("NOTION_HATA"):
            return self._cevap(500, {"message": "sahte hata"})
        p = SATIRLAR[pid]["properties"]
        for ad, deger in g["properties"].items():
            p[ad] = {"type": "select" if "select" in deger else "date", **deger}
        self._cevap(200, {})


def iste(yol, govde=None, jeton=SERVIS_JETON):
    r = urllib.request.Request(f"http://127.0.0.1:{WORKER_PORT}{yol}", method="POST" if govde is not None else "GET",
                               data=json.dumps(govde).encode() if govde is not None else None,
                               headers={"authorization": f"Bearer {jeton}"})
    try:
        with urllib.request.urlopen(r, timeout=30) as c:
            return c.status, c.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


SONUC = []


def kontrol(ad, kosul, ayrinti=""):
    SONUC.append(bool(kosul))
    print(f"[{'TAMAM' if kosul else 'HATA '}] {ad}" + (f" — {ayrinti}" if ayrinti else ""), flush=True)


def tara():
    durum, _ = iste("/__scheduled?cron=*+*+*+*+*")
    time.sleep(2)  # waitUntil içindeki tarama bitsin
    return durum


def main():
    sunucu = ThreadingHTTPServer(("127.0.0.1", SAHTE_PORT), Sahte)
    sunucu.handle_error = lambda *a: None  # wrangler kapanırken kopan bağlantılar
    threading.Thread(target=sunucu.serve_forever, daemon=True).start()
    kalici = tempfile.mkdtemp(prefix="bulut-test-")
    sahte = f"http://127.0.0.1:{SAHTE_PORT}"
    degiskenler = {"NOTION_KEY": "sahte-notion", "TELEGRAM_JETON": "sahte-tg", "TELEGRAM_SOHBET_ID": "4242",
                   "SERVIS_JETON": SERVIS_JETON, "NOTION_API": sahte + "/notion", "TELEGRAM_API": sahte + "/tg",
                   "NABIZ_ESIK_DK": str(NABIZ_ESIK_SN / 60),
                   # şimdiyi içeren sessiz aralık: nabız uyarıları bildirimsiz gitmeli, hatırlatmalar değil
                   "SESSIZ_SAAT": "-".join((dt.datetime.now(TZ) + dt.timedelta(hours=s)).strftime("%H:%M")
                                           for s in (-2, 2))}
    komut = [shutil.which("node") or "node", WRANGLER, "dev", "--test-scheduled", "--port", str(WORKER_PORT),
             "--ip", "127.0.0.1", "--persist-to", kalici, "--show-interactive-dev-session=false"]
    for k, v in degiskenler.items():
        komut += ["--var", f"{k}:{v}"]
    wr = subprocess.Popen(komut, cwd=BULUT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    cikti = []
    threading.Thread(target=lambda: [cikti.append(x) for x in wr.stdout], daemon=True).start()
    try:
        for _ in range(60):
            try:
                if iste("/", jeton="")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            print("".join(cikti[-40:]))
            raise SystemExit("wrangler dev açılmadı")
        senaryo(wr)
    finally:
        wr.terminate()
        try:
            wr.wait(10)
        except subprocess.TimeoutExpired:
            wr.kill()
        sunucu.shutdown()
        shutil.rmtree(kalici, ignore_errors=True)
        if not all(SONUC):
            print("".join(cikti[-60:]))
    print(f"\n{sum(SONUC)}/{len(SONUC)} kontrol geçti")
    sys.exit(0 if all(SONUC) else 1)


def senaryo(wr):
    a, b, c, d, e, f, g, h = (x * 32 for x in "abcdef01")
    satir(a, "tek seferlik <ilaç>", 5)
    zb = satir(b, "günlük spor", 5, tekrar="Günlük")
    satir(c, "gecikme payı içinde", 1)
    satir(d, "brifing", 5, tur="Brifing")
    satir(e, "çok eski", 20 * 60)
    zf = satir(f, "servisin gönderdiği", 5)
    satir(g, "NOTION_HATA kaydı", 5)
    satir(h, "TG_HATA kaydı", 5)
    i, j = "2" * 32, "3" * 32
    zi = satir(i, "eski haftalık seri", 13 * 60, tekrar="Haftalık")
    zj = satir(j, "servisin bıraktığı", 5)

    kontrol("yetkisiz istek reddedilir", iste("/durum", jeton="yanlis")[0] == 401)
    kontrol("bozuk anahtar reddedilir", iste("/sahiplen", {"anahtar": "../x"})[0] == 400)
    anahtar_f = f"{f}@{zf.strftime('%Y-%m-%dT%H:%M')}"
    k1 = json.loads(iste("/sahiplen", {"anahtar": anahtar_f})[1])
    k2 = json.loads(iste("/sahiplen", {"anahtar": anahtar_f})[1])
    kontrol("servis kendi sahiplendiğini yeniden alabilir", k1["kazandi"] and k2["kazandi"], f"{k1} {k2}")
    anahtar_j = f"{j}@{zj.strftime('%Y-%m-%dT%H:%M')}"
    iste("/sahiplen", {"anahtar": anahtar_j})
    b1 = json.loads(iste("/birak", {"anahtar": anahtar_j})[1])
    kontrol("servis gönderemediğini bırakır", b1["birakildi"], b1)

    kontrol("cron tetiklendi", tara() == 200)
    metinler = [x["text"] for x in GIDEN]
    kontrol("tek seferlik ve tekrarlayan gitti", any("&lt;ilaç&gt;" in m for m in metinler)
            and any("günlük spor" in m and "her gün tekrarlanıyor" in m for m in metinler), metinler)
    kontrol("gecikme payı içindeki, brifing, çok eski ve servisinki gitmedi",
            not any(k in m for m in metinler for k in ("gecikme payı", "brifing", "çok eski", "servisin gönderdiği",
                                                         "eski haftalık")))
    kontrol("servisin bıraktığını bulut gönderdi", any("servisin bıraktığı" in m for m in metinler))
    kontrol("Notion hatalı kayıt da bir kez gitti", sum("NOTION_HATA" in m for m in metinler) == 1)
    kontrol("Telegram hatasında gitmedi", not any("TG_HATA" in m for m in metinler))
    kontrol("mesaj HTML, doğru sohbete", all(x["parse_mode"] == "HTML" and x["chat_id"] == "4242" for x in GIDEN))
    kontrol("bağlantı önizlemesi kapalı (link_preview_options)",
            all(x.get("link_preview_options") == {"is_disabled": True} and "disable_web_page_preview" not in x
                for x in GIDEN))
    kontrol("bulut mesajında emoji yok, saat tarih_metni biçiminde",
            not any(e in m for m in metinler for e in ("⏰", "🔁", "☁️", "✅"))
            and all(re.match(r"<b>Hatırlatma</b> · (bugün|dün) \d\d:\d\d", m) for m in metinler), metinler)
    yama = dict(YAMALAR)
    kontrol("tek seferlik Gönderildi oldu", yama.get(a) == {"properties": {"Durum": {"select": {"name": "Gönderildi"}}}},
            yama.get(a))
    ertesi = (zb + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:00+03:00")
    kontrol("tekrarlayan ertesi güne ilerledi", yama.get(b) == {"properties": {"Zaman": {"date": {"start": ertesi}}}},
            yama.get(b))
    haftaya = (zi + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:00+03:00")
    kontrol("çok eski tekrarlayan gönderilmeden ilerledi", yama.get(i) == {"properties": {"Zaman": {"date": {
        "start": haftaya}}}}, yama.get(i))
    bulut_anahtar = f"{b}@{zb.strftime('%Y-%m-%dT%H:%M')}"
    k3 = json.loads(iste("/sahiplen", {"anahtar": bulut_anahtar})[1])
    kontrol("bulutun gönderdiğini servis alamaz", not k3["kazandi"] and k3["sahip"] == "bulut", k3)

    n = len(GIDEN)
    # gecikme payındaki kayıt ilk taramada sınandı; aradaki senaryolar uzadıkça vadesi gerçekten geçebiliyor, ikinci
    # tarama yalnızca sahiplenme davranışını sınasın
    SATIRLAR[c]["properties"]["Durum"]["select"]["name"] = "İptal"
    tara()
    yeni = [x["text"] for x in GIDEN[n:]]
    kontrol("ikinci taramada yalnızca Telegram hatası alan gitti", len(yeni) == 1 and "TG_HATA" in yeni[0], yeni)
    kontrol("Notion yazılamayan kayıt ikinci kez gitmedi", not any("NOTION_HATA" in m for m in yeni))
    durum = json.loads(iste("/durum")[1])
    kontrol("/durum son taramayı gösteriyor", durum["son_tarama"]["gonderilen"] == 1
            and durum["son_olay"]["gonderilen"] == 1, durum)

    # bulut mesajının düğmeleri (servisteki Sohbet.dugme_geldi "hb:" ile işler)
    ilac = next((x for x in GIDEN if "&lt;ilaç&gt;" in x["text"]), {})
    veriler = [d["callback_data"] for d in ilac.get("reply_markup", {}).get("inline_keyboard", [[]])[0]]
    kontrol("bulut mesajında Tamam / 1 saat / Yarın düğmeleri", veriler == [f"hb:ok:{a}", f"hb:60:{a}", f"hb:y:{a}"]
            and all(len(v.encode()) <= 64 for v in veriler), veriler)
    nabiz_senaryosu()


def masaustu():
    return [x["text"] for x in GIDEN if "Masaüstü" in x["text"]]


def nabiz_senaryosu():
    kontrol("hiç nabız yokken uyarı yok", masaustu() == [], masaustu())
    kontrol("yetkisiz nabız reddedilir", iste("/nabiz", {}, jeton="yanlis")[0] == 401)
    kontrol("nabız alındı", iste("/nabiz", {})[0] == 200)
    tara()
    kontrol("taze nabızda uyarı yok", masaustu() == [], masaustu())
    time.sleep(NABIZ_ESIK_SN + 1)
    tara()
    tara()
    m = masaustu()
    kontrol("nabız susunca tek uyarı", len(m) == 1 and m[0].startswith("⚠️ Masaüstü") and "son nabız" in m[0], m)
    durum = json.loads(iste("/durum")[1])
    kontrol("/durum son nabzı gösteriyor", (durum.get("nabiz") or {}).get("uyarildi") is True, durum.get("nabiz"))
    iste("/nabiz", {})
    tara()
    tara()
    m = masaustu()
    kontrol("nabız dönünce tek 'yeniden açık'", len(m) == 2 and m[1].startswith("Masaüstü yeniden açık"), m)
    kontrol("sessiz saatte nabız mesajları bildirimsiz, hatırlatmalar sesli",
            all(x.get("disable_notification") is True for x in GIDEN if "Masaüstü" in x["text"])
            and not any("disable_notification" in x for x in GIDEN if "Masaüstü" not in x["text"]))
    kontrol("temiz kapanış alındı", iste("/kapaniyor", {})[0] == 200)
    time.sleep(NABIZ_ESIK_SN + 1)
    tara()
    kontrol("temiz kapanıştan sonra uyarı yok", len(masaustu()) == 2, masaustu())
    iste("/nabiz", {})
    tara()
    kontrol("kapanıştan sonra açılınca 'yeniden açık' yok (uyarı gitmemişti)", len(masaustu()) == 2, masaustu())


if __name__ == "__main__":
    main()
