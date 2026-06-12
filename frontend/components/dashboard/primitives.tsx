"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { ICON_BTN, SELECT_CLS } from "@/components/dashboard/helpers";

export function PillFilter({
  value,
  options,
  onChange
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 bg-surface-2 rounded-lg p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`px-2.5 h-6 rounded-md text-xs font-medium transition-colors ${
            value === opt.value ? "bg-blue/20 text-blue" : "text-muted hover:text-text"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyRow({ colSpan, message }: { colSpan: number; message: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-10 text-center text-sm text-muted">
        {message}
      </td>
    </tr>
  );
}

export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

export function PaginationControls({
  total,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange
}: {
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, totalPages);
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const end = Math.min(total, safePage * pageSize);
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-divider/60 max-sm:flex-col max-sm:items-start">
      <span className="text-xs text-muted">
        {start}-{end} di {total}
      </span>
      <div className="flex items-center gap-2">
        <select
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          className={SELECT_CLS}
          aria-label="Elementi per pagina"
        >
          {PAGE_SIZE_OPTIONS.map((size) => (
            <option key={size} value={size}>
              {size} / pagina
            </option>
          ))}
        </select>
        <div className="flex items-center gap-1">
          <button
            className={ICON_BTN}
            onClick={() => onPageChange(safePage - 1)}
            disabled={safePage <= 1}
            title="Pagina precedente"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="min-w-12 text-center text-xs text-muted">
            {safePage}/{totalPages}
          </span>
          <button
            className={ICON_BTN}
            onClick={() => onPageChange(safePage + 1)}
            disabled={safePage >= totalPages}
            title="Pagina successiva"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

