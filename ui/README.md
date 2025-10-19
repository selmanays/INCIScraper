# INCIScraper CRM Arayüzü

Bu dizin, `bundui/shadcn-admin-dashboard-free` deposundaki CRM paneli temel
alınarak oluşturulmuş Next.js 14 tabanlı bir arayüz içerir. Tasarım bileşenleri
Shadcn UI, Tailwind CSS ve Recharts üzerine kuruludur ancak uygulama
INCIScraper'ın satış takibi ihtiyaçlarına uyacak şekilde yeniden yapılandırıldı.

## Öne çıkanlar

- **CRM sayfası:** `/dashboard/crm` rotası, lead kaynak dağılımı, görev listesi,
  satış hunisi ve lead tablosu gibi bileşenleri statik verilerle sunar.
- **Marka uyarlamaları:** Kenar çubuğu ve üst menüdeki tüm CTA/Pro reklamları
  kaldırıldı, marka öğeleri INCIScraper kimliğine göre güncellendi.
- **Grafikler:** Recharts bileşenleri `ChartContainer` yardımcılarıyla temaya
  uygun renkleri otomatik olarak kullanır.
- **Dinamik avatarlar:** Kullanıcı avatarları isimlerden türeyen degrade arka
  planlar ve baş harflerle render edilir; depoda ikili varlık bulunmaz.

## Dizindeki önemli dosyalar

| Yol | Açıklama |
| --- | --- |
| `app/page.tsx` | Kök rotayı CRM paneline yönlendirir. |
| `app/dashboard/crm/page.tsx` | CRM görünümünün ana düzeni. |
| `app/dashboard/crm/cards/` | Grafikler, tablo ve metrik kartlarını içeren bileşenler. |
| `components/layout/` | Ortak şablon, kenar çubuğu ve üst bar bileşenleri. |
| `lib/routes-config.tsx` | Navigasyon yapılandırması. |

## Geliştirme

```bash
cd ui
npm install
npm run dev
```

Geliştirme sunucusu `http://localhost:3000` adresinde açılır. Arayüz tamamen
statik verilerle çalıştığından ek bir API veya veritabanı bağlantısına ihtiyaç
yoktur.

## Lisans ve kaynak

Şablon, [bundui/shadcn-admin-dashboard-free](https://github.com/bundui/shadcn-admin-dashboard-free)
projesinden uyarlanmıştır. Orijinal proje MIT lisansı altındadır.
