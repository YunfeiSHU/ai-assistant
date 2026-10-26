import { create } from "zustand";

export type ToastKind = "ok" | "warn" | "bad" | "";

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

interface ToastState {
  items: ToastItem[];
  push: (message: string, kind?: ToastKind) => void;
  dismiss: (id: number) => void;
}

let sequence = 0;

export const useToastStore = create<ToastState>((set) => ({
  items: [],
  push: (message, kind = "") => {
    const id = ++sequence;
    set((state) => ({ items: [...state.items, { id, message, kind }] }));
    window.setTimeout(() => {
      set((state) => ({ items: state.items.filter((item) => item.id !== id) }));
    }, 2800);
  },
  dismiss: (id) => {
    set((state) => ({ items: state.items.filter((item) => item.id !== id) }));
  },
}));

export function toast(message: string, kind: ToastKind = ""): void {
  useToastStore.getState().push(message, kind);
}

export function Toaster() {
  const items = useToastStore((state) => state.items);
  return (
    <div className="toast-stack" aria-live="polite">
      {items.map((item) => (
        <div className={`toast ${item.kind}`} key={item.id}>
          {item.message}
        </div>
      ))}
    </div>
  );
}
