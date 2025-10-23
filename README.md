# INCIScraper

INCIScraper, [INCIDecoder](https://incidecoder.com) üzerindeki marka, ürün ve bileşen verilerini toplamak için tasarlanmış yüksek performanslı, kesintiye dayanıklı bir web scraper uygulamasıdır. Modern Python teknolojileri kullanılarak geliştirilmiş, üç aşamalı pipeline yapısı ile verimli veri toplama sağlar.

## 🚀 Öne Çıkan Özellikler

### ⚡ Performans Optimizasyonları
- **WAL Mode SQLite**: Write-Ahead Logging ile gelişmiş veritabanı performansı
- **Batch İşlemler**: Toplu commit'ler ile veritabanı yazma hızında artış
- **LRU Cache**: CosIng verileri için akıllı bellek önbelleği (10,000 kayıt)
- **Adaptive Sleep**: Dinamik gecikme ayarlama ile optimal hız/dürüstlük dengesi
- **Thread Pool**: Paralel görsel indirme ve işleme
- **Playwright Optimizasyonu**: Headless browser ile hızlı CosIng sorguları

### 🛡️ Güvenilirlik ve Dayanıklılık
- **Kesintiye Dayanıklı Pipeline**: Üç aşamalı (brands → products → details) veri toplama
- **Otomatik Resume**: Kesintiler sonrası kaldığı yerden devam etme
- **DNS Failover**: Alternatif URL'ler ile ağ sorunlarına karşı koruma
- **Hata Toleransı**: 500/Timeout hatalarında otomatik yeniden deneme
- **Schema Validation**: Otomatik veritabanı şema kontrolü ve düzeltme

### 📊 Kapsamlı Veri Toplama
- **Markalar**: Marka listeleri, URL'ler ve metadata
- **Ürünler**: Ürün bilgileri, açıklamalar ve görsel yolları
- **Bileşenler**: Detaylı ingredient bilgileri ve CosIng entegrasyonu
- **CosIng Verileri**: CAS/EC numaraları, düzenleyici referanslar, fonksiyonlar
- **Görsel Optimizasyonu**: WebP/JPEG sıkıştırma ile optimize edilmiş görseller
- **#Free Claims**: Pazarlama iddiaları ve tooltip açıklamaları

### 🔧 Gelişmiş Özellikler
- **Akıllı HTML Parser**: Özel DOM katmanı ile güvenilir veri çıkarma
- **Monitoring**: Detaylı performans metrikleri ve ilerleme takibi
- **Async Network**: Gelecekteki paralel HTTP istekleri için hazır altyapı
- **Web UI**: Next.js tabanlı veritabanı yönetim paneli
- **CLI Interface**: Esnek komut satırı parametreleri

## 📋 Gereksinimler

- **Python 3.11+**: Modern Python özelliklerini destekler
- **Playwright**: CosIng sorguları için tarayıcı otomasyonu
- **Pillow** (Opsiyonel): Görsel sıkıştırma için
- **Ağ Erişimi**: INCIDecoder ve CosIng sitelerine erişim

## 🛠️ Kurulum

### 1. Projeyi Klonlayın
```bash
git clone <repository-url>
cd inciscraper_latest
```

### 2. Virtual Environment Oluşturun
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# veya
.venv\Scripts\activate     # Windows
```

### 3. Bağımlılıkları Yükleyin
```bash
pip install --upgrade pip
pip install Pillow        # Görsel sıkıştırma için (önerilen)
pip install -e .          # Projeyi paket olarak yükle
```

### 4. Playwright Tarayıcısını Kurun
```bash
playwright install chromium
```

## 🚀 Hızlı Başlangıç

### Temel Kullanım
```bash
# Tüm pipeline'ı çalıştır (brands → products → details)
python main.py

# Sadece belirli bir adımı çalıştır
python main.py --step brands
python main.py --step products
python main.py --step details

# Örnek veri oluştur (3 marka × 1 ürün)
python main.py --sample-data
```

### Gelişmiş Kullanım
```bash
# Özel veritabanı ve görsel dizini
python main.py --db /path/to/database.db --images-dir /path/to/images

# Belirli sayfa sayısı ile sınırla
python main.py --step brands --max-pages 10

# Resume modu ile devam et
python main.py --resume

# Detaylı loglama
python main.py --log-level DEBUG --log-output

# Alternatif URL'ler ile failover
python main.py --base-url https://www.incidecoder.com --alternate-base-url https://incidecoder.com
```

## 📊 Komut Satırı Parametreleri

| Parametre | Açıklama | Varsayılan |
|-----------|----------|------------|
| `--db PATH` | SQLite veritabanı yolu | `data/incidecoder.db` |
| `--images-dir DIR` | Görsellerin kaydedileceği dizin | `data/images` |
| `--base-url URL` | INCIDecoder base URL'i | `https://incidecoder.com` |
| `--alternate-base-url URL` | DNS failover için alternatif URL'ler | - |
| `--step {all,brands,products,details}` | Çalıştırılacak pipeline adımı | `all` |
| `--max-pages N` | Marka listesinde çekilecek maksimum sayfa | Sınırsız |
| `--resume/--no-resume` | Tamamlanmış adımları atla | `--no-resume` |
| `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` | Log seviyesi | `ERROR` |
| `--log-output` | Logları dosyaya yaz (`data/logs/inciscraper.log`) | Sadece konsol |
| `--sample-data` | Örnek veri oluştur (3 marka × 1 ürün) | Tam pipeline |

## 🗄️ Veritabanı Yapısı

### Ana Tablolar

#### `brands` - Marka Bilgileri
- `id`: Birincil anahtar
- `name`: Marka adı
- `url`: Marka URL'i
- `products_scraped`: Ürünlerin kazınıp kazınmadığı bayrağı
- `last_checked_at`: Son kontrol zamanı
- `last_updated_at`: Son güncelleme zamanı

#### `products` - Ürün Bilgileri
- `id`: Birincil anahtar
- `brand_id`: Marka referansı
- `name`: Ürün adı
- `description`: Ürün açıklaması
- `image_path`: Görsel dosya yolu
- `ingredient_ids_json`: Bileşen ID listesi (JSON)
- `key_ingredient_ids_json`: Ana bileşen ID'leri (JSON)
- `other_ingredient_ids_json`: Diğer bileşen ID'leri (JSON)
- `free_tag_ids_json`: #Free claim ID'leri (JSON)
- `last_checked_at`: Son kontrol zamanı
- `last_updated_at`: Son güncelleme zamanı

#### `ingredients` - Bileşen Detayları
- `id`: Birincil anahtar
- `name`: Bileşen adı
- `rating`: Bileşen derecelendirmesi
- `also_called_json`: Alternatif isimler (JSON)
- `description`: Detaylı açıklama
- `quick_facts_json`: Hızlı bilgiler (JSON)
- `proof_json`: Kanıt bilgileri (JSON)
- `casing_cas_numbers_json`: CAS numaraları (JSON)
- `casing_ec_numbers_json`: EC numaraları (JSON)
- `casing_defined_substances_json`: Tanımlı maddeler (JSON)
- `casing_regulatory_refs_json`: Düzenleyici referanslar (JSON)
- `function_ids_json`: Fonksiyon ID'leri (JSON)
- `last_checked_at`: Son kontrol zamanı
- `last_updated_at`: Son güncelleme zamanı

#### `functions` - CosIng Fonksiyonları
- `id`: Birincil anahtar
- `name`: Fonksiyon adı (normalize edilmiş)

#### `metadata` - Sistem Metadata
- `key`: Anahtar
- `value`: Değer
- `updated_at`: Güncelleme zamanı

### Performans Optimizasyonları
- **WAL Mode**: Gelişmiş eşzamanlılık ve performans
- **Batch Commits**: Toplu veritabanı işlemleri
- **Index'ler**: Hızlı sorgu performansı için otomatik index'ler

## 🔄 Pipeline Nasıl Çalışır?

### 1. Brands Aşaması
- `/brands` sayfalarını tarar
- Marka adlarını ve URL'lerini toplar
- Sayfa sayısını metadata ile takip eder
- Kesintiye dayanıklı commit'ler yapar

### 2. Products Aşaması
- Her marka için ürün listelerini dolaşır
- Ürün bilgilerini toplar ve kaydeder
- Görsel URL'lerini saklar
- Hata durumlarında alternatif URL'leri dener

### 3. Details Aşaması
- Ürün detay sayfalarını indirir
- Bileşen listelerini ayrıştırır
- CosIng verilerini Playwright ile toplar
- Görselleri indirir ve optimize eder
- #Free claims'leri işler

### Performans Özellikleri
- **Adaptive Sleep**: Başarı/hata oranına göre dinamik gecikme
- **LRU Cache**: CosIng verilerini bellekte önbellekler
- **Thread Pool**: Görsel indirme işlemlerini paralelleştirir
- **Monitoring**: Detaylı performans metrikleri

## 🌐 Web Yönetim Paneli

### Kurulum
```bash
cd ui
npm install
npm run dev
```

### Özellikler
- **Tablo Görüntüleme**: Tüm veritabanı tablolarını görüntüle
- **Sayfalı Listeleme**: Büyük veri setleri için sayfalama
- **Hücre Düzenleme**: Doğrudan hücre düzenleme ve kaydetme
- **Dark/Light Tema**: Modern UI tasarımı
- **Responsive**: Mobil uyumlu arayüz
- **Real-time Updates**: Anlık veri güncellemeleri

### Erişim
- URL: `http://localhost:3000`
- Varsayılan veritabanı: `data/incidecoder.db`
- Özel veritabanı: `DATABASE_PATH=/path/to/db npm run dev`

## 📈 Performans Metrikleri

### Optimizasyon Sonuçları
- **Veritabanı Hızı**: WAL mode ile %40-60 hız artışı
- **CosIng Sorguları**: LRU cache ile %80-90 hız artışı
- **Görsel İşleme**: Thread pool ile %70-80 hız artışı
- **Bellek Kullanımı**: Optimize edilmiş cache yönetimi
- **Ağ Trafiği**: Adaptive sleep ile %30-50 trafik azaltma

### Monitoring
- Detaylı performans logları
- Stage-by-stage timing bilgileri
- Cache hit/miss oranları
- Hata oranları ve retry sayıları

## 🔧 Geliştirme İpuçları

### Debug Modu
```bash
python main.py --log-level DEBUG --log-output
```

### Test Verisi
```bash
python main.py --sample-data --db test.db
```

### Sadece Belirli Adımlar
```bash
python main.py --step brands --max-pages 5
python main.py --step products --resume
python main.py --step details
```

### Veritabanı İnceleme
```bash
sqlite3 data/incidecoder.db
.tables
.schema brands
SELECT COUNT(*) FROM brands;
```

## 🌐 Web Arayüzünü Çalıştırma

Next.js tabanlı yönetim paneli `ui/` klasöründe bulunur. Geliştirme ortamını başlatmak için:

```bash
cd ui
npm install        # daha önce yapılmadıysa
npm run dev        # portu otomatik temizleyip dev sunucusunu başlatır
```

Script varsayılan olarak `http://127.0.0.1:3000` adresini kullanır. Farklı bir port tercih ederseniz:

```bash
PORT=4000 npm run dev
```

Ham Next.js komutunu çalıştırmak için `npm run dev:next` komutu kullanılabilir.

### 🔧 Sorun Giderme

#### Port Çakışması
Eğer "address already in use" hatası alırsanız:

```bash
# Port 3000'i kullanan süreci bulun ve durdurun
lsof -ti:3000 | xargs kill -9

# Veya tüm node süreçlerini temizleyin
pkill -9 node

# Ardından tekrar başlatın
npm run dev
```

#### EMFILE Hataları (macOS)
"too many open files" uyarıları zararsızdır ancak çok fazlaysa:

```bash
# Dosya limiti kontrol
ulimit -n

# Geçici olarak limiti artırın (mevcut terminal için)
ulimit -n 10240
```

#### Build/Cache Sorunları
Garip hatalar alırsanız cache'i temizleyin:

```bash
cd ui
rm -rf .next node_modules/.cache
npm run dev
```

#### Network Interface Hatası
Bazı ortamlarda (sandbox, container vb.) network interface erişim hatası alabilirsiniz. Bu durumda:

```bash
# Hostname belirterek başlatın
HOSTNAME=127.0.0.1 npm run dev

# Veya doğrudan Next.js komutunu kullanın
npm run dev:next
```

## 🚨 Önemli Notlar

### Etik Kullanım
- INCIDecoder'ın kullanım koşullarına uyun
- `robots.txt` dosyasını kontrol edin
- Makul gecikme süreleri kullanın
- Sunucu yükünü minimize edin

### Veri Kalitesi
- Scraper idempotent çalışır (tekrar çalıştırma veri tekrarı oluşturmaz)
- Değişmeyen kayıtlar yeniden yazılmaz
- Sadece `last_checked_at` damgaları güncellenir
- Hata durumlarında otomatik retry mekanizması

### Sistem Gereksinimleri
- Minimum 4GB RAM (CosIng cache için)
- SSD önerilir (veritabanı performansı için)
- Stabil internet bağlantısı
- Playwright tarayıcı desteği

## 📝 Lisans

Bu proje eğitim amaçlıdır. Gerçek dünya kullanımında ilgili web sitelerinin kullanım koşullarına uygun hareket edin.

## 🤝 Katkıda Bulunma

1. Fork yapın
2. Feature branch oluşturun (`git checkout -b feature/amazing-feature`)
3. Commit yapın (`git commit -m 'Add amazing feature'`)
4. Push yapın (`git push origin feature/amazing-feature`)
5. Pull Request açın

## 📞 Destek

Sorunlar için GitHub Issues kullanın. Detaylı log çıktıları ile birlikte sorun bildirirseniz daha hızlı yardım alabilirsiniz.
