"use client";

import { AlertCircle, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ErrorStateProps {
  title?: string;
  message?: string;
  detail?: string | Error | null;
  onRetry?: () => void;
  className?: string;
  variant?: "card" | "inline";
}

function detailString(detail: string | Error | null | undefined): string | null {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (detail instanceof Error) return detail.stack ?? detail.message;
  return String(detail);
}

export function ErrorState({
  title = "Qualcosa e' andato storto",
  message = "Impossibile caricare i dati. Verifica la connessione al backend e riprova.",
  detail,
  onRetry,
  className,
  variant = "card",
}: ErrorStateProps) {
  const [expanded, setExpanded] = useState(false);
  const detailText = detailString(detail);

  const content = (
    <div className={cn("flex flex-col items-center justify-center gap-3 text-center", className)}>
      <div className="grid h-12 w-12 place-items-center rounded-full border border-danger/30 bg-danger/10 text-danger">
        <AlertCircle className="h-5 w-5" />
      </div>
      <div className="space-y-1">
        <div className="text-sm font-medium text-fg">{title}</div>
        <div className="max-w-md text-xs text-fg-3">{message}</div>
      </div>
      <div className="flex items-center gap-2">
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            Riprova
          </Button>
        )}
        {detailText && (
          <Button variant="ghost" size="sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Nascondi dettagli" : "Mostra dettagli"}
          </Button>
        )}
      </div>
      {expanded && detailText && (
        <pre className="mt-2 max-h-48 max-w-md overflow-auto rounded-md border border-border bg-bg p-3 text-left font-mono text-[10px] leading-relaxed text-fg-3">
          {detailText}
        </pre>
      )}
    </div>
  );

  if (variant === "inline") {
    return <div className="py-10">{content}</div>;
  }
  return <Card className="grid place-items-center border-danger/30 bg-danger/5 p-10">{content}</Card>;
}
