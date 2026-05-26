"use client";

import { Toaster as Sonner } from "sonner";

type ToasterProps = React.ComponentProps<typeof Sonner>;

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="dark"
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-bg-1 group-[.toaster]:text-fg group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-fg-3",
          actionButton: "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton: "group-[.toast]:bg-bg-2 group-[.toast]:text-fg-2",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
