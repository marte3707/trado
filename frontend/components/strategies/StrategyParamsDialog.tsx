"use client";

import { useEffect, useMemo, useState } from "react";
import { Save, X } from "lucide-react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { useUpdateStrategy } from "@/lib/hooks/mutations";
import { cn } from "@/lib/utils";
import type { Strategy } from "@/lib/api";

interface Props {
  strategy: Strategy | null;
  onClose: () => void;
}

type FieldKind = "boolean" | "number" | "string" | "json";

interface FieldState {
  key: string;
  kind: FieldKind;
  value: string;
  bool: boolean;
}

function classifyValue(v: unknown): FieldKind {
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "number") return "number";
  if (typeof v === "string") return "string";
  return "json";
}

function valueToString(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}

function parseField(field: FieldState): { ok: boolean; value: unknown; error?: string } {
  if (field.kind === "boolean") return { ok: true, value: field.bool };
  if (field.kind === "number") {
    const n = Number(field.value);
    if (!Number.isFinite(n)) return { ok: false, value: null, error: "Numero non valido" };
    return { ok: true, value: n };
  }
  if (field.kind === "json") {
    try {
      return { ok: true, value: JSON.parse(field.value) };
    } catch {
      return { ok: false, value: null, error: "JSON non valido" };
    }
  }
  return { ok: true, value: field.value };
}

export function StrategyParamsDialog({ strategy, onClose }: Props) {
  const open = strategy !== null;
  const { mutate: updateStrategy, isPending } = useUpdateStrategy();

  const initialFields: FieldState[] = useMemo(() => {
    if (!strategy) return [];
    return Object.entries(strategy.params ?? {}).map(([key, value]) => {
      const kind = classifyValue(value);
      return {
        key,
        kind,
        value: valueToString(value),
        bool: Boolean(value),
      };
    });
  }, [strategy]);

  const [fields, setFields] = useState<FieldState[]>(initialFields);
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    setFields(initialFields);
    setErrors({});
  }, [initialFields]);

  const dirty = useMemo(() => {
    if (fields.length !== initialFields.length) return true;
    return fields.some((f, i) => {
      const init = initialFields[i];
      if (!init) return true;
      if (f.key !== init.key) return true;
      if (f.kind === "boolean") return f.bool !== init.bool;
      return f.value !== init.value;
    });
  }, [fields, initialFields]);

  if (!strategy) return null;

  function updateField(idx: number, patch: Partial<FieldState>) {
    setFields((arr) => arr.map((f, i) => (i === idx ? { ...f, ...patch } : f)));
    if (errors[fields[idx]?.key]) {
      setErrors((e) => {
        const next = { ...e };
        delete next[fields[idx].key];
        return next;
      });
    }
  }

  function handleSave() {
    if (!strategy) return;
    const nextParams: Record<string, unknown> = {};
    const nextErrors: Record<string, string> = {};
    for (const f of fields) {
      const parsed = parseField(f);
      if (!parsed.ok) {
        nextErrors[f.key] = parsed.error ?? "Valore non valido";
      } else {
        nextParams[f.key] = parsed.value;
      }
    }
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      toast.error("Correggi i campi evidenziati prima di salvare.");
      return;
    }
    updateStrategy(
      { id: strategy.id, patch: { params: nextParams } },
      {
        onSuccess: () => {
          toast.success(`Parametri ${strategy.strategy_name} salvati`);
          onClose();
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-xl p-0">
        <DialogHeader className="border-b border-border px-6 py-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <DialogTitle>
                <span className="font-mono">{strategy.strategy_name}</span>
                <Badge variant="outline" className="ml-2 font-mono">
                  {strategy.timeframe}
                </Badge>
              </DialogTitle>
              <DialogDescription>
                {fields.length} parametri · peso attuale {strategy.weight}
              </DialogDescription>
            </div>
            <button
              onClick={onClose}
              className="rounded-md p-1 text-fg-3 transition-colors hover:bg-bg-2 hover:text-fg"
              aria-label="Chiudi"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </DialogHeader>

        <ScrollArea className="max-h-[60vh]">
          <div className="space-y-4 px-6 py-5">
            {fields.length === 0 ? (
              <div className="grid h-24 place-items-center text-sm text-fg-3">
                Nessun parametro configurabile.
              </div>
            ) : (
              fields.map((f, i) => (
                <FieldRow
                  key={f.key}
                  field={f}
                  error={errors[f.key]}
                  onUpdate={(patch) => updateField(i, patch)}
                />
              ))
            )}
          </div>
        </ScrollArea>

        <div className="flex items-center gap-2 border-t border-border px-6 py-3">
          <div className="text-[11px] text-fg-3">
            {dirty ? (
              <span className="text-warn">Modifiche non salvate</span>
            ) : (
              "Nessuna modifica"
            )}
          </div>
          <div className="flex-1" />
          <Button variant="outline" size="sm" onClick={onClose} disabled={isPending}>
            Annulla
          </Button>
          <Button size="sm" disabled={!dirty || isPending} onClick={handleSave}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            {isPending ? "Salvataggio..." : "Salva"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function FieldRow({
  field,
  error,
  onUpdate,
}: {
  field: FieldState;
  error?: string;
  onUpdate: (patch: Partial<FieldState>) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <label className="font-mono text-sm text-fg-2">{field.key}</label>
        <Badge variant="outline" className="font-mono text-[10px]">
          {field.kind}
        </Badge>
      </div>

      {field.kind === "boolean" ? (
        <div className="flex h-9 items-center gap-2 rounded-md border border-border bg-bg-1 px-3">
          <Switch checked={field.bool} onCheckedChange={(v: boolean) => onUpdate({ bool: v })} />
          <span className="font-mono text-xs text-fg-3">
            {field.bool ? "true" : "false"}
          </span>
        </div>
      ) : field.kind === "json" ? (
        <textarea
          value={field.value}
          onChange={(e) => onUpdate({ value: e.target.value })}
          rows={4}
          className={cn(
            "block w-full rounded-md border border-border bg-bg-1 px-3 py-2 font-mono text-xs text-fg",
            "focus:outline-none focus:ring-1 focus:ring-ring",
            error && "border-danger/40",
          )}
        />
      ) : (
        <Input
          type={field.kind === "number" ? "number" : "text"}
          step={field.kind === "number" ? "any" : undefined}
          value={field.value}
          onChange={(e) => onUpdate({ value: e.target.value })}
          className={cn("font-mono", error && "border-danger/40")}
        />
      )}

      {error && <div className="text-[11px] text-danger">{error}</div>}
    </div>
  );
}
