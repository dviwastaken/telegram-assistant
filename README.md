# Telegram Assistant

Telegram üzerinden çalışan, kendi bilgisayarınızda barındırılan kişisel asistan. Hatırlatmaları, okul/iş postasından
çıkan uyarıları, ödev ve teslim takibini ve arka plan işlerinin sonuçlarını Telegram'a yazar; aynı sohbete yazarak
takvim sorabilir, hatırlatma kurabilir, dosya ve fotoğraf gönderebilir, sesli mesajla konuşabilir ve bilgisayardaki
[Claude Code](https://claude.com/claude-code)'a görev verebilirsiniz.

> **English summary.** A self-hosted personal assistant that lives in a Telegram chat. It sends reminders (with
> snooze/done buttons, recurring rules, and a cloud fallback for when your PC is off), watches IMAP mailboxes, tracks
> assignments, transcribes voice messages with Whisper, and talks to a persistent `claude -p` process as its "brain".
> Actions the model proposes are validated by the service, and anything with side effects (tasks, deletions) needs a
> two-step approval via an inline button. Notion is the source of truth. The code base, prompts and UI strings are in
> Turkish; the package name `telefon` is Turkish for "phone" (the project started as a phone-call assistant).

## İçindekiler
- [Özellikler](#özellikler)
- [Mimari](#mimari)
- [Gereksinimler](#gereksinimler)
- [Kurulum](#kurulum)
- [Yapılandırma](#yapılandırma)
- [Çalıştırma](#çalıştırma)
- [Telegram komutları](#telegram-komutları)
- [Bulut hatırlatıcısı](#bulut-hatırlatıcısı)
- [Yerel kontrol arayüzü](#yerel-kontrol-arayüzü)
- [Güvenlik modeli](#güvenlik-modeli)
- [Testler](#testler)
- [Proje yapısı](#proje-yapısı)
- [Lisans](#lisans)

## Özellikler
- **Serbest yazışma.** "Yarın 9'da kütüphaneyi hatırlat", "bu hafta teslimim var mı?" gibi cümleler beyne gider; cevap
  yazıldıkça aynı mesajda güncellenir (akışlı cevap, `AKISLI_CEVAP=0` ile kapatılır).
- **Hatırlatmalar.** Her bildirimin altında *Tamam / 10 dk ertele / 1 saat ertele / Yarına ertele* düğmeleri bulunur.
  Tekrarlayan kurallar (`gunluk`, `haftaici`, `haftalik`, `aylik`) tek kayıt olarak tutulur; kaçırılan tekrarlar
  birikmez.
- **Brifingler ve planlama.** Gece ertesi gün planlanır; brifing saati ders/takvim yoğunluğuna göre seçilir.
- **Posta gözcüleri.** IMAP üzerinden okul postası (salt okunur: `EXAMINE` + `BODY.PEEK`) ve Gmail izlenir; önemli
  olanlar özetlenip uyarı olarak gönderilir.
- **Ödev takibi ve değerlendirme.** Teslim tarihleri izlenir, istenirse bir ödev yalnızca okuma araçlı bir Claude
  turuyla değerlendirilir; sonuç klasöre ve Telegram'a gelir.
- **Sesli mesaj.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) ile ayrı bir alt süreçte çevrilir;
  boşta kalınca GPU belleğini bırakır. Ses kaydı saklanmaz.
- **Dosya ve fotoğraf.** PDF/DOCX/PPTX/metin önizlemesi ve fotoğraflar beyne verilir; büyük görseller küçültülür,
  metinsiz PDF'lerin ilk sayfaları görüntü olarak okunur.
- **Arka plan işleri ve görevler.** Uzun işler arka planda yürür, sonuç Notion sayfası + Telegram özeti olarak gelir.
  Bilgisayardaki Claude Code'a görev vermek iki aşamalı onaya bağlıdır.
- **Haftalık hafıza.** Haftanın yazışması özetlenir ve sonraki oturumlara güvenilmeyen veri olarak verilir.
- **Sessiz saatler.** `SESSIZ_SAAT` aralığında önemli bildirimler sessiz gider; acil uyarılar ve sizin kurduğunuz
  hatırlatmalar her zaman sesli gelir.
- **Dayanıklı gönderim.** Gönderilemeyen mesaj kaybolmaz: üç deneme sonrası kalıcı kuyruğa alınır, dakikada bir
  yeniden denenir. Uzun mesajlar 4096 karakter sınırına göre bölünür.

## Mimari
```
Telegram ⇄ sohbet botu ⇄ telefon/telegram.py   getUpdates uzun yoklama, gönderim + kalıcı kuyruk, düğmeler
                          └─ sohbet.py         mesaj kuyruğu, eylem işaretleri, komutlar
                               └─ beyin.py     kalıcı `claude -p` süreci (stream-json)

servis.py ─┬─ zamanlayici.py   Notion hatırlatmaları → Telegram, brifingler
           ├─ notion.py        REST; 5 dakikada bir çeker, yerel JSON önbelleği tutar
           ├─ planlayici.py    gece planı ve brifing saati
           ├─ isler.py         ⟦İŞ⟧ arka plan işleri
           ├─ gorevler.py      ⟦GÖREV⟧ Claude Code görevleri (onaylı)
           ├─ dersler.py · odevler.py
           └─ gozculer/        okul_posta · gmail · sistem · degerlendir · uyari

bekci.py   servisi ayakta tutan gözcü (çökerse yeniden başlatır)
bulut/     Cloudflare Worker: bilgisayar kapalıyken zamanı geçen hatırlatmaları gönderir
```

Beyin, cevabının içine ⟦EYLEM⟧, ⟦İŞ⟧, ⟦GÖREV⟧, ⟦DERS_NOTU⟧ ve ⟦ODEV⟧ gibi işaretler yazar; servis bunları doğrular
ve uygular. Beynin kabuk ya da dosya aracı yoktur.

## Gereksinimler
- **Python 3.12+** (çekirdek servis yalnızca standart kütüphaneyi kullanır)
- **[Claude Code CLI](https://claude.com/claude-code)**, `PATH` üzerinde ve oturumu açık (`claude auth status`)
- Telegram'da [@BotFather](https://t.me/BotFather) ile oluşturulmuş bir bot
- **Notion** entegrasyonu ve takvim / hatırlatma / not veritabanları
- İsteğe bağlı: IMAP erişimi olan bir posta kutusu, Gmail uygulama şifresi, ses tanıma için (tercihen CUDA'lı) GPU,
  Cloudflare hesabı ve Node.js 22+ (bulut hatırlatıcısı için)
- Başlangıç betikleri Windows (Görev Zamanlayıcı) için yazılmıştır; çekirdek kod platformdan bağımsızdır

## Kurulum
```bash
git clone <depo-adresi> telegram-assistant
cd telegram-assistant

python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt        # isteğe bağlı özellikler için; çekirdek servis bağımlılıksız çalışır

cp .env.example .env                   # Windows: copy .env.example .env
# .env dosyasını açıp en az TELEGRAM_SOHBET_JETONU, TELEGRAM_SOHBET_ID ve NOTION_KEY değerlerini doldurun
```

Görevlerin çalışabileceği proje klasörlerini tanımlamak için `projeler.example.json` dosyasını `projeler.json` olarak
kopyalayıp düzenleyin (dosya `.gitignore` içindedir).

## Yapılandırma
Tüm ayarlar `.env` dosyasından ya da ortam değişkenlerinden okunur (ortam değişkeni önceliklidir). Eksiksiz liste ve
açıklamalar [`.env.example`](.env.example) içindedir. Başlıca gruplar:

| Grup | Anahtarlar |
|---|---|
| Telegram | `TELEGRAM_SOHBET_JETONU`, `TELEGRAM_SOHBET_ID`, `TELEGRAM_YEDEK_JETONU`, `TELEGRAM_YEDEK_SOHBET_ID` |
| Notion | `NOTION_KEY`, `NOTION_TAKVIM_DS`, `NOTION_HATIRLATMA_DS`, `NOTION_NOT_DS`, `NOTION_ANA_SAYFA` |
| Posta | `OKUL_EPOSTA_KULLANICI`, `OKUL_EPOSTA_SIFRE`, `OKUL_IMAP_SUNUCU`, `OKUL_LEARN_GONDEREN`, `GMAIL_KULLANICI`, `GMAIL_UYGULAMA_SIFRESI` |
| Beyin | `BEYIN_MODEL`, `SOHBET_MODEL`, `GOREV_MODEL`, `BEYIN_EFFORT`, `SOHBET_EFFORT`, `AKISLI_CEVAP` |
| Zamanlama | `SESSIZ_SAAT`, `PLAN_SAATI`, `HAFIZA_SAATI`, `TAKIP_SAAT`, `TAKIP_SAATLERI` |
| Ses | `SES_MODEL`, `SES_BOSTA_SN` |
| Bulut | `BULUT_URL`, `BULUT_JETON` |
| Hava | `HAVA_KONUM`, `HAVA_YER` |
| Klasörler | `TAKVIM_KLASORU`, `ORTAK_KLASOR`, `ODEV_KLASORU`, `PANOPTO_KLASORU`, `PANOPTO_ARAC_KLASORU` |

> **Not:** `.env` içinde etkin ama boş bırakılan bir satır (`ANAHTAR=`) varsayılan değeri geçersiz kılar. Kullanmadığınız
> isteğe bağlı anahtarları yorum satırı olarak bırakın.

Servis yalnızca `TELEGRAM_SOHBET_ID` ile eşleşen sohbetten gelen mesajları ve düğmeleri işler.

## Çalıştırma
```bash
python -m telefon        # servisi doğrudan başlatır
python bekci.py          # servisi ayakta tutan gözcüyle başlatır (çökerse ~5 sn içinde yeniden açar)
```

**Windows'ta oturum açılışında otomatik başlatma:** `kur-acilis.cmd` dosyasına çift tıklayın (ya da
`kur-acilis.ps1` betiğini çalıştırın). Betik `ClaudeTelefon` adlı bir zamanlanmış görev kaydeder; kaldırmak için:

```powershell
Unregister-ScheduledTask -TaskName "ClaudeTelefon" -Confirm:$false
```

Günlükler `kayitlar/` klasörüne yazılır (`servis.log`, `bekci.log`, `sohbet.jsonl`); 5 MB'ı geçen günlük `.1`'e döner.
Kod değiştirdiğinizde servis sürecini durdurmanız yeterlidir, gözcü yenisini başlatır.

## Telegram komutları
| Komut | Açıklama |
|---|---|
| `/yardim` | Komut ve kullanım özeti |
| `/hatirlatmalar` | Bekleyen hatırlatmalar, her birinin altında *İptal* düğmesi |
| `/takvim` | Yaklaşan takvim kayıtları |
| `/teslimler` | Yaklaşan teslimler (beyin kullanılmadan, doğrudan takvimden) |
| `/onaylar` | Bekleyen onay istekleri ve kalan süreleri |
| `/durum` | Servis, Telegram, kuyruk, Notion, bulut, gözcüler ve gece planı durumu |
| `/yeni` | Beyin oturumunu sıfırlar |

## Bulut hatırlatıcısı
`bulut/` klasörü, bilgisayarınız kapalıyken bile zamanı gelen hatırlatmaları göndermek için bir
[Cloudflare Worker](https://developers.cloudflare.com/workers/) içerir. Worker her dakika Notion'daki hatırlatma
veritabanına bakar; zamanı geçmiş ve hâlâ "Bekliyor" olanları Telegram'a yollar. Bilgisayardaki servis ile Worker aynı
hatırlatmayı iki kez göndermesin diye bir Durable Object (`Hakem`) hangisinin göndereceğini belirler. Servis ayrıca
buluta düzenli nabız atar; 15 dakika nabız gelmezse bulut tek bir "masaüstü yanıt vermiyor" uyarısı yazar.

Kurulum ve ayrıntılar için [`bulut/README.md`](bulut/README.md) dosyasına bakın. Kısaca:

```bash
cd bulut
npm install
# wrangler.jsonc içindeki NOTION_DS değerini kendi hatırlatma veritabanı kimliğinizle değiştirin
# (saat dilimi farkı dakika cinsinden TZ_DAKIKA ile ayarlanır; varsayılan 180 = UTC+3)
npx wrangler login
powershell -ExecutionPolicy Bypass -File .\kur.ps1   # sırları yazar ve dağıtır
```

`kur.ps1`, üretilen `BULUT_URL` ve `BULUT_JETON` değerlerini `.env` dosyasına yazar.

## Yerel kontrol arayüzü
Servis `127.0.0.1:7341` adresinde (`KONTROL_PORT`) küçük bir HTTP arayüzü açar. Yalnızca yerel makineden erişilebilir
ve her istek, proje kökündeki `kontrol.jeton` dosyasında (ilk açılışta üretilir) yazan değeri `X-Jeton` başlığında taşımalıdır.

| İstek | Açıklama |
|---|---|
| `POST /gonder` | `{"metin": "..."}` gövdesiyle Telegram'a mesaj gönderir (başka betiklerden bildirim için) |
| `GET /durum` | Servis durumunu JSON olarak döner |
| `POST /simule` | `{"mesaj": "..."}` ya da `{"dugme": "...", "mesaj_id": N}` ile Telegram'dan gelmiş gibi mesaj/düğme verir; **görev, göreve cevap ve silme onaylayamaz** |

## Güvenlik modeli
- **Tek kullanıcı.** Yalnızca yapılandırılan sohbetten gelen mesajlar işlenir; diğer sohbetler günlüğe yazılıp yok
  sayılır. Telegram bot sohbetleri uçtan uca şifreli değildir: parola, kart bilgisi gibi şeyleri yazmayın.
- **Beynin yetkisi sınırlıdır.** Sohbet beyninin kabuk ve dosya aracı yoktur (yalnızca web araması). Eylemleri işaretle
  önerir; servis doğrular ve uygular.
- **İki aşamalı onay.** Görev başlatma, göreve cevap ve takvimden silme hiçbir şeyi doğrudan uygulamaz; yalnızca bekleyen
  bir istek açar. Eylemin tam metni düz metin olarak gösterilir ve yalnızca sizin *Başlat / Gönder / Sil* düğmenize
  basmanız uygular. İstek 30 dakika geçerlidir, düğme tek kullanımlıktır ve "evet" yazmak hiçbir şeyi onaylamaz.
- **Güvenilmeyen içerik.** Dosya içerikleri, fotoğraftaki yazılar, iletilmiş mesajlar ve posta özetleri talimat sayılmaz;
  içlerindeki eylem işaretleri ve bölüm etiketleri etkisizleştirilir. Böyle bir tur yalnızca güvenli eylemleri uygular.
- **Posta salt okunur.** Okul kutusu `EXAMINE` + `BODY.PEEK` ile açılır; hiçbir ileti okundu işaretlenmez, taşınmaz ya da
  silinmez.
- **Kontrol arayüzü** yalnızca `127.0.0.1` üzerinde ve jetonla erişilebilir.
- **Sırlar depoda tutulmaz.** `.env`, `kontrol.jeton`, `projeler.json`, günlükler ve çalışma verisi `.gitignore`
  kapsamındadır. Bir jeton yanlışlıkla paylaşılırsa ilgili servisten iptal edip yenisini üretin.

## Testler
```bash
pip install -r requirements-dev.txt
python -m pytest test -q          # Telegram'a, Notion'a ya da Claude'a çıkmaz
node --test bulut/test/birim.test.mjs
```

Bulut katmanı için uçtan uca test (`bulut/test/uctan_uca.py`) yerel `wrangler dev` ile sahte Notion/Telegram sunucularına
karşı çalışır ve dışarı çıkmaz. `test/canli.py` ise çalışan bir servise ve gerçek bota karşı elle çalıştırılan bir
duman testidir; otomatik test paketine dahil değildir.

## Proje yapısı
```
telefon/          servis paketi (telegram, sohbet, beyin, zamanlayici, notion, planlayici, gozculer/ ...)
bekci.py          servis bekçisi
bulut/            Cloudflare Worker (src/, test/, wrangler.jsonc, kur.ps1)
istemler/         beyne verilen sistem istemleri
test/             pytest testleri
kur-acilis.*      Windows açılış görevi kurulumu
projeler.example.json   görev projeleri şablonu
.env.example      yapılandırma şablonu
```

## Lisans
[MIT](LICENSE)
