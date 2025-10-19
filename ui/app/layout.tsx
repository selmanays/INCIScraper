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
  const themeScript = `
    (() => {
      const storageKey = 'theme';
      try {
        const stored = window.localStorage.getItem(storageKey);
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        const theme = stored === 'light' || stored === 'dark' ? stored : prefersDark ? 'dark' : 'light';
        if (theme === 'dark') {
          document.documentElement.classList.add('dark');
        } else {
          document.documentElement.classList.remove('dark');
        }
      } catch (error) {}
    })();
  `;

  return (
    <html lang="tr" suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        {children}
      </body>
    </html>
  );
}
