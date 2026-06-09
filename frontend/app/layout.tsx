import type { Metadata } from "next";
import { SwRegistrar } from "@/components/SwRegistrar";
import { Providers } from "@/app/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trado",
  description: "Trade smarter. Grow further.",
  icons: {
    icon: "/brand/trado_telegram_avatar.png",
    apple: "/brand/trado_telegram_avatar.png",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body>
        <SwRegistrar />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
