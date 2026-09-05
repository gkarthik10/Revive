import { createContext, useContext, useEffect, useState } from "react";

import axios from "axios";

const API_BASE = "http://127.0.0.1:8000/api";

const TOKEN_STORAGE_KEY = "revive_auth_token";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(
    () => localStorage.getItem(TOKEN_STORAGE_KEY) || null,
  );

  const [user, setUser] = useState(null);

  const [status, setStatus] = useState("checking");

  /*
   * checking
   * signed-in
   * signed-out
   */

  useEffect(() => {
    if (token) {
      axios.defaults.headers.common.Authorization = `Bearer ${token}`;

      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      delete axios.defaults.headers.common.Authorization;

      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, [token]);

  /*
   * Validate the stored session whenever
   * the token changes.
   */

  useEffect(() => {
    let cancelled = false;

    if (!token) {
      setUser(null);
      setStatus("signed-out");

      return undefined;
    }

    setStatus("checking");

    axios
      .get(`${API_BASE}/auth/me`)
      .then((response) => {
        if (cancelled) return;

        setUser(response.data);
        setStatus("signed-in");
      })
      .catch(() => {
        if (cancelled) return;

        setToken(null);
        setUser(null);
        setStatus("signed-out");
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  /*
   * Automatically return to login when
   * the backend rejects an expired/invalid JWT.
   */

  useEffect(() => {
    const interceptorId = axios.interceptors.response.use(
      (response) => response,

      (error) => {
        const status = error?.response?.status;
        const url = error?.config?.url || "";

        /*
         * The change-password endpoint can legitimately return a
         * client error when the user types the wrong *current*
         * password. That's a form validation failure, not an
         * invalid/expired session, so it must never force a logout.
         * It is intentionally sent back as 400 by the backend, but
         * we also exclude it here by URL as a second line of defense.
         */
        const isChangePasswordRequest = url.includes("/auth/change-password");

        if (status === 401 && !isChangePasswordRequest) {
          setToken(null);
          setUser(null);
          setStatus("signed-out");
        }

        return Promise.reject(error);
      },
    );

    return () => {
      axios.interceptors.response.eject(interceptorId);
    };
  }, []);

  async function login(email, password) {
    const response = await axios.post(`${API_BASE}/auth/login`, {
      email,
      password,
    });

    setUser(response.data.user);
    setToken(response.data.access_token);
    setStatus("signed-in");

    return response.data.user;
  }

  function logout() {
    setToken(null);
    setUser(null);
    setStatus("signed-out");
  }

  const isAdmin = user?.role === "admin";

  const isOperator = user?.role === "admin" || user?.role === "operator";

  const isViewer = user?.role === "viewer";

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        status,

        login,
        logout,

        isAdmin,
        isOperator,
        isViewer,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);

  if (!context) {
    throw new Error("useAuth must be used inside an <AuthProvider>.");
  }

  return context;
}
