import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "INCIScraper Veritabanı Arayüzü",
  description: "INCIScraper veritabanı kayıtlarını görüntüleyin ve düzenleyin.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="tr" suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground">
        {children}
      </body>
    </html>
  );
}
