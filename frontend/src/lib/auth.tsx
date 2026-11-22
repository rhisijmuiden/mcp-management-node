import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import i18n from "@/i18n";
import { get, setUnauthorizedHandler } from "./api";
import type { AuthStatus, User } from "./types";

interface AuthCtx {
  status: AuthStatus | undefined;
  user: User | null | undefined;
  loading: boolean;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>({ status: undefined, user: undefined, loading: true, refresh: async () => {} });

export function applyTheme(theme: string) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => get<AuthStatus>("api/auth/status") });
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => get<User>("api/auth/me").catch(() => null),
    enabled: !!status.data?.authenticated,
  });

  useEffect(() => {
    setUnauthorizedHandler(() => {
      qc.setQueryData(["me"], null);
      qc.invalidateQueries({ queryKey: ["auth-status"] });
    });
  }, [qc]);

  const user = status.data && !status.data.authenticated ? null : me.data;

  useEffect(() => {
    const stored = localStorageGet("mmn-lang");
    const lang = user?.language || stored || status.data?.default_language || "en";
    if (i18n.language !== lang) i18n.changeLanguage(lang);
    document.documentElement.lang = lang;
    applyTheme(user?.theme || localStorageGet("mmn-theme") || "system");
  }, [user?.language, user?.theme, status.data?.default_language]);

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["auth-status"] });
    await qc.invalidateQueries({ queryKey: ["me"] });
  };

  return (
    <Ctx.Provider value={{ status: status.data, user, loading: status.isLoading || (!!status.data?.authenticated && me.isLoading), refresh }}>
      {children}
    </Ctx.Provider>
  );
}

export function localStorageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function localStorageSet(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export const useAuth = () => useContext(Ctx);
