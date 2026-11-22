import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const button = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 cursor-pointer",
  {
    variants: {
      variant: {
        // One coral button per screen: the main action. Text on coral is always nacht.
        primary: "bg-koraal text-nacht hover:brightness-95 shadow-sm",
        dark: "bg-nacht text-white hover:bg-nacht/90 dark:bg-lucht dark:text-nacht dark:hover:bg-lucht/90",
        secondary: "bg-raised text-ink border border-line hover:bg-sunken",
        ghost: "text-ink hover:bg-sunken",
        danger: "bg-offline text-white hover:brightness-110",
        link: "text-accent-text underline-offset-4 hover:underline px-0 h-auto",
      },
      size: { sm: "h-8 px-3 text-[13px]", md: "h-10 px-4", lg: "h-11 px-5 text-base", icon: "size-9" },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof button> {
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading, children, disabled, ...props }, ref) => (
    <button ref={ref} className={cn(button({ variant, size }), className)} disabled={disabled || loading} {...props}>
      {loading && <Loader2 className="animate-spin" />}
      {children}
    </button>
  ),
);
Button.displayName = "Button";
