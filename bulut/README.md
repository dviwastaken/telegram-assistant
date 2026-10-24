# Bulut hatırlatıcısı (claude-hatirlatici)

Bilgisayar kapalıyken ya da uykudayken hatırlatmaları Telegram'a yazan Cloudflare Worker. Servis açıkken hatırlatmayı
zamanında servis gönderir; Worker yalnızca servisin göndermediklerini yakalar.

```
Notion "Hatırlatma" ──(her dakika, cron)──► Worker ──► Telegram sohbet botu ──► kullanıcı
        ▲                                      │
        │                         Hakem (Durable Object): "kayıt@zaman" yalnızca bir kez sahiplenilir
        │                                      ▲
   servis (PC) ── gönderimden önce POST /sahiplen ┘
```

## Nasıl çalışıyor
- **Cron her dakika** Notion'da durumu **Bekliyor** olan kayıtları okur. Zamanı **2 dakikadan fazla** geçmiş
  (`GECIKME_DK`; servis açıksa o sürede zaten göndermiş olur) ve **12 saatten yeni** (`EN_ESKI_SAAT`; daha eskileri
  bilgisayar açılınca toplu gelir) kayıtları gönderir. **Brifingler atlanır**, onları servisteki beyin yazar.
- **Çift gönderim yok:** her gönderimden önce Hakem'den `<notion kimliği>@<yerel dakika>` anahtarı istenir. Anahtarı
  ilk isteyen gönderir. Servis de Notion'daki bir hatırlatmayı göndermeden önce `POST /sahiplen` ile sorar; Worker
  önce davrandıysa servis o hatırlatmayı göndermez, yalnızca yerelde gönderildi sayar. Sahiplenme 3 gün tutulur.
- Gönderimden sonra Notion güncellenir: tek seferlik kayıt **Gönderildi**, tekrarlayan kayıt sıradaki zamanına
  ilerler (`src/tekrar.js`; servisteki `telefon/tekrar.py` ile aynı kural ve aynı test örnekleri).
- Telegram'a gidemezse sahiplenme bırakılır, bir sonraki dakika yeniden denenir. Notion yazılamazsa sahiplenme
  durduğu için aynı zaman ikinci kez gitmez. Gönderen, Telegram'a gittiğini `teslim` olarak işaretler. Sahiplenilmiş ama
  5 dakika sonra hâlâ "Bekliyor" görünen kayıtta: teslim edilmişse Worker Notion'u kapatır ya da tekrarlıysa
  ilerletir; servis sahiplenip teslim edemediyse (çöktü, kapandı) Worker gönderimi devralır. Teslim edilmemiş bir
  hatırlatma hiçbir zaman "Gönderildi" yapılmaz (kurallar `src/hakem.js`, node testiyle sınanır); 12 saatten eski tekrarlı kayıt da
  göndermeden ilerletilir ki seri takılmasın. Bir taramada en çok 10 kayıt işlenir (ücretsiz plandaki alt istek sınırı).
- Servis kendi sahiplendiğini yeniden isterse yine alır (gönderemeden yeniden başladıysa hatırlatma kaybolmasın).
  Servis sahiplendiği hatırlatmayı Telegram'a gönderemezse kuyruğa almaz, `POST /birak` ile bırakır ve 20 sn sonra
  yeniden dener; bilgisayar o arada kapanırsa Worker gönderir. Zaman aşımında mesaj aslında gitmiş olabileceği için
  bu en fazla 3 tur sürer, sonra mesaj servisin kuyruğuna alınır. Gittiğinde servis `POST /teslim` bildirir.
- Açılışta servis ilk Notion çekişini bekler (en fazla 5 dk): bilgisayar kapalıyken bulutun gönderdikleri yerel
  önbellekte hâlâ "bekliyor" görünebilir. 12 saatten eski toplu listeye de yalnızca kimsenin göndermedikleri girer.
- Worker'ın mesajı servisin hatırlatmasıyla aynı biçimde (`zaman.js` `tarihMetni`, `duzMetin`; ortak örnekler
  `test/hatirlatma_ornekleri.json`). Altında **Tamam / 1 saat ertele / Yarına ertele** düğmeleri var (`hb:ok|60|y:<Notion kimliği>`). Düğmeleri
  bilgisayardaki servis işler: bilgisayar kapalıyken basılırsa Telegram tıklamayı saklar, servis açılınca işler.
  Erteleme işlendiği andan sayılır, mesaj 30 dakikadan eskiyse onayda söylenir; 24 saatten eski düğme işlenmez.
- **Nabız:** servis 5 dk'da bir `POST /nabiz`, temiz kapanışta ve uykuya geçerken `POST /kapaniyor` gönderir. Son nabız
  `NABIZ_ESIK_DK`'dan (15) eskiyse ve arada temiz kapanış yoksa cron **bir kez** "⚠️ Masaüstü N dakikadır yanıt
  vermiyor (son nabız HH:MM)…" yazar; nabız dönünce bir kez "✅ Masaüstü yeniden açık". Hiç nabız gelmediyse uyarı
  olmaz. Bekçinin yeniden başlatması (~5 sn) eşiğin çok altında. Kurallar `src/hakem.js`'te (node testli).
- Servis Worker'a ulaşamazsa hatırlatmayı hemen göndermez, 20 sn'de bir yeniden sorar; ilk denemeden 10 dk sonra
  hâlâ ulaşamıyorsa Notion'daki kayda bakar: kapanmış ya da ilerlemişse göndermez, hâlâ bekliyorsa gönderir
  (`telefon/zamanlayici.py` `BULUT_BEKLE`). 400/401/500 gibi cevaplar kurulum hatası sayılır: günlüğe yazılır ve
  Kullanıcıya bir kez Telegram uyarısı gider.

## Uç noktalar
| Yol | Ne yapar |
|---|---|
| `GET /` | sağlık (`claude-hatirlatici ok`) |
| `POST /sahiplen` `{"anahtar": "<32 hex>@YYYY-MM-DDTHH:MM"}` | `{"kazandi": true/false, "sahip": "servis"/"bulut"}` |
| `POST /birak` `{"anahtar": ...}` | servisin teslim edemediği sahiplenmeyi bırakır (`{"birakildi": ...}`) |
| `POST /teslim` `{"anahtar": ...}` | servisin sahiplendiği hatırlatma Telegram'a gitti (`{"teslim": ...}`) |
| `POST /nabiz` | servis ayakta (`{"acildi": ...}`) |
| `POST /kapaniyor` | temiz kapanış ya da uyku: bir sonraki nabıza kadar "sustu" uyarısı yok |
| `GET /durum` | son tarama, son olay (gönderim ya da hata) ve nabız özeti |

İkisi de `Authorization: Bearer <SERVIS_JETON>` ister (servisin `.env`'inde `BULUT_JETON`).

## Kurulum
```powershell
powershell -ExecutionPolicy Bypass -File .\bulut\kur.ps1
```
Cloudflare girişi yoksa tarayıcıda açar, Worker'ı yayınlar, `.env`'e `BULUT_URL` ve `BULUT_JETON` yazar, sırları
(`NOTION_KEY`, `TELEGRAM_JETON`, `TELEGRAM_SOHBET_ID`, `SERVIS_JETON`) `wrangler secret bulk` ile stdin'den yükler
(ekrana ve diske yazılmaz), `/durum`'u dener ve servisi yeniden başlatır. Yeniden çalıştırmak güvenli.
Sonra Telegram'da `/durum`: "Bulut hatırlatıcısı: son tarama N dk önce" satırı görünmeli.

Ücretsiz plan yeter: dakikada bir cron (günde 1.440 çalışma), her çalışmada bir Notion sorgusu ve bir Durable Object
yazması. Workers ücretsiz planı günde 100.000 istek veriyor.

## Testler (hiçbiri Cloudflare'a, Notion'a ya da Telegram'a çıkmaz)
- `node --test bulut/test/birim.test.mjs`: tekrar kuralı (servisle ortak örnekler), Notion tarihinin yerel dakikaya
  çevrilmesi, mesaj metni.
- `.venv\Scripts\python.exe bulut\test\uctan_uca.py`: `wrangler dev` + sahte Notion/Telegram sunucusu; vadesi gelen,
  gecikme payındaki, brifing, çok eski ve servisin sahiplendiği kayıtlar; Notion ve Telegram hataları; yetki.

wrangler `bulut\node_modules` altına `npm install` ile kurulur. Canlı akış: `wr tail`
(`npx wrangler tail`, bu klasörden).
