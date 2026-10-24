"""Uyarı merkezi: gözcülerden gelen olayları önemine göre Telegram mesajına, Notion notuna ve takvime çevirir."""
import datetime as dt
import difflib
import json
import os
import re
import threading
import time

from .. import eylemler, odevler, telegram
from ..ayarlar import KAYITLAR, ayar, log, sessiz_mi
from .sistem import beri_metni, bulunma_eki

# Yeni ödev takibi (kullanıcının isteği, 2026-10-09): ödev gelince haber ver, "başladım" diyene kadar 2 saatte bir yaz
TAKIP = os.path.join(KAYITLAR, "odev_takip.json")
_normal = eylemler._normal
_kod = eylemler.ders_kodu
_okunur_kod = eylemler.okunur_kod
_YANIT_ONEKI = re.compile(r"^\s*((re|fwd?|ynt|ilt|ileti|i̇lt)\s*:\s*)+", re.I)


def _konu_anahtari(konu):
    """Mail konusunu karşılaştırmak için: küçük harf, Re:/Fwd: önekleri ve fazla boşluk atılır."""
    return re.sub(r"\s+", " ", _YANIT_ONEKI.sub("", (konu or "").lower())).strip()


def _ayni_konu(a, b):
    """Aynı ya da biri ötekini içeren (≥12 harf) konu: 'Ayse Yilmaz: 28 Ekim Telafisi hk.' ~ '28 Ekim Telafisi hk.'"""
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 12 and (a in b or b in a)))


# Aynı duyuru bazen ayrı gözcü turlarında iki ayrı mail olarak gelir (23 Eki BIL 281 telafisi: 15:03 ve 15:19'da
# aynı Learn duyurusu). Gönderilen mail uyarıları 24 saat tutulur; aynı konu aynı özetle yeniden gelirse gitmez.
GONDERILEN = os.path.join(KAYITLAR, "gonderilen_uyarilar.json")
TEKRAR_SURESI = dt.timedelta(hours=24)
BENZERLIK = 0.8
_gonderilen_kilit = threading.Lock()


def _ozet_normal(metin):
    return re.sub(r"\s+", " ", (metin or "").lower()).strip()


def _onceki_uyari(onem, anahtar, ozet, simdi=None):
    """Turlar arası tekrar denetimi; giden uyarıyı kaydeder. Dönüş: None (yeni konu), "tekrar" (gönderme) ya da
    "guncelleme" (konu son 24 saatte gitti ama özet değişti ya da önem önemliden acile çıktı)."""
    simdi = simdi or dt.datetime.now()
    with _gonderilen_kilit:
        try:
            kayitlar = eylemler.oku(GONDERILEN, [])
        except ValueError:  # bozuk kayıt: yalnızca tekrar önleme önbelleği, baştan başlanır
            kayitlar = []
        sinir = (simdi - TEKRAR_SURESI).isoformat(timespec="seconds")
        kayitlar = [k for k in kayitlar if isinstance(k, dict) and k.get("zaman", "") > sinir]
        onceki = [k for k in kayitlar if _ayni_konu(k.get("anahtar"), anahtar)]
        sonuc = None
        if onceki:
            yukseldi = onem == "acil" and all(k.get("onem") != "acil" for k in onceki)
            benzer = any(difflib.SequenceMatcher(None, _ozet_normal(k.get("ozet")), _ozet_normal(ozet)).ratio()
                         >= BENZERLIK for k in onceki)
            sonuc = "guncelleme" if yukseldi or not benzer else "tekrar"
        if sonuc != "tekrar":  # tekrar kaydedilmez: 24 saat ilk gönderimden sayılır
            kayitlar.append({"zaman": simdi.isoformat(timespec="seconds"), "onem": onem, "anahtar": anahtar,
                             "ozet": ozet})
        try:
            eylemler.yaz(GONDERILEN, kayitlar)
        except OSError as e:
            log("gönderilen uyarılar kaydedilemedi:", repr(e))
    return sonuc


def _takip_saati():
    try:
        return min(24.0, max(0.25, float(ayar("TAKIP_SAAT", "2"))))  # geçersiz değer servisi çökertmesin
    except ValueError:
        return 2.0


TAKIP_ARALIGI = dt.timedelta(hours=_takip_saati())  # .env TAKIP_SAAT ile değişir (0,25-24 saat)


_SAAT_BICIMI = re.compile(r"^(\d{1,2}):(\d{2})$")
_saat_uyarilan = set()  # geçersiz TAKIP_SAATLERI değeri için günlüğe yalnızca bir kez yaz


def takip_saatleri():
    """.env TAKIP_SAATLERI ("20:00" ya da "09:00,20:00") → sıralı, tekrarsız dt.time listesi.
    Geçerli saat yoksa boş liste: takip TAKIP_SAAT aralığıyla sürer."""
    ham = str(ayar("TAKIP_SAATLERI", "") or "").strip()
    saatler, gecersiz = set(), []
    for parca in ham.split(","):
        parca = parca.strip()
        if not parca:
            continue
        m = _SAAT_BICIMI.match(parca)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            saatler.add(dt.time(int(m.group(1)), int(m.group(2))))
        else:
            gecersiz.append(parca)
    if gecersiz and ham not in _saat_uyarilan:
        _saat_uyarilan.add(ham)
        log(f"TAKIP_SAATLERI geçersiz saat yok sayıldı: {', '.join(gecersiz)}"
            + ("" if saatler else f" — TAKIP_SAAT ({_takip_saati():g} saat) kullanılacak"))
    return sorted(saatler)


def sonraki_takip(simdi, teslim_iso=None):
    """Bir takip mesajından sonra sıradaki zaman. TAKIP_SAATLERI varsa şimdiden sonraki ilk listelenen saat
    (bugün ya da yarın); yoksa ya da o saat teslimi geçiyorsa simdi + TAKIP_ARALIGI."""
    saatler = takip_saatleri()
    if saatler:
        bugun = simdi.date()
        for gun in (bugun, bugun + dt.timedelta(days=1)):
            for t in saatler:
                aday = dt.datetime.combine(gun, t)
                if aday > simdi:
                    try:
                        gecer = teslim_iso is not None and aday >= dt.datetime.fromisoformat(teslim_iso)
                    except (TypeError, ValueError):
                        gecer = False
                    return simdi + TAKIP_ARALIGI if gecer else aday
    return simdi + TAKIP_ARALIGI


def aralik_metni():
    saatler = [t.strftime("%H:%M") for t in takip_saatleri()]
    if saatler:
        liste = saatler[0] if len(saatler) == 1 else ", ".join(saatler[:-1]) + " ve " + saatler[-1]
        return f"her gün {liste}{bulunma_eki(saatler[-1])}"
    s = TAKIP_ARALIGI.total_seconds() / 3600
    return f"{s:g} saatte bir" if s >= 1 else f"{round(s * 60)} dakikada bir"


YARIN_SAATI = dt.time(10, 0)  # "Yarın hatırlat" düğmesi takibi ertesi gün bu saate atar
TAKIP_BITTI = ("Başlandı", "Gönderildi", "Tamamlandı")
TAKIP_KILIT = threading.Lock()  # modül düzeyinde: eylemler.odev_takip_durdur da kullanıyor


def _takip_oku():
    try:
        with open(TAKIP, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _takip_yaz(liste):
    """Son 100 takip kaydı saklanır."""
    with open(TAKIP, "w", encoding="utf-8") as f:
        json.dump(liste[-100:], f, ensure_ascii=False, indent=1)


def takip_durdur(ders, odev):
    """"Bu ödev için yazmayı bırak": takibi kapat. Dönüş: durdurulan ödev adları."""
    kod = _okunur_kod(_kod(ders)) if _kod(ders) else None
    hedef = _normal(odev)
    with TAKIP_KILIT:
        liste = _takip_oku()
        durdu = []
        for x in liste:
            a = _normal(x["odev"])
            if x.get("aktif") and (not kod or x["kod"] == kod) and hedef and (hedef in a or a in hedef):
                x.update(aktif=False, bitis="kullanıcı durdurdu")
                durdu.append(f"{x['kod']} {x['odev']}")
        if durdu:
            with open(TAKIP, "w", encoding="utf-8") as f:
                json.dump(liste, f, ensure_ascii=False, indent=1)
    return durdu


def takip_teslimi(ders, odev):
    """Açık takibin son teslim zamanı (yoksa None)."""
    kod = _okunur_kod(_kod(ders)) or ders
    hedef = _normal(odev)
    for x in _takip_oku():
        if x.get("aktif") and x["kod"] == kod and hedef and (hedef in _normal(x["odev"]) or _normal(x["odev"]) in hedef):
            try:
                return dt.datetime.fromisoformat(x["teslim"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


def takip_ertele(ders, odev, zaman):
    """Takibin bir sonraki mesajını `zaman`'a at (takip açık kalır). Dönüş: ertelenen ödev adı ya da None."""
    kod = _okunur_kod(_kod(ders)) or ders
    hedef = _normal(odev)
    with TAKIP_KILIT:
        liste = _takip_oku()
        for x in liste:
            if x.get("aktif") and x["kod"] == kod and hedef and (hedef in _normal(x["odev"])
                                                                 or _normal(x["odev"]) in hedef):
                x["sonraki"] = zaman.isoformat(timespec="minutes")
                _takip_yaz(liste)
                return f"{x['kod']} {x['odev']}"
    return None


def takip_baslat(ders, odev, teslim_iso, simdi=None):
    """"Bu ödevi yine takip et": takibi aç (yoksa ekle); ilk mesaj bir dakika içinde gider. Dönüş: ödev etiketi.
    Takvimdeki durum Başlandı/Gönderildi ise çağıran onu önce Yapılacak yapmalı, yoksa takip hemen kapanır."""
    kod = _okunur_kod(_kod(ders)) or ders
    hedef = _normal(odev)
    simdi = (simdi or dt.datetime.now()).isoformat(timespec="minutes")
    with TAKIP_KILIT:
        liste = _takip_oku()
        x = next((x for x in reversed(liste) if x["kod"] == kod and hedef
                  and (hedef in _normal(x["odev"]) or _normal(x["odev"]) in hedef)), None)
        if x is None:
            liste.append({"kod": kod, "odev": odev, "teslim": teslim_iso, "eklendi": simdi, "sonraki": simdi,
                          "arama": 0, "aktif": True})
        else:
            x.update(aktif=True, sonraki=simdi, teslim=teslim_iso, arama=min(x.get("arama", 0), 1))
            x.pop("bitis", None)
        _takip_yaz(liste)
    log(f"ödev takibi yeniden açıldı: {kod} — {odev}")
    return f"{kod} {odev}"


def _ayni_teslim(olay, baslik, zaman, kod):
    """Aynı ders + aynı başlık, ya da aynı ders + ±1 saat (farklı adla girilmiş kopyaları yakalar)."""
    olay_kod = olay.get("ders") or _kod(olay.get("baslik"))
    if kod and olay_kod and kod != olay_kod:
        return False
    b1, b2 = _normal(olay.get("baslik")), _normal(baslik)
    if b1 == b2 or (b1 and b2 and (b1 in b2 or b2 in b1) and kod and olay_kod):
        return True
    try:
        fark = abs(dt.datetime.fromisoformat(olay["zaman"]) - zaman)
    except (KeyError, ValueError):
        return False
    return bool(kod) and kod == olay_kod and fark <= dt.timedelta(hours=1)


class Uyari:
    def __init__(self, baslat=True):
        self.takip_kilit = TAKIP_KILIT
        self.posta = None  # okul postası gözcüsü (servis bağlar): gönderim onayları oradan gelir
        self.baslangic = time.time()
        if baslat:
            threading.Thread(target=self._dongu, daemon=True, name="uyari").start()

    # ------------------------------------------------------------ dışarıdan
    def bildir(self, onem, metin, kaynak):
        """acil: hemen, sesli bildirimle; onemli: gece sessiz saatinde sessiz bildirimle, ayrıca Notion notu."""
        log(f"uyarı [{onem}] {kaynak}: {metin}")
        # metin mail özetinden de gelebilir (güvenilmeyen): bağlantı ya da biçime dönüşmesin
        if onem == "acil":
            telegram.gonder(f"🚨 {telegram.duz_metin(metin)}")
        elif onem == "onemli":
            telegram.gonder(f"**Önemli:** {telegram.duz_metin(metin)}", sessiz=sessiz_mi())
            try:
                eylemler.uygula({"tip": "not", "metin": f"{kaynak}: {metin}"}, kaynak="gözcü")
            except Exception as e:
                log("not eklenemedi:", repr(e))

    def mailleri_isle(self, mailler, kaynak, ilk_tur=False):
        """Değerlendir, teslimleri takvime işle, önemlileri bildir. Değerlendirme başarısızsa False."""
        from .degerlendir import degerlendir
        if not mailler:
            return True
        sonuc = degerlendir(mailler)
        if not sonuc:
            return False
        uyarilar = []  # [önem, konu anahtarı, [özet]]: aynı turda aynı konulu mailler tek mesaj olur
        try:
            self._mailleri_yaz(mailler, sonuc, kaynak, ilk_tur, uyarilar)
        finally:  # bir mail işlenirken hata çıksa da toplanan uyarılar gitsin
            for onem, anahtar, ozetler in uyarilar:
                metin = "\n".join(dict.fromkeys(ozetler))
                onceki = _onceki_uyari(onem, anahtar, metin)
                if onceki == "tekrar":
                    log(f"{kaynak}: tekrar uyarı atlandı (son 24 saatte gitti): {metin[:120]}")
                    continue
                self.bildir(onem, ("Güncelleme: " if onceki == "guncelleme" else "") + metin, kaynak)
        return True

    def _mailleri_yaz(self, mailler, sonuc, kaynak, ilk_tur, uyarilar):
        for m in mailler:
            s = sonuc.get(m["id"])
            if not s:
                continue
            log(f"{kaynak} [{s['onem']}] {m['konu'][:60]} — {s['ozet']}")
            eylemler.mail_kaydet(m, s["onem"], s["ozet"])
            for t in s["teslimler"]:
                self.teslim_isle(t, kaynak)
            if s["onem"] in ("acil", "onemli") and not ilk_tur:  # eski mailler için uyarı yağdırma
                anahtar = _konu_anahtari(m["konu"])
                grup = next((g for g in uyarilar if _ayni_konu(g[1], anahtar)), None)
                if grup is None:
                    uyarilar.append([s["onem"], anahtar, [s["ozet"] or m["konu"]]])
                else:  # ör. hocanın maili + OBS'nin aynı mesajı ilettiği kopya (22 Eki MATH 113 telafisi)
                    grup[2].append(s["ozet"] or m["konu"])
                    grup[0] = "acil" if "acil" in (grup[0], s["onem"]) else grup[0]

    def gonderim_isle(self, ders_kodu, odev):
        """'Ödev gönderildi' onayı: takvimde Gönderildi yap, o ödevin bekleyen hatırlatmalarını iptal et."""
        hedef, kod = _normal(odev), _kod(ders_kodu)
        with eylemler._kilit:
            aramalar = eylemler.oku(eylemler.ARAMALAR, [])
            olay = eylemler.teslim_bul(eylemler.oku(eylemler.TAKVIM, {"olaylar": []}).get("olaylar", []), odev, ders_kodu)
        iptal = [a for a in aramalar if a.get("durum") == "bekliyor" and hedef and hedef in _normal(a.get("konu"))
                 and (not kod or _kod(a.get("konu")) in (None, kod))]
        for a in iptal:
            eylemler.arama_durumu(a["id"], "iptal")
        if olay and olay.get("durum") != "Gönderildi":
            eylemler.takvim_guncelle(olay["id"], {"Durum": "Gönderildi"})
        log(f"gönderim onayı: {ders_kodu} {odev} — {len(iptal)} hatırlatma iptal, takvim: {'güncellendi' if olay else 'kayıt yok'}")
        return len(iptal)

    def teslim_isle(self, t, kaynak):
        """Mailden çıkan teslim/sınav: takvime ekle, tarihi değiştiyse ya da ek süre verildiyse güncelle,
        hatırlatmalarını kur. t: {ders, baslik, zaman, tur, ek_sure?}"""
        try:
            zaman = dt.datetime.fromisoformat(t["zaman"])
        except (KeyError, ValueError, TypeError):
            return
        if zaman < dt.datetime.now() - dt.timedelta(hours=1):
            return  # geçmiş teslim
        kod = _kod(t.get("ders")) or _kod(t.get("baslik"))
        if kod in eylemler.BIRAKILAN_DERSLER:
            return  # kullanıcı bu dersi bıraktı
        baslik = (t.get("baslik") or "").strip()
        if kod:  # başlıktaki ders kodunu at, ders ayrı alanda duruyor
            baslik = re.sub(rf"^\s*{kod[:-3]}\s?{kod[-3:]}\s*[-–—:]?\s*", "", baslik, flags=re.I).strip() or baslik
        tur = t.get("tur") if t.get("tur") in ("Teslim", "Sınav", "Etkinlik") else "Teslim"
        ek_sure = bool(t.get("ek_sure"))
        z_iso = zaman.isoformat(timespec="minutes")
        with eylemler._kilit:
            tk = eylemler.oku(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
            ayni = next((o for o in tk.get("olaylar", []) if _ayni_teslim(o, baslik, zaman, kod)), None)
        etiket = f"{_okunur_kod(kod)} — {baslik}".strip(" —")
        if tur == "Teslim":  # kullanıcı ödev dosyasını koysun diye klasörü hazırla (değerlendirme isteğe bağlı)
            try:
                odevler.klasor(_okunur_kod(kod) or "Diğer", baslik, olustur=True)
            except OSError as e:
                log("ödev klasörü açılamadı:", repr(e))
        if ayni:
            if ayni.get("zaman") == z_iso and not (ek_sure and ayni.get("durum") != "Ek süre"):
                return  # zaten var, değişmemiş
            alanlar = {"Zaman": z_iso}
            if ek_sure:
                alanlar["Durum"] = "Ek süre"
            elif ayni.get("durum") == "Gecikti":
                alanlar["Durum"] = "Yapılacak"
            eski = ayni["zaman"]
            eylemler.takvim_guncelle(ayni["id"], alanlar)
            neden = "ek süre verildi" if ek_sure else "tarihi değişti"
            self.bildir("acil" if zaman - dt.datetime.now() < dt.timedelta(days=2) else "onemli",
                        f"{etiket}: {neden}, {eylemler.tarih_metni(eski)} yerine {eylemler.tarih_metni(zaman)}", kaynak)
        else:
            eylemler.uygula({"tip": "takvim_ekle", "baslik": baslik, "zaman": z_iso, "tur": tur, "ders": kod,
                             "durum": "Ek süre" if ek_sure else ("Yapılacak" if tur in ("Teslim", "Sınav") else None),
                             "kaynak": "İEU Learn" if "Learn" in kaynak else "Gmail" if kaynak == "gmail" else "Okul postası",
                             "not": "Ek gönderim süresi" if ek_sure else ""}, kaynak="gözcü")
        if tur in ("Teslim", "Sınav"):
            self._hatirlatmalari_kur(etiket, zaman)

    def _hatirlatmalari_kur(self, etiket, zaman):
        """Teslimden bir gün önce 20:00'de ve 3 saat önce (08-23 arasındaysa) hatırlat."""
        simdi = dt.datetime.now()
        adaylar = [((zaman - dt.timedelta(days=1)).replace(hour=20, minute=0), f"yarın: {etiket}"),
                   (zaman - dt.timedelta(hours=3), f"3 saat kaldı: {etiket}")]
        with eylemler._kilit:
            mevcut = {(a.get("zaman"), _normal(a.get("konu"))) for a in eylemler.oku(eylemler.ARAMALAR, [])
                      if a.get("durum") == "bekliyor"}
        for z, konu in adaylar:
            if z <= simdi + dt.timedelta(minutes=10) or not (8 <= z.hour <= 23):
                continue
            if (z.isoformat(timespec="minutes"), _normal(konu)) in mevcut:
                continue
            try:
                eylemler.uygula({"tip": "hatirlat", "zaman": z.isoformat(timespec="minutes"), "konu": konu},
                                kaynak="gözcü")
            except ValueError:
                pass

    # ------------------------------------------------------------ gecikme kontrolü
    def gecikme_kontrol(self):
        """Son teslimi geçmiş ama gönderim onayı gelmemiş teslimleri 'Gecikti' yap ve bir kez sor.
        Son 3 günün teslimlerine bakılır (eski kayıtlar için uyarı yağdırmamak için)."""
        simdi = dt.datetime.now()
        with eylemler._kilit:
            olaylar = eylemler.oku(eylemler.TAKVIM, {"olaylar": []}).get("olaylar", [])
        for o in olaylar:
            if (o.get("tur") != "Teslim" or o.get("durum") not in (None, "", "Yapılacak", "Ek süre", "Başlandı")
                    or o.get("ders") in eylemler.BIRAKILAN_DERSLER):
                continue
            try:
                z = dt.datetime.fromisoformat(o["zaman"])
            except (KeyError, ValueError):
                continue
            if not (simdi - dt.timedelta(days=3) <= z <= simdi - dt.timedelta(minutes=15)):
                continue
            # Onay maili son teslimden sonra okul postası okunmadıysa gelmiş ama görülmemiş olabilir (22 Eki: posta
            # 20 Eki'den beri okunamıyordu, servis açılır açılmaz gönderilmiş sunum için "gecikti" yazdı).
            okunmadi = self._posta_okunmadi(z)
            if okunmadi == "acilis":
                continue  # servis yeni açıldı: önce posta bir kez yoklansın
            eylemler.takvim_guncelle(o["id"], {"Durum": "Gecikti"})
            etiket = f"{_okunur_kod(o.get('ders'))} — {o['baslik']}".strip(" —")
            if okunmadi:
                self.bildir("onemli", f"{etiket}: son teslim ({eylemler.tarih_metni(z)}) geçti. {okunmadi} gönderim "
                                      "onayını göremiyorum; gönderdiyseniz söyleyin, takvime işleyeyim.", "takvim")
            else:
                self.bildir("onemli", f"{etiket}: son teslim ({eylemler.tarih_metni(z)}) geçti, gönderim onayı "
                                      "gelmedi. Ek süre verildiyse söyleyin, takvime işleyeyim.", "takvim")

    def _posta_okunmadi(self, son_teslim):
        """Okul postası son teslimden (+15 dk) beri yoklanmadıysa neden metni ("Okul postası ... okunamadığı için"),
        servis yeni açıldıysa "acilis", yoklandıysa (ya da gözcü yoksa) None."""
        posta = getattr(self, "posta", None)
        if posta is None or getattr(posta, "kapali", True):
            return None
        sinir = son_teslim.replace(tzinfo=son_teslim.tzinfo or eylemler.TZ).timestamp() + 15 * 60
        son = posta.son_yoklama or 0
        if son >= sinir:
            return None
        if time.time() - getattr(self, "baslangic", 0) < 15 * 60:
            return "acilis"
        if getattr(posta, "bagli", False) and time.time() - son < 180:
            return "acilis"  # bağlı ve az önce yokladı, sınır yeni geçti: bir sonraki yoklamayı bekle
        ilk_hata = getattr(getattr(posta, "erisim", None), "durum", {}).get("ilk_hata")
        return f"Okul postası {beri_metni(ilk_hata)} okunamadığı için" if ilk_hata else \
            "Okul postası şu an okunamadığı için"

    # ------------------------------------------------------------ yeni ödev takibi
    @staticmethod
    def _takip_oku():
        return _takip_oku()

    @staticmethod
    def _takip_yaz(liste):
        _takip_yaz(liste)

    def odev_takibe_al(self, kod, odev, teslim_iso, simdi=None):
        """Yeni ödev: hemen haber ver, kullanıcı başladığını söyleyene (ya da gönderene / teslim geçene) kadar 2 saatte bir."""
        kod = _okunur_kod(_kod(kod)) or kod
        if _kod(kod) in eylemler.BIRAKILAN_DERSLER:
            return
        simdi = (simdi or dt.datetime.now()).isoformat(timespec="minutes")
        with self.takip_kilit:
            liste = self._takip_oku()
            if any(x["kod"] == kod and _normal(x["odev"]) == _normal(odev) for x in liste):
                return
            liste.append({"kod": kod, "odev": odev, "teslim": teslim_iso, "eklendi": simdi, "sonraki": simdi,
                          "arama": 0, "aktif": True})
            self._takip_yaz(liste)
        log(f"ödev takibe alındı: {kod} — {odev} (son teslim {teslim_iso})")

    def odev_takip(self, simdi=None):
        """Zamanı gelen takipleri bul; durduysa kapat. Yazılacaksa listeyi döndürür ("arama" alanı = kaçıncı mesaj)."""
        simdi = simdi or dt.datetime.now()
        with eylemler._kilit:
            olaylar = eylemler.oku(eylemler.TAKVIM, {"olaylar": []}).get("olaylar", [])
        with self.takip_kilit:
            liste, sirada, degisti = self._takip_oku(), [], False
            for x in liste:
                if not x.get("aktif"):
                    continue
                o = eylemler.teslim_bul(olaylar, x["odev"], x["kod"]) or {}
                try:
                    gecti = dt.datetime.fromisoformat(x["teslim"]) < simdi
                except (TypeError, ValueError):
                    gecti = False
                if o.get("durum") in TAKIP_BITTI or gecti:
                    x.update(aktif=False, bitis=o.get("durum") or "teslim geçti")
                    degisti = True
                    log(f"ödev takibi bitti: {x['kod']} — {x['odev']} ({x['bitis']})")
                elif dt.datetime.fromisoformat(x["sonraki"]) <= simdi:
                    sirada.append(x)
            if sirada and not sessiz_mi(simdi):
                for x in sirada:
                    x["sonraki"] = sonraki_takip(simdi, x.get("teslim")).isoformat(timespec="minutes")
                    x["arama"] += 1
                degisti = True
            else:
                sirada = []
            if degisti:
                self._takip_yaz(liste)
        return [dict(x) for x in sirada]

    @staticmethod
    def odev_dugmeleri(kod, odev):
        k = telegram.dugme_verisi({"tur": "odev", "kod": kod, "odev": odev})
        return [[("Başladım", f"o:bas:{k}"), ("Değerlendir", f"o:deg:{k}")],
                [("Yarın hatırlat", f"o:yar:{k}"), ("Takibi durdur", f"o:dur:{k}")]]

    def odev_yaz(self, sirada):
        for x in sirada:
            try:
                teslim = eylemler.tarih_metni(x["teslim"])
            except (TypeError, ValueError):
                teslim = str(x["teslim"]).replace("T", " ")  # Learn'den bozuk tarih gelmişse
            odev = f"{x['kod']} — {telegram.duz_metin(x['odev'])}"  # ödev adı Learn'den gelir (güvenilmeyen)
            if x["arama"] == 1:
                metin = (f"**Yeni ödev**\n{odev}\nSon teslim: {teslim}\n"
                         f"Başladığınızda haber verin, o zamana kadar {aralik_metni()} hatırlatacağım.")
            else:
                metin = f"{odev} ödevine başladınız mı?\nSon teslim: {teslim}"
            telegram.gonder(metin, dugmeler=self.odev_dugmeleri(x["kod"], x["odev"]))
            log(f"ödev takibi mesajı ({x['arama']}.): {x['kod']} — {x['odev']}")

    def odev_basladi(self, kod, odev):
        """'Başladım' düğmesi: takvimde Başlandı yap (kayıt varsa) ve takibi kapat. Dönüş: kısa sonuç."""
        sonuc = []
        try:
            sonuc.append(eylemler.uygula({"tip": "teslim_guncelle", "baslik": odev, "ders": kod, "durum": "Başlandı"}))
        except ValueError as e:
            sonuc.append(str(e))
        takip_durdur(kod, odev)
        return "; ".join(sonuc)

    def _dongu(self):
        son_takip = 0.0
        # ilk gecikme kontrolü açılıştan 15 dk sonra: posta gözcüsü bağlanıp bir kez yoklamış olsun (servis
        # `posta`yı gözcüler kurulunca bağlıyor)
        son_kontrol = time.time() - 1800 + 15 * 60
        while True:
            time.sleep(10)
            if time.time() - son_takip > 60:
                son_takip = time.time()
                try:
                    sirada = self.odev_takip()
                    if sirada:
                        self.odev_yaz(sirada)
                except Exception as e:
                    log("ödev takibi hatası:", repr(e))
            if time.time() - son_kontrol > 1800:
                son_kontrol = time.time()
                try:
                    self.gecikme_kontrol()
                except Exception as e:
                    log("gecikme kontrolü hatası:", repr(e))
