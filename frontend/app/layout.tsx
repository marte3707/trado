import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { SwRegistrar } from "@/components/SwRegistrar";
import { Providers } from "@/app/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trado",
  description: "Personal Hyperliquid signal dashboard",
  icons: {
    icon: "/csp.png",
    apple: "/csp.png",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body>
        <SwRegistrar />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
