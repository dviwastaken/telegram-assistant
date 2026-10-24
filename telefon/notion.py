"""Notion eşitlemesi: Notion asıl kaynak, servis yerel JSON önbelleği anında okur.

İki arka uç:
- REST (NOTION_JETON varsa): resmî API, kotasız ve hızlı.
- claude.ai Notion bağlantısı (`claude -p` üzerinden): jeton yoksa yedek. Ücretsiz planda
  "Query Data Source" kotalı olduğu için okuma seyrek yapılır. Araç sonuçlarını stream-json
  akışından kendimiz ayrıştırıyoruz; modelin veriyi yeniden yazmasına güvenmiyoruz.
Her iki arka uç da satırları aynı biçime çevirir: {"id": <32 hex>, "Ad": ..., "date:Zaman:start": ...}.
"""
import datetime as dt
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import eylemler, limit, tekrar
from .ayarlar import KOK, ayar, log

# Notion veritabanı (data source) kimlikleri .env'den gelir (bkz. .env.example)
DS = {"takvim": ayar("NOTION_TAKVIM_DS", ""),
      "hatirlatma": ayar("NOTION_HATIRLATMA_DS", ""),
      "not": ayar("NOTION_NOT_DS", "")}
BASLIK = {"takvim": "Ad", "hatirlatma": "Konu", "not": "Metin"}
SECIM = {"Tür", "Tekrar", "Durum", "Ekleyen", "Ders", "Kaynak"}
# ders kodu -> Notion "Ders" seçeneği
DERSLER = {k: v["notion"] for k, v in eylemler.DERSLER.items()}  # tek kaynak: <takvim klasörü>/dersler.json
DERS_KODU = {v: k for k, v in DERSLER.items()}
SIMGE = {"Teslim": "📝", "Sınav": "🧪", "Ders": "📚", "Etkinlik": "🎟️", "Randevu": "🏛️", "Diğer": "📌"}
# "arandi" yerel adı eskiden kaldı: hatırlatma artık aranmıyor, Telegram'dan yazılıyor (Notion'da "Gönderildi")
DURUM_TR = {"bekliyor": "Bekliyor", "arandi": "Gönderildi", "cevapsiz": "Cevapsız", "iptal": "İptal"}
DURUM_YEREL = {**{v: k for k, v in DURUM_TR.items()}, "Arandı": "arandi"}
TUR_TR = {"hatirlatma": "Hatırlatma", "brifing": "Brifing", "acil": "Acil"}
TUR_YEREL = {v: k for k, v in TUR_TR.items()}
JETON = ayar("NOTION_JETON") or ayar("NOTION_KEY") or ayar("NOTION_TOKEN")


def sayfa_id(deger):
    """URL ya da tireli UUID'den 32 hanelik sayfa kimliği."""
    ids = re.findall(r"[0-9a-f]{32}", (deger or "").replace("-", "").lower())
    return ids[-1] if ids else None


# ================================================================== REST arka ucu
def _rest(yontem, yol, govde=None):
    istek = urllib.request.Request(
        "https://api.notion.com/v1" + yol, method=yontem,
        data=json.dumps(govde).encode() if govde is not None else None,
        headers={"Authorization": f"Bearer {JETON}", "Notion-Version": "2025-09-03",
                 "Content-Type": "application/json"})
    for deneme in range(3):
        try:
            return json.load(urllib.request.urlopen(istek, timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 429 and deneme < 2:
                time.sleep(float(e.headers.get("Retry-After", 1)))
                continue
            raise RuntimeError(f"Notion {e.code}: {e.read().decode()[:300]}")


def _duzle(sayfa):
    satir = {"id": sayfa_id(sayfa["id"])}
    for ad, p in sayfa["properties"].items():
        t = p["type"]
        if t in ("title", "rich_text"):
            satir[ad] = "".join(x.get("plain_text", "") for x in p[t])
        elif t == "select":
            satir[ad] = (p["select"] or {}).get("name")
        elif t == "date":
            satir[f"date:{ad}:start"] = (p["date"] or {}).get("start")
        elif t == "checkbox":
            satir[ad] = "__YES__" if p["checkbox"] else "__NO__"
    return satir


def _rest_satirlar(tur):
    satirlar, imlec = [], None
    while True:
        govde = {"page_size": 100, **({"start_cursor": imlec} if imlec else {})}
        d = _rest("POST", f"/data_sources/{DS[tur]}/query", govde)
        satirlar += [_duzle(s) for s in d["results"]]
        if not d.get("has_more"):
            return satirlar
        imlec = d["next_cursor"]


def _rest_ozellik(ad, deger, tur):
    if ad == BASLIK[tur]:
        return {"title": [{"text": {"content": str(deger)[:2000]}}]}
    if ad in SECIM:
        return {"select": {"name": deger} if deger else None}
    if ad == "Not":
        return {"rich_text": [{"text": {"content": str(deger)[:2000]}}] if deger else []}
    if ad == "Okundu":
        return {"checkbox": deger == "__YES__"}
    if ad == "Bağlantı":
        return {"url": deger or None}
    if ad == "Zaman":
        return {"date": {"start": deger}}
    raise ValueError(ad)


def _rest_olustur(tur, ozellik):
    props = {}
    for ad, deger in ozellik.items():
        if ad == "date:Zaman:start":
            props["Zaman"] = {"date": {"start": deger}}
        elif not ad.startswith("date:"):
            props[ad] = _rest_ozellik(ad, deger, tur)
    govde = {"parent": {"type": "data_source_id", "data_source_id": DS[tur]}, "properties": props}
    if tur == "takvim":
        govde["icon"] = {"type": "emoji", "emoji": SIMGE.get(ozellik.get("Tür"), "📌")}
    return sayfa_id(_rest("POST", "/pages", govde)["id"])


def _rest_durum(pid, durum):
    _rest("PATCH", f"/pages/{pid}", {"properties": {"Durum": {"select": {"name": durum}}}})


def _rest_guncelle(pid, alanlar):
    """alanlar: {"Zaman": yerel ISO, "Durum": "Ek süre", ...}"""
    props = {ad: _rest_ozellik(ad, _ofsetli(d) if ad == "Zaman" else d, "takvim") for ad, d in alanlar.items()}
    _rest("PATCH", f"/pages/{pid}", {"properties": props})


# ================================================================== claude.ai bağlantısı arka ucu
ARAC = "mcp__claude_ai_Notion__notion-"
CLAUDE = shutil.which("claude")


def _claude_araclar(istem, araclar, zaman_asimi=180, deneme=3, onek=ARAC):
    """Bağlantı araçları bazen geç yükleniyor: hiç araç çağrısı olmadıysa tekrar dene."""
    for i in range(deneme):
        sonuc = _claude_araclar_bir(istem, araclar, zaman_asimi, onek)
        if sonuc:
            return sonuc
        time.sleep(15 * (i + 1))
    return []


def _claude_araclar_bir(istem, araclar, zaman_asimi, onek=ARAC):
    """claude -p'ye araçları çağırtır; [(arac_adi, girdi, sonuc_metni, hata_mi)] döner."""
    args = [CLAUDE, "-p", "--model", "haiku", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--allowedTools", *[onek + a for a in araclar],
            "--permission-mode", "dontAsk", "--no-session-persistence"]
    p = subprocess.run(args, input=istem, capture_output=True, text=True, encoding="utf-8", cwd=KOK,
                       timeout=zaman_asimi, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    cagrilar, sonuclar = {}, []
    for satir in p.stdout.splitlines():
        try:
            ev = json.loads(satir)
        except json.JSONDecodeError:
            continue
        icerik = (ev.get("message") or {}).get("content")
        if not isinstance(icerik, list):
            continue
        for b in icerik:
            if b.get("type") == "tool_use":
                cagrilar[b["id"]] = (b["name"].removeprefix(onek), b.get("input", {}))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in cagrilar:
                metin = b.get("content")
                if isinstance(metin, list):
                    metin = "".join(x.get("text", "") for x in metin if isinstance(x, dict))
                ad, girdi = cagrilar[b["tool_use_id"]]
                sonuclar.append((ad, girdi, str(metin), bool(b.get("is_error"))))
    limit.kontrol(p.stdout[-2000:] + p.stderr[-500:], "Gmail/Notion bağlantısı")
    if not sonuclar:
        log("notion: araç sonucu gelmedi", p.stdout[-200:], p.stderr[-200:])
    return sonuclar


def _mcp_hepsi():
    istem = ("notion-query-data-sources aracını rows modunda, limit 100 ile, şu üç data source için ayrı ayrı "
             f"çağır: {', '.join('collection://' + d for d in DS.values())}. Başka hiçbir araç kullanma. "
             "Üçü bitince sadece 'bitti' yaz.")
    sonuc = {}
    for ad, girdi, metin, hata in _claude_araclar(istem, ["query-data-sources"]):
        ds = ((girdi.get("data") or {}).get("data_source_url") or "").removeprefix("collection://")
        tur = next((t for t, d in DS.items() if d == ds), None)
        if hata:
            log("notion (bağlantı) sorgu hatası:", metin[:150])
        if hata or not tur:
            continue
        try:
            sonuc[tur] = [{**r, "id": sayfa_id(r.get("url"))} for r in json.loads(metin)["results"]]
        except (json.JSONDecodeError, KeyError):
            log("notion: beklenmeyen sorgu çıktısı", metin[:200])
    return sonuc


def _mcp_yaz(olusturulacak, durumlar):
    """olusturulacak: [(tur, ozellik)], durumlar: [(pid, durum)] -> ([pid|None], {güncellenen pid})"""
    talimat = []
    for tur, ozellik in olusturulacak:
        o = dict(ozellik)
        if "date:Zaman:start" in o:
            o["date:Zaman:is_datetime"] = 1
        talimat.append(f"- notion-create-pages: parent={{\"data_source_id\": \"{DS[tur]}\"}}, allow_async=false, "
                       f"pages=[{{\"properties\": {json.dumps(o, ensure_ascii=False)}}}]")
    for pid, durum in durumlar:
        talimat.append(f"- notion-update-page: page_id=\"{pid}\", command=\"update_properties\", "
                       f"properties={{\"Durum\": \"{durum}\"}}")
    istem = ("Aşağıdaki Notion araç çağrılarını sırayla, verilen argümanlarla birebir yap. Hiçbir değeri "
             "değiştirme, başka araç kullanma. Bitince sadece 'bitti' yaz.\n" + "\n".join(talimat))
    sonuclar = _claude_araclar(istem, ["create-pages", "update-page"])
    olusan = []
    for ad, _, metin, hata in sonuclar:
        if ad == "create-pages":
            try:
                olusan.append(None if hata else sayfa_id(json.loads(metin)["pages"][0]["id"]))
            except (json.JSONDecodeError, KeyError, IndexError):
                olusan.append(None)
    guncellenen = {sayfa_id(g.get("page_id")) for ad, g, _, hata in sonuclar if ad == "update-page" and not hata}
    return olusan, guncellenen


# ================================================================== ortak mantık
def _yerel(z):
    """Notion tarihini (UTC 'Z' ya da ofsetli) İstanbul saatine çevir, ISO dakika (makinenin saat diliminden bağımsız)."""
    if not z:
        return None
    t = dt.datetime.fromisoformat(z.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(eylemler.TZ).replace(tzinfo=None)
    return t.isoformat(timespec="minutes")


def _ofsetli(yerel_iso):
    t = dt.datetime.fromisoformat(yerel_iso)
    t = t.replace(tzinfo=eylemler.TZ) if t.tzinfo is None else t.astimezone(eylemler.TZ)
    return t.isoformat(timespec="seconds")


def satirlari_getir():
    if JETON:
        return {tur: _rest_satirlar(tur) for tur in DS}
    return _mcp_hepsi()


def cek():
    try:
        satirlar = satirlari_getir()
    except Exception as e:
        log("notion: çekme hatası:", repr(e))
        return False
    if len(satirlar) < 3:
        log(f"notion: çekme eksik kaldı ({len(satirlar)}/3), önbellek korunuyor")
        return False
    with eylemler._kilit:
        eski_t = eylemler.oku(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
        eski_a = eylemler.oku(eylemler.ARAMALAR, [])
        eski_n = eylemler.oku(eylemler.NOTLAR, [])

        bekleyen = {sayfa_id(o["notion"]): o["guncelle"] for o in eski_t.get("olaylar", [])
                    if o.get("notion") and o.get("guncelle")}
        olaylar, tekrarlayan = [], []
        for r in satirlar["takvim"]:
            z = _yerel(r.get("date:Zaman:start"))
            if not z or not r["id"]:
                continue
            kayit = {"id": r["id"], "notion": r["id"], "baslik": r.get("Ad") or "", "zaman": z,
                     "tur": r.get("Tür") or "", "not": r.get("Not") or "",
                     "ders": DERS_KODU.get(r.get("Ders")), "durum": r.get("Durum"), "kaynak": r.get("Kaynak")}
            if r["id"] in bekleyen:  # yerelde değişti, henüz Notion'a gitmedi: yerel hâli koru
                g = bekleyen[r["id"]]
                kayit.update({"guncelle": g, "zaman": g.get("Zaman", z), "durum": g.get("Durum", kayit["durum"])})
            if r.get("Tekrar") == "Haftalık":
                t = dt.datetime.fromisoformat(z)
                tekrarlayan.append({**kayit, "gun": t.weekday(), "saat": t.strftime("%H:%M")})
            else:
                olaylar.append(kayit)
        olaylar += [o for o in eski_t.get("olaylar", []) if not o.get("notion")]

        bekleyen_durum = {sayfa_id(a["notion"]): a["durum"] for a in eski_a
                          if a.get("notion") and a.get("durum_gonderilmedi")}
        bekleyen_zaman = {sayfa_id(a["notion"]): a["guncelle"] for a in eski_a
                          if a.get("notion") and a.get("guncelle")}
        aramalar = []
        for r in satirlar["hatirlatma"]:
            z = _yerel(r.get("date:Zaman:start"))
            if not z or not r["id"]:
                continue
            a = {"id": r["id"], "notion": r["id"], "zaman": z, "konu": r.get("Konu") or "",
                 "tur": TUR_YEREL.get(r.get("Tür"), "hatirlatma"),
                 "durum": DURUM_YEREL.get(r.get("Durum"), "bekliyor"), "not": r.get("Not") or ""}
            try:
                if tekrar.kod(r.get("Tekrar")):
                    a["tekrar"] = tekrar.kod(r.get("Tekrar"))
            except ValueError:
                log(f"notion: tanınmayan tekrar seçeneği ({r.get('Tekrar')}), tek seferlik sayıldı")
            if r["id"] in bekleyen_durum:  # durum yerelde değişti, henüz Notion'a gitmedi
                a["durum"], a["durum_gonderilmedi"] = bekleyen_durum[r["id"]], True
            if r["id"] in bekleyen_zaman:  # tekrarlayan kayıt yerelde ilerledi, henüz Notion'a gitmedi
                g = bekleyen_zaman[r["id"]]
                a.update({"guncelle": g, "zaman": g.get("Zaman", z)})
            aramalar.append(a)
        aramalar += [a for a in eski_a if not a.get("notion")]

        notlar = [{"id": r["id"], "notion": r["id"], "metin": r.get("Metin") or "",
                   "okundu": r.get("Okundu") == "__YES__"} for r in satirlar["not"] if r["id"]]
        notlar += [n for n in eski_n if not n.get("notion")]

        eylemler.yaz(eylemler.TAKVIM, {"tekrarlayan": tekrarlayan, "olaylar": olaylar})
        eylemler.yaz(eylemler.ARAMALAR, aramalar)
        eylemler.yaz(eylemler.NOTLAR, notlar)
    # her çekişi değil, yalnızca değişeni günlüğe yaz (günde 700'ü aşan aynı satır); sayı değiştirmeyen
    # değişiklikler (iptal, saat) de görünsün diye ham satırlar karşılaştırılır
    imza = {tur: {r["id"]: repr(sorted(r.items())) for r in satirlar[tur] if r.get("id")} for tur in TUR_ADI}
    onceki, _son_imza[0] = _son_imza[0], imza
    if onceki is None:
        log(f"notion: çekildi ({'REST' if JETON else 'bağlantı'}) — {len(olaylar)} olay, {len(tekrarlayan)} "
            f"haftalık, {len(aramalar)} hatırlatma, {len(notlar)} not")
    elif imza != onceki:
        log(f"notion: değişti — {degisiklik_ozeti(onceki, imza)}")
    ilk_cekis.set()
    return True


TUR_ADI = {"takvim": "takvim", "hatirlatma": "hatırlatma", "not": "not"}
_son_imza = [None]


def degisiklik_ozeti(onceki, simdi):
    """İki çekiş arasındaki fark türe göre: "hatırlatma: 1 eklendi, 2 güncellendi; not: 1 silindi"."""
    parcalar = []
    for tur, ad in TUR_ADI.items():
        o, s = onceki.get(tur, {}), simdi.get(tur, {})
        sayilar = [(len(s.keys() - o.keys()), "eklendi"), (len(o.keys() - s.keys()), "silindi"),
                   (sum(1 for k in s.keys() & o.keys() if s[k] != o[k]), "güncellendi")]
        metin = ", ".join(f"{n} {fiil}" for n, fiil in sayilar if n)
        if metin:
            parcalar.append(f"{ad}: {metin}")
    return "; ".join(parcalar) or "fark yok"
ilk_cekis = threading.Event()  # açılıştan beri bir kez başarıyla çekildi (zamanlayıcı bayat önbellekle başlamasın)


def kayit_oku(pid):
    """Tek bir hatırlatmanın Notion'daki güncel hâli: {"durum": yerel ad, "zaman": yerel ISO dakika}. Olmazsa None."""
    if not JETON:
        return None
    try:
        r = _duzle(_rest("GET", f"/pages/{sayfa_id(pid)}"))
    except Exception as e:
        log("notion: kayıt okunamadı:", repr(e)[:150])
        return None
    return {"durum": DURUM_YEREL.get(r.get("Durum"), "bekliyor"), "zaman": _yerel(r.get("date:Zaman:start"))}


def _ozellik(kayit, tur):
    if tur == "takvim":
        t = kayit.get("tur") or "Diğer"
        return {"Ad": kayit["baslik"], "date:Zaman:start": _ofsetli(kayit["zaman"]), "Tür": t, "Tekrar": "Yok",
                "Not": kayit.get("not", ""), "Ekleyen": "Claude",
                "Ders": DERSLER.get(kayit.get("ders") or "", "Diğer" if t in ("Teslim", "Sınav") else None),
                "Durum": kayit.get("durum") or ("Yapılacak" if t in ("Teslim", "Sınav") else None),
                "Kaynak": kayit.get("kaynak") or "Claude"}
    if tur == "hatirlatma":
        return {"Konu": kayit["konu"], "date:Zaman:start": _ofsetli(kayit["zaman"]),
                "Tür": TUR_TR.get(kayit.get("tur"), "Hatırlatma"),
                "Durum": DURUM_TR.get(kayit.get("durum"), "Bekliyor"), "Ekleyen": "Claude",
                "Tekrar": tekrar.TR.get(kayit.get("tekrar"), "Yok")}
    return {"Metin": kayit["metin"], "Okundu": "__NO__", "Ekleyen": "Claude"}


def gonder():
    with eylemler._kilit:
        t = eylemler.oku(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
        a = eylemler.oku(eylemler.ARAMALAR, [])
        n = eylemler.oku(eylemler.NOTLAR, [])
    yeni = ([("takvim", x) for x in t.get("olaylar", []) if not x.get("notion")]
            + [("hatirlatma", x) for x in a if not x.get("notion")]
            + [("not", x) for x in n if not x.get("notion")])
    durumlar = [(sayfa_id(x["notion"]), DURUM_TR[x["durum"]]) for x in a
                if x.get("notion") and x.get("durum_gonderilmedi")]
    gonderilen_durum = {pid: d for pid, d in durumlar}
    zamanlar = [(sayfa_id(x["notion"]), x["guncelle"]) for x in t.get("olaylar", []) + a
                if x.get("notion") and x.get("guncelle")]
    if not yeni and not durumlar and not (zamanlar and JETON):
        return
    zaman_tamam = set()
    if JETON:
        for pid, alanlar in zamanlar:  # zaman/durum güncellemeleri yalnızca REST'te (bağlantı yolunda bekler)
            try:
                _rest_guncelle(pid, alanlar)
                zaman_tamam.add(pid)
            except Exception as e:
                log("notion: takvim güncelleme hatası:", repr(e))
        olusan, guncellenen = [], set()
        for tur, x in yeni:
            try:
                olusan.append(_rest_olustur(tur, _ozellik(x, tur)))
            except Exception as e:
                log("notion: oluşturma hatası:", repr(e))
                olusan.append(None)
        for pid, durum in durumlar:
            try:
                _rest_durum(pid, durum)
                guncellenen.add(pid)
            except Exception as e:
                log("notion: durum hatası:", repr(e))
    else:
        olusan, guncellenen = _mcp_yaz([(tur, _ozellik(x, tur)) for tur, x in yeni], durumlar)
    idler = {x["id"]: pid for (_, x), pid in zip(yeni, olusan) if pid}
    anlik = {x["id"]: x for _, x in yeni}                       # oluşturulurken gönderilen hâl
    gonderilen_zaman = {pid: alanlar for pid, alanlar in zamanlar}
    with eylemler._kilit:  # sonuçları yerele işle
        t = eylemler.oku(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
        a = eylemler.oku(eylemler.ARAMALAR, [])
        n = eylemler.oku(eylemler.NOTLAR, [])
        for liste in (t.get("olaylar", []), a, n):
            for x in liste:
                if x["id"] in idler:
                    x["notion"] = idler[x["id"]]
                    eski = anlik[x["id"]]
                    if liste is a and x.get("durum") != eski.get("durum"):  # ör. oluşturulurken gönderildi
                        x["durum_gonderilmedi"] = True
                    if liste is a and x.get("zaman") != eski.get("zaman"):  # ör. tekrarlayan ilerledi
                        x["guncelle"] = {**x.get("guncelle", {}), "Zaman": x["zaman"]}
                pid = sayfa_id(x["notion"]) if x.get("notion") else None
                if pid in guncellenen and DURUM_TR.get(x.get("durum")) == gonderilen_durum.get(pid):
                    x.pop("durum_gonderilmedi", None)
                if pid in zaman_tamam and x.get("guncelle") == gonderilen_zaman.get(pid):
                    x.pop("guncelle", None)
        eylemler.yaz(eylemler.TAKVIM, t)
        eylemler.yaz(eylemler.ARAMALAR, a)
        eylemler.yaz(eylemler.NOTLAR, n)
    log(f"notion: {len(idler)}/{len(yeni)} kayıt gönderildi, {len(guncellenen)}/{len(durumlar)} durum, "
        f"{len(zaman_tamam)}/{len(zamanlar)} zaman güncellendi")


YAKIN_DK = 10  # zamanı bu kadar yakın bir hatırlatma varken Notion dakikada bir çekilir (iptal/değişiklik kaçmasın)


def yakin_hatirlatma_var(simdi=None, aramalar=None):
    simdi = simdi or dt.datetime.now()
    try:
        aramalar = aramalar if aramalar is not None else eylemler.oku(eylemler.ARAMALAR, [])
    except (json.JSONDecodeError, OSError):
        return False
    for a in aramalar:
        try:
            z = dt.datetime.fromisoformat(a["zaman"])
        except (KeyError, ValueError):
            continue
        if a.get("durum") == "bekliyor" and simdi <= z <= simdi + dt.timedelta(minutes=YAKIN_DK):
            return True
    return False


class Esitleyici:
    """Değişiklik olunca hemen gönderir. Okuma REST'te 2 dk'da bir (yakın hatırlatma varken 1 dk), bağlantıda
    (kotalı) 2 saatte bir. 2 dk: kullanıcının Notion'a eklediği yakın bir hatırlatma da en geç 2 dk'da görülsün."""

    def __init__(self):
        self.okuma_araligi = 120 if JETON else 7200
        self.son_okuma = 0.0      # son deneme (başarısızsa bir sonraki denemenin zamanına göre ayarlanır)
        self.son_basari = 0.0     # son başarılı çekiş (/durum bunu gösterir)
        self.tetik = threading.Event()
        threading.Thread(target=self._dongu, daemon=True, name="notion").start()

    def tetikle(self):
        self.tetik.set()

    def aralik(self):
        return 60 if JETON and yakin_hatirlatma_var() else self.okuma_araligi

    def _dongu(self):
        while True:
            try:
                gonder()
                if time.time() - self.son_okuma >= self.aralik():
                    if cek():
                        self.son_okuma = self.son_basari = time.time()
                    else:  # başarısız: REST'te 3 dk sonra (yakın hatırlatma iptali kaçmasın), bağlantıda (kota) 30 dk
                        self.son_okuma = time.time() - self.aralik() + (180 if JETON else 1800)
            except Exception as e:
                log("notion eşitleme hatası:", repr(e))
            self.tetik.wait(timeout=60)
            self.tetik.clear()
            time.sleep(3)  # art arda gelen eylemleri birleştir
