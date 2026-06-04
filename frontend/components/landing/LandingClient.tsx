"use client";

import { useEffect, useMemo, useState } from "react";

import { MESSAGES, type Lang } from "@/lib/landing-i18n";

type Props = {
  botUsername: string | null;
  configured: boolean;
  source: string;
  initialLang: Lang;
};

function buildBotLink(username: string | null, source: string): string | null {
  if (!username) return null;
  const cleaned = username.replace(/^@/, "");
  const safeSource = source.replace(/[^a-zA-Z0-9_]/g, "").slice(0, 64) || "landing";
  return `https://t.me/${cleaned}?start=${safeSource}`;
}

export function LandingClient({ botUsername, configured, source, initialLang }: Props) {
  const [lang, setLang] = useState<Lang>(initialLang);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const t = MESSAGES[lang];
  const botLink = useMemo(() => buildBotLink(botUsername, source), [botUsername, source]);
  const ctaDisabled = !configured || !botLink;

  return (
    <div className="min-h-screen bg-bg text-fg">
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="text-lg font-semibold tracking-tight">Trado</div>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-fg-3">{t.langSwitcherLabel}:</span>
            <button
              type="button"
              onClick={() => setLang(lang === "it" ? "en" : "it")}
              className="rounded-md border border-border-strong px-3 py-1 text-fg hover:bg-bg-hover"
            >
              {t.langSwitchTo}
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 pb-24">
        {/* Hero */}
        <section className="pt-16 pb-12">
          <div className="text-xs font-medium uppercase tracking-widest text-primary">
            {t.heroEyebrow}
          </div>
          <h1 className="mt-4 text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            {t.heroTitle}
          </h1>
          <p className="mt-6 max-w-2xl text-lg text-fg-2">{t.heroLead}</p>

          <div className="mt-8 flex flex-wrap gap-3">
            {ctaDisabled ? (
              <span className="rounded-md border border-border-strong bg-bg-2 px-5 py-3 text-sm text-fg-3">
                {t.ctaNotConfigured}
              </span>
            ) : (
              <a
                href={botLink ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground hover:opacity-90"
              >
                {t.ctaPrimary}
              </a>
            )}
            <a
              href="#how"
              className="rounded-md border border-border-strong px-5 py-3 text-sm font-medium hover:bg-bg-hover"
            >
              {t.ctaSecondary}
            </a>
          </div>

          <div className="mt-6 flex flex-wrap gap-2 text-xs text-fg-3">
            {[t.badgeFree, t.badgeNoEmail, t.badgeOpenSource, t.badgeHL].map((badge) => (
              <span key={badge} className="rounded-full border border-border px-3 py-1">
                {badge}
              </span>
            ))}
          </div>
        </section>

        {/* How it works */}
        <section id="how" className="border-t border-border pt-12 pb-12">
          <h2 className="text-2xl font-semibold tracking-tight">{t.howItWorksTitle}</h2>
          <p className="mt-2 max-w-2xl text-fg-2">{t.howItWorksLead}</p>
          <div className="mt-8 grid gap-4 sm:grid-cols-3">
            {t.steps.map((step) => (
              <div
                key={step.title}
                className="rounded-lg border border-border bg-bg-1 p-5"
              >
                <div className="text-sm font-semibold text-primary">{step.title}</div>
                <div className="mt-2 text-sm text-fg-2">{step.body}</div>
              </div>
            ))}
          </div>
        </section>

        {/* What you get */}
        <section className="border-t border-border pt-12 pb-12">
          <h2 className="text-2xl font-semibold tracking-tight">{t.whatYouGetTitle}</h2>
          <div className="mt-8 grid gap-4 sm:grid-cols-2">
            {t.features.map((feature) => (
              <div
                key={feature.title}
                className="rounded-lg border border-border bg-bg-1 p-5"
              >
                <div className="text-base font-semibold">{feature.title}</div>
                <div className="mt-2 text-sm text-fg-2">{feature.body}</div>
              </div>
            ))}
          </div>
        </section>

        {/* Honesty */}
        <section className="border-t border-border pt-12 pb-12">
          <h2 className="text-2xl font-semibold tracking-tight">{t.honestyTitle}</h2>
          <ul className="mt-6 space-y-3">
            {t.honestyBullets.map((bullet) => (
              <li key={bullet} className="flex gap-3 text-fg-2">
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                <span>{bullet}</span>
              </li>
            ))}
          </ul>
        </section>

        {/* FAQ */}
        <section className="border-t border-border pt-12 pb-12">
          <h2 className="text-2xl font-semibold tracking-tight">{t.faqTitle}</h2>
          <div className="mt-8 space-y-4">
            {t.faqs.map((faq) => (
              <details
                key={faq.q}
                className="group rounded-lg border border-border bg-bg-1 p-5"
              >
                <summary className="cursor-pointer text-sm font-semibold text-fg">
                  {faq.q}
                </summary>
                <div className="mt-3 text-sm text-fg-2">{faq.a}</div>
              </details>
            ))}
          </div>
        </section>

        {/* Final CTA */}
        <section className="border-t border-border pt-12 pb-12">
          <div className="rounded-xl border border-border-strong bg-bg-1 p-8 text-center">
            <h2 className="text-2xl font-semibold tracking-tight">{t.finalCtaTitle}</h2>
            <p className="mx-auto mt-3 max-w-xl text-fg-2">{t.finalCtaBody}</p>
            <div className="mt-6 flex justify-center">
              {ctaDisabled ? (
                <span className="rounded-md border border-border-strong bg-bg-2 px-6 py-3 text-sm text-fg-3">
                  {t.ctaNotConfigured}
                </span>
              ) : (
                <a
                  href={botLink ?? "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground hover:opacity-90"
                >
                  {t.ctaPrimary}
                </a>
              )}
            </div>
          </div>
        </section>

        {/* Disclaimer */}
        <section className="border-t border-border pt-12">
          <h2 className="text-base font-semibold uppercase tracking-widest text-fg-3">
            {t.disclaimerTitle}
          </h2>
          <p className="mt-4 text-sm text-fg-3">{t.disclaimerBody}</p>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto max-w-5xl px-6 py-6 text-xs text-fg-3">{t.footer}</div>
      </footer>
    </div>
  );
}
