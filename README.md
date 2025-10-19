# INCIScraper

INCIScraper, [INCIDecoder](https://incidecoder.com) üzerindeki marka, ürün ve
bileşen verilerini toplamak için tasarlanmış uçtan uca bir komut satırı
toolkit'idir. Uygulama; esnek bir HTML ayrıştırıcısı, kesintiye dayanıklı bir
pipeline ve hız/etik dengesi gözeten ağ katmanıyla tamamlanmış tam özellikli bir
scraper sunar.

## Öne Çıkan Özellikler

- **Üç aşamalı pipeline:** Markaları listeleyip kaydeder, her marka için ürün
  sayfalarını dolaşır ve ürün detaylarını (tanım, bileşen listeleri, görseller,
  #free iddiaları ve kilit bileşen vurguları vb.) veri tabanına işler.【F:src/inciscraper/mixins/brands.py†L21-L169】【F:src/inciscraper/mixins/products.py†L21-L221】【F:src/inciscraper/mixins/details.py†L51-L137】
- **Kaldığı yerden devam etme:** Çalışma durumu `metadata` tablosunda saklandığı
  için kesilen oturumlar marka, ürün ve ürün detayı adımlarında otomatik olarak
  kaldığı yerden devam eder.【F:src/inciscraper/mixins/database.py†L98-L165】【F:src/inciscraper/scraper.py†L114-L138】
- **Dayanıklı veritabanı şeması:** Scraper açılışta gerekli tabloları oluşturur,
  eksik sütunları ekler ve beklenmeyen yapıları temizleyerek veri tutarlılığı
  sağlar.【F:src/inciscraper/mixins/database.py†L19-L233】
- **Otomatik durum sıfırlama:** Ürün tablosu temizlendiğinde marka
  ``products_scraped`` bayrakları ve ilgili metaveri otomatik olarak
  sıfırlanır; böylece yeniden tarama hatasız başlar.【F:src/inciscraper/mixins/database.py†L143-L166】
- **Bağımlılık dostu HTML ayrıştırıcı:** `html.parser` üzerine kurulu özel DOM
  katmanı BeautifulSoup benzeri bir API sunarak ek bağımlılıklara gerek
  bırakmaz.【F:src/inciscraper/parser.py†L1-L159】【F:src/inciscraper/parser.py†L321-L414】
- **Ağ hatası toleransı:** DNS sorunlarında alternatif alan adlarına geçer,
  DNS-over-HTTPS ile IP çözer ve gerekirse doğrudan IP üzerinden TLS bağlantısı
  kurar.【F:src/inciscraper/mixins/network.py†L49-L273】
- **Görsel optimizasyonu:** Ürün görselleri indirilip WebP (mümkünse lossless)
  olarak sıkıştırılır; Pillow bulunamazsa orijinal veri saklanır.【F:src/inciscraper/mixins/network.py†L328-L407】
- **Zengin bileşen içerikleri:** Detay metni paragrafların yanı sıra madde
  işaretli listeleri de koruyacak biçimde ayrıştırılır; Quick Facts ve "Show me
  some proof" bölümleri JSON olarak saklanır. Ingredient sayfalarının yeni
  `itemprop` tabanlı yerleşimleri de desteklenerek "Also-called", irritasyon/
  komedojenik değerleri ve anlatım metinleri eksiksiz toplanır. CosIng verileri
  artık Playwright ile resmi arama formu doldurularak alınır; CAS/EC
  numaraları, tanımlanan diğer maddeler ve düzenleyici referanslar temizlenip
  JSON dizileri şeklinde depolanır, fonksiyon adları ise baş harfleri büyük
  olacak biçimde `functions` tablosuna yazılıp ingredient kayıtlarına ID
  listeleriyle bağlanır. Slash (`/`) ile alternatif isimler içeren bileşenler
  CosIng'de otomatik olarak her varyant için sırayla sorgulanır; birleşik kayıt
  bulunamazsa ilgili varyantın kendisi tam eşleşme verdiğinde doğrudan o sonuç
  açılır. Böylece arayüz tek terimle sonuç vermediğinde bile veri
  kaçmaz.【F:src/inciscraper/mixins/details.py†L102-L448】【F:src/inciscraper/mixins/details.py†L726-L918】
- **CosIng önbelleği ve metrikler:** Resmi portaldan indirilen HTML yanıtları
  normalize edilmiş isim anahtarlarıyla SQLite tabanlı bir cache tablosunda
  saklanır ve çalışma süresi boyunca bellekte tutulur. Bozuk kayıtlar tespit
  edildiğinde otomatik olarak temizlenir; her arama için toplam süre ve hangi
  kaynaktan (bellek/disk/ağ) geldiği DEBUG loglarına yazılır. Böylece tekrar
  eden ürünlerde Playwright bekleme süreleri minimize edilir ve darboğazlar
  kolayca ölçülür.【F:src/inciscraper/mixins/details.py†L580-L667】【F:src/inciscraper/mixins/details.py†L669-L712】【F:src/inciscraper/mixins/database.py†L19-L52】
- **Vurguları bileşen kayıtlarına bağlama:** "Key Ingredients" ve "Other
  Ingredients" bölümlerinde listelenen öğeler ürünün ana bileşen listesiyle
  eşleştirilir ve sonuçlar JSON formatındaki kimlik listeleri olarak saklanır.【F:src/inciscraper/mixins/details.py†L157-L335】
- **Akıllı yeniden tarama:** Varsayılan çalıştırma tüm marka, ürün ve detay
  sayfalarını baştan kontrol eder; içerikte değişiklik yoksa satırlar
  yeniden yazılmaz, yalnızca `last_checked_at` damgaları güncellenir. Değişiklik
  tespit edildiğinde ise ilgili kayıtlar güncellenip `last_updated_at`
  güncellenir.【F:main.py†L100-L168】【F:src/inciscraper/mixins/brands.py†L131-L169】【F:src/inciscraper/mixins/products.py†L231-L336】【F:src/inciscraper/mixins/details.py†L205-L335】

## Gereksinimler

- Python 3.11 veya üzeri
- CosIng sorguları için [Playwright](https://playwright.dev/python/) ve en az
  bir tarayıcı ikilisi (`playwright install chromium` gibi bir komutla
  yüklenebilir).
- (Opsiyonel) Görsel sıkıştırma için [`Pillow`](https://python-pillow.org/).
  Kurulmaması durumunda scraper görselleri orijinal biçimleriyle kaydeder.
- Dış ağ erişimi (gerçek veri toplamak için gereklidir).

## Kurulum

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install Pillow  # Opsiyonel fakat tavsiye edilir
```

Projeyi paket olarak kullanmak için depo kökünde şu komutu çalıştırabilirsiniz:

```bash
pip install -e .
```

## Hızlı Başlangıç

Varsayılan davranış tüm pipeline'ı sırayla yürütür. Komut satırı arabirimi
`main.py` dosyasında yer alır ve `inciscraper.INCIScraper` sınıfını kullanır.

```bash
python main.py
```

Scraper başlarken veritabanındaki durumu özetler, ardından eksik adımları
çalıştırır ve sonunda bağlantıyı kapatır.【F:main.py†L63-L118】
Varsayılan mod `--no-resume` olduğu için tüm sayfalar her çalıştırmada baştan
taransa da değişmeyen kayıtlar yeniden yazılmaz; yalnızca son kontrol
damgaları güncellenir.【F:main.py†L100-L168】【F:src/inciscraper/mixins/brands.py†L131-L169】【F:src/inciscraper/mixins/products.py†L231-L336】【F:src/inciscraper/mixins/details.py†L205-L335】

Varsayılan çalışma sırasında tüm çıktı `data/` klasörü altında toplanır: veritabanı `data/incidecoder.db`, ürün görselleri `data/images/`, örnek veri görselleri `data/sample_images/` ve talep edilirse günlükler `data/logs/inciscraper.log` yoluna yazılır.【F:src/inciscraper/scraper.py†L33-L65】【F:main.py†L34-L133】

### Örnek Veri Tabanı Oluşturma

Uygulamanın çalışma zincirini hızlıca doğrulamak için yalnızca üç marka ve her
markadan bir ürün içeren örnek bir veritabanı oluşturabilirsiniz. Komut,
veritabanı dosya adınızın başına otomatik olarak `sample_` öneki ekler.

```bash
python main.py --sample-data --db data/incidecoder.db
```

Bu işlem ilgili markaların ürün detaylarını da kazır ve sonuçları sıkıştırılmış
görsellerle birlikte `data/sample_images/` dizinine kaydeder.【F:main.py†L96-L132】【F:src/inciscraper/scraper.py†L33-L114】

## Komut Satırı Parametreleri

| Parametre | Açıklama |
| --- | --- |
| `--db PATH` | Kullanılacak SQLite dosyasının yolu (varsayılan `data/incidecoder.db`). |
| `--images-dir DIR` | Görsellerin kaydedileceği dizin (varsayılan `data/images`). |
| `--base-url URL` | Gerekirse farklı bir INCIDecoder tabanı kullanın. |
| `--alternate-base-url URL` | DNS hatalarında denenecek ek taban URL'ler; birden fazla kez verilebilir. |
| `--step {all,brands,products,details}` | Pipeline'ın belirli bir bölümünü çalıştırır. |
| `--max-pages N` | Marka listelemede çekilecek sayfa sayısını sınırlar. |
| `--resume/--no-resume` | `all` adımı çalışırken tamamlanmış aşamaları atlayıp atlamayacağını belirler (varsayılan `--no-resume`). |
| `--log-level LEVEL` | Günlük çıktısının ayrıntı düzeyini ayarlar (varsayılan `ERROR`). |
| `--log-output` | Konsolun yanı sıra günlükleri `data/logs/inciscraper.log` dosyasına yazar. |
| `--sample-data` | Tüm pipeline yerine üç marka × bir ürünlük örnek veritabanı oluşturur (`sample_` öneki eklenir). |

Negatif veya sıfır `--max-pages` değerleri kabul edilmez; CLI uygun hatayı
verir.【F:main.py†L55-L69】

## Veritabanı Yapısı

Scraper aşağıdaki tabloları oluşturur ve kontrol eder:

- **brands** – Marka adı, özgün URL, ürünlerinin işlenip işlenmediğini gösteren
  bayrak ile `last_checked_at`/`last_updated_at` damgaları.【F:src/inciscraper/mixins/database.py†L26-L33】
- **products** – Marka ilişkisi, ürün adı, açıklama, görsel yolu, bileşen
  kimlikleri (`ingredient_ids_json`), öne çıkarılan bileşen kimlikleri
  (`key_ingredient_ids_json`, `other_ingredient_ids_json`), #free etiketlerinin
  kimlikleri (`free_tag_ids_json`) ve detay verilerinin en son ne zaman kontrol
  edildiğine dair damgalar.【F:src/inciscraper/mixins/database.py†L35-L51】【F:src/inciscraper/mixins/details.py†L205-L335】
- **ingredients** – Bileşenin derecelendirmesi, "başka adları", CosIng'den
  alınan CAS/EC numaraları, tanımlanmış diğer maddeler ve düzenleyici
  referanslar gibi veriler, Quick Facts / Show me some proof listeleri ve detay
  bölümünün metni; tümü son kontrol/güncelleme damgalarıyla birlikte saklanır.
  "Also-called" alanındaki değerler virgül ayraçlarından temizlenip JSON dizileri
  olarak saklanır; CosIng fonksiyon kimlikleri ayrıca `functions` tablosuna referanslanır.【F:src/inciscraper/mixins/database.py†L35-L70】【F:src/inciscraper/mixins/details.py†L520-L918】
- **functions** – CosIng fonksiyon sözlüğünü barındırır; normalize edilen
  fonksiyon adları küçük/büyük harf duyarsız eşleştirmeyle tekilleştirilir ve
  yalnızca isimler saklanır.【F:src/inciscraper/mixins/database.py†L26-L103】【F:src/inciscraper/mixins/details.py†L1247-L1338】
- **frees** – #alcohol-free gibi hashtag tarzı pazarlama iddialarını ve ilgili
  tooltip açıklamalarını saklar; ürünler bu tablodaki kimliklere bağlanır.【F:src/inciscraper/mixins/database.py†L80-L84】【F:src/inciscraper/mixins/details.py†L485-L520】
- **metadata** – Kaldığı yerden devam edebilmek için kullanılan yardımcı
  anahtar/değer deposu.【F:src/inciscraper/mixins/database.py†L86-L132】
- **cosing_cache** – CosIng sorgularının normalize edilmiş anahtarlarıyla
  saklanan HTML yanıtları, kullanılan arama terimi ve son güncelleme damgası.
  Uygulama bozulmuş kayıtları otomatik temizleyip yeni sonuçlarla
  günceller.【F:src/inciscraper/mixins/database.py†L19-L52】【F:src/inciscraper/mixins/details.py†L620-L667】

Schema ve kolonlar uygulama tarafından doğrulanır; beklenmeyen tablo veya
sütunlar tespit edilirse kaldırılır.【F:src/inciscraper/mixins/database.py†L223-L280】

## Nasıl Çalışır?

1. **Markalar:** `/brands` sayfalarındaki bağlantıları tarar, marka adlarını ve
   URL'lerini kaydeder. Sayfa sayısı bilinmiyorsa metadata kayıtları ile takip
   edilir.【F:src/inciscraper/mixins/brands.py†L21-L169】【F:src/inciscraper/mixins/database.py†L98-L166】
2. **Ürünler:** Her marka için paginasyonlu ürün listelerini dolaşır, hata
   durumlarında alternatif URL denemeleri yapar ve yeni ürünleri ekler veya
   isimleri günceller.【F:src/inciscraper/mixins/products.py†L21-L221】
3. **Ürün Detayları:** Ürün sayfalarını indirir, bileşen listelerini, fonksiyon
   tablolarını, hashtag öne çıkanlarını ve varsa "discontinued" uyarılarını
   ayrıştırır; ardından görselleri indirip optimize eder.【F:src/inciscraper/mixins/details.py†L51-L206】【F:src/inciscraper/mixins/network.py†L328-L380】
4. **Bileşen Detayları:** Ürünlerde görülen her bileşenin kendi sayfasını
   ziyaret eder, derecelendirme bilgilerini ve COSING bölümünü çıkarır, ilgili
   bağlantıları normalize eder.【F:src/inciscraper/mixins/details.py†L466-L918】

Bu adımların tümü idempotent olduğundan scraper'ı tekrar çalıştırmak veri
tekrarı oluşturmaz.

## Proje Yapısı

```
INCIScraper/
├── main.py                # Komut satırı arayüzü
├── README.md              # Bu dosya
├── src/inciscraper/       # Scraper paketinin kaynak kodu
└── ui/                    # shadcn-ui bileşenleriyle Next.js tabanlı yönetim paneli
```

## Web Arayüzü

Scraper veritabanını görsel olarak inceleyip düzenlemek için `ui/` dizininde
Next.js ve [shadcn/ui](https://ui.shadcn.com/) bileşenleriyle hazırlanmış bir
kontrol paneli yer alır. Arayüz SQLite veritabanındaki tabloları listeler,
satırları sayfalı olarak gösterir ve hücreleri doğrudan düzenleyip kaydetmeye
imkân tanır.

### Kurulum ve Çalıştırma

```bash
cd ui
npm install
npm run dev
```

Varsayılan olarak arayüz depo kökündeki `data/incidecoder.db` dosyasına bağlanır.
Farklı bir veritabanı kullanmak için `DATABASE_PATH` ortam değişkenini
tanımlayabilirsiniz:

```bash
DATABASE_PATH=/path/to/your.db npm run dev
```

Sunucu varsayılan olarak `http://localhost:3000` adresinde çalışır. Tarayıcıda
tablo seçici üzerinden veri tabanındaki tablolar arasında geçiş yapabilir,
hücreleri düzenledikten sonra **Kaydet** düğmesiyle toplu olarak
güncelleyebilirsiniz. Birincil anahtar sütunları koruma amacıyla yalnızca
okunur olarak gelir; diğer alanlar düzenlenebilir. Sorgular satır sayfa boyutu
ve sayfa numarasına göre sınırlanır.

## Geliştirme İpuçları

- Scraper sürekli log yazar; `--log-level DEBUG` ile ayrıntıları görebilirsiniz.
- Ürün veya marka ayrıştırmasında değişiklik yaparken gerçek HTML'yi kaydedip
  `parse_html` fonksiyonuna vererek hızlıca manuel testler yapabilirsiniz.
- Ağa erişimin olmadığı durumlarda sahte HTML yanıtları dönen bir test sunucusu
  kurarak scraper'ı doğrulayabilirsiniz.

## Lisans

Bu depo eğitim amaçlıdır; gerçek dünya kullanımında INCIDecoder'ın kullanım
koşullarını ve robots.txt dosyasını dikkate alınız.
