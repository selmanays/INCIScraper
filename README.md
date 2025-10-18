# INCIScraper

INCIScraper, [INCIDecoder](https://incidecoder.com) üzerindeki marka, ürün ve
bileşen verilerini toplamak için tasarlanmış uçtan uca bir komut satırı
toolkit'idir. Uygulama; esnek bir HTML ayrıştırıcısı, kesintiye dayanıklı bir
pipeline ve hız/etik dengesi gözeten ağ katmanıyla tamamlanmış tam özellikli bir
scraper sunar.

## Öne Çıkan Özellikler

- **Üç aşamalı pipeline:** Markaları listeleyip kaydeder, her marka için ürün
  sayfalarını dolaşır ve ürün detaylarını (tanım, bileşen listeleri, görseller,
  hashtag öne çıkarmaları vb.) veri tabanına işler.
- **Kaldığı yerden devam etme:** Çalışma durumu `metadata` tablosunda saklandığı
  için kesilen oturumlar marka, ürün ve ürün detayı adımlarında otomatik olarak
  kaldığı yerden devam eder.【F:src/inciscraper/scraper.py†L322-L401】【F:src/inciscraper/scraper.py†L351-L603】
- **Dayanıklı veritabanı şeması:** Scraper açılışta gerekli tabloları oluşturur,
  eksik sütunları ekler ve beklenmeyen yapıları temizleyerek veri tutarlılığı
  sağlar.【F:src/inciscraper/scraper.py†L627-L905】
- **Otomatik durum sıfırlama:** Ürün tablosu temizlendiğinde marka
  ``products_scraped`` bayrakları ve ``sqlite_sequence`` sayaçları otomatik
  olarak sıfırlanır; böylece yeniden tarama hatasız başlar.【F:src/inciscraper/scraper.py†L351-L480】【F:src/inciscraper/scraper.py†L734-L812】
- **Bağımlılık dostu HTML ayrıştırıcı:** `html.parser` üzerine kurulu özel DOM
  katmanı BeautifulSoup benzeri bir API sunarak ek bağımlılıklara gerek
  bırakmaz.【F:src/inciscraper/parser.py†L1-L159】【F:src/inciscraper/parser.py†L321-L414】
- **Ağ hatası toleransı:** DNS sorunlarında alternatif alan adlarına geçer,
  DNS-over-HTTPS ile IP çözer ve gerekirse doğrudan IP üzerinden TLS bağlantısı
  kurar.【F:src/inciscraper/scraper.py†L2066-L2396】
- **Görsel optimizasyonu:** Ürün görselleri indirilip WebP (mümkünse lossless)
  olarak sıkıştırılır; Pillow bulunamazsa orijinal veri saklanır.【F:src/inciscraper/scraper.py†L2408-L2475】
- **Uzun bileşen açıklamaları:** `ingredients.details_text` sütunu sınırsız
  uzunlukta metni destekleyecek şekilde otomatik olarak yükseltilir; geçmiş
  veriler kaybedilmeden yeni içerikler tam hâliyle saklanır.【F:src/inciscraper/scraper.py†L627-L905】【F:src/inciscraper/scraper.py†L782-L859】
- **Akıllı yeniden tarama:** Varsayılan çalıştırma tüm marka, ürün ve detay
  sayfalarını baştan kontrol eder; içerikte değişiklik yoksa satırlar
  yeniden yazılmaz, yalnızca `last_checked_at` damgaları güncellenir. Değişiklik
  tespit edildiğinde ise ilgili kayıtlar güncellenip `last_updated_at`
  güncellenir.【F:main.py†L100-L168】【F:src/inciscraper/scraper.py†L1065-L1293】【F:src/inciscraper/scraper.py†L1520-L1655】【F:src/inciscraper/scraper.py†L1918-L2013】

## Gereksinimler

- Python 3.11 veya üzeri
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
damgaları güncellenir.【F:main.py†L100-L168】【F:src/inciscraper/scraper.py†L1520-L1655】

### Örnek Veri Tabanı Oluşturma

Uygulamanın çalışma zincirini hızlıca doğrulamak için yalnızca üç marka ve her
markadan bir ürün içeren örnek bir veritabanı oluşturabilirsiniz. Komut,
veritabanı dosya adınızın başına otomatik olarak `sample_` öneki ekler.

```bash
python main.py --sample-data --db incidecoder.db
```

Bu işlem ilgili markaların ürün detaylarını da kazır ve sonuçları sıkıştırılmış
görsellerle birlikte kaydeder.【F:main.py†L96-L118】【F:src/inciscraper/scraper.py†L253-L298】

## Komut Satırı Parametreleri

| Parametre | Açıklama |
| --- | --- |
| `--db PATH` | Kullanılacak SQLite dosyasının yolu (varsayılan `incidecoder.db`). |
| `--images-dir DIR` | Görsellerin kaydedileceği dizin (varsayılan `images`). |
| `--base-url URL` | Gerekirse farklı bir INCIDecoder tabanı kullanın. |
| `--alternate-base-url URL` | DNS hatalarında denenecek ek taban URL'ler; birden fazla kez verilebilir. |
| `--step {all,brands,products,details}` | Pipeline'ın belirli bir bölümünü çalıştırır. |
| `--max-pages N` | Marka listelemede çekilecek sayfa sayısını sınırlar. |
| `--resume/--no-resume` | `all` adımı çalışırken tamamlanmış aşamaları atlayıp atlamayacağını belirler (varsayılan `--no-resume`). |
| `--log-level LEVEL` | Günlük çıktısının ayrıntı düzeyini ayarlar. |
| `--sample-data` | Tüm pipeline yerine üç marka × bir ürünlük örnek veritabanı oluşturur (`sample_` öneki eklenir). |

Negatif veya sıfır `--max-pages` değerleri kabul edilmez; CLI uygun hatayı
verir.【F:main.py†L55-L69】

## Veritabanı Yapısı

Scraper aşağıdaki tabloları oluşturur ve kontrol eder:

- **brands** – Marka adı, özgün URL, ürünlerinin işlenip işlenmediğini gösteren
  bayrak ile `last_checked_at`/`last_updated_at` damgaları.
- **products** – Marka ilişkisi, ürün adı, açıklama, görsel yolu, JSON
  formatında bileşen referansları (`ingredient_references_json`) ve detay
  verilerinin en son ne zaman kontrol edildiğine dair damgalar.
- **ingredients** – Bileşenin derecelendirmesi, "başka adları", resmi COSING
  bilgileri ve detay bölümünün HTML içeriği dahil kapsamlı metrikler ile son
  kontrol/güncelleme zamanları.
- **metadata** – Kaldığı yerden devam edebilmek için kullanılan yardımcı
  anahtar/değer deposu.

Schema ve kolonlar uygulama tarafından doğrulanır; beklenmeyen tablo veya
sütunlar tespit edilirse kaldırılır.【F:src/inciscraper/scraper.py†L627-L905】

## Nasıl Çalışır?

1. **Markalar:** `/brands` sayfalarındaki bağlantıları tarar, marka adlarını ve
   URL'lerini kaydeder. Sayfa sayısı bilinmiyorsa metadata kayıtları ile takip
   edilir.【F:src/inciscraper/scraper.py†L351-L480】【F:src/inciscraper/scraper.py†L322-L401】
2. **Ürünler:** Her marka için paginasyonlu ürün listelerini dolaşır, hata
   durumlarında alternatif URL denemeleri yapar ve yeni ürünleri ekler veya
   isimleri günceller.【F:src/inciscraper/scraper.py†L481-L603】
3. **Ürün Detayları:** Ürün sayfalarını indirir, bileşen listelerini, fonksiyon
   tablolarını, hashtag öne çıkanlarını ve varsa "discontinued" uyarılarını
   ayrıştırır; ardından görselleri indirip optimize eder.【F:src/inciscraper/scraper.py†L1298-L1655】【F:src/inciscraper/scraper.py†L2408-L2475】
4. **Bileşen Detayları:** Ürünlerde görülen her bileşenin kendi sayfasını
   ziyaret eder, derecelendirme bilgilerini ve COSING bölümünü çıkarır, ilgili
   bağlantıları normalize eder.【F:src/inciscraper/scraper.py†L1918-L2013】

Bu adımların tümü idempotent olduğundan scraper'ı tekrar çalıştırmak veri
tekrarı oluşturmaz.

## Proje Yapısı

```
INCIScraper/
├── main.py                # Komut satırı arayüzü
├── README.md              # Bu dosya
└── src/inciscraper/
    ├── __init__.py        # Paket giriş noktası
    ├── parser.py          # Özel HTML parser & yardımcılar
    └── scraper.py         # Scraper iş mantığı ve veri katmanı
```

## Geliştirme İpuçları

- Scraper sürekli log yazar; `--log-level DEBUG` ile ayrıntıları görebilirsiniz.
- Ürün veya marka ayrıştırmasında değişiklik yaparken gerçek HTML'yi kaydedip
  `parse_html` fonksiyonuna vererek hızlıca manuel testler yapabilirsiniz.
- Ağa erişimin olmadığı durumlarda sahte HTML yanıtları dönen bir test sunucusu
  kurarak scraper'ı doğrulayabilirsiniz.

## Lisans

Bu depo eğitim amaçlıdır; gerçek dünya kullanımında INCIDecoder'ın kullanım
koşullarını ve robots.txt dosyasını dikkate alınız.
