"""Telegram: kullanıcıyla yazışma kanalı.

İki bot kullanılabilir:
- Sohbet botu (`TELEGRAM_SOHBET_JETONU`): çift yönlü. Servis `getUpdates` ile kullanıcının mesajlarını ve düğme
  tıklamalarını okur, cevapları ve bildirimleri buradan yazar.
- Yedek bot (`TELEGRAM_YEDEK_JETONU` + `TELEGRAM_YEDEK_SOHBET_ID`, isteğe bağlı): yalnızca tek yönlü gönderim.
  Bir botun mesajlarını aynı anda tek okuyucu alabildiği için servis onda `getUpdates` çağırmaz (başka bir
  uygulama okuyor olabilir). Sohbet botunun jetonu yoksa bildirimler buradan gider.

Gönderilemeyen mesaj kaybolmaz: `kayitlar/telegram_kuyruk.json`'a yazılır ve dakikada bir yeniden denenir
(servis yeniden başlasa da). Her başarılı gönderim günlüğe mesaj numarasıyla yazılır.
"""
import datetime as dt
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .ayarlar import KAYITLAR, ayar, log

KUYRUK = os.path.join(KAYITLAR, "telegram_kuyruk.json")
OFSET = os.path.join(KAYITLAR, "telegram_ofset.txt")
SINIR = 3900                      # ham parça sınırı; HTML'e çevrilmiş hâli ayrıca TELEGRAM_SINIR'a göre ölçülür
TELEGRAM_SINIR = 4096             # Telegram'ın bir mesajda kabul ettiği en uzun metin
KUCUK_PARCA = 1500                # "message is too long" dönerse düz metin bu boyda parçalarla yeniden gider
ONIZLEME_YOK = {"is_disabled": True}  # link_preview_options (disable_web_page_preview'ın yerini aldı)
KUYRUK_EN_UZUN_SAAT = 72          # bundan eski bekleyen mesaj atılır (günlüğe yazılarak)

SOHBET_JETONU = ayar("TELEGRAM_SOHBET_JETONU", "")
gecmis_dinleyicisi = None         # Sohbet buraya bağlanır: giden bildirimler sohbet geçmişine de işlensin
_kuyruk_kilit = threading.Lock()


class TelegramHatasi(Exception):
    def __init__(self, kod, aciklama, bekle=0):
        super().__init__(f"{kod}: {aciklama}")
        self.kod, self.aciklama, self.bekle = kod, aciklama, bekle


def _yedek_bot():
    """(jeton, sohbet_id) — yedek bot .env'de tanımlı değilse TelegramHatasi."""
    jeton, sohbet = ayar("TELEGRAM_YEDEK_JETONU", ""), ayar("TELEGRAM_YEDEK_SOHBET_ID", "")
    if not (jeton and sohbet):
        raise TelegramHatasi(401, "yedek bot ayarlı değil (TELEGRAM_YEDEK_JETONU / TELEGRAM_YEDEK_SOHBET_ID)")
    return jeton, str(sohbet)


def sohbet_id():
    """Kullanıcının Telegram kimliği: .env'de yoksa yedek botun sohbet kimliğiyle aynı (aynı kişi)."""
    deger = ayar("TELEGRAM_SOHBET_ID", "")
    if deger:
        return str(deger)
    try:
        return _yedek_bot()[1]
    except TelegramHatasi:
        return ""


def cift_yonlu():
    return bool(SOHBET_JETONU)


def api(yontem, veri=None, jeton=None, sure=20, dosya=None):
    """Bot API çağrısı. Dönüş: `result`. Hata: TelegramHatasi (kod 0 = ağ hatası)."""
    jeton = jeton or SOHBET_JETONU or _yedek_bot()[0]
    url = f"https://api.telegram.org/bot{jeton}/{yontem}"
    if dosya:  # (alan_adı, dosya_adı, bayt) -> multipart
        sinir = uuid.uuid4().hex
        govde = b""
        for k, v in (veri or {}).items():
            govde += (f"--{sinir}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        alan, ad, bayt = dosya
        govde += (f"--{sinir}\r\nContent-Disposition: form-data; name=\"{alan}\"; filename=\"{ad}\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n").encode() + bayt + f"\r\n--{sinir}--\r\n".encode()
        istek = urllib.request.Request(url, data=govde, headers={"Content-Type": f"multipart/form-data; boundary={sinir}"})
    else:
        istek = urllib.request.Request(url, data=json.dumps(veri or {}).encode(),
                                       headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(istek, timeout=sure) as r:
            cevap = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            cevap = json.loads(e.read().decode("utf-8", "replace"))
        except (ValueError, OSError):
            raise TelegramHatasi(e.code, str(e.reason))
        raise TelegramHatasi(e.code, cevap.get("description", ""),
                             (cevap.get("parameters") or {}).get("retry_after", 0))
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise TelegramHatasi(0, repr(e)[:150])
    if not cevap.get("ok"):
        raise TelegramHatasi(cevap.get("error_code", -1), cevap.get("description", ""))
    return cevap.get("result")


# ------------------------------------------------------------------ biçim
def html_bicim(metin):
    """Beynin yazdığı sade markdown'ı Telegram HTML'ine çevir (kaçış güvenli). Bilinmeyen biçim düz metin kalır."""
    parcalar = re.split(r"(```.*?```)", metin, flags=re.S)
    cikti = []
    for p in parcalar:
        if p.startswith("```") and p.endswith("```") and len(p) >= 6:
            ic = re.sub(r"^```[\w+-]*\n?", "", p)[:-3]
            cikti.append(f"<pre>{html.escape(ic.strip(chr(10)))}</pre>")
            continue
        p = html.escape(p, quote=False)
        p = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", p)
        p = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", p)
        p = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", p)
        p = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', p)
        cikti.append(p)
    return "".join(cikti)


def duz_metin(metin):
    """Güvenilmeyen metin (mail, Learn, Notion başlığı) html_bicim'den geçince biçime ya da gizli bağlantıya
    dönüşmesin: '[yazı](adres)' -> 'yazı (adres)', '*', '`', '[', ']' ve satır başı '#' atılır. Adres görünür kalır."""
    metin = re.sub(r"\[([^\]\n]*)\]\(([^)\s]+)\)", r"\1 (\2)", str(metin))
    metin = re.sub(r"[*`\[\]]", "", metin)
    return re.sub(r"(?m)^(\s*)#+\s*", r"\1", metin)


def _kesim(metin, sinir):
    """Metnin ilk sinir karakteri içinde en iyi kesim yeri: paragraf, satır, boşluk; olmazsa tam sınır (bir '**'
    ya da '```' işaretinin ortası değil)."""
    kes = max(metin.rfind("\n\n", 0, sinir), metin.rfind("\n", 0, sinir))
    if kes < sinir // 3:
        kes = metin.rfind(" ", 0, sinir)
    if kes < sinir // 3:
        kes = sinir
        while kes > sinir - 3 and metin[kes - 1] in "*`":  # en uzun işaret 3 karakter
            kes -= 1
    return kes


def _acik_kod(metin):
    """Metin açık kalmış bir ``` bloğuyla bitiyorsa bloğun dili ('' dilsiz), bitmiyorsa None (html_bicim gibi
    işaretleri sırayla eşler)."""
    dil = None
    for m in re.finditer(r"```([\w+-]*)", metin):
        dil = m.group(1) if dil is None else None
    return dil


def bol(metin, sinir=SINIR, bicimli=True):
    """Uzun metni paragraf/satır sınırlarından parçala. Biçimliyse her parçanın HTML hâli de Telegram sınırına
    (TELEGRAM_SINIR) sığar ('&' -> '&amp;' gibi kaçışlar metni uzatır); sığmayan parça daha kısa kesilir. Parça
    sınırına düşen ``` bloğu o parçada kapatılıp sonrakinde aynı dille yeniden açılır; aynı satırda yarım kalan
    '**' çifti de öyle (html_bicim kalını satır içinde eşler)."""
    metin = metin.strip()
    parcalar, on_ek = [], ""
    while metin:
        aday, s = on_ek + metin, sinir
        while True:
            kes = len(aday) if len(aday) <= s else _kesim(aday, s)
            parca, kalan, yeni_on_ek = aday[:kes].rstrip(), aday[kes:], ""
            if bicimli and kalan.strip():
                dil = _acik_kod(parca)
                if dil is not None:
                    parca, yeni_on_ek = parca + "\n```", f"```{dil[:20]}\n"
                    kalan = kalan.lstrip("\n")  # kod satırının girintisi kalsın
                elif (not kalan.startswith("\n")  # satır ortasından kesildi: o satırdaki kalın yarım kalmasın
                      and re.sub(r"```.*?```", "", parca, flags=re.S).rsplit("\n", 1)[-1].count("**") % 2):
                    parca, yeni_on_ek = parca + "**", "**"
            if (len(parca) <= sinir and (not bicimli or len(html_bicim(parca)) <= TELEGRAM_SINIR)) or s <= 100:
                break
            s = max(100, int(s * 0.8))
        if parca.strip(" \n*`"):
            parcalar.append(parca)
        metin = kalan if yeni_on_ek.startswith("```") else kalan.strip()
        on_ek = yeni_on_ek if metin.strip() else ""
    return parcalar


DUGMELER = os.path.join(KAYITLAR, "telegram_dugmeler.json")
_dugme_kilit = threading.Lock()


def _dugmeleri_oku():
    """Düğme deposu (çağıran _dugme_kilit'i tutar). Dosya yoksa ya da bozuksa boş."""
    try:
        with open(DUGMELER, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _dugmeleri_yaz(kayit):
    """Atomik yazım (çağıran _dugme_kilit'i tutar)."""
    gecici = DUGMELER + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(kayit, f, ensure_ascii=False)
    os.replace(gecici, DUGMELER)


def dugme_verisi(veri):
    """Düğmenin taşıyacağı veriyi sakla, 8 haneli anahtar döndür (callback_data 64 baytla sınırlı)."""
    anahtar = uuid.uuid4().hex[:8]
    with _dugme_kilit:
        sinir = time.time() - 30 * 86400
        kayit = {k: v for k, v in _dugmeleri_oku().items() if v.get("_zaman", 0) > sinir}
        kayit[anahtar] = {**veri, "_zaman": time.time()}
        _dugmeleri_yaz(kayit)
    return anahtar


def dugme_coz(anahtar):
    with _dugme_kilit:
        return _dugmeleri_oku().get(anahtar)


def dugme_tuket(anahtar, tur):
    """Tek kullanımlık düğme: türü tutan kaydı döndür ve sil (ikinci tıklama None alır). Tür tutmazsa dokunmaz."""
    with _dugme_kilit:
        kayit = _dugmeleri_oku()
        v = kayit.get(anahtar)
        if not v or v.get("tur") != tur:
            return None
        del kayit[anahtar]
        _dugmeleri_yaz(kayit)
        return v


def klavye(dugmeler):
    """[[(yazı, veri), ...], ...] -> inline_keyboard. veri en fazla 64 bayt."""
    if not dugmeler:
        return None
    return {"inline_keyboard": [[{"text": y, "callback_data": v[:64]} for y, v in satir] for satir in dugmeler]}


# ------------------------------------------------------------------ gönderim
def _gonder_bir(metin, dugmeler, sessiz, yanit, bicimli, yedek=False):
    cift = bool(SOHBET_JETONU) and not yedek
    hedef_jeton = SOHBET_JETONU if cift else _yedek_bot()[0]
    veri = {"chat_id": sohbet_id(), "text": html_bicim(metin) if bicimli else metin,
            "link_preview_options": ONIZLEME_YOK, "disable_notification": bool(sessiz)}
    if bicimli:
        veri["parse_mode"] = "HTML"
    if dugmeler and cift:  # düğmeler yalnızca sohbet botunda (tıklamayı yalnız onu okuyabiliyoruz)
        veri["reply_markup"] = klavye(dugmeler)
    if yanit:
        veri["reply_parameters"] = {"message_id": yanit, "allow_sending_without_reply": True}
    try:
        return api("sendMessage", veri, jeton=hedef_jeton)
    except TelegramHatasi as e:
        if bicimli and e.kod == 400 and "parse" in e.aciklama.lower():  # biçim bozuksa düz metin
            log("telegram: HTML reddedildi, düz metin gönderiliyor:", e.aciklama[:120])
            return _gonder_bir(metin, dugmeler, sessiz, yanit, False, yedek)
        raise


def gonder(metin, dugmeler=None, sessiz=False, yanit=None, gecmise=True, bicimli=True, kuyruga=True):
    """Kullanıcıya yaz. Dönüş: son parçanın mesaj numarası; gönderilemediyse None (mesaj kuyruğa alınır).
    dugmeler: [[(yazı, veri)]] (yalnızca son parçaya eklenir). gecmise: sohbet geçmişine bildirim olarak işle."""
    metin = str(metin or "").strip()
    if not metin:
        return None
    if gecmise and gecmis_dinleyicisi:
        try:
            gecmis_dinleyicisi(metin)
        except Exception as e:
            log("telegram: geçmişe işlenemedi:", repr(e)[:150])
    parcalar = bol(metin, bicimli=bicimli)
    son = None
    for i, p in enumerate(parcalar):
        ilerleme = {}  # 'too long' bölmesinde bazı alt parçalar gittiyse: {"kalan": gitmeyen düz alt parçalar}
        son = _dene(p, dugmeler if i == len(parcalar) - 1 else None, sessiz, yanit if i == 0 else None, bicimli,
                    ilerleme=ilerleme)
        if son is None and SOHBET_JETONU and not ilerleme:  # sohbet botu olmadıysa (ör. jeton geçersiz) ESP botuyla dene
            # ESP de alt parçaların bir kısmını gönderip düşebilir: gitmişler kopyalanmasın
            son = _dene(p, None, sessiz, None, bicimli, deneme=1, yedek=True, ilerleme=ilerleme)
        if son is None:
            if kuyruga:
                if ilerleme:  # gitmiş alt parçalar yeniden gitmesin: yalnızca gitmeyenler (ve sonraki parçalar) kuyruğa
                    _kuyruga_ekle_kayitlar(_kismi_kayitlar(time.time(), ilerleme["kalan"], parcalar[i + 1:], dugmeler,
                                                           sessiz, bicimli))
                else:
                    _kuyruga_ekle(parcalar[i:], dugmeler, sessiz, bicimli)
            return None
    return son


def _dene(metin, dugmeler, sessiz, yanit, bicimli, deneme=3, yedek=False, ilerleme=None):
    bekle = 1
    for i in range(deneme):
        try:
            m = _gonder_bir(metin, dugmeler, sessiz, yanit, bicimli, yedek)
            log(f"telegram ➜ #{m.get('message_id')}{' (yedek bot)' if yedek else ''}: {metin[:90]!r}")
            return m.get("message_id")
        except TelegramHatasi as e:
            if e.kod == 400 and "chat not found" in e.aciklama.lower():
                log("telegram: sohbet bulunamadı. Kullanıcı botu açıp /start'a basmalı.", e.aciklama)
                return None
            if e.kod == 400 and "too long" in e.aciklama.lower() and len(metin) > KUCUK_PARCA // 2:
                # Telegram uzunluğu bizden farklı saydı: kuyruğa girmesin (her dakika aynı hatayla arkasındakileri
                # tıkardı), daha küçük düz metin parçalarıyla hemen yeniden gitsin
                kucuk = bol(metin, sinir=min(KUCUK_PARCA, len(metin) // 2), bicimli=False)
                log(f"telegram: mesaj çok uzun bulundu, {len(kucuk)} düz metin parçasıyla yeniden gönderiliyor")
                son = None
                for j, p in enumerate(kucuk):
                    alt = {}
                    son = _dene(p, dugmeler if j == len(kucuk) - 1 else None, sessiz, yanit if j == 0 else None,
                                False, deneme, yedek, ilerleme=alt)
                    if son is None:
                        # gitmiş alt parçalar yeniden gitmesin: çağıran yalnızca gitmeyenleri kuyruğa alır (j == 0 ve
                        # iç ilerleme yoksa hiçbir şey gitmemiştir: çağıran tüm parçayı kuyruğa alır, eskisi gibi)
                        if ilerleme is not None and (j or alt):
                            ilerleme["kalan"] = alt.get("kalan", kucuk[j:j + 1]) + kucuk[j + 1:]
                        return None
                return son
            if e.kod in (400, 401, 403):  # yeniden denemekle düzelmez
                log("telegram gönderilemedi:", str(e)[:200], "| metin:", repr(metin[:80]))
                if e.kod == 400 and (dugmeler or yanit):  # düğme/yanıt sorunluysa yalın hâlini bir kez dene
                    return _dene(metin, None, sessiz, None, False, 1, yedek)
                return None
            log(f"telegram gönderilemedi ({i + 1}/{deneme}):", str(e)[:200])
            time.sleep(max(bekle, e.bekle))
            bekle *= 3
    return None


def _kuyruk_oku():
    try:
        with open(KUYRUK, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _kuyruk_yaz(liste):
    gecici = KUYRUK + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(gecici, KUYRUK)


def _kuyruga_ekle(parcalar, dugmeler, sessiz, bicimli):
    _kuyruga_ekle_kayitlar([{"zaman": time.time(), "parcalar": parcalar, "dugmeler": dugmeler, "sessiz": sessiz,
                             "bicimli": bicimli}])


def _kuyruga_ekle_kayitlar(kayitlar):
    with _kuyruk_kilit:
        liste = _kuyruk_oku()
        liste.extend(kayitlar)
        _kuyruk_yaz(liste)
    log(f"telegram: mesaj kuyruğa alındı ({sum(len(k['parcalar']) for k in kayitlar)} parça), "
        "dakikada bir yeniden denenecek")


def _kismi_kayitlar(zaman, kalan_alt, sonraki, dugmeler, sessiz, bicimli):
    """Bir parçanın alt parçalarının bir kısmı gitti, biri ağ hatasıyla düştü: kuyruğa yalnızca gitmeyenler girer.
    1) gitmeyen düz alt parçalar (önceki mesajın doğrudan devamı: öneksiz, 'isaretli'); 2) varsa sonraki ham
    parçalar (kendi biçimleriyle; gecikmeli giderse '(devamı, gecikmeli ...)' öneki alır, 'devami'). Düğmeler son
    parçada kalır."""
    kayitlar = [{"zaman": zaman, "parcalar": list(kalan_alt), "dugmeler": None if sonraki else dugmeler,
                 "sessiz": sessiz, "bicimli": False, "isaretli": True}]
    if sonraki:
        kayitlar.append({"zaman": zaman, "parcalar": list(sonraki), "dugmeler": dugmeler, "sessiz": sessiz,
                         "bicimli": bicimli, "devami": True})
    return kayitlar


def kuyrugu_bosalt():
    """Bekleyen mesajları sırayla göndermeyi dene. Dönüş: kalan sayısı."""
    from . import eylemler  # en üstte içe aktarılırsa döngü olur (eylemler telegram'ı kullanıyor)
    with _kuyruk_kilit:
        liste = _kuyruk_oku()
        kalan = []
        for k in liste:
            if time.time() - k["zaman"] > KUYRUK_EN_UZUN_SAAT * 3600:
                log("telegram: kuyrukta çok bekleyen mesaj atıldı:", repr(k["parcalar"][0][:80]))
                continue
            if kalan:  # sıra bozulmasın: öncekisi gitmediyse sonrakileri deneme
                kalan.append(k)
                continue
            gecikme = eylemler.tarih_metni(dt.datetime.fromtimestamp(k["zaman"], eylemler.TZ))
            kismi = None
            while k["parcalar"]:
                p = k["parcalar"][0]
                if not k.get("isaretli"):
                    onek = f"(devamı, gecikmeli {gecikme})\n" if k.get("devami") else f"(gecikmeli, {gecikme})\n"
                    p = onek + p
                    if len(p) > SINIR or (k["bicimli"] and len(html_bicim(p)) > TELEGRAM_SINIR):
                        # önek sınıra yakın parçayı Telegram'ın sınırının üstüne taşırdı: önek dahil yeniden böl
                        k["parcalar"][0:1] = bol(p, bicimli=k["bicimli"])
                        p = k["parcalar"][0]
                        k["isaretli"] = True  # önek ilk parçanın içinde
                ilerleme = {}
                if _dene(p, k["dugmeler"] if len(k["parcalar"]) == 1 else None, k["sessiz"], None,
                         k["bicimli"], deneme=1, ilerleme=ilerleme) is None:
                    if ilerleme:  # alt parçaların bir kısmı gitti: kopyalanmasınlar, yalnızca gitmeyenler kalsın
                        kismi = _kismi_kayitlar(k["zaman"], ilerleme["kalan"], k["parcalar"][1:], k["dugmeler"],
                                                k["sessiz"], k["bicimli"])
                    break
                k["parcalar"].pop(0)
                k["isaretli"] = True
            if kismi:
                kalan.extend(kismi)
            elif k["parcalar"]:
                kalan.append(k)
        if kalan != liste:
            _kuyruk_yaz(kalan)
        return len(kalan)


def dosya_gonder(yol, aciklama="", gecmise=True):
    """Bir dosyayı (ör. ders notu .md) belge olarak gönder. Sohbet botu yoksa ESP botuyla."""
    try:
        with open(yol, "rb") as f:
            bayt = f.read()
        if gecmise and gecmis_dinleyicisi:
            gecmis_dinleyicisi(f"[dosya gönderildi: {os.path.basename(yol)}] {aciklama}")
        m = api("sendDocument", {"chat_id": sohbet_id(), "caption": aciklama[:1000]},
                jeton=SOHBET_JETONU or None, sure=60, dosya=("document", os.path.basename(yol), bayt))
        log(f"telegram ➜ #{m.get('message_id')} dosya: {os.path.basename(yol)}")
        return m.get("message_id")
    except (OSError, TelegramHatasi) as e:
        log("telegram: dosya gönderilemedi:", os.path.basename(yol), str(e)[:150])
        return None


INDIRME_SINIRI = 20 * 2**20  # Bot API getFile en fazla 20 MB veriyor


def dosya_indir(file_id, hedef):
    """Kullanıcının gönderdiği dosyayı (belge, fotoğraf, ses) sohbet botundan indir. Dönüş: hedef yolu.
    Hata: TelegramHatasi (ör. 20 MB'tan büyük dosya)."""
    if not SOHBET_JETONU:
        raise TelegramHatasi(0, "sohbet botu yok")
    bilgi = api("getFile", {"file_id": file_id})
    if (bilgi.get("file_size") or 0) > INDIRME_SINIRI:
        raise TelegramHatasi(400, "dosya 20 MB'tan büyük, bot indiremiyor")
    url = f"https://api.telegram.org/file/bot{SOHBET_JETONU}/{bilgi['file_path']}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            veri = r.read(INDIRME_SINIRI + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise TelegramHatasi(0, f"indirilemedi ({type(e).__name__})")  # repr adresi, yani jetonu içerir
    if len(veri) > INDIRME_SINIRI:
        raise TelegramHatasi(400, "dosya 20 MB'tan büyük")
    os.makedirs(os.path.dirname(hedef), exist_ok=True)
    with open(hedef, "wb") as f:
        f.write(veri)
    log(f"telegram ⬇ dosya indirildi: {os.path.basename(hedef)} ({len(veri) // 1024} KB)")
    return hedef


class Akis:
    """Cevabı yazıldıkça gösterir: ilk parça sendMessage, sonra en sık ARALIK saniyede bir editMessageText. Ara hâller
    düz metin ('**', '`' ve satır başı '#' atılır, yarım kalmış biçim HTML'i bozmasın), son hâl biçimli. Ara
    güncellemede bir hata olursa akış susar; son metin bitir()'de her durumda gider (gerekirse taslak silinip
    normal mesajla, o da olmazsa kuyruğa)."""
    ARALIK = 2.5   # Telegram sohbet başına saniyede ~1 düzenlemeye izin veriyor; seyrek güncelleme ekran okuyucunun
                   # aynı metni tekrar tekrar okumasını da azaltır
    ILK = 40       # karakter: kısa cevaplar için ara mesaj açılmasın, doğrudan son hâli gitsin
    IMLEC = " …"

    def __init__(self):
        self.mesaj_id, self.son, self.son_zaman, self.sustu, self.bitti = None, "", 0.0, False, False

    def guncelle(self, metin):
        metin = (metin or "").strip()
        if (self.sustu or self.bitti or not SOHBET_JETONU or not metin or metin == self.son
                or (self.mesaj_id is None and len(metin) < self.ILK)
                or time.monotonic() - self.son_zaman < self.ARALIK):
            return
        self.son_zaman = time.monotonic()
        taslak = re.sub(r"(?m)^(\s*)#+\s*", r"\1", metin.replace("**", "").replace("`", ""))
        veri = {"chat_id": sohbet_id(), "text": taslak[:SINIR] + self.IMLEC, "link_preview_options": ONIZLEME_YOK}
        try:
            if self.mesaj_id is None:
                self.mesaj_id = api("sendMessage", veri, sure=10)["message_id"]
                log(f"telegram ➜ #{self.mesaj_id} (akış taslağı)")
            else:
                api("editMessageText", {**veri, "message_id": self.mesaj_id}, sure=10)
            self.son = metin
        except TelegramHatasi as e:
            if e.kod != 429 and "not modified" not in e.aciklama:  # sınıra takıldıysa bir sonrakinde dener
                log("telegram: akış durdu, cevap bitince tek parça gidecek:", str(e)[:150])
                self.sustu = True
        except Exception as e:  # ör. yarım okunan cevap: beyin akışı kesilmesin (tur kayardı)
            log("telegram: akış durdu (beklenmeyen hata):", repr(e)[:150])
            self.sustu = True

    def bitir(self, metin, bicimli=True):
        """Son hâli yaz (boşsa taslağı kaldır). Dönüş: son parçanın mesaj numarası ya da None."""
        if self.bitti:  # ikinci çağrı (ör. sonradan çıkan bir hata) gitmiş son cevabı silmesin
            return None
        self.bitti = True
        metin = (metin or "").strip()
        if self.mesaj_id is None:
            return gonder(metin, gecmise=False, bicimli=bicimli) if metin else None
        parcalar = bol(metin, bicimli=bicimli) if metin else []
        if parcalar and self._duzenle(parcalar[0], bicimli):
            log(f"telegram ✎ #{self.mesaj_id} (akış bitti): {parcalar[0][:90]!r}")
            son = self.mesaj_id
            for p in parcalar[1:]:
                son = gonder(p, gecmise=False, bicimli=bicimli)
            return son
        try:  # düzenlenemedi ya da cevap boş çıktı: yarım taslak ekranda kalmasın
            api("deleteMessage", {"chat_id": sohbet_id(), "message_id": self.mesaj_id}, sure=10)
        except TelegramHatasi as e:
            log("telegram: taslak silinemedi:", str(e)[:120])
        return gonder(metin, gecmise=False, bicimli=bicimli) if metin else None

    def _duzenle(self, metin, bicimli):
        veri = {"chat_id": sohbet_id(), "message_id": self.mesaj_id, "link_preview_options": ONIZLEME_YOK,
                "text": html_bicim(metin) if bicimli else metin, **({"parse_mode": "HTML"} if bicimli else {})}
        for deneme in range(3):
            try:
                api("editMessageText", veri, sure=15)
                return True
            except TelegramHatasi as e:
                if "not modified" in e.aciklama:
                    return True
                if bicimli and e.kod == 400 and "parse" in e.aciklama.lower():
                    return self._duzenle(metin, False)
                if e.kod not in (0, 429) or deneme == 2:
                    log("telegram: akışın son hâli yazılamadı:", str(e)[:150])
                    return False
                time.sleep(max(1, e.bekle))
        return False


def yaziyor():
    if SOHBET_JETONU:
        try:
            api("sendChatAction", {"chat_id": sohbet_id(), "action": "typing"}, sure=10)
        except TelegramHatasi:
            pass


YAZIYOR_ARALIK = 4.5              # Telegram "yazıyor…" göstergesi ~5 sn sürüyor; bitmeden yenilenir
_yaziyor_kilit = threading.Lock()
_yaziyor_sayac = 0
_yaziyor_bitti = threading.Event()
_yaziyor_calisiyor = False


def _yaziyor_dongu():
    global _yaziyor_calisiyor
    while True:
        try:
            yaziyor()             # ada göre çağrılır: testler modüldeki yaziyor'u değiştirebilsin
        except Exception as e:
            log("telegram: yazıyor göstergesi:", repr(e)[:150])
        if _yaziyor_bitti.wait(YAZIYOR_ARALIK):
            with _yaziyor_kilit:
                if _yaziyor_sayac == 0:
                    _yaziyor_calisiyor = False
                    return
                _yaziyor_bitti.clear()   # bu arada yeni biri girdi, sürdür


class yaziyor_goster:
    """`with` bloğu sürdükçe sohbette "yazıyor…" görünür. İç içe ve paralel bloklar tek bir iş parçacığını
    paylaşır (sayaçla), böylece aynı anda iki gösterge isteği gitmez."""

    def __enter__(self):
        global _yaziyor_sayac, _yaziyor_calisiyor
        with _yaziyor_kilit:
            _yaziyor_sayac += 1
            _yaziyor_bitti.clear()
            if not _yaziyor_calisiyor:
                _yaziyor_calisiyor = True
                threading.Thread(target=_yaziyor_dongu, daemon=True, name="yaziyor").start()
        return self

    def __exit__(self, *_):
        global _yaziyor_sayac
        with _yaziyor_kilit:
            _yaziyor_sayac -= 1
            if _yaziyor_sayac == 0:
                _yaziyor_bitti.set()
        return False


def dugme_cevapla(sorgu_id, metin="", mesaj_id=None, dugmeleri_kaldir=True):
    """Tıklanan düğmeye cevap (küçük bildirim) ve isterse mesajdaki düğmeleri kaldır."""
    if sorgu_id != "simule":  # yerel test kancasından gelen tıklamanın Telegram'da karşılığı yok
        try:
            api("answerCallbackQuery", {"callback_query_id": sorgu_id, "text": metin[:190]}, sure=10)
        except TelegramHatasi as e:
            log("telegram: düğme cevabı gitmedi:", str(e)[:120])
    if mesaj_id and dugmeleri_kaldir:
        try:
            api("editMessageReplyMarkup", {"chat_id": sohbet_id(), "message_id": mesaj_id,
                                           "reply_markup": {"inline_keyboard": []}}, sure=10)
        except TelegramHatasi as e:
            if "not modified" not in e.aciklama:
                log("telegram: mesaj düzenlenemedi:", str(e)[:120])


# ------------------------------------------------------------------ dinleme
class Dinleyici:
    """Sohbet botunun güncellemelerini uzun yoklamayla okur. Yalnızca kullanıcının sohbetinden gelenler işlenir."""

    def __init__(self, mesaj_geldi, dugme_geldi):
        self.mesaj_geldi, self.dugme_geldi = mesaj_geldi, dugme_geldi
        self.son_basari = 0.0
        self.son_hata = ""
        self.bot_adi = None
        self.yabancilar = set()
        if not SOHBET_JETONU:
            log("telegram: TELEGRAM_SOHBET_JETONU yok, yalnızca tek yönlü bildirim (ESP botu)")
            return
        threading.Thread(target=self._dongu, daemon=True, name="telegram-dinle").start()
        threading.Thread(target=self._kuyruk_dongu, daemon=True, name="telegram-kuyruk").start()

    def saglikli(self):
        """Son 3 dakikada en az bir yoklama başarılıysa sağlıklı."""
        return (not SOHBET_JETONU) or time.time() - self.son_basari < 180

    def _ofset(self):
        try:
            with open(OFSET, encoding="utf-8") as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _ofset_yaz(self, deger):
        with open(OFSET, "w", encoding="utf-8") as f:
            f.write(str(deger))

    def _kuyruk_dongu(self):
        while True:
            time.sleep(60)
            try:
                kuyrugu_bosalt()
            except Exception as e:
                log("telegram kuyruk hatası:", repr(e)[:150])

    def _dongu(self):
        bekleme = 2
        while self.bot_adi is None:
            try:
                self.bot_adi = api("getMe")["username"]
                self.son_basari = time.time()  # ilk uzun yoklama 50 sn sürebilir: açılışta "sorunlu" görünmesin
                log(f"telegram: sohbet botu @{self.bot_adi} dinleniyor (sohbet {sohbet_id()})")
                api("setMyCommands", {"commands": [
                    {"command": "hatirlatmalar", "description": "Kurulu hatırlatmalar"},
                    {"command": "takvim", "description": "Önümüzdeki günler"},
                    {"command": "teslimler", "description": "Yaklaşan teslim ve sınavlar"},
                    {"command": "onaylar", "description": "Bekleyen onay istekleri"},
                    {"command": "durum", "description": "Servisin durumu"},
                    {"command": "yeni", "description": "Sohbeti sıfırla"},
                    {"command": "yardim", "description": "Neler yapabilirim"}]})
            except TelegramHatasi as e:
                self.son_hata = str(e)
                log("telegram: bota bağlanılamadı:", str(e)[:150])
                time.sleep(min(bekleme, 300))
                bekleme *= 2
        ofset, bekleme = self._ofset(), 2
        while True:
            try:
                guncellemeler = api("getUpdates", {"offset": ofset, "timeout": 50,
                                                   "allowed_updates": ["message", "callback_query"]}, sure=65)
                self.son_basari, bekleme = time.time(), 2
            except TelegramHatasi as e:
                self.son_hata = str(e)
                if e.kod == 409:
                    log("telegram: bu botu başka bir süreç de okuyor (409). 30 sn bekleniyor.")
                    time.sleep(30)
                else:
                    log("telegram yoklama hatası:", str(e)[:150])
                    time.sleep(min(bekleme, 120))
                    bekleme *= 2
                continue
            for g in guncellemeler:
                ofset = g["update_id"] + 1
                self._ofset_yaz(ofset)  # işlemeden önce: bir mesaj servisi çökertiyorsa döngüye girmesin
                try:
                    self._isle(g)
                except Exception as e:
                    log("telegram: güncelleme işlenemedi:", repr(e)[:200])

    def _izinli(self, sohbet):
        return str(sohbet) == sohbet_id()

    def _isle(self, g):
        if "callback_query" in g:
            q = g["callback_query"]
            mesaj = q.get("message") or {}
            if not self._izinli((mesaj.get("chat") or {}).get("id")) or str(q["from"]["id"]) != sohbet_id():
                return
            self.dugme_geldi(q.get("data", ""), q["id"], mesaj)
            return
        m = g.get("message")
        if not m:
            return
        sohbet = m["chat"]["id"]
        if not self._izinli(sohbet):
            if sohbet not in self.yabancilar:
                self.yabancilar.add(sohbet)
                log(f"telegram: yabancı sohbetten mesaj yok sayıldı (sohbet {sohbet}, "
                    f"@{(m.get('from') or {}).get('username')})")
            return
        self.mesaj_geldi(m)
