export type Lang = "it" | "en";

export type LandingMessages = {
  htmlLang: string;
  metaTitle: string;
  metaDescription: string;
  langSwitcherLabel: string;
  langSwitchTo: string;
  brandTagline: string;
  heroEyebrow: string;
  heroTitle: string;
  heroLead: string;
  ctaPrimary: string;
  ctaSecondary: string;
  ctaNotConfigured: string;
  badgeFree: string;
  badgeNoEmail: string;
  badgeOpenSource: string;
  badgeHL: string;
  howItWorksTitle: string;
  howItWorksLead: string;
  steps: { title: string; body: string }[];
  whatYouGetTitle: string;
  features: { title: string; body: string }[];
  honestyTitle: string;
  honestyBullets: string[];
  faqTitle: string;
  faqs: { q: string; a: string }[];
  finalCtaTitle: string;
  finalCtaBody: string;
  disclaimerTitle: string;
  disclaimerBody: string;
  footer: string;
};

export const MESSAGES: Record<Lang, LandingMessages> = {
  it: {
    htmlLang: "it",
    metaTitle: "Trado · Canale segnali crypto su Hyperliquid",
    metaDescription:
      "Segnali di trading crypto generati da un sistema multi-strategia su Hyperliquid. Canale Telegram gratuito, niente promesse, niente email.",
    langSwitcherLabel: "Lingua",
    langSwitchTo: "EN",
    brandTagline: "Trade smarter. Grow further.",
    heroEyebrow: "Canale Telegram · Gratuito",
    heroTitle: "Segnali crypto generati da un sistema, non da un guru.",
    heroLead:
      "Trado è un motore di segnali multi-strategia che opera su Hyperliquid. Pubblica setup di trading in tempo reale su un canale Telegram pubblico. Niente promesse di profitto, niente email richieste, niente upsell.",
    ctaPrimary: "Accedi al canale",
    ctaSecondary: "Come funziona",
    ctaNotConfigured:
      "Il canale è in fase di configurazione. Riprova tra poco.",
    badgeFree: "100% gratuito",
    badgeNoEmail: "Nessuna email",
    badgeOpenSource: "Logica trasparente",
    badgeHL: "Mercati Hyperliquid",
    howItWorksTitle: "Come funziona",
    howItWorksLead:
      "Tre passi rapidi, nessun modulo da compilare. Il bot ti fa qualche domanda veloce, poi ti manda l'invito al canale.",
    steps: [
      {
        title: "1. Avvia il bot",
        body: "Clicca il pulsante qui sopra. Si apre Telegram sul nostro bot di onboarding.",
      },
      {
        title: "2. Lingua e disclaimer",
        body: "Scegli italiano o inglese, indichi il tuo livello di esperienza e leggi il disclaimer di rischio.",
      },
      {
        title: "3. Ricevi l'invito",
        body: "Il bot ti genera un invito monouso al canale. Da quel momento ricevi i segnali in tempo reale.",
      },
    ],
    whatYouGetTitle: "Cosa ricevi",
    features: [
      {
        title: "Setup completi",
        body: "Ogni segnale include direzione, prezzo di ingresso, stop loss, fino a tre take profit e rapporto rischio/rendimento.",
      },
      {
        title: "Score di convinzione",
        body: "Ogni segnale ha uno score 0-100 che combina più strategie. Tu decidi la tua soglia di intervento.",
      },
      {
        title: "Aggiornamenti sulle chiusure",
        body: "Il sistema notifica anche quando una posizione viene chiusa, con esito e percentuale di PnL.",
      },
      {
        title: "Mercati selezionati",
        body: "Coppie liquide su Hyperliquid Perpetual. Niente alt minori, niente shitcoin pumpate.",
      },
    ],
    honestyTitle: "Come lavoriamo (la parte onesta)",
    honestyBullets: [
      "Trado è un sistema sperimentale. Pubblichiamo i segnali, non garantiamo che ci guadagnerai.",
      "Il motore opera su Hyperliquid con strategie testate; gli stessi segnali alimentano un account paper trading.",
      "Non vendiamo corsi, non chiediamo email, non gestiamo i tuoi fondi.",
      "Quando una strategia smette di funzionare la disattiviamo. Quando il sistema entra in modalità difensiva, lo segnaliamo.",
    ],
    faqTitle: "Domande frequenti",
    faqs: [
      {
        q: "È davvero gratis? Dov'è la fregatura?",
        a: "Sì, gratis. Non vendiamo corsi, non gestiamo capitali altrui. In futuro potremmo aggiungere un livello premium con segnali in tempo reale; il canale base resterà gratuito.",
      },
      {
        q: "Quanti segnali al giorno?",
        a: "Dipende dal mercato. In condizioni normali da 0 a 10 segnali al giorno su timeframe 15m–4h. Quando il regime di mercato è incerto il sistema riduce o sospende l'attività.",
      },
      {
        q: "Devo per forza usare Hyperliquid?",
        a: "I segnali sono pensati per Hyperliquid (perpetui) ma puoi replicarli su qualsiasi exchange su cui il pair è disponibile. Considera però differenze di liquidità e funding.",
      },
      {
        q: "Posso fidarmi della performance?",
        a: "Pubblicheremo un track record verificabile a breve. Per ora, sei tu a giudicare: osserva il canale, paper-tradalo per qualche settimana e decidi.",
      },
      {
        q: "Posso uscire in qualsiasi momento?",
        a: "Sì. Esci dal canale Telegram quando vuoi, oppure manda /pause al bot per sospendere temporaneamente le notifiche.",
      },
    ],
    finalCtaTitle: "Pronto a provare?",
    finalCtaBody:
      "Apri il bot, rispondi a due domande, entra nel canale. Quindici secondi in tutto.",
    disclaimerTitle: "Disclaimer di rischio",
    disclaimerBody:
      "I segnali pubblicati da Trado hanno finalità informativa ed educativa e non costituiscono consulenza finanziaria personalizzata ai sensi del TUF (D.Lgs. 58/1998). Il trading di derivati crypto comporta rischio di perdita totale del capitale. I risultati passati non garantiscono risultati futuri. Sei l'unico responsabile delle tue decisioni di investimento.",
    footer: "Trado · Segnali crypto su Hyperliquid",
  },
  en: {
    htmlLang: "en",
    metaTitle: "Trado · Crypto signal channel on Hyperliquid",
    metaDescription:
      "Crypto trading signals generated by a multi-strategy system on Hyperliquid. Free Telegram channel, no promises, no email required.",
    langSwitcherLabel: "Language",
    langSwitchTo: "IT",
    brandTagline: "Trade smarter. Grow further.",
    heroEyebrow: "Telegram channel · Free",
    heroTitle: "Crypto signals generated by a system, not a guru.",
    heroLead:
      "Trado is a multi-strategy signal engine that runs on Hyperliquid. It publishes trade setups in real time on a public Telegram channel. No profit promises, no email required, no upsells.",
    ctaPrimary: "Join the channel",
    ctaSecondary: "How it works",
    ctaNotConfigured:
      "The channel is being configured. Please check back soon.",
    badgeFree: "100% free",
    badgeNoEmail: "No email",
    badgeOpenSource: "Transparent logic",
    badgeHL: "Hyperliquid markets",
    howItWorksTitle: "How it works",
    howItWorksLead:
      "Three quick steps, no forms to fill out. The bot asks a couple of fast questions, then sends you the channel invite.",
    steps: [
      {
        title: "1. Open the bot",
        body: "Click the button above. Telegram opens on our onboarding bot.",
      },
      {
        title: "2. Language & disclaimer",
        body: "Pick English or Italian, tell us your experience level, and read the risk disclaimer.",
      },
      {
        title: "3. Get your invite",
        body: "The bot generates a single-use channel invite. From then on you get signals in real time.",
      },
    ],
    whatYouGetTitle: "What you get",
    features: [
      {
        title: "Complete setups",
        body: "Every signal includes direction, entry price, stop loss, up to three take profits, and risk/reward ratio.",
      },
      {
        title: "Conviction score",
        body: "Each signal has a 0-100 score combining multiple strategies. You decide where to set your own threshold.",
      },
      {
        title: "Close updates",
        body: "The system also notifies when a position is closed, including outcome and PnL percentage.",
      },
      {
        title: "Curated markets",
        body: "Liquid pairs on Hyperliquid Perpetual. No micro-cap alts, no pumped shitcoins.",
      },
    ],
    honestyTitle: "How we operate (the honest part)",
    honestyBullets: [
      "Trado is an experimental system. We publish signals; we do not guarantee you will profit.",
      "The engine runs on Hyperliquid with tested strategies; the same signals feed a paper trading account.",
      "We don't sell courses, we don't ask for emails, we don't manage your funds.",
      "When a strategy stops working we disable it. When the system enters defensive mode, we flag it.",
    ],
    faqTitle: "Frequently asked",
    faqs: [
      {
        q: "Is it really free? What's the catch?",
        a: "Yes, free. We don't sell courses and we don't manage capital. We may add a premium tier with real-time signals later; the base channel will stay free.",
      },
      {
        q: "How many signals per day?",
        a: "It depends on the market. In normal conditions 0 to 10 signals per day on 15m–4h timeframes. When the market regime is unclear, the system throttles or pauses.",
      },
      {
        q: "Do I have to use Hyperliquid?",
        a: "Signals target Hyperliquid (perpetuals) but you can replicate them on any exchange that lists the same pair — just account for differences in liquidity and funding.",
      },
      {
        q: "Can I trust the track record?",
        a: "A verifiable public track record is coming soon. For now, judge for yourself: watch the channel, paper-trade for a few weeks, then decide.",
      },
      {
        q: "Can I leave whenever I want?",
        a: "Yes. Leave the Telegram channel at any time, or send /pause to the bot to temporarily silence notifications.",
      },
    ],
    finalCtaTitle: "Ready to try it?",
    finalCtaBody:
      "Open the bot, answer two questions, join the channel. Fifteen seconds in total.",
    disclaimerTitle: "Risk disclaimer",
    disclaimerBody:
      "Trado's signals are provided for informational and educational purposes only and do not constitute personalized financial advice. Trading crypto derivatives carries risk of total capital loss. Past performance does not guarantee future results. You are solely responsible for your own investment decisions.",
    footer: "Trado · Crypto signals on Hyperliquid",
  },
};
