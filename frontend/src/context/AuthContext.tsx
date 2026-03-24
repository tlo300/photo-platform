"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import {
  login as apiLogin,
  register as apiRegister,
  logout as apiLogout,
  refresh,
} from "@/lib/api";

interface AuthContextValue {
  token: string | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, displayName: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    refresh().then((t) => {
      setToken(t);
      setReady(true);
    });
  }, []);

  async function login(email: string, password: string) {
    const t = await apiLogin(email, password);
    setToken(t);
  }

  async function register(email: string, displayName: string, password: string) {
    const t = await apiRegister(email, displayName, password);
    setToken(t);
  }

  async function logout() {
    await apiLogout();
    setToken(null);
  }

  return (
    <AuthContext.Provider value={{ token, ready, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
