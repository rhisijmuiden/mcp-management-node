import { lazy, Suspense, type ComponentProps } from "react";
import { Spinner } from "@/components/ui/misc";

// noVNC and xterm.js are large: load them only when a console is opened.
const Inner = lazy(() => import("./console").then((m) => ({ default: m.ConsoleView })));

export function ConsoleView(props: ComponentProps<typeof Inner>) {
  return (
    <Suspense fallback={<div className="grid h-40 place-items-center"><Spinner /></div>}>
      <Inner {...props} />
    </Suspense>
  );
}
