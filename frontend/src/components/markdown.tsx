import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/* Markdown for customer documentation. Raw HTML in the text is not rendered (react-markdown escapes it) and
   unsafe link protocols are removed, so a page can never run script in the app. */
const components: Components = {
  h1: ({ children }) => <h1 className="display mb-3 mt-6 text-2xl first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-6 border-b border-line pb-1 text-lg font-bold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-2 mt-5 text-base font-bold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-4 text-sm font-bold uppercase tracking-wide text-muted">{children}</h4>,
  p: ({ children }) => <p className="my-2.5 leading-relaxed">{children}</p>,
  ul: ({ children, className }) => <ul className={cn("my-2.5 space-y-1 pl-6", className?.includes("contains-task-list") ? "list-none pl-1" : "list-disc")}>{children}</ul>,
  ol: ({ children }) => <ol className="my-2.5 list-decimal space-y-1 pl-6">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed marker:text-muted">{children}</li>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="font-medium text-accent-text underline underline-offset-2">
      {children}
    </a>
  ),
  blockquote: ({ children }) => <blockquote className="my-3 border-l-4 border-koraal/60 bg-sunken/60 px-4 py-1 text-ink/90">{children}</blockquote>,
  hr: () => <hr className="my-6 border-line" />,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto rounded-md border border-line">
      <table className="w-full border-collapse text-[13.5px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-sunken text-left">{children}</thead>,
  th: ({ children }) => <th className="border-b border-line px-3 py-2 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-b border-line/70 px-3 py-1.5 align-top">{children}</td>,
  pre: ({ children }) => <pre className="mono my-3 overflow-x-auto rounded-md bg-nacht p-3 text-[12.5px] text-white">{children}</pre>,
  code: ({ children, className }) =>
    className ? <code className={className}>{children}</code> : <code className="mono rounded bg-sunken px-1 py-0.5 text-[12.5px]">{children}</code>,
  img: ({ src, alt }) => <img src={typeof src === "string" ? src : undefined} alt={alt ?? ""} className="my-3 max-w-full rounded-md border border-line" />,
  input: ({ checked }) => <input type="checkbox" checked={!!checked} readOnly disabled className="mr-2 translate-y-[1px] accent-koraal" />,
};

export function Markdown({ children, className, empty }: { children: string; className?: string; empty?: ReactNode }) {
  if (!children.trim()) return <div className={cn("text-sm text-muted", className)}>{empty ?? "–"}</div>;
  return (
    <div className={cn("min-w-0 break-words text-[14.5px] text-ink", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
