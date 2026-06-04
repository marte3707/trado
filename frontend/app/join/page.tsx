import { LandingClient } from "@/components/landing/LandingClient";
import { API_BASE } from "@/lib/api";
import type { Metadata } from "next";

export const dynamic = "force-dynamic";

type BotInfo = {
  bot_username: string | null;
  configured: boolean;
};

async function loadBotInfo(): Promise<BotInfo> {
  try {
    const res = await fetch(`${API_BASE}/api/public/bot-info`, { cache: "no-store" });
    if (!res.ok) return { bot_username: null, configured: false };
    return (await res.json()) as BotInfo;
  } catch {
    return { bot_username: null, configured: false };
  }
}

export const metadata: Metadata = {
  title: "Trado · Free crypto signals on Hyperliquid",
  description:
    "Multi-strategy crypto signal engine. Free Telegram channel, no email, no promises.",
};

export default async function JoinPage({
  searchParams,
}: {
  searchParams: { ref?: string; lang?: string };
}) {
  const info = await loadBotInfo();
  const source = (searchParams.ref ?? "landing").slice(0, 64);
  const initialLang = searchParams.lang === "it" ? "it" : "en";
  return (
    <LandingClient
      botUsername={info.bot_username}
      configured={info.configured}
      source={source}
      initialLang={initialLang}
    />
  );
}
